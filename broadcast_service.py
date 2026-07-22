"""一斉指示（ブロードキャスト）の実行ロジック。

登録済みの各エージェント（モデル + システムプロンプト + MCPサーバー群）に対して、
同じ指示を Claude Messages API のリモートMCPコネクタ経由で並列に実行し、
それぞれの応答と使用したMCPツール名を返す。
"""
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# Anthropic のリモートMCPコネクタを有効化する beta ヘッダ
MCP_BETA = "mcp-client-2025-04-04"

# 並列実行の上限（過剰なレート制限を避けるため）
MAX_WORKERS = 8


class BroadcastError(RuntimeError):
    """ブロードキャスト全体を止める致命的なエラー。"""


def _get_client():
    """Anthropic クライアントを生成する。APIキー未設定なら分かりやすく失敗させる。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise BroadcastError(
            "ANTHROPIC_API_KEY が設定されていません。"
            "環境変数に Anthropic API キーを設定してください。"
        )
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - 依存未導入時のガイド
        raise BroadcastError(
            "anthropic パッケージが見つかりません。"
            "`pip install -r requirements.txt` を実行してください。"
        ) from exc
    return Anthropic(api_key=api_key)


def _build_mcp_servers(agent) -> list:
    """エージェント設定を Messages API の mcp_servers 形式へ変換する。"""
    servers = []
    for item in agent.mcp_servers:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        server = {
            "type": "url",
            "url": url,
            "name": (item.get("name") or "mcp").strip() or "mcp",
        }
        token = (item.get("authorization_token") or "").strip()
        if token:
            server["authorization_token"] = token
        servers.append(server)
    return servers


def _extract_text_and_tools(message) -> tuple:
    """API 応答からテキスト本文と使用MCPツール名を抽出する。"""
    text_parts = []
    tools_used = []
    for block in getattr(message, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", ""))
        elif block_type == "mcp_tool_use":
            name = getattr(block, "name", None)
            if name:
                tools_used.append(name)
    return "\n".join(p for p in text_parts if p).strip(), tools_used


def _run_one(client, agent, instruction: str) -> dict:
    """1エージェント分を実行して結果 dict を返す（例外は結果に畳み込む）。"""
    base = {
        "agent_name": agent.name,
        "model": agent.model,
    }
    try:
        kwargs = {
            "model": agent.model,
            "max_tokens": agent.max_tokens or 2048,
            "messages": [{"role": "user", "content": instruction}],
        }
        if agent.system_prompt:
            kwargs["system"] = agent.system_prompt

        mcp_servers = _build_mcp_servers(agent)
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
            kwargs["betas"] = [MCP_BETA]
            message = client.beta.messages.create(**kwargs)
        else:
            # MCPサーバー未設定なら通常の Messages API で実行
            message = client.messages.create(**kwargs)

        text, tools_used = _extract_text_and_tools(message)
        return {
            **base,
            "status": "success",
            "response": text or "(空の応答)",
            "tools_used": tools_used,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - 個別失敗は結果に記録して継続
        return {
            **base,
            "status": "error",
            "response": None,
            "tools_used": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_broadcast(instruction: str, agents: list) -> list:
    """指示を全エージェントへ並列に一斉送信し、結果リストを返す。

    Returns: [{agent_name, model, status, response, tools_used, error}, ...]
             （agents の順序を維持する）
    """
    if not agents:
        return []

    client = _get_client()

    results_by_index = {}
    workers = min(MAX_WORKERS, len(agents))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_one, client, agent, instruction): idx
            for idx, agent in enumerate(agents)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results_by_index[idx] = future.result()

    return [results_by_index[i] for i in range(len(agents))]
