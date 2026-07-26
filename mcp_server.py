"""このアプリを「MCPサーバー」として公開するモジュール。

各AI（Claude Code / ChatGPT / Grok など）がこのサーバーに接続し、
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
from mcp.server.fastmcp import FastMCP, Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    SOURCE_FREEE,
    SOURCE_MF,
    AiTask,
    BankDocument,
    BankDocumentReview,
    BankEntry,
    DealAnalysis,
    FreeeConnection,
    ImportedDeal,
    ImportedReceipt,
    MFConnection,
    db,
    check_balance_continuity,
    make_scope_key,
    match_bank_entries,
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
        "payment_methods": d.wallet_types,
    }


WALLET_LABELS = {
    "wallet": "現金",
    "credit_card": "クレジットカード",
    "bank_account": "銀行口座",
    "private_account_item": "プライベート資金",
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

    - ai_name:    どのAIによる解析か（例: "Claude", "ChatGPT", "Grok"）
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


@mcp.tool()
def bulk_write_ok(
    ai_name: str,
    check_type: str,
    result: str = "",
    exclude_deal_ids: list[int] | None = None,
    only_with_receipt: bool = False,
    company_id: int | None = None,
    office_id: str | None = None,
) -> dict:
    """候補に挙がらなかった取引へ「問題なし（verdict=ok）」を1回でまとめて記録する。

    チェックの証跡を全取引に残すためのツール。個別に判定した（またはこれから判定する）
    取引は exclude_deal_ids で除外すること。

    - ai_name:          どのAIによる記録か（write_analysis と同じ）
    - check_type:       "duplicate" / "cross_payment" / "receipt_link" など。
                        "ocr" は不可（OCRチェックは証憑付き取引を全件個別判定する運用）
    - result:           記録する本文（省略時は「候補抽出で該当なし（問題なし）」）
    - exclude_deal_ids: 除外する取引ID（候補に挙がった取引など）
    - only_with_receipt: True で証憑が紐付いている取引だけを対象にする
                        （証憑紐付けチェックの「証憑あり→問題なし」一括記録用）
    - 同じ ai_name × check_type の記録が既にある取引はスキップする（再実行しても重複しない）
    """
    ai_name = (ai_name or "").strip()
    check_type = (check_type or "").strip()[:40]
    if not ai_name:
        return {"ok": False, "error": "ai_name は必須です。"}
    if not check_type:
        return {"ok": False, "error": "check_type は必須です。"}
    if check_type == "ocr":
        return {
            "ok": False,
            "error": "OCRチェックの一括記録はできません。証憑付き取引は check_receipt_ocr で全件個別に判定し、write_analysis で記録してください。",
        }
    result = (result or "").strip() or "候補抽出で該当なし（問題なし）"
    exclude = set(exclude_deal_ids or [])

    with SessionLocal() as s:
        scope_key, source = _resolve_scope(s, company_id, office_id)
        if not scope_key:
            return {"ok": False, "error": "事業所が特定できません。company_id / office_id を指定してください。"}

        deals = (
            s.query(ImportedDeal)
            .filter(ImportedDeal.scope_key == scope_key)
            .all()
        )
        existing = {
            row[0]
            for row in s.query(DealAnalysis.deal_id)
            .filter(
                DealAnalysis.scope_key == scope_key,
                DealAnalysis.ai_name == ai_name[:80],
                DealAnalysis.check_type == check_type,
            )
            .all()
        }

        created = 0
        skipped_excluded = 0
        skipped_existing = 0
        skipped_no_receipt = 0
        for d in deals:
            if d.deal_id in exclude:
                skipped_excluded += 1
                continue
            if only_with_receipt and not d.has_receipt:
                skipped_no_receipt += 1
                continue
            if d.deal_id in existing:
                skipped_existing += 1
                continue
            s.add(
                DealAnalysis(
                    source=source,
                    scope_key=scope_key,
                    company_id=d.company_id,
                    office_id=d.office_id,
                    deal_id=d.deal_id,
                    ai_name=ai_name[:80],
                    check_type=check_type,
                    result=result,
                    verdict="ok",
                )
            )
            created += 1
        s.commit()
        return {
            "ok": True,
            "check_type": check_type,
            "created": created,
            "skipped_excluded": skipped_excluded,
            "skipped_existing": skipped_existing,
            "skipped_without_receipt": skipped_no_receipt,
            "total_deals": len(deals),
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
def find_cross_payment_duplicates(
    company_id: int | None = None,
    office_id: str | None = None,
    date_window_days: int = 3,
) -> dict:
    """クレジットカード×現金など、決済手段をまたぐ二重計上の候補を検出する。

    「カード明細の自動取込」と「領収書の現金手入力」で同じ支出が二重計上される
    典型パターンを狙うチェック。金額が一致し、発生日の差が date_window_days 以内の
    取引ペアを抽出する（取引先名の表記違いは不問）。決済手段が異なるペアを
    cross_payment=true として最優先で返す。
    """
    from datetime import date as _date

    def _parse(dstr):
        try:
            return _date.fromisoformat((dstr or "")[:10])
        except ValueError:
            return None

    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(ImportedDeal)
        if scope_key:
            q = q.filter(ImportedDeal.scope_key == scope_key)
        rows = q.all()

        by_amount: dict = {}
        for d in rows:
            if not d.amount:
                continue
            by_amount.setdefault((d.deal_type, d.amount), []).append(d)

        pairs = []
        skipped_groups = []
        window = max(0, min(date_window_days, 31))
        for (dtype, amount), items in by_amount.items():
            if len(items) < 2:
                continue
            # 同額が多数ある場合（日次の送料・手数料等の反復取引）はペア列挙しない
            if len(items) > 8:
                skipped_groups.append(
                    {"amount": amount, "count": len(items), "type": dtype}
                )
                continue
            items = sorted(items, key=lambda x: x.issue_date or "")
            for i in range(len(items)):
                for j in range(i + 1, len(items)):
                    a, b = items[i], items[j]
                    da, db_ = _parse(a.issue_date), _parse(b.issue_date)
                    if da is None or db_ is None:
                        continue
                    diff = abs((db_ - da).days)
                    if diff > window:
                        continue
                    wa, wb = a.wallet_types, b.wallet_types
                    cross = bool(wa and wb and set(wa) != set(wb))
                    pairs.append(
                        {
                            "amount": amount,
                            "type": dtype,
                            "date_diff_days": diff,
                            "cross_payment": cross,
                            "deals": [
                                {
                                    "deal_id": x.deal_id,
                                    "issue_date": x.issue_date,
                                    "partner": x.partner_name,
                                    "account_items": x.account_items,
                                    "payment_methods": [
                                        WALLET_LABELS.get(w, w) for w in x.wallet_types
                                    ],
                                    "has_receipt": x.has_receipt,
                                }
                                for x in (a, b)
                            ],
                        }
                    )

        # 決済手段が異なるペア → 日付差が小さい順
        pairs.sort(key=lambda p: (not p["cross_payment"], p["date_diff_days"], -p["amount"]))
        return {
            "pairs": pairs[:50],
            "pair_count": len(pairs),
            "skipped_recurring_groups": skipped_groups,
            "note": (
                "cross_payment=true は決済手段（現金/カード等）が異なるペアで、"
                "二重計上の可能性が最も高い。skipped_recurring_groups は同額多数の"
                "反復取引（送料・手数料等）でペア列挙を省略したグループ。"
            ),
        }


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
def list_bank_entries(
    company_id: int | None = None, office_id: str | None = None, limit: int = 300
) -> list[dict]:
    """通帳照合用。アプリに取り込まれた通帳明細（通帳データ化サービスのCSV由来）を返す。

    明細が0件の場合、通帳データが未取込なので通帳照合チェックはスキップしてよい。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(BankEntry)
        if scope_key:
            q = q.filter(BankEntry.scope_key == scope_key)
        rows = q.order_by(BankEntry.entry_date).limit(max(1, min(limit, 1000))).all()
        return [e.to_dict() for e in rows]


