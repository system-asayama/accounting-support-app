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
