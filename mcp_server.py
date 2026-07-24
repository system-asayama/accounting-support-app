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
from datetime import datetime, timedelta

import requests
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    SOURCE_FREEE,
    SOURCE_MF,
    DealAnalysis,
    FreeeConnection,
    ImportedDeal,
    ImportedReceipt,
    MFConnection,
    db,
    make_scope_key,
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


def _resolve_scope(session, company_id=None, office_id=None):
    """(scope_key, source) を返す。

    明示指定（company_id=freee / office_id=MF）が無ければ、有効な接続から判定する
    （freee 接続を優先、無ければ MF 接続）。
    """
    if company_id is not None:
        return make_scope_key(SOURCE_FREEE, company_id=company_id), SOURCE_FREEE
    if office_id is not None:
        return make_scope_key(SOURCE_MF, office_id=office_id), SOURCE_MF
    fc = session.get(FreeeConnection, 1)
    if fc and fc.company_id:
        return make_scope_key(SOURCE_FREEE, company_id=fc.company_id), SOURCE_FREEE
    mf = session.get(MFConnection, 1)
    if mf and mf.office_id:
        return make_scope_key(SOURCE_MF, office_id=mf.office_id), SOURCE_MF
    return None, None


def _deal_to_dict(d: ImportedDeal) -> dict:
    return {
        "deal_id": d.deal_id,
        "source": d.source,
        "company_id": d.company_id,
        "office_id": d.office_id,
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
def _transport_security():
    """HTTP公開時のHost/Origin検証設定。

    リバースプロキシ経由で公開ドメインのHostヘッダが届くため、既定のDNSリバインディング
    保護（localhost以外を421で拒否）を公開ドメイン許可に緩和する。秘密パスで保護している
    ため、Host検証は実質不要だが、明示的に許可リストを設定する。
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["localhost", "127.0.0.1", "mcp", "accounting-support.samurai-hub.com"]
    extra = (os.environ.get("MCP_ALLOWED_HOSTS") or "").strip()
    if extra:
        hosts += [h.strip() for h in extra.split(",") if h.strip()]
    # ポート付きHostヘッダにも対応
    hosts += [f"{h}:8001" for h in list(hosts)] + [f"{h}:443" for h in list(hosts)]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=hosts,
        allowed_origins=["https://claude.ai", "https://chatgpt.com", "https://chat.openai.com"],
    )


mcp = FastMCP("accounting-support-app", transport_security=_transport_security())


@mcp.tool()
def list_deals(
    company_id: int | None = None, office_id: str | None = None, limit: int = 50
) -> list[dict]:
    """取り込んだ取引（仕訳）一覧を返す。

    対象事業所は、freee は company_id、マネーフォワードは office_id で指定する。
    どちらも省略すると、アプリで選択中の事業所（有効な接続）を対象にする。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(ImportedDeal)
        if scope_key:
            q = q.filter(ImportedDeal.scope_key == scope_key)
        rows = (
            q.order_by(ImportedDeal.issue_date.desc())
            .limit(max(1, min(limit, 200)))
            .all()
        )
        return [_deal_to_dict(r) for r in rows]


@mcp.tool()
def get_deal(
    deal_id: int, company_id: int | None = None, office_id: str | None = None
) -> dict:
    """取引1件の詳細（明細と、これまでに書き込まれた解析結果）を返す。"""
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(ImportedDeal).filter(ImportedDeal.deal_id == deal_id)
        if scope_key:
            q = q.filter(ImportedDeal.scope_key == scope_key)
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
                DealAnalysis.scope_key == d.scope_key,
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
    office_id: str | None = None,
) -> dict:
    """取引に対する解析結果をアプリへ書き込む（追記／履歴として残す）。

    - ai_name:    どのAIによる解析か（例: "Claude", "ChatGPT", "Gemini"）
    - result:     解析本文
    - check_type: チェック種別 "duplicate"(重複) / "receipt_link"(証憑紐付け) /
                  "ocr"(読み取り結果) / "general"
    - verdict:    任意のラベル（例: "ok" / "warning" / "error"）
    - company_id / office_id: 対象事業所（freee は company_id、MF は office_id）
    """
    ai_name = (ai_name or "").strip()
    result = (result or "").strip()
    if not ai_name:
        return {"ok": False, "error": "ai_name は必須です。"}
    if not result:
        return {"ok": False, "error": "result は必須です。"}

    with SessionLocal() as s:
        scope_key, source = _resolve_scope(s, company_id, office_id)
        if not scope_key:
            return {"ok": False, "error": "事業所が特定できません。アプリで事業所を選択するか、company_id / office_id を指定してください。"}

        # 対象取引が取り込まれているか確認（無ければ書き込みを拒否）
        target = (
            s.query(ImportedDeal)
            .filter(
                ImportedDeal.scope_key == scope_key,
                ImportedDeal.deal_id == deal_id,
            )
            .first()
        )
        if target is None:
            return {
                "ok": False,
                "error": f"取引 {deal_id} は取り込まれていません。先にアプリで取り込んでください。",
            }

        analysis = DealAnalysis(
            source=source,
            scope_key=scope_key,
            company_id=target.company_id,
            office_id=target.office_id,
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
def find_duplicate_candidates(
    company_id: int | None = None, office_id: str | None = None
) -> list[dict]:
    """仕訳の重複チェック用。

    取り込んだ取引のうち、発生日・金額・取引先が一致する取引を「重複候補」の
    グループとして返す（同一グループに2件以上ある場合のみ）。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(ImportedDeal)
        if scope_key:
            q = q.filter(ImportedDeal.scope_key == scope_key)
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
def list_deals_without_receipt(
    company_id: int | None = None, office_id: str | None = None, limit: int = 100
) -> list[dict]:
    """証憑（領収書・レシート）の紐付けチェック用。

    取り込んだ取引のうち、証憑（ファイルボックス）が1件も紐付いていない取引を返す。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(ImportedDeal)
        if scope_key:
            q = q.filter(ImportedDeal.scope_key == scope_key)
        rows = (
            q.order_by(ImportedDeal.issue_date.desc())
            .limit(max(1, min(limit, 500)))
            .all()
        )
        return [_deal_to_dict(d) for d in rows if not d.has_receipt]


@mcp.tool()
def list_receipts(
    company_id: int | None = None,
    office_id: str | None = None,
    only_unlinked: bool = False,
    limit: int = 100,
) -> list[dict]:
    """取り込んだ証憑（ファイルボックス）一覧を返す。OCR読み取り結果を含む。

    only_unlinked=True の場合、どの取引にも紐付いていない証憑だけを返す
    （＝証憑側から見た紐付け漏れチェック）。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        rq = s.query(ImportedReceipt)
        if scope_key:
            rq = rq.filter(ImportedReceipt.scope_key == scope_key)
        receipts = rq.order_by(ImportedReceipt.created_at.desc()).limit(
            max(1, min(limit, 500))
        ).all()

        if only_unlinked:
            dq = s.query(ImportedDeal)
            if scope_key:
                dq = dq.filter(ImportedDeal.scope_key == scope_key)
            linked = set()
            for d in dq.all():
                linked.update(d.receipt_ids)
            receipts = [r for r in receipts if r.receipt_id not in linked]

        return [_receipt_to_dict(r) for r in receipts]


@mcp.tool()
def check_receipt_ocr(
    deal_id: int, company_id: int | None = None, office_id: str | None = None
) -> dict:
    """領収書・レシートの読み取り（OCR）結果のチェック用。

    指定した取引の値と、紐付いた証憑のOCR読み取り値（取引先・日付・金額）を並べ、
    自動判定した不一致フラグを添えて返す。AIはこれを元に妥当性を判断する。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        dq = s.query(ImportedDeal).filter(ImportedDeal.deal_id == deal_id)
        if scope_key:
            dq = dq.filter(ImportedDeal.scope_key == scope_key)
        d = dq.first()
        if d is None:
            return {"error": f"取引 {deal_id} は取り込まれていません。"}

        comparisons = []
        for rid in d.receipt_ids:
            r = (
                s.query(ImportedReceipt)
                .filter(
                    ImportedReceipt.scope_key == d.scope_key,
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
def list_analyses(
    deal_id: int, company_id: int | None = None, office_id: str | None = None
) -> list[dict]:
    """指定した取引に書き込まれた、各AIの解析結果を返す（比較用）。"""
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(DealAnalysis).filter(DealAnalysis.deal_id == deal_id)
        if scope_key:
            q = q.filter(DealAnalysis.scope_key == scope_key)
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
# 汎用パススルー（freee / MF のあらゆる情報を読み取り専用で取得）
#
# 個別テーブルに全部取り込むのではなく、アプリが保持するトークンを使って
# 各社APIへ直接GETし、生データをAIへ渡す。書き込み(POST/PUT/DELETE)は行わない。
# ---------------------------------------------------------------------------
FREEE_TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
FREEE_SERVICE_BASE = {
    "accounting": "https://api.freee.co.jp",
    "hr": "https://api.freee.co.jp/hr",
    "invoice": "https://api.freee.co.jp/iv",
    "pm": "https://api.freee.co.jp/pm",
    "sm": "https://api.freee.co.jp/sm",
    "it_management": "https://api.freee.co.jp",
}
MF_TOKEN_URL = "https://api.biz.moneyforward.com/token"


def _refresh_freee(session, conn) -> bool:
    cid = conn.client_id or os.environ.get("FREEE_CLIENT_ID")
    secret = conn.client_secret or os.environ.get("FREEE_CLIENT_SECRET")
    if not (cid and secret and conn.refresh_token):
        return False
    try:
        r = requests.post(
            FREEE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": cid,
                "client_secret": secret,
                "refresh_token": conn.refresh_token,
            },
            timeout=30,
        )
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    p = r.json()
    conn.access_token = p.get("access_token")
    conn.refresh_token = p.get("refresh_token") or conn.refresh_token
    if p.get("expires_in"):
        conn.token_expires_at = datetime.utcnow() + timedelta(seconds=int(p["expires_in"]))
    session.commit()
    return True


@mcp.tool()
def freee_get(path: str, params: dict | None = None, service: str = "accounting") -> dict:
    """freee API に直接 GET して生データを返す（読み取り専用）。

    3チェック用の限定データではなく、freee の“あらゆる情報”を取得するための汎用ツール。
    - path:    例 "/api/1/reports/trial_pl" や "/api/1/journals" など
    - params:  クエリ（例 {"limit": 100, "type": "income"}）。accounting では company_id を自動補完する
    - service: accounting(既定) / hr / invoice / pm / sm / it_management
    利用可能なパスは freee_list_paths を参照。company_id は freee_context で取得できる。
    """
    base = FREEE_SERVICE_BASE.get(service)
    if not base:
        return {"error": f"未対応の service です: {service}"}
    q = dict(params or {})
    with SessionLocal() as s:
        conn = s.get(FreeeConnection, 1)
        if not conn or not conn.access_token:
            return {"error": "freee と連携されていません。アプリで連携してください。"}
        # accounting は company_id 必須のものが多いので自動補完
        if service == "accounting" and "company_id" not in q and conn.company_id:
            q["company_id"] = conn.company_id

        def _do():
            return requests.get(
                f"{base}{path}",
                headers={"Authorization": f"Bearer {conn.access_token}", "Accept": "application/json"},
                params=q,
                timeout=30,
            )

        try:
            resp = _do()
            if resp.status_code == 401 and _refresh_freee(s, conn):
                resp = _do()
        except requests.RequestException as exc:
            return {"error": f"freee への接続に失敗しました: {exc}"}
        if resp.status_code >= 400:
            return {"error": f"freee API エラー（{resp.status_code}）", "body": resp.text[:2000]}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:5000]}