@mcp.tool()
def find_bank_unmatched(
    company_id: int | None = None,
    office_id: str | None = None,
    date_window_days: int = 3,
) -> dict:
    """通帳照合チェック用。通帳明細と取り込み済み取引を機械照合し、不一致を返す。

    照合ルール: 金額一致・入出金の向き一致（入金=income / 出金=expense）・日付±date_window_days。
    - bank_only:   通帳にあるが帳簿に見当たらない明細（記帳漏れ候補）
    - ledger_only: 銀行口座決済なのに通帳に見当たらない取引（deal_id あり →
                   write_analysis(check_type="bank") で判定を記録できる）
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        if not scope_key:
            return {"error": "事業所が特定できません。company_id / office_id を指定してください。"}
        entries = (
            s.query(BankEntry).filter(BankEntry.scope_key == scope_key).all()
        )
        if not entries:
            return {
                "entries_count": 0,
                "note": "通帳明細が取り込まれていません。アプリの「通帳照合」ページでCSVを取り込むと照合できます。このチェックはスキップしてください。",
            }
        deals = (
            s.query(ImportedDeal).filter(ImportedDeal.scope_key == scope_key).all()
        )
        matched, bank_only, ledger_only = match_bank_entries(
            entries, deals, max(0, min(date_window_days, 14))
        )
        balance_issues = check_balance_continuity(entries)
        return {
            "entries_count": len(entries),
            "matched_count": len(matched),
            "balance_issues": balance_issues[:50],
            "bank_only": [e.to_dict() for e in bank_only[:100]],
            "ledger_only": [_deal_to_dict(d) for d in ledger_only[:100]],
            "note": (
                "balance_issues は残高の連続性エラー（データ化の行抜け・読み誤り候補。原本との目視確認を促す）。"
                "bank_only は記帳漏れ候補（チャットで報告）。"
                "ledger_only は各 deal_id へ write_analysis(check_type=\"bank\") で判定を記録する。"
            ),
        }


@mcp.tool()
def list_bank_documents(
    company_id: int | None = None, office_id: str | None = None
) -> list[dict]:
    """データ化対象の原本（通帳・クレカ明細の画像）一覧を返す。

    entries_count=0 の原本は未データ化。get_bank_document で画像を取得して読み取り、
    write_bank_entries で明細を保存する。entries_count>0 の原本は書き起こし済みなので、
    画像と保存済み明細を突き合わせて write_document_review で検証結果を記録する。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(BankDocument)
        if scope_key:
            q = q.filter(BankDocument.scope_key == scope_key)
        docs = q.order_by(BankDocument.uploaded_at).all()
        out = []
        for d in docs:
            cnt = (
                s.query(BankEntry).filter(BankEntry.document_id == d.id).count()
            )
            reviews = (
                s.query(BankDocumentReview)
                .filter(BankDocumentReview.document_id == d.id)
                .order_by(BankDocumentReview.created_at)
                .all()
            )
            latest = {}
            for r in reviews:
                latest[r.ai_name] = {"verdict": r.verdict, "result": r.result[:200]}
            out.append(
                {
                    "document_id": d.id,
                    "doc_type": d.doc_type,
                    "account_name": d.account_name,
                    "filename": d.filename,
                    "entries_count": cnt,
                    "reviews": latest,
                }
            )
        return out


