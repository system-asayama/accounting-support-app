# accounting-support-app

ログイン認証と、**MCP連携できるAIエージェントへの一斉指示（ブロードキャスト）**機能を持つ
Flask + SQLAlchemy アプリです。

## 機能

### 認証・ユーザー管理
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

### freee 連携（会計データの取得）
freee 会計 API と OAuth2 連携し、事業所の取引（仕訳）データを取得・表示します。
「複数AIで仕訳チェック」を行う前の土台となる、データ取得の仕組みです。

- **連携** (`/freee`)
  - OAuth2 認可コードフロー。リダイレクトURIを登録できない環境向けに **OOB**（コード貼り付け）にも対応
  - すでにアクセストークンがある場合は直接貼り付けても連携可能（簡易）
  - アクセストークン期限切れ時はリフレッシュトークンで自動更新
- **事業所選択** (`/freee/companies`)
  - 連携アカウントで参照できる事業所一覧から対象を選択
- **取引データ** (`/freee/deals`)
  - 発生日・収支区分で絞り込んで取引一覧を表示
  - 勘定科目ID・取引先IDは名称に変換して表示（明細も表示）

必要な環境変数（OAuth を使う場合）:

| 変数 | 説明 |
| --- | --- |
| `FREEE_CLIENT_ID` | freee アプリのクライアントID |
| `FREEE_CLIENT_SECRET` | freee アプリのクライアントシークレット |
| `FREEE_REDIRECT_URI` | リダイレクトURI（未設定時は OOB を使用） |

> freee アプリは [freee アプリ管理](https://app.secure.freee.co.jp/developers/applications) で登録します。

### MCP連携AIへの一斉指示（マルチプロバイダ・ブロードキャスト）
1つの指示を、**Claude / ChatGPT / Gemini** を横断して複数AIへまとめて送り、
結果を横並びで比較できます。例えば freee をそれぞれの AI に MCP 連携しておき、
「同じ仕訳チェック」を一斉に実行 → 各 AI の異なる解析結果を人間が目視で見比べる、
といった運用ができます。

**対応プロバイダ**

| プロバイダ | MCP連携方式 | 必要な環境変数 |
| --- | --- | --- |
| Claude (Anthropic) | Messages API のリモートMCPコネクタ | `ANTHROPIC_API_KEY` |
| ChatGPT (OpenAI) | Responses API のリモートMCPツール | `OPENAI_API_KEY` |
| Gemini (Google) | Gen AI SDK + MCPクライアントセッション（Streamable HTTP） | `GEMINI_API_KEY` |

- **エージェント登録** (`/agents`)
  - 「プロバイダ + モデル + システムプロンプト + 利用するMCPサーバー群」を1エージェントとして登録
  - MCPサーバーはリモートURL（+任意の認証トークン）で複数指定可能
  - 有効/無効を切り替えて一斉指示の対象を制御
- **一斉指示** (`/broadcast`)
  - 指示を1回入力 → 有効な全エージェントへ**並列**にファンアウト
  - 各エージェントは自分のプロバイダの MCP 連携経由でツール（freee 等）を使って処理
  - 各AIの応答・使用したMCPツール名を横並びで一覧表示
  - 1件が失敗しても他は継続（プロバイダ／キー未設定などのエラーは結果カードに表示）
- **履歴** (`/broadcast/history`)
  - 過去の一斉指示と各エージェントの応答を保存・再表示

> **前提**: 各プロバイダの API がインターネット経由で接続するため、freee 等の MCP サーバーは
> **リモート（HTTP/URL）で到達可能なエンドポイント**として公開されている必要があります
> （認証トークンはエージェントごとに設定可能）。
> API キーは使うプロバイダの分だけ設定すれば十分で、未設定のプロバイダは
> そのエージェントの結果カードにエラーとして表示されます（エージェント登録自体はキー無しでも可能）。

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
| `ANTHROPIC_API_KEY` | Claude (Anthropic) エージェントの実行に必要な API キー |
| `OPENAI_API_KEY` | ChatGPT (OpenAI) エージェントの実行に必要な API キー |
| `GEMINI_API_KEY` | Gemini (Google) エージェントの実行に必要な API キー（`GOOGLE_API_KEY` でも可） |
| `FREEE_CLIENT_ID` | freee アプリのクライアントID（freee OAuth連携用） |
| `FREEE_CLIENT_SECRET` | freee アプリのクライアントシークレット |
| `FREEE_REDIRECT_URI` | freee リダイレクトURI（未設定時は OOB を使用） |
