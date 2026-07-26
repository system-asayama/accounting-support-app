"""データベースモデル定義。"""
import json
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# 対応AIプロバイダ
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_GEMINI = "gemini"
PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_OPENAI, PROVIDER_GEMINI)

# プロバイダごとの表示名・デフォルトモデル
PROVIDER_LABELS = {
    PROVIDER_ANTHROPIC: "Claude (Anthropic)",
    PROVIDER_OPENAI: "ChatGPT (OpenAI)",
    PROVIDER_GEMINI: "Gemini (Google)",
}
PROVIDER_DEFAULT_MODEL = {
    PROVIDER_ANTHROPIC: "claude-sonnet-5",
    PROVIDER_OPENAI: "gpt-5",
    PROVIDER_GEMINI: "gemini-2.5-pro",
}

# 一斉指示のデフォルトモデル（互換用）
DEFAULT_MODEL = PROVIDER_DEFAULT_MODEL[PROVIDER_ANTHROPIC]


class AppSetting(db.Model):
    """アプリ全体のキー/バリュー設定（MCP秘密トークンなど）。"""

    __tablename__ = "app_settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=True)


def get_or_create_mcp_secret(session) -> str:
    """MCP接続URL用の秘密トークンを取得する（無ければ自動生成して保存）。

    web アプリと MCP サーバーの両方から同じ DB を介して呼ばれるため、
    どちらが先に起動しても同じ値を共有できる。
    """
    import secrets as _secrets

    row = session.get(AppSetting, "mcp_url_secret")
    if row is None or not (row.value or "").strip():
        row = AppSetting(key="mcp_url_secret", value=_secrets.token_urlsafe(24))
        session.merge(row)
        session.commit()
        row = session.get(AppSetting, "mcp_url_secret")
    return row.value.strip()

# 利用可能なロール（権限）
ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)


class User(db.Model):
    """ログインユーザー。admin / user の2種類のロールを持つ。"""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_USER)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<User {self.username} ({self.role})>"