@mcp.tool()
def get_bank_document(document_id: int) -> Image:
    """原本（通帳・クレカ明細）の画像を返す。読み取って write_bank_entries で保存する。"""
    with SessionLocal() as s:
        d = s.get(BankDocument, document_id)
        if d is None or not d.data:
            raise ValueError(f"原本 {document_id} が見つかりません。")
        fmt = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(
            d.content_type or "", "png"
        )
        return Image(data=d.data, format=fmt)


@mcp.tool()
def write_bank_entries(
    document_id: int, ai_name: str, entries: list[dict]
) -> dict:
    """原本から読み取った明細を保存する（同じ原本の既存明細は入れ替え）。

    entries の各要素: {"entry_date": "yyyy-mm-dd", "description": "摘要",
                       "deposit": 入金額 or null, "withdrawal": 出金額 or null,
                       "balance": 残高 or null}
    入出金はどちらか一方に金額を入れる。行の並びは原本の記載順どおりにすること
    （残高連続性チェックが並び順を前提にするため）。
    返り値に balance_issues（残高連続性エラー）が含まれるので、エラーがあれば
    画像の該当箇所を読み直し、修正版で再度このツールを呼ぶこと。
    """
    ai_name = (ai_name or "").strip()
    if not ai_name:
        return {"ok": False, "error": "ai_name は必須です。"}
    if not entries:
        return {"ok": False, "error": "entries が空です。"}
    with SessionLocal() as s:
        d = s.get(BankDocument, document_id)
        if d is None:
            return {"ok": False, "error": f"原本 {document_id} が見つかりません。"}
        label = d.account_name or (d.filename or f"原本{d.id}")
        label = f"{label}（AIデータ化）"[:120]
        s.query(BankEntry).filter(BankEntry.document_id == d.id).delete()
        saved = []
        for row in entries:
            if not isinstance(row, dict):
                continue
            dep = row.get("deposit")
            wd = row.get("withdrawal")
            if dep is None and wd is None:
                continue
            e = BankEntry(
                source=d.source,
                scope_key=d.scope_key,
                scope_name=d.scope_name,
                company_id=d.company_id,
                office_id=d.office_id,
                account_name=label,
                entry_date=(str(row.get("entry_date") or "")[:20] or None),
                description=(str(row.get("description") or "")[:255] or None),
                deposit=int(dep) if dep is not None else None,
                withdrawal=int(wd) if wd is not None else None,
                balance=int(row["balance"]) if row.get("balance") is not None else None,
                document_id=d.id,
            )
            s.add(e)
            saved.append(e)
        s.commit()
        issues = check_balance_continuity(saved)
        return {
            "ok": True,
            "document_id": d.id,
            "saved": len(saved),
            "balance_issues": issues[:50],
            "note": (
                "balance_issues が空なら読み取りは整合。エラーがあれば画像の該当箇所を読み直し、"
                "修正した全行で再度 write_bank_entries を呼ぶ（入れ替え保存）。"
                "検証が済んだら write_document_review で記録すること。"
            ),
        }