@mcp.tool()
def freee_list_paths() -> dict:
    """freee で読み取り可能な主なエンドポイント一覧（freee_get の path に使う）。"""
    return {
        "accounting": [
            "/api/1/companies",
            "/api/1/deals",
            "/api/1/deals/{id}",
            "/api/1/account_items",
            "/api/1/partners",
            "/api/1/items",
            "/api/1/sections",
            "/api/1/tags",
            "/api/1/walletables",
            "/api/1/wallet_txns",
            "/api/1/manual_journals",
            "/api/1/transfers",
            "/api/1/receipts",
            "/api/1/journals",
            "/api/1/reports/trial_bs",
            "/api/1/reports/trial_pl",
            "/api/1/reports/general_ledgers",
            "/api/1/expense_applications",
            "/api/1/payment_requests",
            "/api/1/fixed_assets",
            "/api/1/taxes/codes",
            "/api/1/users/me",
        ],
        "hr": ["/api/v1/employees", "/api/v1/salaries/employee_payroll_statements"],
        "invoice": ["/invoices", "/quotations", "/delivery_slips"],
        "pm": ["/projects", "/workloads"],
        "sm": ["/sales", "/sales_orders", "/quotations"],
        "note": "company_id は accounting で自動補完。reports 系は fiscal_year 等の指定が必要な場合あり。",
    }


