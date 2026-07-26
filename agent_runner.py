"""CLI型AIエージェントの自動巡回ランナー。

サーバー内にインストールした各社CLI（定額プランの認証で動作）に
差分巡回プロンプトを流し込み、アプリのMCPサーバー経由でチェック・記録させる。

- Claude  : Claude Code CLI（`claude -p`）
- ChatGPT : Codex CLI（`codex exec`）
- Gemini  : Gemini CLI（`gemini -p`）

実行方法:
  1回だけ実行:  python agent_runner.py run <claude|codex|gemini>
  常駐ループ:    python agent_runner.py loop
    （60秒ごとに設定を確認し、有効かつ認証済みのエージェントを間隔どおり実行）

認証情報は HOME（dockerでは agent_home ボリューム）に保存され、初回ログインだけ
手動で行う。以後は無人で動く。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import AgentRun, AppSetting, db, get_or_create_mcp_secret


def _resolve_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url
    base = os.path.dirname(os.path.abspath(__file__))
    instance = os.path.join(base, "instance")
    os.makedirs(instance, exist_ok=True)
    return "sqlite:///" + os.path.join(instance, "app.db")


_engine = create_engine(_resolve_database_url())
db.metadata.create_all(_engine)
SessionLocal = sessionmaker(bind=_engine)

HOME = os.path.expanduser("~")
CLAUDE_MCP_JSON = os.path.join(HOME, "agent-mcp.json")

AGENTS = {
    "claude": {
        "label": "Claude（Claude Code CLI）",
        "ai_name": "Claude",
        "cred": os.path.join(HOME, ".claude", ".credentials.json"),
        "login_hint": "docker compose exec -it runner claude",
    },
    "codex": {
        "label": "ChatGPT（Codex CLI）",
        "ai_name": "ChatGPT",
        "cred": os.path.join(HOME, ".codex", "auth.json"),
        "login_hint": "docker compose exec -it runner codex login",
    },
    "gemini": {
        "label": "Gemini（Gemini CLI）",
        "ai_name": "Gemini",
        "cred": os.path.join(HOME, ".gemini", "oauth_creds.json"),
        "login_hint": "docker compose exec -it runner gemini",
    },
}

DEFAULT_INTERVAL_HOURS = 24
RUN_TIMEOUT_SECONDS = 1200  # 1回の巡回は最長20分
OUTPUT_TAIL_CHARS = 20000


def mcp_endpoint() -> str:
    """CLIが接続するMCPエンドポイント（compose内部URL）。"""
    explicit = (os.environ.get("AGENT_MCP_URL") or "").strip()
    if explicit:
        return explicit
    secret = (
        os.environ.get("MCP_URL_SECRET") or os.environ.get("MCP_AUTH_TOKEN") or ""
    ).strip()
    if not secret:
        with SessionLocal() as s:
            secret = get_or_create_mcp_secret(s)
    host = (os.environ.get("AGENT_MCP_HOST") or "mcp:8001").strip()
    return f"http://{host}/mcp/{secret}"


def ensure_configs() -> None:
    """各CLIのMCP接続設定を書き込む（毎回上書きしてエンドポイント変更に追従）。"""
    url = mcp_endpoint()

    # Claude Code: --mcp-config で渡すJSON
    with open(CLAUDE_MCP_JSON, "w", encoding="utf-8") as f:
        json.dump(
            {"mcpServers": {"accounting-support": {"type": "http", "url": url}}},
            f,
        )

    # Codex: ~/.codex/config.toml（認証は auth.json 側なので上書きしてよい）
    codex_dir = os.path.join(HOME, ".codex")
    os.makedirs(codex_dir, exist_ok=True)
    with open(os.path.join(codex_dir, "config.toml"), "w", encoding="utf-8") as f:
        f.write(
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n'
            "\n"
            "[mcp_servers.accounting-support]\n"
            f'url = "{url}"\n'
        )

    # Gemini CLI: ~/.gemini/settings.json（既存設定に mcpServers をマージ）
    gemini_dir = os.path.join(HOME, ".gemini")
    os.makedirs(gemini_dir, exist_ok=True)
    settings_path = os.path.join(gemini_dir, "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except (ValueError, OSError):
            settings = {}
    settings.setdefault("mcpServers", {})["accounting-support"] = {"httpUrl": url}
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def is_authed(name: str) -> bool:
    info = AGENTS.get(name)
    return bool(info and os.path.exists(info["cred"]))


def patrol_prompt(ai_name: str) -> str:
    return (
        f'あなたは会計レビュー担当のAI「{ai_name}」です。接続済みの MCP サーバー'
        '「accounting-support」のツールだけを使い、差分巡回を行ってください。\n'
        "1. list_bank_documents で原本を確認する。\n"
        "   - entries_count=0 の原本は get_bank_document で画像を読み、write_bank_entries で全行書き起こし、\n"
        f'     balance_issues が空になるまで修正のうえ write_document_review(ai_name="{ai_name}") で記録する。\n'
        f"   - 書き起こし済みで {ai_name} のレビューが無い原本は、画像と保存済み明細"
        "（list_bank_entries で document_id が一致する行）を突き合わせ、write_document_review で検証を記録する。\n"
        "   - 画像を取得できない場合はその旨を出力し、数値整合の確認のみ行う。\n"
        f"2. list_analyses を確認し、他のAIが warning/error を付けているのに {ai_name} の判定が無い取引を\n"
        f'   get_deal で確認し、write_analysis(ai_name="{ai_name}", check_type=同じ種別) で自分の見解を記録する。\n'
        "3. freee側の登録・修正が必要だと判断した指摘は create_task で提案する（実行はしない）。\n"
        "4. 最後に、実施した内容と新しい発見を3行以内で出力する。未処理が無ければ「変化なし」と出力する。\n"
        "freee や外部サービスへの書き込み、ファイルの作成・編集は行わない。\n"
    )


def build_cmd(name: str, prompt: str) -> list:
    if name == "claude":
        return [
            "claude",
            "-p",
            prompt,
            "--mcp-config",
            CLAUDE_MCP_JSON,
            "--dangerously-skip-permissions",
            "--output-format",
            "text",
        ]
    if name == "codex":
        return ["codex", "exec", "--skip-git-repo-check", prompt]
    if name == "gemini":
        return ["gemini", "-p", prompt, "--yolo"]
    raise ValueError(f"unknown agent: {name}")


def _has_running(s, name: str) -> bool:
    run = (
        s.query(AgentRun)
        .filter(AgentRun.agent == name, AgentRun.status == "running")
        .order_by(AgentRun.started_at.desc())
        .first()
    )
    if run is None:
        return False
    # 30分以上 running のまま残っていたら異常終了とみなす
    if (datetime.utcnow() - run.started_at).total_seconds() > RUN_TIMEOUT_SECONDS + 600:
        run.status = "error"
        run.output = (run.output or "") + "\n[runner] タイムアウト扱いで打ち切りました。"
        run.finished_at = datetime.utcnow()
        s.commit()
        return False
    return True


def run_agent(name: str, trigger: str = "manual") -> int:
    """エージェントを1回実行し、AgentRun のIDを返す。"""
    if name not in AGENTS:
        raise ValueError(f"unknown agent: {name}")

    with SessionLocal() as s:
        if _has_running(s, name):
            run = AgentRun(agent=name, trigger=trigger, status="error",
                           output="前回の実行がまだ動作中のためスキップしました。",
                           finished_at=datetime.utcnow())
            s.add(run)
            s.commit()
            return run.id
        run = AgentRun(agent=name, trigger=trigger, status="running")
        s.add(run)
        s.commit()
        run_id = run.id

    if not is_authed(name):
        _finish(run_id, "error", f"未認証です。サーバーで初回ログインが必要です: {AGENTS[name]['login_hint']}")
        return run_id

    try:
        ensure_configs()
        cmd = build_cmd(name, patrol_prompt(AGENTS[name]["ai_name"]))
        env = dict(os.environ)
        env.setdefault("HOME", HOME)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            env=env,
            cwd="/tmp",
        )
        output = (proc.stdout or "") + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
        status = "success" if proc.returncode == 0 else "error"
        if proc.returncode != 0:
            output += f"\n[runner] exit code {proc.returncode}"
        _finish(run_id, status, output)
    except subprocess.TimeoutExpired:
        _finish(run_id, "error", f"[runner] {RUN_TIMEOUT_SECONDS}秒でタイムアウトしました。")
    except FileNotFoundError as e:
        _finish(run_id, "error", f"[runner] CLIが見つかりません: {e}")
    except Exception as e:  # noqa: BLE001
        _finish(run_id, "error", f"[runner] 実行エラー: {e}")
    return run_id


def _finish(run_id: int, status: str, output: str) -> None:
    with SessionLocal() as s:
        run = s.get(AgentRun, run_id)
        if run is None:
            return
        run.status = status
        run.output = (output or "")[-OUTPUT_TAIL_CHARS:]
        run.finished_at = datetime.utcnow()
        s.commit()


def get_setting(s, key: str, default: str = "") -> str:
    row = s.get(AppSetting, key)
    return row.value if row and row.value is not None else default


def set_setting(s, key: str, value: str) -> None:
    s.merge(AppSetting(key=key, value=value))
    s.commit()


def agent_config(s, name: str) -> dict:
    return {
        "enabled": get_setting(s, f"agent.{name}.enabled", "0") == "1",
        "interval_hours": int(
            get_setting(s, f"agent.{name}.interval_hours", str(DEFAULT_INTERVAL_HOURS))
            or DEFAULT_INTERVAL_HOURS
        ),
    }


def _due(s, name: str, interval_hours: int) -> bool:
    last = (
        s.query(AgentRun)
        .filter(AgentRun.agent == name, AgentRun.trigger == "schedule")
        .order_by(AgentRun.started_at.desc())
        .first()
    )
    if last is None:
        return True
    return (datetime.utcnow() - last.started_at).total_seconds() >= interval_hours * 3600


def loop() -> None:
    print("[runner] loop start", flush=True)
    while True:
        try:
            for name in AGENTS:
                with SessionLocal() as s:
                    cfg = agent_config(s, name)
                    if not cfg["enabled"] or not is_authed(name):
                        continue
                    if not _due(s, name, cfg["interval_hours"]):
                        continue
                print(f"[runner] run {name} (schedule)", flush=True)
                run_agent(name, trigger="schedule")
        except Exception as e:  # noqa: BLE001 - ループは止めない
            print(f"[runner] loop error: {e}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if mode == "run" and len(sys.argv) > 2:
        rid = run_agent(sys.argv[2], trigger="manual")
        with SessionLocal() as s:
            r = s.get(AgentRun, rid)
            print(f"status={r.status}\n{r.output}")
    else:
        loop()
