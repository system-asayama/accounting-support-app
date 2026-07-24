"""マネーフォワード クラウド会計 API 連携クライアント。

freee_client と同じ構造。OAuth2（認可コードフロー）でアクセストークンを取得し、
会計APIから事業所・取引データを取得する。

OAuth エンドポイントは公式に確認済み:
  authorize: GET  https://api.biz.moneyforward.com/authorize
  token:     POST https://api.biz.moneyforward.com/token
会計APIの各エンドポイント・パスは開発者ポータル（認証必須）に準拠する想定で、
実環境に合わせて環境変数で上書きできるようにしている（下記 *_PATH）。
"""
import os
from datetime import datetime, timedelta

import requests

AUTH_BASE = "https://api.biz.moneyforward.com"
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"

EXPIRY_SKEW_SECONDS = 60
REQUEST_TIMEOUT = 30


class MFError(RuntimeError):
    """MF API 連携で発生したエラー。"""


class MFNotConfigured(MFError):
    pass


class MFNotConnected(MFError):
    pass


# ---------------------------------------------------------------------------
# 設定（クレデンシャル・エンドポイントは環境変数で指定）
# ---------------------------------------------------------------------------
def get_config() -> dict:
    """アプリ情報を返す。画面から保存した値（DB）を優先し、無ければ環境変数を使う。"""
    conn = None
    try:
        from models import MFConnection

        conn = MFConnection.get()
    except Exception:  # noqa: BLE001
        conn = None
    return {
        "client_id": (conn.client_id if conn and conn.client_id else "")
        or os.environ.get("MF_CLIENT_ID", ""),
        "client_secret": (conn.client_secret if conn and conn.client_secret else "")
        or os.environ.get("MF_CLIENT_SECRET", ""),
        "redirect_uri": (conn.redirect_uri if conn and conn.redirect_uri else "")
        or os.environ.get("MF_REDIRECT_URI", OOB_REDIRECT),
        "scope": os.environ.get("MF_SCOPE", ""),
        # 会計API のベースURLとパス（実環境に合わせて上書き可能）
        "api_base": os.environ.get("MF_API_BASE", "https://api.biz.moneyforward.com"),
        "offices_path": os.environ.get("MF_OFFICES_PATH", "/accounting/v1/offices"),
        "deals_path": os.environ.get("MF_DEALS_PATH", "/accounting/v1/offices/{office_id}/deals"),
    }


def is_configured() -> bool:
    cfg = get_config()
    return bool(cfg["client_id"] and cfg["client_secret"])


def _require_config() -> dict:
    cfg = get_config()
    if not cfg["client_id"] or not cfg["client_secret"]:
        raise MFNotConfigured(
            "MF_CLIENT_ID と MF_CLIENT_SECRET が設定されていません。"
            "マネーフォワード クラウドのアプリ開発でアプリを登録し、環境変数に設定してください。"
        )
    return cfg


def authorize_url(state: str = "") -> str:
    cfg = _require_config()
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
    }
    if cfg["scope"]:
        params["scope"] = cfg["scope"]
    if state:
        params["state"] = state
    query = "&".join(
        f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items()
    )
    return f"{AUTH_BASE}/authorize?{query}"


# ---------------------------------------------------------------------------
# トークン
# ---------------------------------------------------------------------------
def _post(url: str, data: dict) -> requests.Response:
    try:
        return requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise MFError(f"マネーフォワードへの接続に失敗しました: {exc}") from exc


def _store_token(conn, payload: dict) -> None:
    from models import db

    conn.access_token = payload.get("access_token")
    conn.refresh_token = payload.get("refresh_token") or conn.refresh_token
    expires_in = payload.get("expires_in")
    if expires_in:
        conn.token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
    db.session.commit()


def exchange_code(code: str):
    from models import MFConnection

    cfg = _require_config()
    resp = _post(
        f"{AUTH_BASE}/token",
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code.strip(),
            "redirect_uri": cfg["redirect_uri"],
        },
    )
    if resp.status_code != 200:
        raise MFError(f"トークン取得に失敗しました（{resp.status_code}）: {resp.text}")
    conn = MFConnection.get()
    _store_token(conn, resp.json())
    return conn


def refresh_token(conn) -> None:
    cfg = _require_config()
    if not conn.refresh_token:
        raise MFNotConnected("リフレッシュトークンがありません。再連携してください。")
    resp = _post(
        f"{AUTH_BASE}/token",
        {
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": conn.refresh_token,
        },
    )
    if resp.status_code != 200:
        raise MFError(f"トークン更新に失敗しました（{resp.status_code}）: {resp.text}")
    _store_token(conn, resp.json())


def save_manual_token(access_token: str, refresh_tok: str = ""):
    from models import MFConnection, db

    conn = MFConnection.get()
    conn.access_token = access_token.strip()
    if refresh_tok:
        conn.refresh_token = refresh_tok.strip()
    conn.token_expires_at = None
    db.session.commit()
    return conn


def disconnect() -> None:
    from models import MFConnection, db

    conn = MFConnection.get()
    conn.access_token = None
    conn.refresh_token = None
    conn.token_expires_at = None
    conn.office_id = None
    conn.office_name = None
    db.session.commit()


# ---------------------------------------------------------------------------
# API 呼び出し
# ---------------------------------------------------------------------------
def _ensure_token(conn) -> None:
    if not conn.access_token:
        raise MFNotConnected("マネーフォワードと連携されていません。")
    if conn.token_expires_at and datetime.utcnow() >= (
        conn.token_expires_at - timedelta(seconds=EXPIRY_SKEW_SECONDS)
    ):
        if conn.refresh_token and is_configured():
            refresh_token(conn)


def api_get(conn, path: str, params: dict = None) -> dict:
    """MF 会計 API に GET。401 の場合は1度だけ更新して再試行する。"""
    _ensure_token(conn)
    cfg = get_config()
    url = f"{cfg['api_base']}{path}"

    def _do() -> requests.Response:
        try:
            return requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {conn.access_token}",
                    "Accept": "application/json",
                },
                params=params or {},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise MFError(f"マネーフォワードへの接続に失敗しました: {exc}") from exc

    resp = _do()
    if resp.status_code == 401 and conn.refresh_token and is_configured():
        refresh_token(conn)
        resp = _do()
    if resp.status_code == 401:
        raise MFNotConnected("認証に失敗しました。マネーフォワードと再連携してください。")
    if resp.status_code >= 400:
        raise MFError(f"マネーフォワード API エラー（{resp.status_code}）: {resp.text}")
    try:
        return resp.json()
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# 便利ラッパー（レスポンス構造は実環境に合わせて防御的に扱う）
# ---------------------------------------------------------------------------
def list_offices(conn) -> list:
    cfg = get_config()
    data = api_get(conn, cfg["offices_path"])
    if isinstance(data, list):
        return data
    return data.get("offices") or data.get("data") or []


def list_deals(conn, office_id: str, **filters) -> dict:
    cfg = get_config()
    path = cfg["deals_path"].replace("{office_id}", str(office_id))
    params = {k: v for k, v in filters.items() if v}
    return api_get(conn, path, params)