class Agent(db.Model):
    """一斉指示の宛先となるAIエージェント。

    「エージェント = モデル + システムプロンプト + 利用するMCPサーバー群」として登録し、
    1つの指示を登録済みの全エージェントへファンアウトする。
    """

    __tablename__ = "agents"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    provider = db.Column(db.String(20), nullable=False, default=PROVIDER_ANTHROPIC)
    model = db.Column(db.String(80), nullable=False, default=DEFAULT_MODEL)
    system_prompt = db.Column(db.Text, nullable=True)
    # MCPサーバー群を JSON 文字列で保持: [{"name","url","authorization_token"}]
    mcp_servers_json = db.Column(db.Text, nullable=False, default="[]")
    max_tokens = db.Column(db.Integer, nullable=False, default=2048)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def mcp_servers(self) -> list:
        try:
            data = json.loads(self.mcp_servers_json or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @mcp_servers.setter
    def mcp_servers(self, value) -> None:
        self.mcp_servers_json = json.dumps(value or [], ensure_ascii=False)

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return f"<Agent {self.name} ({self.provider}/{self.model})>"


class Broadcast(db.Model):
    """1回の一斉指示（指示文と、その実行結果の集合）。"""

    __tablename__ = "broadcasts"

    id = db.Column(db.Integer, primary_key=True)
    instruction = db.Column(db.Text, nullable=False)
    created_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    results = db.relationship(
        "BroadcastResult",
        backref="broadcast",
        cascade="all, delete-orphan",
        order_by="BroadcastResult.id",
    )


class BroadcastResult(db.Model):
    """一斉指示に対する、エージェント1件分の応答結果。"""

    __tablename__ = "broadcast_results"

    id = db.Column(db.Integer, primary_key=True)
    broadcast_id = db.Column(
        db.Integer, db.ForeignKey("broadcasts.id"), nullable=False
    )
    # エージェントは後で削除されうるので名前をスナップショットとして保持
    agent_name = db.Column(db.String(80), nullable=False)
    provider = db.Column(db.String(20), nullable=True)
    model = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="success")  # success / error
    response = db.Column(db.Text, nullable=True)
    tools_used_json = db.Column(db.Text, nullable=True)  # 使用したMCPツール名の JSON 配列
    error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def tools_used(self) -> list:
        try:
            data = json.loads(self.tools_used_json or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []


# 会計ソース
SOURCE_FREEE = "freee"
SOURCE_MF = "mf"


def make_scope_key(source: str, company_id=None, office_id=None) -> str:
    """事業所スコープを表す一意キー。freee は company_id、MF は office_id で識別する。"""
    if source == SOURCE_MF:
        return f"mf:{office_id}"
    return f"freee:{company_id}"


class ImportedDeal(db.Model):
    """会計ソース（freee / MF）から取り込んだ取引（仕訳）のスナップショット。

    MCPサーバー経由で各AIが読み取る「解析対象データ」。live取得と切り離し、
    取り込み時点のデータを保持する。scope_key で事業所単位に絞り込む。
    """

    __tablename__ = "imported_deals"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), nullable=False, default=SOURCE_FREEE)
    scope_key = db.Column(db.String(120), nullable=True, index=True)
    company_id = db.Column(db.BigInteger, nullable=True, index=True)  # freee 事業所ID
    office_id = db.Column(db.String(80), nullable=True)  # MF 事業所ID
    scope_name = db.Column(db.String(255), nullable=True)  # 事業所名スナップショット
    deal_id = db.Column(db.BigInteger, nullable=False)  # ソース上の取引ID
    issue_date = db.Column(db.String(20), nullable=True)
    deal_type = db.Column(db.String(20), nullable=True)  # income / expense
    amount = db.Column(db.BigInteger, nullable=True)
    partner_name = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), nullable=True)
    account_items = db.Column(db.Text, nullable=True)  # 明細の勘定科目名（可読用）
    details_json = db.Column(db.Text, nullable=True)  # 明細の生データ(JSON)
    # 紐付いた証憑（ファイルボックス）ID の JSON 配列
    receipt_ids_json = db.Column(db.Text, nullable=False, default="[]")
    # 支払行（決済口座）の生データ(JSON)。from_walletable_type に
    # wallet(現金) / credit_card / bank_account 等が入る
    payments_json = db.Column(db.Text, nullable=True)
    imported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("company_id", "deal_id", name="uq_imported_deal"),
    )

    @property
    def receipt_ids(self) -> list:
        try:
            data = json.loads(self.receipt_ids_json or "[]")
            return data if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @receipt_ids.setter
    def receipt_ids(self, value) -> None:
        self.receipt_ids_json = json.dumps(value or [], ensure_ascii=False)

    @property
    def has_receipt(self) -> bool:
        return len(self.receipt_ids) > 0

    @property
    def wallet_types(self) -> list:
        """決済に使われた口座区分の一覧（例: ["wallet"], ["credit_card"]）。"""
        try:
            data = json.loads(self.payments_json or "[]")
        except (ValueError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        return sorted(
            {
                p.get("from_walletable_type")
                for p in data
                if isinstance(p, dict) and p.get("from_walletable_type")
            }
        )

    analyses = db.relationship(
        "DealAnalysis",
        primaryjoin="and_(foreign(DealAnalysis.scope_key)==ImportedDeal.scope_key, "
        "foreign(DealAnalysis.deal_id)==ImportedDeal.deal_id)",
        viewonly=True,
        order_by="DealAnalysis.created_at",
    )


class ImportedReceipt(db.Model):
    """freee ファイルボックスから取り込んだ証憑（領収書・レシート）のスナップショット。

    OCR 解析結果（receipt_metadatum）を保持し、取引との紐付け・読み取り結果の
    チェックに使う。
    """

    __tablename__ = "imported_receipts"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), nullable=False, default=SOURCE_FREEE)
    scope_key = db.Column(db.String(120), nullable=True, index=True)
    company_id = db.Column(db.BigInteger, nullable=True, index=True)
    office_id = db.Column(db.String(80), nullable=True)
    receipt_id = db.Column(db.BigInteger, nullable=False)  # ソース上の証憑ID
    status = db.Column(db.String(20), nullable=True)
    description = db.Column(db.String(255), nullable=True)
    document_type = db.Column(db.String(20), nullable=True)  # receipt / invoice / other
    origin = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.String(40), nullable=True)  # アップロード日時(ISO8601)
    # OCR 読み取り結果（receipt_metadatum から抽出）
    ocr_partner_name = db.Column(db.String(255), nullable=True)
    ocr_issue_date = db.Column(db.String(20), nullable=True)
    ocr_amount = db.Column(db.BigInteger, nullable=True)
    metadatum_json = db.Column(db.Text, nullable=True)  # receipt_metadatum の生データ
    imported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("company_id", "receipt_id", name="uq_imported_receipt"),
    )


