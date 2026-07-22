# easy-login-system-app

管理者(admin)と利用者(user)がログインできるシンプルな認証システムです。
Flask + SQLAlchemy で実装しています。

## 機能

- ユーザー登録 / ログイン / ログアウト（セッションベース認証）
- パスワードはハッシュ化して保存
- ロールによるアクセス制御（`admin` / `user`）
- 管理者と利用者でログインページを分離
  - 利用者ログイン: `/login`
  - 管理者ログイン: `/admin/login`
  - 相手側のページからログインしようとすると正しいページへ誘導
- ログインID・パスワードの変更（本人確認のため現在のパスワードが必須）
  - 管理者: `/admin/settings`
  - 利用者: `/settings`
- 管理者向けユーザー管理画面
  - ユーザー一覧表示
  - ユーザー新規作成（ロール指定可）
  - ロール変更
  - ユーザー削除
  - ※最後の管理者は削除・降格できない安全装置付き

## 初期管理者アカウント

起動時に管理者アカウントが自動作成されます（既存なら何もしません）。

| 項目 | デフォルト | 環境変数 |
| --- | --- | --- |
| ユーザー名 | `admin` | `ADMIN_USERNAME` |
| パスワード | `admin123` | `ADMIN_PASSWORD` |

本番では必ず `ADMIN_PASSWORD` と `SECRET_KEY` を変更してください。

## 起動方法

### Docker Compose（Flask + PostgreSQL）

```bash
docker compose up --build
```

http://localhost:8000 にアクセスします。

### ローカル単体実行（SQLite にフォールバック）

`DATABASE_URL` が未設定の場合は SQLite (`app.db`) を使います。

```bash
pip install -r requirements.txt
python app.py
```

## 環境変数

| 変数 | 説明 |
| --- | --- |
| `DATABASE_URL` | DB 接続先。未設定なら SQLite を使用 |
| `SECRET_KEY` | セッション署名鍵。本番では必ず変更 |
| `ADMIN_USERNAME` | 初期管理者のユーザー名 |
| `ADMIN_PASSWORD` | 初期管理者のパスワード |
