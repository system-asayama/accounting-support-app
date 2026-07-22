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

from models import (
    DealAnalysis,
    FreeeConnection,
    ImportedDeal,
    ImportedReceipt,
    db,
)


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
        "receipt_ids": d.receipt_ids,
        "has_receipt": d.has_receipt,
    }


def _receipt_to_dict(r: ImportedReceipt) -> dict:
    return {
        "receipt_id": r.receipt_id,
        "company_id": r.company_id,
        "status": r.status,
        "description": r.description,
        "document_type": r.document_type,
        "origin": r.origin,
        "uploaded_at": r.created_at,
        "ocr": {
            "partner_name": r.ocr_partner_name,
            "issue_date": r.ocr_issue_date,
            "amount": r.ocr_amount,
        },
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
    check_type: str = "general",
    verdict: str = "",
    company_id: int | None = None,
) -> dict:
    """取引に対する解析結果をアプリへ書き込む（追記／履歴として残す）。

    - ai_name:    どのAIによる解析か（例: "Claude", "ChatGPT", "Gemini"）
    - result:     解析本文
    - check_type: チェック種別 "duplicate"(重複) / "receipt_link"(証憑紐付け) /
                  "ocr"(読み取り結果) / "general"
    - verdict:    任意のラベル（例: "ok" / "warning" / "error"）
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
            check_type=(check_type or "general").strip()[:40] or "general",
            result=result,
            verdict=(verdict or "").strip()[:40] or None,
        )
        s.add(analysis)
        s.commit()
        return {
            "ok": True,
            "analysis_id": analysis.id,
            "deal_id": deal_id,
            "ai_name": ai_name,
            "check_type": analysis.check_type,
        }


# ---------------------------------------------------------------------------
# 会計チェック用ツール（重複 / 証憑紐付け / OCR読み取り結果）
# ---------------------------------------------------------------------------
@mcp.tool()
def find_duplicate_candidates(company_id: int | None = None) -> list[dict]:
    """仕訳の重複チェック用。

    取り込んだ取引のうち、発生日・金額・取引先が一致する取引を「重複候補」の
    グループとして返す（同一グループに2件以上ある場合のみ）。
    """
    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        q = s.query(ImportedDeal)
        if cid:
            q = q.filter(ImportedDeal.company_id == cid)
        rows = q.all()

        groups: dict[tuple, list] = {}
        for d in rows:
            key = (d.issue_date, d.amount, (d.partner_name or ""))
            groups.setdefault(key, []).append(d)

        out = []
        for (issue_date, amount, partner), items in groups.items():
            if len(items) >= 2:
                out.append(
                    {
                        "issue_date": issue_date,
                        "amount": amount,
                        "partner": partner or None,
                        "count": len(items),
                        "deal_ids": [d.deal_id for d in items],
                        "deals": [_deal_to_dict(d) for d in items],
                    }
                )
        out.sort(key=lambda g: g["count"], reverse=True)
        return out


@mcp.tool()
def list_deals_without_receipt(company_id: int | None = None, limit: int = 100) -> list[dict]:
    """証憑（領収書・レシート）の紐付けチェック用。

    取り込んだ取引のうち、証憑（ファイルボックス）が1件も紐付いていない取引を返す。
    """
    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        q = s.query(ImportedDeal)
        if cid:
            q = q.filter(ImportedDeal.company_id == cid)
        rows = (
            q.order_by(ImportedDeal.issue_date.desc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return [_deal_to_dict(d) for d in rows if not d.has_receipt]


@mcp.tool()
def list_receipts(
    company_id: int | None = None, only_unlinked: bool = False, limit: int = 100
) -> list[dict]:
    """取り込んだ証憑（ファイルボックス）一覧を返す。OCR読み取り結果を含む。

    only_unlinked=True の場合、どの取引にも紐付いていない証憑だけを返す
    （＝証憑側から見た紐付け漏れチェック）。
    """
    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        rq = s.query(ImportedReceipt)
        if cid:
            rq = rq.filter(ImportedReceipt.company_id == cid)
        receipts = rq.order_by(ImportedReceipt.created_at.desc()).limit(
            max(1, min(limit, 500))
        ).all()

        if only_unlinked:
            dq = s.query(ImportedDeal)
            if cid:
                dq = dq.filter(ImportedDeal.company_id == cid)
            linked = set()
            for d in dq.all():
                linked.update(d.receipt_ids)
            receipts = [r for r in receipts if r.receipt_id not in linked]

        return [_receipt_to_dict(r) for r in receipts]


@mcp.tool()
def check_receipt_ocr(deal_id: int, company_id: int | None = None) -> dict:
    """領収書・レシートの読み取り（OCR）結果のチェック用。

    指定した取引の値と、紐付いた証憑のOCR読み取り値（取引先・日付・金額）を並べ、
    自動判定した不一致フラグを添えて返す。AIはこれを元に妥当性を判断する。
    """
    with SessionLocal() as s:
        cid = company_id or _default_company_id(s)
        dq = s.query(ImportedDeal).filter(ImportedDeal.deal_id == deal_id)
        if cid:
            dq = dq.filter(ImportedDeal.company_id == cid)
        d = dq.first()
        if d is None:
            return {"error": f"取引 {deal_id} は取り込まれていません。"}

        comparisons = []
        for rid in d.receipt_ids:
            r = (
                s.query(ImportedReceipt)
                .filter(
                    ImportedReceipt.company_id == d.company_id,
                    ImportedReceipt.receipt_id == rid,
                )
                .first()
            )
            if r is None:
                comparisons.append(
                    {"receipt_id": rid, "note": "証憑が未取り込み。アプリで期間を指定して取り込んでください。"}
                )
                continue
            amount_mismatch = (
                r.ocr_amount is not None
                and d.amount is not None
                and r.ocr_amount != d.amount
            )
            date_mismatch = (
                bool(r.ocr_issue_date)
                and bool(d.issue_date)
                and r.ocr_issue_date != d.issue_date
            )
            comparisons.append(
                {
                    "receipt_id": rid,
                    "ocr": {
                        "partner_name": r.ocr_partner_name,
                        "issue_date": r.ocr_issue_date,
                        "amount": r.ocr_amount,
                    },
                    "flags": {
                        "amount_mismatch": amount_mismatch,
                        "date_mismatch": date_mismatch,
                    },
                }
            )

        return {
            "deal": _deal_to_dict(d),
            "has_receipt": d.has_receipt,
            "comparisons": comparisons,
        }


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
