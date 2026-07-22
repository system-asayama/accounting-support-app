"""このアプリを「MCPサーバー」として公開するモジュール。

各AI（Claude Code / ChatGPT / Gemini など）がこのサーバーに接続し、
- 取り込んだ freee の取引（仕訳）データを読む
- 解析結果をアプリへ書き戻す
という操作を MCP ツール経由で行える。

実行方法:
  stdio（ローカル接続 / Claude Code・Gemini CLI 向け）:
      python mcp_server.py
  HTTP（公開接続 / ChatGPT 開発者モード向け・トークン認証付き）:
      MCP_TRANSPORT=http MCP_AUTH_TOKEN=xxxxx python mcp_server.py

DB は Flask アプリと同じものを共有する（DATABASE_URL、無ければ instance/app.db）。
"""
import json
import os
from datetime import datetime

from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import DealAnalysis, FreeeConnection, ImportedDeal, db


# ---------------------------------------------------------------------------
# DB（Flask アプリと同一の DB を共有）
# ---------------------------------------------------------------------------
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
db.metadata.create_all(_engine)  # テーブルが無ければ作成
SessionLocal = sessionmaker(bind=_engine)


def _default_company_id(session) -> int | None:
    conn = session.get(FreeeConnection, 1)
    return conn.company_id if conn else None


def _deal_to_dict(d: ImportedDeal) -> dict:
    return {
        "deal_id": d.deal_id,
        "company_id": d.company_id,
        "issue_date": d.issue_date,
        "type": d.deal_type,
        "amount": d.amount,
        "partner": d.partner_name,
        "status": d.status,
        "account_items": d.account_items,
    }


# ---------------------------------------------------------------------------
# MCP サーバー定義
# ---------------------------------------------------------------------------
mcp = FastMCP("accounting-support-app")


@mcp.tool()
def list_deals(company_id: int | None = None, limit: int = 50) -> list[dict]:
    """取り込んだ freee の取引（仕訳）一覧を返す。

    company_id を省略すると、アプリで選択中の事業所を対象にする。
    """
    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        q = s.query(ImportedDeal)
        if cid:
            q = q.filter(ImportedDeal.company_id == cid)
        rows = (
            q.order_by(ImportedDeal.issue_date.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return [_deal_to_dict(r) for r in rows]


@mcp.tool()
def get_deal(deal_id: int, company_id: int | None = None) -> dict:
    """取引1件の詳細（明細と、これまでに書き込まれた解析結果）を返す。"""
    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        q = s.query(ImportedDeal).filter(ImportedDeal.deal_id == deal_id)
        if cid:
            q = q.filter(ImportedDeal.company_id == cid)
        d = q.first()
        if d is None:
            return {"error": f"取引 {deal_id} は見つかりませんでした。"}

        details = []
        try:
            details = json.loads(d.details_json or "[]")
        except (ValueError, TypeError):
            details = []

        analyses = (
            s.query(DealAnalysis)
            .filter(
                DealAnalysis.company_id == d.company_id,
                DealAnalysis.deal_id == d.deal_id,
            )
            .order_by(DealAnalysis.created_at)
            .all()
        )
        result = _deal_to_dict(d)
        result["details"] = details
        result["analyses"] = [
            {
                "ai_name": a.ai_name,
                "verdict": a.verdict,
                "result": a.result,
                "created_at": a.created_at.isoformat(),
            }
            for a in analyses
        ]
        return result


@mcp.tool()
def write_analysis(
    deal_id: int,
    ai_name: str,
    result: str,
    verdict: str = "",
    company_id: int | None = None,
) -> dict:
    """取引に対する解析結果をアプリへ書き込む（追記／履歴として残す）。

    - ai_name: どのAIによる解析か（例: "Claude", "ChatGPT", "Gemini"）
    - result:  解析本文
    - verdict: 任意のラベル（例: "ok" / "warning" / "error"）
    """
    ai_name = (ai_name or "").strip()
    result = (result or "").strip()
    if not ai_name:
        return {"ok": False, "error": "ai_name は必須です。"}
    if not result:
        return {"ok": False, "error": "result は必須です。"}

    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        if not cid:
            return {"ok": False, "error": "company_id が特定できません。アプリで事業所を選択するか、company_id を指定してください。"}

        # 対象取引が取り込まれているか確認（無ければ書き込みを拒否）
        exists = (
            s.query(ImportedDeal)
            .filter(
                ImportedDeal.company_id == cid,
                ImportedDeal.deal_id == deal_id,
            )
            .first()
        )
        if exists is None:
            return {
                "ok": False,
                "error": f"取引 {deal_id} は取り込まれていません。先にアプリで取り込んでください。",
            }

        analysis = DealAnalysis(
            company_id=cid,
            deal_id=deal_id,
            ai_name=ai_name[:80],
            result=result,
            verdict=(verdict or "").strip()[:40] or None,
        )
        s.add(analysis)
        s.commit()
        return {"ok": True, "analysis_id": analysis.id, "deal_id": deal_id, "ai_name": ai_name}


@mcp.tool()
def list_analyses(deal_id: int, company_id: int | None = None) -> list[dict]:
    """指定した取引に書き込まれた、各AIの解析結果を返す（比較用）。"""
    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        q = s.query(DealAnalysis).filter(DealAnalysis.deal_id == deal_id)
        if cid:
            q = q.filter(DealAnalysis.company_id == cid)
        rows = q.order_by(DealAnalysis.created_at).all()
        return [
            {
                "ai_name": a.ai_name,
                "verdict": a.verdict,
                "result": a.result,
                "created_at": a.created_at.isoformat(),
            }
            for a in rows
        ]


# ---------------------------------------------------------------------------
# HTTP 実行時のトークン認証（ChatGPT 等の公開接続向け）
# ---------------------------------------------------------------------------
class TokenAuthMiddleware:
    """Authorization: Bearer <token> を検証する最小 ASGI ミドルウェア。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if auth != f"Bearer {self.token}":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error":"unauthorized"}',
                    }
                )
                return
        await self.app(scope, receive, send)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        import uvicorn

        app = mcp.streamable_http_app()
        token = os.environ.get("MCP_AUTH_TOKEN")
        if token:
            app = TokenAuthMiddleware(app, token)
        else:
            print("WARNING: MCP_AUTH_TOKEN 未設定のため認証なしで公開します。", flush=True)
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8001"))
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
