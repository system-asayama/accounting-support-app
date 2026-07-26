"""各AIへ配布するチェック用プロンプトの雛形と、各AIアプリのURL。

「事業所（顧問先）と期間を選ぶ → その内容が埋め込まれた指示文を表示」する形で使う。
3社（Claude / ChatGPT / Grok）へ同じ雛形を渡し、結果を「解析比較」で見比べる。
"""

# チェックカテゴリ（check_type）の表示名。解析比較のセクション分けにも使う（この並び順で表示）
CHECK_TYPE_LABELS = {
    "duplicate": "仕訳の重複チェック",
    "cross_payment": "クレカ×現金の二重計上チェック",
    "receipt_link": "領収書・レシートの紐付けチェック",
    "ocr": "OCR読み取り結果チェック",
    "bank": "通帳照合チェック",
    "general": "その他",
}

# 各AIのアプリを開くためのURL（新規タブ）
AI_APPS = [
    {"name": "Claude", "url": "https://claude.ai/new", "hint": "Claude Code / claude.ai"},
    {"name": "ChatGPT", "url": "https://chatgpt.com/", "hint": "開発者モードでMCP接続"},
    {"name": "Grok", "url": "https://grok.com/", "hint": "Connectors でMCP接続"},
]


def _preamble(company_name: str, company_id, start_date: str, end_date: str) -> str:
    """全プロンプト共通の前置き（対象・準備手順・書き戻しルール）。"""
    if company_id:
        id_line = f"（company_id={company_id}。find_company の確認は不要です）"
    else:
        id_line = "（company_id 不明。まず find_company で特定してください）"

    if start_date and end_date:
        period = f"{start_date} 〜 {end_date}"
        import_line = (
            f'2. import_deals(company_id, start_date="{start_date}", '
            f'end_date="{end_date}") で取引と証憑（OCR結果）を取り込む。\n'
        )
    else:
        period = "全期間"
        import_line = "2. import_deals(company_id) で取引を取り込む。\n"

    return (
        "あなたは会計レビュー担当です。接続されている MCP サーバー "
        "「accounting-support-app」のツールだけを使って作業してください。\n"
        f"対象事業所: {company_name} {id_line}\n"
        f"対象期間: {period}\n"
        "\n"
        "準備:\n"
        f'1. find_company("{company_name}") で company_id を特定する。\n'
        + import_line
        + "3. 以降のツール呼び出しには必ず company_id を渡すこと。\n"
        "\n"
        "記録のルール: 判断結果は必ず write_analysis で書き戻すこと。\n"
        "- ai_name にはあなたのモデル名（例: Claude / ChatGPT / Grok）を入れる\n"
        "- verdict は ok / warning / error のいずれか\n"
        "- result には判断根拠を日本語で簡潔に書く\n"
    )


