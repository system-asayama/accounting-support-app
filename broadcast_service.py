"""一斉指示（ブロードキャスト）の実行ロジック。

登録済みの各エージェント（プロバイダ + モデル + システムプロンプト + MCPサーバー群）に
同じ指示を並列で実行し、各AIの応答と使用したMCPツール名を返す。

対応プロバイダ:
- anthropic : Claude Messages API のリモートMCPコネクタ
- openai    : OpenAI Responses API のリモートMCPツール
- gemini    : Google Gen AI SDK + MCP クライアントセッション（Streamable HTTP）

各プロバイダは freee などのリモートMCPサーバー（URL + 任意トークン）へ接続して
仕訳チェック等のツールを利用する。1エージェントの失敗は他に波及させず、
エラーはそのエージェントの結果として記録する。
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AsyncExitStack

from models import PROVIDER_ANTHROPIC, PROVIDER_GEMINI, PROVIDER_OPENAI

# Anthropic のリモートMCPコネクタを有効化する beta ヘッダ
ANTHROPIC_MCP_BETA = "mcp-client-2025-04-04"

# 並列実行の上限（過剰なレート制限を避けるため）
MAX_WORKERS = 8


class BroadcastError(RuntimeError):
    """ブロードキャスト全体を止める致命的なエラー。"""


class AgentRunError(RuntimeError):
    """エージェント1件の実行失敗（結果カードにメッセージを表示）。"""


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------
def _require_env(name: str, provider_label: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise AgentRunError(
            f"{provider_label} を使うには環境変数 {name} の設定が必要です。"
        )
    return value


def _clean_mcp_servers(agent) -> list:
    """URL が入っている MCP サーバー定義だけを返す。"""
    servers = []
    for item in agent.mcp_servers:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url:
            continue
        servers.append(
            {
                "name": (item.get("name") or "mcp").strip() or "mcp",
                "url": url,
                "token": (item.get("authorization_token") or "").strip(),
            }
        )
    return servers


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------
def _run_anthropic(agent, instruction: str) -> tuple:
    api_key = _require_env("ANTHROPIC_API_KEY", "Claude (Anthropic)")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise AgentRunError("anthropic パッケージが未インストールです。") from exc

    client = Anthropic(api_key=api_key)
    kwargs = {
        "model": agent.model,
        "max_tokens": agent.max_tokens or 2048,
        "messages": [{"role": "user", "content": instruction}],
    }
    if agent.system_prompt:
        kwargs["system"] = agent.system_prompt

    servers = _clean_mcp_servers(agent)
    if servers:
        mcp_servers = []
        for s in servers:
            entry = {"type": "url", "url": s["url"], "name": s["name"]}
            if s["token"]:
                entry["authorization_token"] = s["token"]
            mcp_servers.append(entry)
        kwargs["mcp_servers"] = mcp_servers
        kwargs["betas"] = [ANTHROPIC_MCP_BETA]
        message = client.beta.messages.create(**kwargs)
    else:
        message = client.messages.create(**kwargs)

    text_parts, tools_used = [], []
    for block in getattr(message, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", ""))
        elif btype == "mcp_tool_use":
            name = getattr(block, "name", None)
            if name:
                tools_used.append(name)
    return "\n".join(p for p in text_parts if p).strip(), tools_used


# ---------------------------------------------------------------------------
# OpenAI (ChatGPT)
# ---------------------------------------------------------------------------
def _run_openai(agent, instruction: str) -> tuple:
    api_key = _require_env("OPENAI_API_KEY", "ChatGPT (OpenAI)")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise AgentRunError("openai パッケージが未インストールです。") from exc

    client = OpenAI(api_key=api_key)
    kwargs = {
        "model": agent.model,
        "input": instruction,
        "max_output_tokens": agent.max_tokens or 2048,
    }
    if agent.system_prompt:
        kwargs["instructions"] = agent.system_prompt

    servers = _clean_mcp_servers(agent)
    if servers:
        tools = []
        for s in servers:
            tool = {
                "type": "mcp",
                "server_label": s["name"],
                "server_url": s["url"],
                "require_approval": "never",
            }
            if s["token"]:
                tool["headers"] = {"Authorization": f"Bearer {s['token']}"}
            tools.append(tool)
        kwargs["tools"] = tools

    resp = client.responses.create(**kwargs)

    text = (getattr(resp, "output_text", "") or "").strip()
    tools_used = []
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) == "mcp_call":
            name = getattr(item, "name", None)
            if name:
                tools_used.append(name)
    return text, tools_used


# ---------------------------------------------------------------------------
# Gemini (Google)
# ---------------------------------------------------------------------------
def _run_gemini(agent, instruction: str) -> tuple:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise AgentRunError(
            "Gemini (Google) を使うには環境変数 GEMINI_API_KEY（または GOOGLE_API_KEY）の設定が必要です。"
        )
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise AgentRunError("google-genai パッケージが未インストールです。") from exc

    servers = _clean_mcp_servers(agent)

    config_kwargs = {"max_output_tokens": agent.max_tokens or 2048}
    if agent.system_prompt:
        config_kwargs["system_instruction"] = agent.system_prompt

    if not servers:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=agent.model,
            contents=instruction,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        return (getattr(resp, "text", "") or "").strip(), []

    # MCP サーバーがある場合は Streamable HTTP のクライアントセッションを張り、
    # google-genai の自動関数呼び出しにツールとして渡す。
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        raise AgentRunError("mcp パッケージが未インストールです。") from exc

    async def _go():
        async with AsyncExitStack() as stack:
            sessions = []
            for s in servers:
                headers = {"Authorization": f"Bearer {s['token']}"} if s["token"] else None
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(s["url"], headers=headers)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions.append(session)

            client = genai.Client(api_key=api_key)
            resp = await client.aio.models.generate_content(
                model=agent.model,
                contents=instruction,
                config=types.GenerateContentConfig(tools=sessions, **config_kwargs),
            )
            text = (getattr(resp, "text", "") or "").strip()

            tools_used = []
            history = getattr(resp, "automatic_function_calling_history", None) or []
            for content in history:
                for part in getattr(content, "parts", None) or []:
                    fc = getattr(part, "function_call", None)
                    if fc is not None and getattr(fc, "name", None):
                        tools_used.append(fc.name)
            return text, tools_used

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# ディスパッチ
# ---------------------------------------------------------------------------
_RUNNERS = {
    PROVIDER_ANTHROPIC: _run_anthropic,
    PROVIDER_OPENAI: _run_openai,
    PROVIDER_GEMINI: _run_gemini,
}


def _run_one(agent, instruction: str) -> dict:
    base = {
        "agent_name": agent.name,
        "provider": agent.provider,
        "model": agent.model,
    }
    try:
        runner = _RUNNERS.get(agent.provider)
        if runner is None:
            raise AgentRunError(f"未対応のプロバイダです: {agent.provider}")
        text, tools_used = runner(agent, instruction)
        return {
            **base,
            "status": "success",
            "response": text or "(空の応答)",
            "tools_used": tools_used,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - 個別失敗は結果に記録して継続
        message = str(exc) if isinstance(exc, AgentRunError) else f"{type(exc).__name__}: {exc}"
        return {
            **base,
            "status": "error",
            "response": None,
            "tools_used": [],
            "error": message,
        }


def run_broadcast(instruction: str, agents: list) -> list:
    """指示を全エージェントへ並列に一斉送信し、結果リストを返す（順序維持）。

    Returns: [{agent_name, provider, model, status, response, tools_used, error}, ...]
    """
    if not agents:
        return []

    results_by_index = {}
    workers = min(MAX_WORKERS, len(agents))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_one, agent, instruction): idx
            for idx, agent in enumerate(agents)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results_by_index[idx] = future.result()

    return [results_by_index[i] for i in range(len(agents))]
