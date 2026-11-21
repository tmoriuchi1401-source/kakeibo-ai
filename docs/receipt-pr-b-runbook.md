# PR-B: 一般レシートの確認・登録・backend検証（金融write OFF）

開発基準は PR #201 / `d2440c4b43c5a54411e0abd91cfa7ded0ad674c5`。
P1のCase2成功記録（run 44907265127）は基準実績であり、このPRの実データ試験結果ではない。
本PRはコード・合成テスト・Draft PRまで。実データ、既存ブック、デプロイ、Variables / Secrets、
トリガー、OAuth、Cloud設定は変更していない。main mergeも行わない。

## 実装された経路

1. `drive_receipts.process_review_inbox` が既存の `receipt_batch` で一般原本を最大1件選択。
   folder内の列挙が不完全、原本版が取得前後で変化した場合は停止。原本移動の引数はない。
2. `ReviewPipeline` が既存のprivacy gate / `ReceiptPipeline._analyze` / 解析候補形式を利用。
   既存のfinancial materializerは呼ばない。新規解析と保存済み候補の `make_case` 変換に対応する。
   医療・不明は完全手入力のまま。PDFは一般／混在を含め丸ごと保留し、外部AIへ送らない。
3. `_receipt_cases` に原本ID・SHA256・Drive version・取込／候補／カテゴリ基準・予約UUIDを保存し、
   「確認」へ未登録行を同じbatchで生成。途中メモや下書きのある既存行を再生成・上書きしない。
4. iPhoneの「確認」1タブ、1行＝1案件。案件ID列・見出し固定、横スクロール、原本リンクを維持。
   商品名・数量・行金額・大／小カテゴリ・種類・対象商品・採用／除外・原本照合を8枠まで編集。
   未使用枠への入力で追加。数量は小数3桁まで、行金額は整数円で数量と別管理。
   税の扱い（税込／税別／不明）と総額の原本照合を明示する。8件超は切捨て処理せず保留。
5. I列のチェック1回で `ready:UUID` → `register:UUID`。
   ロック下のローカルenabledと、PR-Aの `receiptSheetsAcceptance_` によるGitHub受付ゲートの
   両方がONの場合だけ、入力版hashを含む固定snapshotを `_receipt_requests` に1回保存する。
   案件・原本・版・hash・UUIDはセル座標ではなく独立したcatalogから照合する。
6. Python backendが `受付済み` をclaimし、`検証中` → `検証済み・未記帳`／`修正が必要`／`保留`。
   元画像・所有者・親folder・version・hashを検証前後で読取り照合。現在のUIセルは読まない。
   snapshotをセル入力から再構築し、入力版・操作・カテゴリ・金額・税／値引きの配賦を検証する。
7. G列はUUID・案件・原本ID／版／hash・基準digest・結果bindingを照合して状態と理由を表示。
   登録後の編集・メモ追記は下書きに保持され、結果は常に「登録時の内容」を指す。
   総額／商品合計／差額を別々に保持し、差額を埋める架空行は作らない。

税別の外税と負額の値引きは、同一カテゴリまたは明示した対象商品と整合する場合だけ通る。
税込の内税を追加加算、不明な税処理、複数カテゴリへの配賦不明は保留。
数量だけ変更して行金額を未確認のままにした場合も修正理由を返す。

## 排他・履歴・UNKNOWN

受付・intake・backendのclaim／finishはすべて同じApps ScriptのScriptLockを使う。
PR-Aの直接Sheets PUTは、固定一案件・他writer排除の試験専用のまま変更しない。
PR-Bは既存ScriptのExecution API経由で状態更新し、二つのbackendが同じ受付行をclaimできない。
Webアプリ、Cloud Run、新規Webサーバー、署名、厳密な編集者識別は追加していない。
実行ownerの一致は確認するが、ON_EDITの実行ownerをセル編集者本人の認証とは呼ばない。

catalogと受付履歴は永続保持し、UUID・案件ID・原本ID・原本hashの重複を拒否する。
端末やPythonプロセスの再起動、別UUID、同じ画像の別Drive IDでも重複処理しない。
履歴を削除して再利用しない。上限は見出しを含め1000行で、満杯は停止し自動ローテーションしない。
管理タブは非表示・保護する。ownerによる保護回避や管理タブの構造変更は対応外であり、
有効化中は管理タブの編集・削除・並べ替え、別Scriptからのwriterを禁止する。
表示タブの完全な行並べ替えは対応。案件／原本／機械IDが食い違う部分ソート・コピー・重複・欠落は拒否する。
identityを保持した値だけのコピーは手編集と識別できないため、登録前の全商品の原本照合を必要とする。

各変更の前に永続STOPを保存し、確定したread-back後だけ解除する。応答喪失・途中終了では
STOPが残る。Pythonも最初のclaim／AI送信より前にprivate SQLite attempt fenceをcommitする。
このDBは同一runnerで継続使用し、プロセスを再起動しても捨てない。
UNKNOWNは自動再送・再登録・再claim・再解析しない。後日の独立した読取り照合のみ可能。
登録後の再編集で受付snapshotや結果を更新しない。同一案件の再受付・UNKNOWN回復操作は本PRに含めない。
Startは既存トリガー・履歴・両ゲートを確認し、Stopはローカル受付OFFとPATコピー削除のみ行う。
Stopは履歴・下書き・STOPラッチを消さず、実行中backendの完了を保証する操作でもない。

## 合成検証