@mcp.tool()
def freee_context() -> dict:
    """freee 連携の現在状態（選択中の事業所ID・名称など）を返す。"""
    with SessionLocal() as s:
        conn = s.get(FreeeConnection, 1)
        if not conn or not conn.access_token:
            return {"connected": False}
        return {
            "connected": True,
            "company_id": conn.company_id,
            "company_name": conn.company_name,
        }


def _refresh_mf(session, conn) -> bool:
    cid = conn.client_id or os.environ.get("MF_CLIENT_ID")
    secret = conn.client_secret or os.environ.get("MF_CLIENT_SECRET")
    if not (cid and secret and conn.refresh_token):
        return False
    try:
        r = requests.post(
            MF_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": cid,
                "client_secret": secret,
                "refresh_token": conn.refresh_token,
            },
            timeout=30,
        )
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    p = r.json()
    conn.access_token = p.get("access_token")
    conn.refresh_token = p.get("refresh_token") or conn.refresh_token
    if p.get("expires_in"):
        conn.token_expires_at = datetime.utcnow() + timedelta(seconds=int(p["expires_in"]))
    session.commit()
    return True


@mcp.tool()
def mf_get(path: str, params: dict | None = None) -> dict:
    """マネーフォワード クラウド会計 API に直接 GET して生データを返す（読み取り専用）。

    - path:   例 "/accounting/v1/offices" など（実環境の仕様に合わせる）
    - params: クエリ
    ベースURLは MF_API_BASE 環境変数（既定 https://api.biz.moneyforward.com）。
    """
    base = os.environ.get("MF_API_BASE", "https://api.biz.moneyforward.com")
    q = dict(params or {})
    with SessionLocal() as s:
        conn = s.get(MFConnection, 1)
        if not conn or not conn.access_token:
            return {"error": "マネーフォワードと連携されていません。"}

        def _do():
            return requests.get(
                f"{base}{path}",
                headers={"Authorization": f"Bearer {conn.access_token}", "Accept": "application/json"},
                params=q,
                timeout=30,
            )

        try:
            resp = _do()
            if resp.status_code == 401 and _refresh_mf(s, conn):
                resp = _do()
        except requests.RequestException as exc:
            return {"error": f"マネーフォワードへの接続に失敗しました: {exc}"}
        if resp.status_code >= 400:
            return {"error": f"MF API エラー（{resp.status_code}）", "body": resp.text[:2000]}
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:5000]}


