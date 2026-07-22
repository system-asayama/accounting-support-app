"""freee API 連携クライアント。

OAuth2（認可コードフロー）でアクセストークンを取得し、freee会計APIからデータを取得する。
リダイレクトURIを登録できない環境向けに、OOB（コードを画面表示してコピペ）にも対応。
アクセストークンの有効期限が切れていればリフレッシュトークンで自動更新する。
"""
import os
from datetime import datetime, timedelta

import requests

from models import FreeeConnection, db

AUTH_BASE = "https://accounts.secure.freee.co.jp"
API_BASE = "https://api.freee.co.jp"
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"

# 有効期限のバッファ（この秒数手前で切れたとみなして更新する）
EXPIRY_SKEW_SECONDS = 60
REQUEST_TIMEOUT = 30


class FreeeError(RuntimeError):
    """freee API 連携で発生したエラー。"""


class FreeeNotConfigured(FreeeError):
    """client_id / client_secret 等が未設定。"""


class FreeeNotConnected(FreeeError):
    """アクセストークンが未取得（未連携）。"""


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
def get_config() -> dict:
    return {
        "client_id": os.environ.get("FREEE_CLIENT_ID", ""),
        "client_secret": os.environ.get("FREEE_CLIENT_SECRET", ""),
        "redirect_uri": os.environ.get("FREEE_REDIRECT_URI", OOB_REDIRECT),
    }


def is_configured() -> bool:
    cfg = get_config()
    return bool(cfg["client_id"] and cfg["client_secret"])


def _require_config() -> dict:
    cfg = get_config()
    if not cfg["client_id"] or not cfg["client_secret"]:
        raise FreeeNotConfigured(
            "FREEE_CLIENT_ID と FREEE_CLIENT_SECRET が設定されていません。"
            "freee のアプリ管理でアプリを登録し、環境変数に設定してください。"
        )
    return cfg


def authorize_url(state: str = "") -> str:
    cfg = _require_config()
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
    }
    if state:
        params["state"] = state
    query = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{AUTH_BASE}/public_api/authorize?{query}"


# ---------------------------------------------------------------------------
# トークン取得・更新
# ---------------------------------------------------------------------------
def _store_token(conn: FreeeConnection, payload: dict) -> None:
    conn.access_token = payload.get("access_token")
    conn.refresh_token = payload.get("refresh_token") or conn.refresh_token
    expires_in = payload.get("expires_in")
    if expires_in:
        conn.token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
    db.session.commit()


def _post(url: str, data: dict) -> requests.Response:
    """ネットワーク例外を FreeeError に変換する POST。"""
    try:
        return requests.post(url, data=data, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise FreeeError(f"freee への接続に失敗しました: {exc}") from exc


def exchange_code(code: str) -> FreeeConnection:
    """認可コードをアクセストークンに交換して保存する。"""
    cfg = _require_config()
    resp = _post(
        f"{AUTH_BASE}/public_api/token",
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code.strip(),
            "redirect_uri": cfg["redirect_uri"],
        },
    )
    if resp.status_code != 200:
        raise FreeeError(f"トークン取得に失敗しました（{resp.status_code}）: {resp.text}")

    conn = FreeeConnection.get()
    _store_token(conn, resp.json())
    return conn


def refresh_token(conn: FreeeConnection) -> None:
    """リフレッシュトークンでアクセストークンを更新する。"""
    cfg = _require_config()
    if not conn.refresh_token:
        raise FreeeNotConnected("リフレッシュトークンがありません。再連携してください。")

    resp = _post(
        f"{AUTH_BASE}/public_api/token",
        {
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": conn.refresh_token,
        },
    )
    if resp.status_code != 200:
        raise FreeeError(f"トークン更新に失敗しました（{resp.status_code}）: {resp.text}")
    _store_token(conn, resp.json())


def save_manual_token(access_token: str, refresh_tok: str = "") -> FreeeConnection:
    """（簡易）アクセストークンを直接保存する。有効期限は不明なので未設定。"""
    conn = FreeeConnection.get()
    conn.access_token = access_token.strip()
    if refresh_tok:
        conn.refresh_token = refresh_tok.strip()
    conn.token_expires_at = None
    db.session.commit()
    return conn


def disconnect() -> None:
    conn = FreeeConnection.get()
    conn.access_token = None
    conn.refresh_token = None
    conn.token_expires_at = None
    conn.company_id = None
    conn.company_name = None
    db.session.commit()


# ---------------------------------------------------------------------------
# API 呼び出し
# ---------------------------------------------------------------------------
def _ensure_token(conn: FreeeConnection) -> None:
    if not conn.access_token:
        raise FreeeNotConnected("freee と連携されていません。")
    if conn.token_expires_at and datetime.utcnow() >= (
        conn.token_expires_at - timedelta(seconds=EXPIRY_SKEW_SECONDS)
    ):
        if conn.refresh_token and is_configured():
            refresh_token(conn)


def api_get(conn: FreeeConnection, path: str, params: dict = None) -> dict:
    """freee 会計 API に GET リクエスト。401 の場合は1度だけ更新して再試行する。"""
    _ensure_token(conn)

    def _do() -> requests.Response:
        try:
            return requests.get(
                f"{API_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {conn.access_token}",
                    "Accept": "application/json",
                },
                params=params or {},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise FreeeError(f"freee への接続に失敗しました: {exc}") from exc

    resp = _do()
    if resp.status_code == 401 and conn.refresh_token and is_configured():
        refresh_token(conn)
        resp = _do()

    if resp.status_code == 401:
        raise FreeeNotConnected("認証に失敗しました。freee と再連携してください。")
    if resp.status_code >= 400:
        raise FreeeError(f"freee API エラー（{resp.status_code}）: {resp.text}")
    return resp.json()


# ---------------------------------------------------------------------------
# 便利ラッパー
# ---------------------------------------------------------------------------
def get_current_user(conn: FreeeConnection) -> dict:
    return api_get(conn, "/api/1/users/me", {"companies": "true"})


def list_companies(conn: FreeeConnection) -> list:
    data = api_get(conn, "/api/1/companies")
    return data.get("companies", [])


def list_deals(conn: FreeeConnection, company_id: int, **filters) -> dict:
    """取引（収入・支出）一覧を取得。filters に start_issue_date 等を渡せる。"""
    params = {"company_id": company_id, "limit": filters.pop("limit", 50)}
    params.update({k: v for k, v in filters.items() if v})
    return api_get(conn, "/api/1/deals", params)


def list_account_items(conn: FreeeConnection, company_id: int) -> list:
    data = api_get(conn, "/api/1/account_items", {"company_id": company_id})
    return data.get("account_items", [])


def list_partners(conn: FreeeConnection, company_id: int, limit: int = 100) -> list:
    data = api_get(
        conn, "/api/1/partners", {"company_id": company_id, "limit": limit}
    )
    return data.get("partners", [])


def get_trial_pl(conn: FreeeConnection, company_id: int, fiscal_year: int = None) -> dict:
    params = {"company_id": company_id}
    if fiscal_year:
        params["fiscal_year"] = fiscal_year
    return api_get(conn, "/api/1/reports/trial_pl", params)