class DealAnalysis(db.Model):
    """各AIが書き戻した、取引1件に対する解析結果（追記型・履歴として残す）。"""

    __tablename__ = "deal_analyses"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), nullable=False, default=SOURCE_FREEE)
    scope_key = db.Column(db.String(120), nullable=True, index=True)
    company_id = db.Column(db.BigInteger, nullable=True, index=True)
    office_id = db.Column(db.String(80), nullable=True)
    deal_id = db.Column(db.BigInteger, nullable=False, index=True)
    ai_name = db.Column(db.String(80), nullable=False)  # Claude / ChatGPT / Gemini など
    # チェック種別: duplicate（重複）/ receipt_link（証憑紐付け）/ ocr（読み取り結果）/ general
    check_type = db.Column(db.String(40), nullable=True)
    result = db.Column(db.Text, nullable=False)
    verdict = db.Column(db.String(40), nullable=True)  # ok / warning / error など任意ラベル
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class FreeeConnection(db.Model):
    """freee API との接続情報（トークン・選択中の事業所）。

    アプリ全体で1件だけ持つシングルトン的なレコード（id=1）として扱う。
    """

    __tablename__ = "freee_connections"

    id = db.Column(db.Integer, primary_key=True)
    # アプリ情報（画面から設定可能。未設定時は環境変数を使う）
    client_id = db.Column(db.String(255), nullable=True)
    client_secret = db.Column(db.Text, nullable=True)
    redirect_uri = db.Column(db.String(255), nullable=True)
    access_token = db.Column(db.Text, nullable=True)
    refresh_token = db.Column(db.Text, nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    # 選択中の事業所
    company_id = db.Column(db.BigInteger, nullable=True)
    company_name = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    @property
    def is_connected(self) -> bool:
        return bool(self.access_token)

    @property
    def is_expired(self) -> bool:
        if not self.token_expires_at:
            return False
        return datetime.utcnow() >= self.token_expires_at

    @classmethod
    def get(cls) -> "FreeeConnection":
        """唯一の接続レコードを取得（無ければ作成）する。"""
        conn = db.session.get(cls, 1)
        if conn is None:
            conn = cls(id=1)
            db.session.add(conn)
            db.session.commit()
        return conn


class MFConnection(db.Model):
    """マネーフォワード クラウド会計 API との接続情報（トークン・選択中の事業所）。

    freee と同じくシングルトン（id=1）。office_id は文字列（UUID等の可能性）で保持する。
    """

    __tablename__ = "mf_connections"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.String(255), nullable=True)
    client_secret = db.Column(db.Text, nullable=True)
    redirect_uri = db.Column(db.String(255), nullable=True)
    access_token = db.Column(db.Text, nullable=True)
    refresh_token = db.Column(db.Text, nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    office_id = db.Column(db.String(80), nullable=True)  # 選択中の事業所ID
    office_name = db.Column(db.String(255), nullable=True)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    @property
    def is_connected(self) -> bool:
        return bool(self.access_token)

    @property
    def is_expired(self) -> bool:
        if not self.token_expires_at:
            return False
        return datetime.utcnow() >= self.token_expires_at

    @classmethod
    def get(cls) -> "MFConnection":
        conn = db.session.get(cls, 1)
        if conn is None:
            conn = cls(id=1)
            db.session.add(conn)
            db.session.commit()
        return conn


class BankEntry(db.Model):
    """通帳データ化サービス（CSV）から取り込んだ通帳明細1行。

    帳簿（ImportedDeal）との照合に使う。入金は deposit、出金は withdrawal に入る。
    """

    __tablename__ = "bank_entries"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), default=SOURCE_FREEE, nullable=False)
    scope_key = db.Column(db.String(120), nullable=True, index=True)
    scope_name = db.Column(db.String(255), nullable=True)
    company_id = db.Column(db.BigInteger, nullable=True)
    office_id = db.Column(db.String(80), nullable=True)
    account_name = db.Column(db.String(120), nullable=True)  # 口座ラベル（例: 〇〇銀行 普通）
    entry_date = db.Column(db.String(20), nullable=True)  # yyyy-mm-dd
    description = db.Column(db.String(255), nullable=True)  # 摘要
    deposit = db.Column(db.BigInteger, nullable=True)  # 入金
    withdrawal = db.Column(db.BigInteger, nullable=True)  # 出金
    balance = db.Column(db.BigInteger, nullable=True)  # 残高
    document_id = db.Column(db.Integer, nullable=True, index=True)  # AIデータ化の元原本
    imported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "entry_id": self.id,
            "account_name": self.account_name,
            "entry_date": self.entry_date,
            "description": self.description,
            "deposit": self.deposit,
            "withdrawal": self.withdrawal,
            "balance": self.balance,
            "document_id": self.document_id,
        }


