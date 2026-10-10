# 一般レシート共通カードと部分補完（offline candidate）

今回の変更はPR #91内の実装・synthetic検証まで。live Sheets、Apps Script、Cloud Run、実HGA、会計データを変更しない。既存10件・29,034円、p1入力、親PDFにも操作しない。

## 共通UI

既存`PDFページ確認`の一般完全手入力カードをベースに、`pdf_page_general.input_rows()`を共通化した。必須は支払日・実支払額・既存カテゴリ、任意は店舗名・支払方法・メモ。完全成功・部分欠損・完全手入力でラベルと入力行は同じ。`general_receipt_completion.card()`は同じ`PageReviewSheet.publish_cards()`に渡す。別Sheet・Gemini失敗画面・独自カレンダーは作らない。

可視列は既存のA95px/B190px。日付は意味で検出する既存DATE_IS_VALID/strict/`yyyy/mm/dd`、金額は正の整数/`#,##0`、カテゴリは既存マスタdropdownを使用。入力セルは黄色、すべて編集可能。空欄は入力途中として許可し、確定時に必須3項目を検証する。日本円の小数を丸めて通さない。日付serial/直接入力はcalendar dateへ変換し、timezone変換しない。

Receipt Unitごとに縦カードを作り、凍結した位置manifestから安定順序・Unit IDを引き継ぐ。Gemini配列順を使わない。元ページのDriveリンクを残す。今回crop画像の生成・永続コピーは追加しない。画像からの枚数入力や全items再入力を利用者に要求しない。

## 事前入力とprovenance

独立した2回以上のschema-valid読取が一致し、field validationを通る場合だけ事前入力する。明示されたconfidenceは有限値0.8以上、明示的な不明/低confidence/ambiguityは空欄。header confidenceが供給されない場合は再読取一致とfield validationで判断し、架空のconfidence数値を記録しない。

日付は有効な日付、金額は正の整数かつ各読取のitem sumと一致、カテゴリは既存pairかつitem confidenceを検証。支払方法は既存のlocal原本文字裏付けpolicyを通した値だけ。未取得・矛盾・不正・再読取不一致は対象fieldを空欄にする。欠損時の推測、履歴、ファイル名等による補完はない。

内部provenanceは`gemini / human / human_override / missing`。同じ日付のslash表記やserial表示への変更はoverrideにしない。Gemini値の変更・追加は最終値を検証し採用する。候補digestはoriginal baselineに固定し、編集を理由にstale候補のidentityを変えない。

## validationとpartial completion

`needs_human_completion → ready_to_confirm`は必須3項目完成後。構造上の問題があれば`needs_review`。

既存`manual_values`、`validate_receipt_result`、`apply_receipt_policy`を再利用。取得済みitemsの名前・quantity・amountを維持し、架空調整を作らない。本人のカテゴリ選択はそのReceipt Unitに属するitemsへ適用する。複数カテゴリや不明カテゴリは事前入力せず本人選択を求める。

正しいitemsの合計と本人が確認したtotalが一致する場合はtotalの読取誤りを補完できる。不一致のtotal、item構造の再読取変化、未確認調整明細、取引種別不明/変化/原本矛盾は、必須欄が埋まっても確定不可。分離count/重複bbox/item ownershipが不安定ならページ全体をsegmentation reviewに保ち、カード候補を作らない。

完全に結果がない場合も同じ空欄カードで完全手入力する。itemsを捏造せず、将来のwrite時には既存general manual writer経路を利用する。

## durable検証intentとreplay

`GeneralCompletion`はprotected Page/manifest/HGA/source/owner snapshot/category/duplicate adapterを注入する。Spreadsheetのhidden identityを正本にしない。source SHA256、stable page、review/revision、Receipt Unit ID、manifest、processing state、候補digest、現在入力snapshot、request UUIDを再照合する。duplicateがunknownの場合も拒否する。

`general-receipt-completion-v1`は既存strong ETag/If-Match/strict JSON/exact read-back storeを再利用。schema、binding、generation、候補、request snapshot/plan digestを検証。stale CASは再試行・無条件fallbackなし。同一UUIDと同一snapshotのreplayはbytes/version/generationを変更しない。UI反映失敗後は保存済みintentを読み、再適用せずprojectionを復旧する。`projected(read_current=...)`で現在の入力と状態を再評価できる。共通Sheet publisherにはMedical等も含めた全カードを渡し、他カードを消さないこと。

このPhaseのintentは`validated_not_written`、`posting_authority=false`、`accounting_allowed=false`、`medical_handoff_allowed=false`、`archive_allowed=false`。writerをimport/実行しない。Apps Scriptの新completion kindは、既存dispatch flagがtrueでも旧workerへ送らず受付だけ。入力だけではrequestを生成しない。新しい会計writer・定期処理・scheduleもない。

## HGA

`privacy_unresolved`をunknown系HGA候補理由に追加する。自動privacy結果は変更しない。既存署名検証・issuer/sub allowlist・nonce/state/PKCE・CSRF・明示POST・freshness・conditional保存の機構を使用する。受付セルや旧「一般」回答をAI許可へ変換しない。確認画面には「一般レシート確認」と「対象ページだけGemini送信許可」を明示する。

automatic Medical/payroll、clearly_sensitive、観測不完全は拒否。送信直前のexact PNG OCR、Medical/payroll/conflicting-sensitive/explicit PII拒否、fresh render proofとpayload hash guardは無変更。元PDF送信は不可。

synthetic E2Eは一時RSA鍵によるGoogle形式OIDC署名/allowlist検証→明示確認→HGA→exact RGB PNG mock Gemini→日付欠損→共通カード空欄→本人補完→validation-only intentまで通す。実Google認証・実Gemini・実authority発行は今回実行しない。

## 検証と次のGate

必須欄の各欠損組合せ、不正日付/カテゴリ、曖昧金額、optional空欄、human override、複数Receipt Units、source/revision/candidate/snapshot stale、duplicate/unknown、CAS競合、replay、UI失敗回復をsynthetic検証。既存Medical/manual/receipt/bank等のfull suiteも実行する。

375px幅のローカル静的表示プレビューで横スクロールなしを確認する。実Google Sheetsアプリ/iPhoneで今回のカードを配置・操作した確認とは区別する。

実p14はまだcanary未実施。live Cloud Runは既存synthetic-only構成で、この変更をdeployしていない。実Drive正本freshness/store adapter、explicit PII等を否定できる現行観測、認証付き新HGA確認、共通カードの限定projection/受付を別canary前に接続・検証する必要がある。`privacy_unresolved`の候補追加だけで実p14へ送信/記帳できる状態にはしない。本番write・PDF移動・定期接続は別canary。
