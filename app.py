"""管理者と利用者がログインできるシンプルな認証システム。

- セッションベースの認証
- パスワードはハッシュ化して保存
- admin / user のロールによるアクセス制御
- 管理者はユーザー一覧・作成・ロール変更・削除が可能
"""
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

from models import ROLE_ADMIN, ROLE_USER, ROLES, User, db


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
        _seed_admin()

    _register_routes(app)
    return app


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
        return {"current_user": current_user()}

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


def _admin_count() -> int:
    return User.query.filter_by(role=ROLE_ADMIN).count()


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
