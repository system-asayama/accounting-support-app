"""管理者と利用者がログインできるシンプルな認証システム。

- セッションベースの認証
- パスワードはハッシュ化して保存
- admin / user のロールによるアクセス制御
- 管理者はユーザー一覧・作成・ロール変更・削除が可能
"""
import json
import os
from functools import wraps

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import freee_client
from broadcast_service import BroadcastError, run_broadcast
from models import (
    DEFAULT_MODEL,
    PROVIDER_DEFAULT_MODEL,
    PROVIDER_LABELS,
    PROVIDERS,
    ROLE_ADMIN,
    ROLE_USER,
    ROLES,
    Agent,
    Broadcast,
    BroadcastResult,
    FreeeConnection,
    User,
    db,
)


def _normalize_db_url(url: str) -> str:
    # SQLAlchemy は postgres:// を認識しないため postgresql:// に変換
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        app.config["SQLALCHEMY_DATABASE_URI"] = _normalize_db_url(database_url)
    else:
        # DATABASE_URL が無い場合はローカル SQLite にフォールバック
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _ensure_schema()
        _seed_admin()

    _register_routes(app)
    return app


def _ensure_schema() -> None:
    """後から追加した列を、既存テーブルに対して不足していれば足す簡易マイグレーション。

    本格的なマイグレーションツールは使わず、新規カラムのみを冪等に ADD COLUMN する。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    existing_tables = inspector.get_table_names()

    # (テーブル, カラム, 追加DDL) の一覧
    additions = [
        ("agents", "provider", "ALTER TABLE agents ADD COLUMN provider VARCHAR(20) DEFAULT 'anthropic'"),
        ("broadcast_results", "provider", "ALTER TABLE broadcast_results ADD COLUMN provider VARCHAR(20)"),
    ]
    for table, column, ddl in additions:
        if table not in existing_tables:
            continue
        columns = {c["name"] for c in inspector.get_columns(table)}
        if column not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(ddl))


def _seed_admin() -> None:
    """初期管理者アカウントを作成する（既に存在する場合は何もしない）。"""
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")

    if User.query.filter_by(username=admin_username).first() is None:
        admin = User(username=admin_username, role=ROLE_ADMIN)
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()


# ---------------------------------------------------------------------------
# 認証ヘルパー
# ---------------------------------------------------------------------------
def current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return db.session.get(User, user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            flash("ログインが必要です。", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            flash("管理者ログインが必要です。", "error")
            return redirect(url_for("admin_login"))
        if not user.is_admin:
            flash("管理者権限が必要です。", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# ルーティング
# ---------------------------------------------------------------------------
def _register_routes(app: Flask) -> None:
    @app.context_processor
    def inject_user():
        return {
            "current_user": current_user(),
            "providers": PROVIDERS,
            "provider_labels": PROVIDER_LABELS,
            "provider_default_model": PROVIDER_DEFAULT_MODEL,
        }

    @app.route("/")
    def index():
        if current_user() is not None:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        """利用者（user ロール）の新規登録。"""
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm") or ""

            if not username or not password:
                flash("ユーザー名とパスワードを入力してください。", "error")
            elif password != confirm:
                flash("パスワードが一致しません。", "error")
            elif User.query.filter_by(username=username).first() is not None:
                flash("そのユーザー名は既に使われています。", "error")
            else:
                user = User(username=username, role=ROLE_USER)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash("登録が完了しました。ログインしてください。", "success")
                return redirect(url_for("login"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """利用者用ログインページ。"""
        if current_user() is not None:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()
            if user is not None and user.check_password(password):
                if user.is_admin:
                    # 管理者は管理者用ログインを使う
                    flash("管理者は管理者ログインページからログインしてください。", "error")
                    return redirect(url_for("admin_login"))
                session.clear()
                session["user_id"] = user.id
                flash(f"ようこそ、{user.username} さん。", "success")
                return redirect(url_for("dashboard"))

            flash("ユーザー名またはパスワードが正しくありません。", "error")

        return render_template("login.html")

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        """管理者用ログインページ。"""
        user = current_user()
        if user is not None:
            return redirect(url_for("admin_users" if user.is_admin else "dashboard"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""

            user = User.query.filter_by(username=username).first()
            if user is not None and user.check_password(password):
                if not user.is_admin:
                    # 一般利用者はこのページからログインできない
                    flash("このページは管理者専用です。利用者ログインをご利用ください。", "error")
                    return redirect(url_for("login"))
                session.clear()
                session["user_id"] = user.id
                flash(f"管理者としてログインしました（{user.username}）。", "success")
                return redirect(url_for("admin_users"))

            flash("ユーザー名またはパスワードが正しくありません。", "error")

        return render_template("admin_login.html")

    @app.route("/logout")
    def logout():
        was_admin = (current_user() or None) and current_user().is_admin
        session.clear()
        flash("ログアウトしました。", "success")
        return redirect(url_for("admin_login" if was_admin else "login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html", user=current_user())

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        """利用者が自分のログインIDとパスワードを変更する。"""
        user = current_user()

        if request.method == "POST":
            current_password = request.form.get("current_password") or ""
            new_username = (request.form.get("username") or "").strip()
            new_password = request.form.get("new_password") or ""
            confirm = request.form.get("confirm") or ""

            # 本人確認のため現在のパスワードを必須にする
            if not user.check_password(current_password):
                flash("現在のパスワードが正しくありません。", "error")
            elif not new_username:
                flash("ログインIDを入力してください。", "error")
            elif (
                new_username != user.username
                and User.query.filter_by(username=new_username).first() is not None
            ):
                flash("そのログインIDは既に使われています。", "error")
            elif new_password and new_password != confirm:
                flash("新しいパスワードが一致しません。", "error")
            else:
                user.username = new_username
                if new_password:
                    user.set_password(new_password)
                db.session.commit()
                msg = "ログインIDを更新しました。"
                if new_password:
                    msg = "ログインIDとパスワードを更新しました。"
                flash(msg, "success")
                return redirect(url_for("settings"))

        return render_template("settings.html", user=user)

    # --- 管理者専用 ------------------------------------------------------
    @app.route("/admin/settings", methods=["GET", "POST"])
    @admin_required
    def admin_settings():
        """管理者が自分のログインIDとパスワードを変更する。"""
        user = current_user()

        if request.method == "POST":
            current_password = request.form.get("current_password") or ""
            new_username = (request.form.get("username") or "").strip()
            new_password = request.form.get("new_password") or ""
            confirm = request.form.get("confirm") or ""

            # 本人確認のため現在のパスワードを必須にする
            if not user.check_password(current_password):
                flash("現在のパスワードが正しくありません。", "error")
            elif not new_username:
                flash("ログインIDを入力してください。", "error")
            elif (
                new_username != user.username
                and User.query.filter_by(username=new_username).first() is not None
            ):
                flash("そのログインIDは既に使われています。", "error")
            elif new_password and new_password != confirm:
                flash("新しいパスワードが一致しません。", "error")
            else:
                user.username = new_username
                if new_password:
                    user.set_password(new_password)
                db.session.commit()
                msg = "ログインIDを更新しました。"
                if new_password:
                    msg = "ログインIDとパスワードを更新しました。"
                flash(msg, "success")
                return redirect(url_for("admin_settings"))

        return render_template("admin_settings.html", user=user)

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        users = User.query.order_by(User.created_at.asc()).all()
        return render_template("admin_users.html", users=users, roles=ROLES)

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_create_user():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = request.form.get("role") or ROLE_USER

        if role not in ROLES:
            role = ROLE_USER

        if not username or not password:
            flash("ユーザー名とパスワードを入力してください。", "error")
        elif User.query.filter_by(username=username).first() is not None:
            flash("そのユーザー名は既に使われています。", "error")
        else:
            user = User(username=username, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"ユーザー「{username}」を作成しました。", "success")

        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/role", methods=["POST"])
    @admin_required
    def admin_update_role(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            flash("ユーザーが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        new_role = request.form.get("role")
        if new_role not in ROLES:
            flash("無効なロールです。", "error")
            return redirect(url_for("admin_users"))

        # 最後の管理者を降格させないよう保護
        if user.is_admin and new_role != ROLE_ADMIN and _admin_count() <= 1:
            flash("最後の管理者の権限は変更できません。", "error")
            return redirect(url_for("admin_users"))

        user.role = new_role
        db.session.commit()
        flash(f"「{user.username}」のロールを {new_role} に変更しました。", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @admin_required
    def admin_delete_user(user_id):
        user = db.session.get(User, user_id)
        if user is None:
            flash("ユーザーが見つかりません。", "error")
            return redirect(url_for("admin_users"))

        if user.id == current_user().id:
            flash("自分自身は削除できません。", "error")
            return redirect(url_for("admin_users"))

        if user.is_admin and _admin_count() <= 1:
            flash("最後の管理者は削除できません。", "error")
            return redirect(url_for("admin_users"))

        db.session.delete(user)
        db.session.commit()
        flash(f"ユーザー「{user.username}」を削除しました。", "success")
        return redirect(url_for("admin_users"))


    # --- 一斉指示（MCP連携AIへのブロードキャスト） ----------------------
    @app.route("/agents")
    @login_required
    def agents():
        """登録済みエージェントの一覧。"""
        all_agents = Agent.query.order_by(Agent.created_at.asc()).all()
        return render_template("agents.html", agents=all_agents)

    @app.route("/agents/new", methods=["GET", "POST"])
    @login_required
    def agent_new():
        """エージェントを新規登録する。"""
        if request.method == "POST":
            error = _save_agent_from_form(None)
            if error is None:
                flash("エージェントを登録しました。", "success")
                return redirect(url_for("agents"))
            flash(error, "error")
            return render_template(
                "agent_form.html",
                agent=None,
                default_model=DEFAULT_MODEL,
                form=request.form,
            )
        return render_template(
            "agent_form.html", agent=None, default_model=DEFAULT_MODEL, form=None
        )

    @app.route("/agents/<int:agent_id>/edit", methods=["GET", "POST"])
    @login_required
    def agent_edit(agent_id):
        """エージェントを編集する。"""
        agent = db.session.get(Agent, agent_id)
        if agent is None:
            flash("エージェントが見つかりません。", "error")
            return redirect(url_for("agents"))

        if request.method == "POST":
            error = _save_agent_from_form(agent)
            if error is None:
                flash("エージェントを更新しました。", "success")
                return redirect(url_for("agents"))
            flash(error, "error")
            return render_template(
                "agent_form.html",
                agent=agent,
                default_model=DEFAULT_MODEL,
                form=request.form,
            )
        return render_template(
            "agent_form.html", agent=agent, default_model=DEFAULT_MODEL, form=None
        )

    @app.route("/agents/<int:agent_id>/delete", methods=["POST"])
    @login_required
    def agent_delete(agent_id):
        agent = db.session.get(Agent, agent_id)
        if agent is None:
            flash("エージェントが見つかりません。", "error")
            return redirect(url_for("agents"))
        db.session.delete(agent)
        db.session.commit()
        flash(f"エージェント「{agent.name}」を削除しました。", "success")
        return redirect(url_for("agents"))

    @app.route("/broadcast", methods=["GET", "POST"])
    @login_required
    def broadcast():
        """指示を入力し、有効な全エージェントへ一斉送信して結果を表示する。"""
        enabled_agents = (
            Agent.query.filter_by(enabled=True)
            .order_by(Agent.created_at.asc())
            .all()
        )

        if request.method == "POST":
            instruction = (request.form.get("instruction") or "").strip()
            if not instruction:
                flash("指示内容を入力してください。", "error")
            elif not enabled_agents:
                flash("有効なエージェントがありません。先に登録してください。", "error")
            else:
                try:
                    results = run_broadcast(instruction, enabled_agents)
                except BroadcastError as exc:
                    flash(str(exc), "error")
                    return render_template(
                        "broadcast.html",
                        agents=enabled_agents,
                        instruction=instruction,
                    )

                record = Broadcast(
                    instruction=instruction,
                    created_by=(current_user().username if current_user() else None),
                )
                db.session.add(record)
                db.session.flush()  # record.id を確定
                for r in results:
                    db.session.add(
                        BroadcastResult(
                            broadcast_id=record.id,
                            agent_name=r["agent_name"],
                            provider=r.get("provider"),
                            model=r.get("model"),
                            status=r["status"],
                            response=r.get("response"),
                            tools_used_json=json.dumps(
                                r.get("tools_used") or [], ensure_ascii=False
                            ),
                            error=r.get("error"),
                        )
                    )
                db.session.commit()
                flash("一斉送信が完了しました。", "success")
                return redirect(url_for("broadcast_detail", broadcast_id=record.id))

        return render_template("broadcast.html", agents=enabled_agents, instruction="")

    @app.route("/broadcast/history")
    @login_required
    def broadcast_history():
        records = Broadcast.query.order_by(Broadcast.created_at.desc()).all()
        return render_template("broadcast_history.html", broadcasts=records)

    @app.route("/broadcast/<int:broadcast_id>")
    @login_required
    def broadcast_detail(broadcast_id):
        record = db.session.get(Broadcast, broadcast_id)
        if record is None:
            flash("履歴が見つかりません。", "error")
            return redirect(url_for("broadcast_history"))
        return render_template("broadcast_detail.html", broadcast=record)


    # --- freee 連携 ------------------------------------------------------
    @app.route("/freee")
    @login_required
    def freee_status():
        conn = FreeeConnection.get()
        return render_template(
            "freee_status.html",
            conn=conn,
            configured=freee_client.is_configured(),
            redirect_uri=freee_client.get_config()["redirect_uri"],
            oob=freee_client.OOB_REDIRECT,
        )

    @app.route("/freee/oauth/start")
    @login_required
    def freee_oauth_start():
        try:
            return redirect(freee_client.authorize_url())
        except freee_client.FreeeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("freee_status"))

    @app.route("/freee/oauth/callback")
    @login_required
    def freee_oauth_callback():
        """登録済みリダイレクトURIに戻ってきたときのコールバック。"""
        code = request.args.get("code")
        error = request.args.get("error")
        if error:
            flash(f"freee 認証がキャンセル/失敗しました: {error}", "error")
            return redirect(url_for("freee_status"))
        if not code:
            flash("認可コードが取得できませんでした。", "error")
            return redirect(url_for("freee_status"))
        return _do_exchange(code)

    @app.route("/freee/oauth/code", methods=["POST"])
    @login_required
    def freee_oauth_code():
        """OOB で画面表示された認可コードを貼り付けて交換する。"""
        code = (request.form.get("code") or "").strip()
        if not code:
            flash("認可コードを入力してください。", "error")
            return redirect(url_for("freee_status"))
        return _do_exchange(code)

    def _do_exchange(code):
        try:
            freee_client.exchange_code(code)
            flash("freee と連携しました。事業所を選択してください。", "success")
            return redirect(url_for("freee_companies"))
        except freee_client.FreeeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("freee_status"))

    @app.route("/freee/token", methods=["POST"])
    @login_required
    def freee_token():
        """（簡易）アクセストークンを直接保存する。"""
        access_token = (request.form.get("access_token") or "").strip()
        refresh_tok = (request.form.get("refresh_token") or "").strip()
        if not access_token:
            flash("アクセストークンを入力してください。", "error")
            return redirect(url_for("freee_status"))
        freee_client.save_manual_token(access_token, refresh_tok)
        flash("アクセストークンを保存しました。", "success")
        return redirect(url_for("freee_companies"))

    @app.route("/freee/disconnect", methods=["POST"])
    @login_required
    def freee_disconnect():
        freee_client.disconnect()
        flash("freee 連携を解除しました。", "success")
        return redirect(url_for("freee_status"))

    @app.route("/freee/companies")
    @login_required
    def freee_companies():
        conn = FreeeConnection.get()
        if not conn.is_connected:
            flash("先に freee と連携してください。", "error")
            return redirect(url_for("freee_status"))
        try:
            companies = freee_client.list_companies(conn)
        except freee_client.FreeeError as exc:
            flash(str(exc), "error")
            return redirect(url_for("freee_status"))
        return render_template("freee_companies.html", conn=conn, companies=companies)

    @app.route("/freee/select-company", methods=["POST"])
    @login_required
    def freee_select_company():
        conn = FreeeConnection.get()
        company_id = request.form.get("company_id")
        company_name = request.form.get("company_name") or ""
        if not company_id:
            flash("事業所を選択してください。", "error")
            return redirect(url_for("freee_companies"))
        conn.company_id = int(company_id)
        conn.company_name = company_name
        db.session.commit()
        flash(f"事業所「{company_name}」を選択しました。", "success")
        return redirect(url_for("freee_deals"))

    @app.route("/freee/deals")
    @login_required
    def freee_deals():
        conn = FreeeConnection.get()
        if not conn.is_connected:
            flash("先に freee と連携してください。", "error")
            return redirect(url_for("freee_status"))
        if not conn.company_id:
            flash("先に事業所を選択してください。", "error")
            return redirect(url_for("freee_companies"))

        start = (request.args.get("start") or "").strip()
        end = (request.args.get("end") or "").strip()
        deal_type = (request.args.get("type") or "").strip()

        deals, total, account_map, partner_map, error = [], 0, {}, {}, None
        try:
            result = freee_client.list_deals(
                conn,
                conn.company_id,
                start_issue_date=start,
                end_issue_date=end,
                type=deal_type,
                limit=100,
            )
            deals = result.get("deals", [])
            total = (result.get("meta") or {}).get("total_count", len(deals))
            # 勘定科目・取引先のID→名称マップ（明細を人間が読める形にする）
            account_map = {
                a["id"]: a.get("name", "")
                for a in freee_client.list_account_items(conn, conn.company_id)
            }
            partner_map = {
                p["id"]: p.get("name", "")
                for p in freee_client.list_partners(conn, conn.company_id)
            }
        except freee_client.FreeeError as exc:
            error = str(exc)

        return render_template(
            "freee_deals.html",
            conn=conn,
            deals=deals,
            total=total,
            account_map=account_map,
            partner_map=partner_map,
            start=start,
            end=end,
            deal_type=deal_type,
            error=error,
        )


def _save_agent_from_form(agent):
    """フォーム内容から Agent を作成/更新する。成功で None、失敗でエラーメッセージ。"""
    name = (request.form.get("name") or "").strip()
    provider = (request.form.get("provider") or "").strip()
    if provider not in PROVIDERS:
        return "AIプロバイダを選択してください。"
    model = (request.form.get("model") or "").strip() or PROVIDER_DEFAULT_MODEL[provider]
    system_prompt = (request.form.get("system_prompt") or "").strip() or None
    max_tokens_raw = (request.form.get("max_tokens") or "").strip()
    enabled = request.form.get("enabled") == "on"

    if not name:
        return "エージェント名を入力してください。"

    # 名前の重複チェック（自分自身は除外）
    existing = Agent.query.filter_by(name=name).first()
    if existing is not None and (agent is None or existing.id != agent.id):
        return "そのエージェント名は既に使われています。"

    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw else 2048
        if max_tokens <= 0:
            raise ValueError
    except ValueError:
        return "最大トークン数は正の整数で入力してください。"

    # MCPサーバーは name / url / token の3列を行ごとに受け取る
    names = request.form.getlist("mcp_name")
    urls = request.form.getlist("mcp_url")
    tokens = request.form.getlist("mcp_token")
    servers = []
    for i in range(len(urls)):
        url = (urls[i] or "").strip()
        if not url:
            continue
        servers.append(
            {
                "name": (names[i] if i < len(names) else "").strip() or "mcp",
                "url": url,
                "authorization_token": (
                    tokens[i] if i < len(tokens) else ""
                ).strip(),
            }
        )

    if agent is None:
        agent = Agent(name=name)
        db.session.add(agent)

    agent.name = name
    agent.provider = provider
    agent.model = model
    agent.system_prompt = system_prompt
    agent.max_tokens = max_tokens
    agent.enabled = enabled
    agent.mcp_servers = servers
    db.session.commit()
    return None


def _admin_count() -> int:
    return User.query.filter_by(role=ROLE_ADMIN).count()


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