@mcp.tool()
def mf_context() -> dict:
    """マネーフォワード連携の現在状態（選択中の事業所ID・名称）を返す。"""
    with SessionLocal() as s:
        conn = s.get(MFConnection, 1)
        if not conn or not conn.access_token:
            return {"connected": False}
        return {
            "connected": True,
            "office_id": conn.office_id,
            "office_name": conn.office_name,
        }


# ---------------------------------------------------------------------------
# HTTP 実行（公開接続向け）
#
# claude.ai / ChatGPT のWebコネクタは接続前に OAuth を試みるため、401 を返すと
# 「サインイン登録に失敗」になる。そこで 401 は返さず、URLパスに秘密トークンを
# 埋め込む方式（/mcp/<secret>）でアクセス制御する。正しいパス以外は 404 になり、
# OAuth フローに入らないので、URLを貼るだけで各AIが接続できる。
# ---------------------------------------------------------------------------
def mcp_secret() -> str:
    """秘密トークンを返す。環境変数を優先し、無ければDBから取得（自動生成）。"""
    env = (os.environ.get("MCP_URL_SECRET") or os.environ.get("MCP_AUTH_TOKEN") or "").strip()
    if env:
        return env
    from models import get_or_create_mcp_secret

    with SessionLocal() as s:
        return get_or_create_mcp_secret(s)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        import uvicorn

        secret = mcp_secret()
        if secret:
            # 秘密パスで公開（認証ヘッダ不要・401なし）
            mcp.settings.streamable_http_path = f"/mcp/{secret}"
        else:
            print(
                "WARNING: MCP_URL_SECRET / MCP_AUTH_TOKEN 未設定のため /mcp を認証なしで公開します。",
                flush=True,
            )
        app = mcp.streamable_http_app()
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8001"))
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