def match_bank_entries(entries, deals, date_window_days: int = 3):
    """通帳明細と取引を機械照合する。

    金額一致・入出金の向き一致（入金=income / 出金=expense）・日付±date_window_days で
    1対1に対応付ける（日付差が最小の取引を優先）。
    返り値: (matched[(entry, deal)], bank_only[entry], ledger_only[deal])
    ledger_only は「銀行口座で決済された取引のうち通帳に見当たらないもの」。
    """
    from datetime import datetime as _dt

    def _parse(s):
        try:
            return _dt.strptime((s or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    matched, bank_only = [], []
    used_deal_pks = set()
    for e in sorted(entries, key=lambda x: x.entry_date or ""):
        amount = e.deposit if e.deposit else e.withdrawal
        dtype = "income" if e.deposit else "expense"
        e_date = _parse(e.entry_date)
        best = None
        if amount:
            for d in deals:
                if d.id in used_deal_pks:
                    continue
                if (d.amount or 0) != amount or (d.deal_type or "") != dtype:
                    continue
                d_date = _parse(d.issue_date)
                if e_date is None or d_date is None:
                    continue
                diff = abs((e_date - d_date).days)
                if diff <= date_window_days and (best is None or diff < best[0]):
                    best = (diff, d)
        if best is not None:
            used_deal_pks.add(best[1].id)
            matched.append((e, best[1]))
        else:
            bank_only.append(e)

    ledger_only = [
        d
        for d in deals
        if d.id not in used_deal_pks and "bank_account" in (d.wallet_types or [])
    ]
    return matched, bank_only, ledger_only


def check_balance_continuity(entries):
    """口座ごとに残高の連続性（前行残高±入出金額＝当行残高）を検査する。

    通帳データ化の結果検証用。乱れがある行は「データ化の抜け・金額誤りの候補」。
    残高が入っていない明細はスキップする。返り値: 乱れのリスト。
    """
    issues = []
    by_account = {}
    for e in entries:
        by_account.setdefault(e.account_name or "", []).append(e)
    for account, rows in by_account.items():
        rows = [r for r in rows if r.balance is not None]
        # 取込順（=CSV/明細の並び順）を同日内の順序として使う
        rows.sort(key=lambda r: (r.entry_date or "", r.id or 0))
        prev = None
        for r in rows:
            if prev is not None:
                expected = (prev.balance or 0) + (r.deposit or 0) - (r.withdrawal or 0)
                if expected != r.balance:
                    issues.append(
                        {
                            "account_name": account or None,
                            "entry_date": r.entry_date,
                            "description": r.description,
                            "deposit": r.deposit,
                            "withdrawal": r.withdrawal,
                            "expected_balance": expected,
                            "actual_balance": r.balance,
                            "difference": (r.balance or 0) - expected,
                        }
                    )
            prev = r
    return issues


class BankDocument(db.Model):
    """データ化対象の原本（通帳・クレカ明細の画像）。

    AIが MCP 経由で画像を読み、BankEntry へ書き起こす。書き起こし結果への
    各AIのレビューは BankDocumentReview に記録する。
    """

    __tablename__ = "bank_documents"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), default=SOURCE_FREEE, nullable=False)
    scope_key = db.Column(db.String(120), nullable=True, index=True)
    scope_name = db.Column(db.String(255), nullable=True)
    company_id = db.Column(db.BigInteger, nullable=True)
    office_id = db.Column(db.String(80), nullable=True)
    doc_type = db.Column(db.String(20), default="bankbook", nullable=False)  # bankbook / credit_card
    account_name = db.Column(db.String(120), nullable=True)
    filename = db.Column(db.String(255), nullable=True)
    content_type = db.Column(db.String(80), nullable=True)
    data = db.Column(db.LargeBinary, nullable=True)  # 画像そのもの
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def doc_type_label(self) -> str:
        return {"bankbook": "通帳", "credit_card": "クレカ明細"}.get(
            self.doc_type or "", self.doc_type or ""
        )


