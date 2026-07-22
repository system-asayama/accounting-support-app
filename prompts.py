"""各AIへ配布するチェック用プロンプトの雛形と、各AIアプリのURL。

このアプリ（accounting-support-app）を MCP サーバーとして接続した AI に対し、
同じ観点でチェックさせるための定型文を提供する。3社（Claude / ChatGPT / Gemini）へ
同じ雛形を渡し、結果を「解析比較」画面で見比べる運用を想定。
"""

# 各AIのアプリを開くためのURL（新規タブ）
AI_APPS = [
    {"name": "Claude", "url": "https://claude.ai/new", "hint": "Claude Code / claude.ai"},
    {"name": "ChatGPT", "url": "https://chatgpt.com/", "hint": "開発者モードでMCP接続"},
    {"name": "Gemini", "url": "https://gemini.google.com/app", "hint": "Gemini CLI 等"},
]

# 全プロンプト共通の前置き（MCP接続前提と、結果の書き戻し方）
_COMMON = (
    "あなたは会計レビュー担当です。接続されている MCP サーバー "
    "「accounting-support-app」のツールだけを使って作業してください。\n"
    "判断結果は必ず write_analysis で書き戻すこと。その際:\n"
    "- ai_name にはあなたのモデル名（例: Claude / ChatGPT / Gemini）を入れる\n"
    "- verdict は ok / warning / error のいずれか\n"
    "- result には判断根拠を日本語で簡潔に書く\n"
)

CHECK_PROMPTS = [
    {
        "key": "duplicate",
        "title": "仕訳の重複チェック",
        "check_type": "duplicate",
        "body": _COMMON
        + (
            "\n【タスク】仕訳の重複チェック\n"
            "手順:\n"
            "1. find_duplicate_candidates を呼び、発生日・金額・取引先が一致する重複候補グループを取得する。\n"
            "2. 各グループについて get_deal で明細を確認し、二重計上の重複か／正当な別取引かを判断する。\n"
            "3. 各グループの代表 deal_id に対して write_analysis(check_type=\"duplicate\") で記録する。\n"
            "   - 重複の疑いが強い → verdict=\"warning\"、どの deal_id と重複か根拠を書く\n"
            "   - 問題なし → verdict=\"ok\"、別取引と判断した理由を書く\n"
        ),
    },
    {
        "key": "receipt_link",
        "title": "領収書・レシートの紐付けチェック",
        "check_type": "receipt_link",
        "body": _COMMON
        + (
            "\n【タスク】領収書・レシートの紐付けチェック\n"
            "手順:\n"
            "1. list_deals_without_receipt で、証憑が紐付いていない取引を取得する。\n"
            "2. list_receipts(only_unlinked=True) で、どの取引にも紐付いていない証憑を取得する。\n"
            "3. 各取引について、金額・勘定科目から本来証憑の添付が必要かを評価し、\n"
            "   write_analysis(check_type=\"receipt_link\") で記録する。\n"
            "   - 証憑を添付すべき → verdict=\"warning\"\n"
            "   - 添付不要または問題なし → verdict=\"ok\"\n"
            "   - 未紐付けの証憑があれば、どの取引に紐付けるべきかの候補も result に書く\n"
        ),
    },
    {
        "key": "ocr",
        "title": "領収書・レシートの読み取り（OCR）結果チェック",
        "check_type": "ocr",
        "body": _COMMON
        + (
            "\n【タスク】領収書・レシートのOCR読み取り結果チェック\n"
            "手順:\n"
            "1. list_deals で証憑が紐付いている取引（has_receipt=true）を確認する。\n"
            "2. その取引について check_receipt_ocr(deal_id) を呼び、取引値とOCR値（取引先・日付・金額）の\n"
            "   不一致フラグ(flags)を確認する。\n"
            "3. 不一致があれば原因を推測し（入力ミス／別証憑の紐付け／税込・税抜の差 など）、\n"
            "   write_analysis(check_type=\"ocr\") で記録する。\n"
            "   - 明確な不一致 → verdict=\"error\"\n"
            "   - 軽微・要確認 → verdict=\"warning\"\n"
            "   - 一致・問題なし → verdict=\"ok\"\n"
            "   - result に不一致の内容と考えられる原因を書く\n"
        ),
    },
]