`python -m pytest -q tests/test_receipt_pr_b.py` は実際のApps ScriptをNode VMで実行し、Pythonの
実validatorへ固定snapshotを渡す。偽HTTP／Sheets／Drive／AIのみ。金融DB生成とネットワークの
禁止カウンターを、例外が握り潰された場合も含めて0件と確認する。
実際のSheets API batch payload、RAW相当のstringValue（数式注入防止）、チェックボックス、
カテゴリ／税／値引き入力規則、商品合計・差額・結果の数式生成も確認する。
実Google Sheetsでの数式評価、OAuth、実画面、iPhoneでの8商品横スクロールは未実測。
PC／合成テストの成功を実機完了と扱わない。

ローカル結果（Python 3.12.14 / Node 22.23.2）:

- 全体回帰: **5,025 PASS**（PR-B基本64ケースを含む。その後の追加ケースは以下で確認）。
- 最終対象: **215 PASS**（PR-B 83、既存P1 105、Drive取込19、production workflow契約8）。
- 既存Node checks: **13 PASS**。compileall / whitespace checks: PASS。
- PR-B禁止カウンター: 金融DB生成0、実ネットワーク0。医療／不明／混在PDFのAI呼出し0。
- 実データ取込・実iPhone・実OAuth・実API executable・実Sheets数式評価: **未実施**。

既存 `receipt_sheet_cells`、PR-Aのsnapshot／canonical／UUID／受付ゲート、候補照合、商品修正previewを再利用。
一般の合計一致候補もreviewできるよう `general_review=True` を追加したが、既存呼出しは既定値のまま。
既存P1 worker・authority・context、金融writer、workflow本文・本番設定は変更しない。

## 実レシート1件の試験前に別途承認する対象と副作用

実行はまだ行わない。最初に対象の原本1件、読取りfolder、独立reviewブック／Script、owner、
実行するcommit、利用するOAuth client／権限、試験時間、停止手順を具体的なIDで提示し承認を得る。

| 対象 | 承認後の副作用 |
|---|---|
| 新規の正常な一般画像1件 | Driveメタデータ／画像の読取り。保存済み解析候補を再利用できれば外部AI送信なし。新規解析が必要なら正常判定済みのその1画像だけAI送信することを別途明示する。医療・不明・PDFは送信しない。 |
| 独立reviewブック | 空の「確認」と二つの管理タブの準備、1案件／1受付snapshot／検証結果の書込み。コードの書込み先はその3タブに限定し、金融シートへ接続しない。owner OAuthのscope自体はファイル単位の権限隔離を保証しない。既存本番／P1試験ブックは変更しない。 |
| 既存または承認された独立Script | `Code.gs`, `Sheets.gs`, `Review.gs`, `ReviewSetup.gs` と既存manifestを基準commitから配置。`receiptReviewPrepare` は空の独立UIでのみ実行し、既存管理タブがあれば停止。API executableとowner作成のON_EDITトリガー1個が必要。コードは資源・トリガーを自動作成しない。 |
| OAuth／API設定 | owner OAuthと必要scope、Scriptとclientの共通standard Cloud project、Apps Script APIの利用可否を確認。未設定なら変更内容を提示して承認後に設定。SA鍵は作成・流用しない。Cloud Runは不要。 |
| 受付ゲート | ScriptPropertiesのPR-B設定・PATコピー、既存GitHub受付Variableの短時間ON/OFF。変更対象とP1への影響を先に提示し、無断で切り替えない。本番Variables／Secretsは変更しない条件を維持し、共有受付フラグが本番扱いなら別の承認済み試験経路が必要。 |
| ローカルrunner | 明示指定したprivate設定と永続attempt DB。owner access tokenの期限切れは停止し、自動refresh／再送しない。必要なら正常画像だけのAI鍵を明示入力する。 |

Execution APIのOAuth／共通Cloud project／API executable要件は
[Google公式仕様](https://developers.google.com/apps-script/api/how-tos/execute)を参照する。
API executableはHTTP公開Webアプリではない。設定が未承認・未検証なら実試験は開始しない。

runner設定schemaは `receipt-review-runner-b1`、`enabled=true`、`financial_write_enabled=false`、
`script_id`、`folder_id`、`owner_email`、`categories`（大／小の配列）。
Script設定は `RECEIPT_REVIEW_CONFIG_JSON` に `enabled=false`、`spreadsheet_id`、`folder_id`、
`owner_email`、`script_id`、`repository=tmoriuchi1401-source/kakeibo-ai` のみ。
実値をGit／PR／CIへ保存しない。P1 profile、固定UUID、Case2 Secret、一時SA鍵は使わない。

承認後のコマンド形（ここでは未実行）:

```text
python -m scripts.run_receipt_review --config PRIVATE_JSON --attempt-db PRIVATE_DB intake
python -m scripts.run_receipt_review --config PRIVATE_JSON --attempt-db PRIVATE_DB watch --max-polls 120 --poll-seconds 5
```

`RECEIPT_REVIEW_OWNER_ACCESS_TOKEN` と、解析時だけ `RECEIPT_REVIEW_GEMINI_API_KEY` を明示する。
watchは最大120回の読取りpoll。受付済みだけ検証し、1件でもUNKNOWNならその場で終了する。
資源・既存workflow・スケジュールは自動作成／変更しない。単発backendは `validate UUID` または `pending`。

一般画像1件を取込 → iPhoneで原本を比較して商品1点を修正 → 原本照合／総額確認 → 登録1回 →
backend／G列を独立照合 → Stop／受付OFFを実測する。金融write 0、重複0、再送0、原本移動0を確認。
金融write・医療AI送信・原本移動・main merge・未承認データ変更が必要になった時点で停止する。
