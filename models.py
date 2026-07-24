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
    company_id = db.Column(db.Integer, nullable=True, index=True)  # freee 事業所ID
    office_id = db.Column(db.String(80), nullable=True)  # MF 事業所ID
    deal_id = db.Column(db.Integer, nullable=False)  # ソース上の取引ID
    issue_date = db.Column(db.String(20), nullable=True)
    deal_type = db.Column(db.String(20), nullable=True)  # income / expense
    amount = db.Column(db.BigInteger, nullable=True)
    partner_name = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), nullable=True)
    account_items = db.Column(db.Text, nullable=True)  # 明細の勘定科目名（可読用）
    details_json = db.Column(db.Text, nullable=True)  # 明細の生データ(JSON)
    # 紐付いた証憑（ファイルボックス）ID の JSON 配列
    receipt_ids_json = db.Column(db.Text, nullable=False, default="[]")
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
    company_id = db.Column(db.Integer, nullable=True, index=True)
    office_id = db.Column(db.String(80), nullable=True)
    receipt_id = db.Column(db.Integer, nullable=False)  # ソース上の証憑ID
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
    company_id = db.Column(db.Integer, nullable=True, index=True)
    office_id = db.Column(db.String(80), nullable=True)
    deal_id = db.Column(db.Integer, nullable=False, index=True)
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
    company_id = db.Column(db.Integer, nullable=True)
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