@mcp.tool()
def write_document_review(
    document_id: int, ai_name: str, result: str, verdict: str = ""
) -> dict:
    """書き起こし済み明細への検証レビューを記録する（追記型・AIごとの相互チェック用）。

    - 自分が書き起こした場合も、他AIの書き起こしを検証した場合も記録する
    - verdict: ok（原本と一致）/ warning（軽微な相違・要確認）/ error（明確な誤り）
    - result:  検証方法と相違点を日本語で具体的に（例: 3行目の出金 12,000 が画像では 12,800）
    """
    ai_name = (ai_name or "").strip()
    result = (result or "").strip()
    if not ai_name or not result:
        return {"ok": False, "error": "ai_name と result は必須です。"}
    with SessionLocal() as s:
        d = s.get(BankDocument, document_id)
        if d is None:
            return {"ok": False, "error": f"原本 {document_id} が見つかりません。"}
        r = BankDocumentReview(
            document_id=d.id,
            ai_name=ai_name[:80],
            verdict=(verdict or "").strip()[:40] or None,
            result=result,
        )
        s.add(r)
        s.commit()
        return {"ok": True, "review_id": r.id, "document_id": d.id}


@mcp.tool()
def create_task(
    ai_name: str,
    title: str,
    instruction: str,
    task_type: str = "other",
    evidence: str = "",
    related_deal_id: int | None = None,
    company_id: int | None = None,
    office_id: str | None = None,
) -> dict:
    """freee側の修正・登録が必要な作業を ToDo として提案する（チェックAI用）。

    提案されたタスクは「承認待ち」になり、アプリのToDoページで人間が承認してから
    実行AIに渡る。このツールで freee が直接変更されることはない。
    - title:       一目で分かる要約（例: 「7/15 支払手数料 ¥1,900 の重複を削除」）
    - instruction: 実行AIがそのまま作業できる具体的内容
                   （日付・金額・取引先・科目・対象 deal_id・freeeでの操作手順）
    - task_type:   register_deal(登録) / fix_deal(修正) / delete_deal(削除) /
                   link_receipt(証憑紐付け) / other
    - evidence:    根拠（どのチェックで何を検出したか）
    - 同じ事業所に同じ title の未処理タスクがあれば重複作成せずスキップする
    """
    ai_name = (ai_name or "").strip()
    title = (title or "").strip()
    instruction = (instruction or "").strip()
    if not ai_name or not title or not instruction:
        return {"ok": False, "error": "ai_name / title / instruction は必須です。"}
    with SessionLocal() as s:
        scope_key, source = _resolve_scope(s, company_id, office_id)
        if not scope_key:
            return {"ok": False, "error": "事業所が特定できません。company_id / office_id を指定してください。"}
        dup = (
            s.query(AiTask)
            .filter(
                AiTask.scope_key == scope_key,
                AiTask.title == title[:255],
                AiTask.status.in_(["proposed", "approved"]),
            )
            .first()
        )
        if dup is not None:
            return {
                "ok": True,
                "task_id": dup.id,
                "status": dup.status,
                "note": "同じ内容の未処理タスクが既にあるため、新規作成せず既存タスクを返しました。",
            }
        scope_name = None
        sample = (
            s.query(ImportedDeal).filter(ImportedDeal.scope_key == scope_key).first()
        )
        if sample is not None:
            scope_name = sample.scope_name
        t = AiTask(
            source=source,
            scope_key=scope_key,
            scope_name=scope_name,
            company_id=company_id if company_id is not None else (sample.company_id if sample else None),
            office_id=office_id,
            task_type=(task_type or "other").strip()[:40] or "other",
            title=title[:255],
            instruction=instruction,
            evidence=(evidence or "").strip() or None,
            related_deal_id=related_deal_id,
            created_by=ai_name[:80],
            status="proposed",
        )
        s.add(t)
        s.commit()
        return {
            "ok": True,
            "task_id": t.id,
            "status": "proposed",
            "note": "承認待ちとして登録しました。人間がToDoページで承認すると実行AIに渡ります。",
        }