def build_check_prompts(
    company_name: str, company_id=None, start_date: str = "", end_date: str = ""
) -> list:
    """事業所・期間を埋め込んだチェックの指示文を返す（先頭は全項目の一括版）。"""
    pre = _preamble(company_name, company_id, start_date, end_date)
    return [
        {
            "key": "all",
            "title": "一括チェック（全4項目まとめて実行）",
            "check_type": "duplicate / cross_payment / receipt_link / ocr",
            "body": pre
            + (
                "\n【タスク】一括チェック（4項目すべてを順に実行）\n"
                "取り込み（import_deals）は最初の1回だけでよい。以降は取り込み済みデータに対して\n"
                "次の4つのチェックをすべて実行し、項目ごとに指定の check_type で記録すること。\n"
                "重要: 最終的に全取引へ判定の証跡を残す。個別判定しなかった取引には\n"
                "bulk_write_ok で「問題なし」を一括記録する（OCRだけは全件個別判定）。\n"
                "\n"
                "■1. 仕訳の重複チェック（check_type=\"duplicate\"）\n"
                "  - find_duplicate_candidates(company_id) で重複候補グループを取得する。\n"
                "  - 各グループを get_deal で確認し、二重計上の疑い（warning・相手の deal_id と根拠を記載）か、\n"
                "    正当な別取引（ok・理由を記載）かを代表の deal_id へ記録する。\n"
                "  - 候補に挙がらなかった残りの取引へ bulk_write_ok(ai_name, check_type=\"duplicate\",\n"
                "    exclude_deal_ids=[候補グループ内の全 deal_id]) を1回呼び、問題なしを一括記録する。\n"
                "\n"
                "■2. クレカ×現金の二重計上チェック（check_type=\"cross_payment\"）\n"
                "  - find_cross_payment_duplicates(company_id) を呼ぶ。cross_payment=true のペアを最優先で\n"
                "    get_deal で確認し、同一支出の二重計上か偶然の同額かを判断する。\n"
                "  - ペアの両方の deal_id へ記録する（warning は相手方 deal_id と根拠、ok は理由）。\n"
                "  - ペアに含まれなかった残りの取引へ bulk_write_ok(ai_name, check_type=\"cross_payment\",\n"
                "    exclude_deal_ids=[ペアに含まれる全 deal_id]) を1回呼び、問題なしを一括記録する。\n"
                "\n"
                "■3. 領収書・レシートの紐付けチェック（check_type=\"receipt_link\"）\n"
                "  - list_deals_without_receipt(company_id, limit=500) で証憑なしの取引を全件取得し、\n"
                "    全件を write_analysis で個別判定する（添付すべき→warning／少額等で不要→ok、理由を書く）。\n"
                "  - 証憑ありの取引は bulk_write_ok(ai_name, check_type=\"receipt_link\",\n"
                "    only_with_receipt=True, result=\"証憑が紐付いているため問題なし\") で一括記録する。\n"
                "  - list_receipts(company_id, only_unlinked=True) の未紐付け証憑は、紐付け先候補を該当取引の result に書く。\n"
                "\n"
                "■4. 領収書・レシートのOCR読み取り結果チェック（check_type=\"ocr\"）\n"
                "  - list_deals(company_id, limit=500) で has_receipt=true の取引を全件洗い出し、\n"
                "    その全件について check_receipt_ocr(deal_id, company_id) で取引値とOCR値を突き合わせる。\n"
                "  - 全件を write_analysis で記録する（明確な不一致→error／軽微・要確認→warning／一致→ok。\n"
                "    一致でも ok を記録して証跡を残す）。OCRチェックに bulk_write_ok は使わない。\n"
                "\n"
                "■5. 通帳照合チェック（check_type=\"bank\"）\n"
                "  - import_bank_txns(company_id, start_date, end_date) で freee の入出金明細を取り込み、\n"
                "    find_bank_unmatched(company_id) を呼ぶ。銀行口座なし・明細0件ならスキップしてよい。\n"
                "  - balance_issues（残高連続性エラー＝データ化ミス候補）があれば最優先で整理して報告する。\n"
                "  - ledger_only（銀行口座決済なのに通帳に無い取引）を get_deal で確認し、\n"
                "    write_analysis(check_type=\"bank\") で記録する（疑わしい→warning／理由があれば→ok）。\n"
                "  - bank_only（通帳にあるが帳簿に無い明細＝記帳漏れ候補）は日付・金額・摘要を\n"
                "    整理してサマリーで報告する。\n"
                "\n"
                "最後に、各項目の結果（記録した件数と warning/error の要点）を\n"
                "日本語のサマリーとしてチャットにも報告すること。\n"
            ),
        },
        {
            "key": "duplicate",
            "title": "仕訳の重複チェック",
            "check_type": "duplicate",
            "body": pre
            + (
                "\n【タスク】仕訳の重複チェック\n"
                "手順:\n"
                "1. find_duplicate_candidates(company_id) で、発生日・金額・取引先が一致する重複候補グループを取得する。\n"
                "2. 各グループについて get_deal で明細を確認し、二重計上の重複か／正当な別取引（出荷ごとの送料・決済ごとの手数料など）かを判断する。\n"
                "3. グループごとに代表の deal_id へ write_analysis(check_type=\"duplicate\") で記録する。\n"
                "   - 重複の疑いが強い → verdict=\"warning\"、どの deal_id と重複か根拠を書く\n"
                "   - 問題なし → verdict=\"ok\"、別取引と判断した理由を書く\n"
                "4. 候補に挙がらなかった残りの取引へ bulk_write_ok(ai_name, check_type=\"duplicate\",\n"
                "   exclude_deal_ids=[候補グループ内の全 deal_id]) を1回呼び、問題なしを一括記録して\n"
                "   全取引に証跡を残す。\n"
            ),
        },
        {
            "key": "cross_payment",
            "title": "クレカ×現金の二重計上チェック",
            "check_type": "cross_payment",
            "body": pre
            + (
                "\n【タスク】決済手段をまたぐ二重計上チェック（クレカ×現金など）\n"
                "カード明細の自動取込と、領収書の現金手入力で同じ支出が二重計上される"
                "パターンを検出します。\n"
                "手順:\n"
                "1. find_cross_payment_duplicates(company_id) を呼ぶ。金額一致・発生日±3日以内の"
                "ペアが返り、決済手段が異なるペアは cross_payment=true が付く。\n"
                "2. cross_payment=true のペアを最優先で、get_deal で両方の明細・科目・決済口座を確認し、"
                "同一支出の二重計上か／偶然の同額別取引かを判断する。\n"
                "3. ペアごとに両方の deal_id へ write_analysis(check_type=\"cross_payment\") で記録する。\n"
                "   - 二重計上の疑いが強い → verdict=\"warning\"、相手方の deal_id と根拠を書く\n"
                "   - 別取引と判断 → verdict=\"ok\"、理由を書く\n"
                "4. skipped_recurring_groups（同額多数の反復取引）は対象外でよいが、気になる点があれば言及する。\n"
                "5. ペアに含まれなかった残りの取引へ bulk_write_ok(ai_name, check_type=\"cross_payment\",\n"
                "   exclude_deal_ids=[ペアに含まれる全 deal_id]) を1回呼び、問題なしを一括記録して\n"
                "   全取引に証跡を残す。\n"
            ),
        },
        {
            "key": "receipt_link",
            "title": "領収書・レシートの紐付けチェック",
            "check_type": "receipt_link",
            "body": pre
            + (
                "\n【タスク】領収書・レシートの紐付けチェック\n"
                "手順:\n"
                "1. list_deals_without_receipt(company_id, limit=500) で、証憑が紐付いていない取引を全件取得する。\n"
                "2. 証憑なしの取引は全件、write_analysis(check_type=\"receipt_link\") で個別判定する。\n"
                "   - 証憑を添付すべき → verdict=\"warning\"\n"
                "   - 少額・添付不要または問題なし → verdict=\"ok\"（理由を書く）\n"
                "3. 証憑ありの取引は bulk_write_ok(ai_name, check_type=\"receipt_link\",\n"
                "   only_with_receipt=True, result=\"証憑が紐付いているため問題なし\") を1回呼んで一括記録する。\n"
                "4. list_receipts(company_id, only_unlinked=True) で未紐付けの証憑を確認し、\n"
                "   どの取引に紐付けるべきかの候補を該当取引の result に書く。\n"
            ),
        },
        {
            "key": "digitize",
            "title": "通帳・クレカ明細のデータ化（画像読み取り＋相互検証）",
            "check_type": "digitize",
            "body": pre
            + (
                "\n【タスク】通帳・クレカ明細のデータ化（画像読み取り＋相互検証）\n"
                "アプリにアップロードされた原本（画像）を読み取り、明細データに書き起こします。\n"
                "手順:\n"
                "1. list_bank_documents(company_id) で原本一覧を取得する。0件ならその旨報告して終了。\n"
                "2. 各原本について:\n"
                "   a. entries_count=0（未データ化）の場合 → get_bank_document(document_id) で画像を取得し、\n"
                "      記載順どおりに全行を書き起こして write_bank_entries(document_id, ai_name, entries) で保存する。\n"
                "      返ってくる balance_issues（残高連続性エラー）が空になるまで、画像を読み直して\n"
                "      修正版で再保存する。最後に write_document_review で自己検証結果を記録する。\n"
                "   b. entries_count>0（書き起こし済み）の場合 → get_bank_document で画像を取得し、\n"
                "      list_bank_entries(company_id) の該当明細（document_id が一致する行）と1行ずつ突き合わせる。\n"
                "      write_document_review(document_id, ai_name, verdict, result) で検証結果を記録する。\n"
                "      - 完全一致 → verdict=\"ok\"\n"
                "      - 相違あり → verdict=\"warning\" または \"error\"、どの行がどう違うか具体的に書く\n"
                "3. 画像を読み取れない場合（対応していないAIの場合）は、その旨を報告し、\n"
                "   残高連続性チェック（find_bank_unmatched の balance_issues）による数値検証だけ行う。\n"
                "4. 最後に、原本ごとの行数・検証結果をサマリーで報告する。\n"
                "\n"
                "※ freeeへの明細登録はこのアプリでは行いません。検証済みの明細をfreeeに登録する場合は、\n"
                "   freee公式MCPを接続したAIに「この明細を wallet_txns として登録して」と依頼してください。\n"
            ),
        },
        {
            "key": "bank",
            "title": "通帳照合チェック",
            "check_type": "bank",
            "body": pre
            + (
                "\n【タスク】通帳照合チェック（データ化結果の検証と帳簿の突合）\n"
                "freee「データ化申込」等で作成された通帳の入出金明細が、元の通帳と整合しているか・\n"
                "帳簿と一致しているかを検証します。\n"
                "手順:\n"
                "1. import_bank_txns(company_id, start_date, end_date) で freee の入出金明細\n"
                "   （データ化結果）を取り込む。銀行口座が無い場合や明細0件ならその旨報告して終了。\n"
                "   ※ 他社サービスのCSVはアプリの「通帳照合」ページから取込済みの場合があるので、\n"
                "   その場合はこの手順を飛ばして 2 へ。\n"
                "2. find_bank_unmatched(company_id) を呼ぶ。\n"
                "3. balance_issues（残高の連続性エラー）を最優先で確認する。これは前行残高±入出金額と\n"
                "   当行残高が合わない箇所で、データ化の行抜け・金額読み誤りの候補。日付・摘要・差額を\n"
                "   整理し、「元の通帳のこの前後を目視確認」とサマリーで報告する。\n"
                "4. ledger_only（銀行口座決済なのに通帳に見当たらない取引）を get_deal で確認し、\n"
                "   write_analysis(check_type=\"bank\") で記録する。\n"
                "   - 二重計上・誤入力などの疑い → verdict=\"warning\"、根拠を書く\n"
                "   - 対象期間外・別口座決済など説明が付く → verdict=\"ok\"、理由を書く\n"
                "5. bank_only（通帳にあるが帳簿に見当たらない明細＝記帳漏れ候補）は、\n"
                "   日付・金額・摘要を整理し、推定される勘定科目も添えてチャットで報告する。\n"
                "6. 最後に、一致件数・残高エラー件数・不一致件数と要点をサマリーとして報告する。\n"
            ),
        },
        {
            "key": "ocr",
            "title": "領収書・レシートの読み取り（OCR）結果チェック",
            "check_type": "ocr",
            "body": pre
            + (
                "\n【タスク】領収書・レシートのOCR読み取り結果チェック\n"
                "手順:\n"
                "1. list_deals(company_id, limit=500) で証憑が紐付いている取引（has_receipt=true）を全件洗い出す。\n"
                "2. その全件について check_receipt_ocr(deal_id, company_id) を呼び、取引値とOCR値（取引先・日付・金額）の不一致フラグ(flags)を確認する。\n"
                "3. 全件を write_analysis(check_type=\"ocr\") で記録する（一致でも ok を記録して証跡を残す）。\n"
                "   不一致があれば原因を推測して書く（入力ミス／別証憑の紐付け／税込・税抜の差 など）。\n"
                "   - 明確な不一致 → verdict=\"error\"\n"
                "   - 軽微・要確認 → verdict=\"warning\"\n"
                "   - 一致・問題なし → verdict=\"ok\"\n"
                "※ OCRチェックでは bulk_write_ok を使わず、証憑付き取引は必ず全件個別に判定する。\n"
            ),
        },
    ]