class BankDocumentReview(db.Model):
    """書き起こし結果（BankEntry）に対する各AIの検証レビュー（追記型）。"""

    __tablename__ = "bank_document_reviews"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, nullable=False, index=True)
    ai_name = db.Column(db.String(80), nullable=False)
    verdict = db.Column(db.String(40), nullable=True)  # ok / warning / error
    result = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


TASK_TYPE_LABELS = {
    "register_deal": "取引の登録",
    "fix_deal": "取引の修正",
    "delete_deal": "取引の削除",
    "link_receipt": "証憑の紐付け",
    "other": "その他",
}

# proposed(承認待ち) → approved(実行待ち) → done(完了) ／ rejected(却下)
TASK_STATUSES = ("proposed", "approved", "done", "rejected")

TASK_STATUS_LABELS = {
    "proposed": "承認待ち",
    "approved": "実行待ち",
    "done": "完了",
    "rejected": "却下",
}


class AiTask(db.Model):
    """AIへの作業指示（ToDo）。チェックAIが提案し、人間が承認し、freee接続AIが実行する。

    このアプリ自身は freee に書き込まない。承認済み（approved）のタスクだけを
    実行AIが list_tasks で取得し、実行後 complete_task で報告する。
    """

    __tablename__ = "ai_tasks"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20), default=SOURCE_FREEE, nullable=False)
    scope_key = db.Column(db.String(120), nullable=True, index=True)
    scope_name = db.Column(db.String(255), nullable=True)
    company_id = db.Column(db.BigInteger, nullable=True)
    office_id = db.Column(db.String(80), nullable=True)
    task_type = db.Column(db.String(40), default="other", nullable=False)
    title = db.Column(db.String(255), nullable=False)
    instruction = db.Column(db.Text, nullable=False)  # 実行AI向けの具体的な作業内容
    evidence = db.Column(db.Text, nullable=True)  # 根拠（どのチェックで何を検出したか）
    related_deal_id = db.Column(db.BigInteger, nullable=True)
    created_by = db.Column(db.String(80), nullable=True)  # 提案者（AI名または人）
    status = db.Column(db.String(20), default="proposed", nullable=False, index=True)
    approved_by = db.Column(db.String(80), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    executed_by = db.Column(db.String(80), nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)
    result_note = db.Column(db.Text, nullable=True)  # 実行AIの報告
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def task_type_label(self) -> str:
        return TASK_TYPE_LABELS.get(self.task_type or "", self.task_type or "")

    @property
    def status_label(self) -> str:
        return TASK_STATUS_LABELS.get(self.status or "", self.status or "")

    def to_dict(self) -> dict:
        return {
            "task_id": self.id,
            "scope_name": self.scope_name,
            "company_id": self.company_id,
            "office_id": self.office_id,
            "task_type": self.task_type,
            "title": self.title,
            "instruction": self.instruction,
            "evidence": self.evidence,
            "related_deal_id": self.related_deal_id,
            "created_by": self.created_by,
            "status": self.status,
            "result_note": self.result_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