@mcp.tool()
def list_tasks(
    status: str = "approved",
    company_id: int | None = None,
    office_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """ToDoタスクの一覧を返す。

    実行AIは status="approved"（人間が承認済み・実行待ち）だけを取得して作業すること。
    status="proposed"（承認待ち）のタスクを実行してはならない。
    status は proposed / approved / done / rejected / all。
    """
    with SessionLocal() as s:
        scope_key, _ = _resolve_scope(s, company_id, office_id)
        q = s.query(AiTask)
        if scope_key:
            q = q.filter(AiTask.scope_key == scope_key)
        if status and status != "all":
            q = q.filter(AiTask.status == status)
        rows = q.order_by(AiTask.created_at).limit(max(1, min(limit, 200))).all()
        return [t.to_dict() for t in rows]


@mcp.tool()
def complete_task(
    task_id: int, ai_name: str, result: str, success: bool = True
) -> dict:
    """承認済みタスクの実行結果を報告する（実行AI用）。

    - success=True:  タスクを「完了」にし、result に実行内容（freee側で何をどう
                     登録・修正したか、作成された取引IDなど）を記録する
    - success=False: タスクは「実行待ち」のまま、result に失敗理由を記録する
    承認待ち（proposed）のタスクには使えない。
    """
    ai_name = (ai_name or "").strip()
    result = (result or "").strip()
    if not ai_name or not result:
        return {"ok": False, "error": "ai_name と result は必須です。"}
    with SessionLocal() as s:
        t = s.get(AiTask, task_id)
        if t is None:
            return {"ok": False, "error": f"タスク {task_id} が見つかりません。"}
        if t.status == "proposed":
            return {"ok": False, "error": "このタスクはまだ人間の承認待ちです。実行しないでください。"}
        if t.status in ("done", "rejected"):
            return {"ok": False, "error": f"このタスクは既に {t.status} です。"}
        stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        note = f"[{stamp} {ai_name}] {'完了' if success else '失敗'}: {result}"
        t.result_note = (t.result_note + "\n" + note) if t.result_note else note
        if success:
            t.status = "done"
            t.executed_by = ai_name[:80]
            t.executed_at = datetime.utcnow()
        s.commit()
        return {"ok": True, "task_id": t.id, "status": t.status}


@mcp.tool()
def import_bank_txns(
    company_id: int, start_date: str = "", end_date: str = ""
) -> dict:
    """freee のデータ化結果（銀行口座の入出金明細 wallet_txns）を通帳明細として取り込む。

    freee「データ化申込」で通帳をデータ化した結果は入出金明細としてfreeeに入るため、
    これを取り込めば find_bank_unmatched で残高連続性チェックと帳簿突合ができる。
    - start_date / end_date: 対象期間 (yyyy-mm-dd)。省略で全期間
    - 同じ口座の既存freee明細は入れ替え（重複しない）
    """
    with SessionLocal() as s:
        conn = s.get(FreeeConnection, 1)
        if not conn or not conn.access_token:
            return {"error": "freee と連携されていません。"}
        scope_key = make_scope_key(SOURCE_FREEE, company_id=company_id)
        sample = (
            s.query(ImportedDeal).filter(ImportedDeal.scope_key == scope_key).first()
        )

        w = _freee_api(s, conn, "/api/1/walletables", {"company_id": company_id})
        if "error" in w:
            return w
        accounts = [
            x for x in w.get("walletables", []) if x.get("type") == "bank_account"
        ]
        if not accounts:
            return {
                "ok": False,
                "error": "freeeに銀行口座（walletable）が登録されていません。",
            }

        total, imported_accounts = 0, []
        for acc in accounts:
            label = f"{acc.get('name') or acc.get('id')}（freee明細）"
            s.query(BankEntry).filter(
                BankEntry.scope_key == scope_key, BankEntry.account_name == label
            ).delete()
            offset = 0
            imported_this = 0
            while offset < 5000:
                params = {
                    "company_id": company_id,
                    "walletable_type": "bank_account",
                    "walletable_id": acc["id"],
                    "limit": 100,
                    "offset": offset,
                }
                if start_date:
                    params["start_date"] = start_date
                if end_date:
                    params["end_date"] = end_date
                page = _freee_api(s, conn, "/api/1/wallet_txns", params)
                if "error" in page:
                    return page
                txns = page.get("wallet_txns", [])
                if not txns:
                    break
                for t in txns:
                    side = t.get("entry_side")
                    amount = t.get("amount")
                    s.add(
                        BankEntry(
                            source=SOURCE_FREEE,
                            scope_key=scope_key,
                            scope_name=(sample.scope_name if sample else None),
                            company_id=company_id,
                            account_name=label,
                            entry_date=t.get("date"),
                            description=(t.get("description") or "")[:255] or None,
                            deposit=amount if side == "income" else None,
                            withdrawal=amount if side == "expense" else None,
                            balance=t.get("balance"),
                        )
                    )
                    total += 1
                    imported_this += 1
                offset += 100
                if len(txns) < 100:
                    break
            if imported_this:
                imported_accounts.append({"account": label, "count": imported_this})
        s.commit()
        return {
            "ok": True,
            "company_id": company_id,
            "entries_imported": total,
            "accounts": imported_accounts,
            "note": "続けて find_bank_unmatched で残高連続性チェックと帳簿突合を実行してください。",
        }


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


def _freee_api(session, conn, path: str, params: dict, service: str = "accounting") -> dict:
    """freee API へGET（トークン自動更新付き）。エラーは {"error": ...} で返す。"""
    base = FREEE_SERVICE_BASE.get(service)
    if not base:
        return {"error": f"未対応の service です: {service}"}

    def _do():
        return requests.get(
            f"{base}{path}",
            headers={"Authorization": f"Bearer {conn.access_token}", "Accept": "application/json"},
            params=params,
            timeout=30,
        )

    try:
        resp = _do()
        if resp.status_code == 401 and _refresh_freee(session, conn):
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
def freee_get(path: str, params: dict | None = None, service: str = "accounting") -> dict:
    """freee API に直接 GET して生データを返す（読み取り専用）。

    3チェック用の限定データではなく、freee の“あらゆる情報”を取得するための汎用ツール。
    - path:    例 "/api/1/reports/trial_pl" や "/api/1/journals" など
    - params:  クエリ（例 {"limit": 100, "type": "income"}）。accounting では company_id を自動補完する
    - service: accounting(既定) / hr / invoice / pm / sm / it_management
    利用可能なパスは freee_list_paths を参照。company_id は freee_context / find_company で取得できる。
    """
    q = dict(params or {})
    with SessionLocal() as s:
        conn = s.get(FreeeConnection, 1)
        if not conn or not conn.access_token:
            return {"error": "freee と連携されていません。アプリで連携してください。"}
        if service == "accounting" and "company_id" not in q and conn.company_id:
            q["company_id"] = conn.company_id
        return _freee_api(s, conn, path, q, service)


@mcp.tool()
def find_company(name: str) -> dict:
    """顧問先（事業所）を名前で検索し、company_id を返す。

    部分一致で候補を返す。以降のツール（import_deals / list_deals / freee_get 等）に
    その company_id を渡せば、事業所を切り替えながら作業できる。
    """
    name = (name or "").strip()
    if not name:
        return {"error": "name を指定してください。"}
    with SessionLocal() as s:
        conn = s.get(FreeeConnection, 1)
        if not conn or not conn.access_token:
            return {"error": "freee と連携されていません。"}
        data = _freee_api(s, conn, "/api/1/companies", {})
        if "error" in data:
            return data
        companies = data.get("companies", [])
        matches = []
        for c in companies:
            disp = c.get("display_name") or c.get("name") or ""
            if name in disp:
                matches.append({"company_id": c.get("id"), "name": disp})
        return {
            "query": name,
            "count": len(matches),
            "matches": matches[:20],
            "note": "候補が多い場合は名前をより具体的にしてください。",
        }


@mcp.tool()
def import_deals(
    company_id: int,
    start_date: str = "",
    end_date: str = "",
    deal_type: str = "",
    max_deals: int = 500,
) -> dict:
    """freee から取引（仕訳）と証憑（OCR結果）をアプリへ取り込む。

    チェックツール（find_duplicate_candidates 等）や write_analysis は取り込み済み
    データが対象のため、チェック前にこのツールで対象顧問先のデータを取り込む。
    - company_id: find_company で取得した事業所ID
    - start_date / end_date: 発生日の範囲 (yyyy-mm-dd)。省略時は取り込んだ取引の
      発生日から自動決定して証憑も必ず取り込む
    - deal_type: "income" / "expense"（省略で全て）
    - max_deals: 取り込み上限（既定500）
    実行後は取引・証憑・OCR結果が揃い、そのまま check_receipt_ocr まで実行できる。
    """
    with SessionLocal() as s:
        conn = s.get(FreeeConnection, 1)
        if not conn or not conn.access_token:
            return {"error": "freee と連携されていません。"}

        # 名称マップ
        account_map = {}
        partner_map = {}
        acc = _freee_api(s, conn, "/api/1/account_items", {"company_id": company_id})
        if "error" in acc:
            return acc
        for a in acc.get("account_items", []):
            account_map[a["id"]] = a.get("name", "")
        par = _freee_api(s, conn, "/api/1/partners", {"company_id": company_id, "limit": 100})
        for p in par.get("partners", []) if "error" not in par else []:
            partner_map[p["id"]] = p.get("name", "")

        # 事業所名スナップショット（選択画面の表示用）
        scope_name = None
        comp = _freee_api(s, conn, f"/api/1/companies/{company_id}", {})
        if "error" not in comp:
            c = comp.get("company") or {}
            scope_name = c.get("display_name") or c.get("name")

        # 取引（ページング）
        scope_key = make_scope_key(SOURCE_FREEE, company_id=company_id)
        created, updated, offset = 0, 0, 0
        cap = max(1, min(max_deals, 2000))
        while offset < cap:
            params = {"company_id": company_id, "limit": 100, "offset": offset}
            if start_date:
                params["start_issue_date"] = start_date
            if end_date:
                params["end_issue_date"] = end_date
            if deal_type in ("income", "expense"):
                params["type"] = deal_type
            page = _freee_api(s, conn, "/api/1/deals", params)
            if "error" in page:
                return page
            deals = page.get("deals", [])
            if not deals:
                break
            for d in deals:
                details = d.get("details") or []
                names = " / ".join(
                    account_map.get(det.get("account_item_id"), str(det.get("account_item_id")))
                    for det in details
                )
                receipt_ids = [r.get("id") for r in (d.get("receipts") or []) if r.get("id")]
                row = (
                    s.query(ImportedDeal)
                    .filter(ImportedDeal.scope_key == scope_key, ImportedDeal.deal_id == d["id"])
                    .first()
                )
                if row is None:
                    row = ImportedDeal(deal_id=d["id"])
                    s.add(row)
                    created += 1
                else:
                    updated += 1
                row.source = SOURCE_FREEE
                row.scope_key = scope_key
                row.scope_name = scope_name
                row.company_id = company_id
                row.issue_date = d.get("issue_date")
                row.deal_type = d.get("type")
                row.amount = d.get("amount")
                row.partner_name = partner_map.get(d.get("partner_id")) or None
                row.status = d.get("status")
                row.account_items = names or None
                row.details_json = json.dumps(details, ensure_ascii=False)
                row.receipt_ids = receipt_ids
                row.payments_json = json.dumps(
                    d.get("payments") or [], ensure_ascii=False
                )
                row.imported_at = datetime.utcnow()
            offset += 100
            if len(deals) < 100:
                break

        def store_receipt(r: dict) -> None:
            meta = r.get("receipt_metadatum") or {}
            amount = meta.get("amount")
            ir = (
                s.query(ImportedReceipt)
                .filter(
                    ImportedReceipt.scope_key == scope_key,
                    ImportedReceipt.receipt_id == r["id"],
                )
                .first()
            )
            if ir is None:
                ir = ImportedReceipt(receipt_id=r["id"])
                s.add(ir)
            ir.source = SOURCE_FREEE
            ir.scope_key = scope_key
            ir.company_id = company_id
            ir.status = r.get("status")
            ir.description = (r.get("description") or "")[:255] or None
            ir.document_type = r.get("document_type")
            ir.origin = r.get("origin")
            ir.created_at = r.get("created_at")
            ir.ocr_partner_name = meta.get("partner_name") or None
            ir.ocr_issue_date = meta.get("issue_date") or None
            ir.ocr_amount = int(amount) if isinstance(amount, (int, float)) else None
            ir.metadatum_json = json.dumps(meta, ensure_ascii=False)
            ir.imported_at = datetime.utcnow()

        # 証憑は常に取り込む。期間未指定なら取り込んだ取引の発生日から範囲を自動決定する
        # （freee の /receipts は start_date / end_date が必須のため）。
        receipts_imported = 0
        r_start, r_end = start_date, end_date
        if not (r_start and r_end):
            from sqlalchemy import func as _func

            span = (
                s.query(
                    _func.min(ImportedDeal.issue_date),
                    _func.max(ImportedDeal.issue_date),
                )
                .filter(ImportedDeal.scope_key == scope_key)
                .first()
            )
            if span and span[0] and span[1]:
                r_start, r_end = str(span[0]), str(span[1])
        if r_start and r_end:
            r_offset = 0
            while r_offset < 2000:
                rec = _freee_api(
                    s,
                    conn,
                    "/api/1/receipts",
                    {
                        "company_id": company_id,
                        "start_date": r_start,
                        "end_date": r_end,
                        "limit": 100,
                        "offset": r_offset,
                    },
                )
                if "error" in rec:
                    break
                receipts = rec.get("receipts", [])
                if not receipts:
                    break
                for r in receipts:
                    if r.get("id"):
                        store_receipt(r)
                        receipts_imported += 1
                r_offset += 100
                if len(receipts) < 100:
                    break
        s.flush()

        # 取引に紐付いているのに一覧に出てこなかった証憑（発生日が期間外 等）はID指定で個別取得し、
        # check_receipt_ocr が「証憑が未取り込み」にならないようにする。
        linked_ids = set()
        for d_row in (
            s.query(ImportedDeal).filter(ImportedDeal.scope_key == scope_key).all()
        ):
            linked_ids.update(d_row.receipt_ids)
        existing_ids = {
            rid
            for (rid,) in s.query(ImportedReceipt.receipt_id)
            .filter(ImportedReceipt.scope_key == scope_key)
            .all()
        }
        receipts_fetched_individually = 0
        for rid in sorted(linked_ids - existing_ids)[:200]:
            one = _freee_api(
                s, conn, f"/api/1/receipts/{rid}", {"company_id": company_id}
            )
            if "error" in one:
                continue
            r = one.get("receipt") or {}
            if r.get("id"):
                store_receipt(r)
                receipts_fetched_individually += 1

        s.commit()
        return {
            "ok": True,
            "company_id": company_id,
            "deals_created": created,
            "deals_updated": updated,
            "receipts_imported": receipts_imported,
            "receipts_fetched_individually": receipts_fetched_individually,
            "receipt_period": {"start_date": r_start or None, "end_date": r_end or None},
            "note": "取引・証憑（OCR結果）を取り込みました。以降のチェックツールには company_id を渡してください。",
        }


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
