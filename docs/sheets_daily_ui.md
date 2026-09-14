# Google Sheets 日常UI

対象: Google Sheets「家計簿AI」、Spreadsheet ID
`1G44cDDUryVpZazTDwuCT4eZrir5KJb2WVm9baHTRPow`。

これはUI設定の実装・運用手順であり、本番適用の承認記録ではない。
初回はdry-run。具体的な変更プレビューと復元方法を提示し、利用者の承認後だけ適用する。

## 対象と表示順

先頭は **ホーム → 支出一覧 → 要確認 → Amazon要確認**。
設定・給与・Coverageをその右側に置く。内部台帳は非表示にし、削除しない。

| シート | sheetId | UI変更 | 用途・入力導線 / コード契約 |
|---|---:|---|---|
| ホーム | 1909140001 | 先頭、2列320px | 月の選択・数式集計・日常シートへのリンク。所有マーカーで再実行時の重複を防ぐ |
| 支出一覧 | 1889438451 | 2番目、書式、J列非表示 | `ExpenseViewPipeline.refresh()`が有効支出をA2:Jに再生成。説明・合計・独自数式を挿入しない |
| 要確認 | 1682462052 | 3番目、書式 | `ReviewPipeline.refresh()` / `ReviewApprovalPipeline`。J:N、Rを入力色。Kの統合先、Mの従来入力も維持 |
| Amazon要確認 | 1248165085 | 4番目、書式 | `amazon_review.py` / `amazon_review_preview.py` / schema install。空でも保持。Hの候補選択を入力色。新しい承認処理は追加しない |
| カテゴリ | 1571056330 | 右側、表示 | 大・小カテゴリマスタ。review Lの候補、AI分類から参照。`ensure_schema(categories)`に更新処理あり |
| 商品マスタ | 1674154424 | 右側、表示 | 商品カテゴリの確認・編集。Amazon import/reclassification等が読書き |
| 店舗 | 987268279 | 右側、表示 | 店舗設定用。`ensure_schema`作成対象。空であっても維持 |
| 給与明細ヘッダ | 1356414173 | 右側、表示 | Payrollの閲覧と履歴。専用branchの`payroll_sheets.py`はタイトル指定read-only |
| 給与明細項目 | 467192626 | 右側、表示 | 帳票値・確定値・確認状態の閲覧。手入力の可能性を残す |
| 給与標準項目 | 59038810 | 右側、表示 | 給与設定・参照マスタ |
| 給与項目別名 | 1059447273 | 右側、表示 | 給与の名寄せ設定。標準項目と勤務先に依存 |
| 勤務先マスタ | 166495142 | 右側、表示 | 勤務先設定・給与参照 |
| Coverage確認 | 81236831 | 右側、表示 | 期間確認の履歴・identity。専用branchの`coverage_confirmation_sheets_apply.py`がcreate/header/append、previewがread。確認導線を保持 |
| 支出明細 | 0 | 非表示 | 支出元台帳。receipt / Amazon / auto-expense / review等が読書き。日常入力は要確認、閲覧は支出一覧。手修正時は再表示 |
| レシート | 620056485 | 右側、表示 | 画像URLの唯一の既存表示先。取込処理が読書き。要確認に画像URLが複写されないため表示を維持し、ホームA23からリンク |
| 取込データ | 1833120072 | 右側、表示 | 統合先取込IDの参照先。全共通処理の台帳。通常review件数は状態列を参照。既存の統合先指定のため表示を維持し、ホームB23からリンク |
| Amazon注文 | 847076207 | 非表示 | 商品明細・注文baseline・hash。Amazon / reconciliation / review candidate生成が読書き |
| Amazon照合候補 | 1149502063 | 非表示 | review refreshが再生成。候補の確認・選択は要確認P:Tで行う |
| Amazonイベント | 312392508 | 非表示 | Gmailイベント・hash・match/apply履歴。取込・reparse・照合処理から参照 |
| Amazon注文ヘッダ | 1878689512 | 非表示 | 注文合計・状態・重複防止。通常購入等が読書き |
| _要確認カテゴリ候補 | 754328221 | 非表示、廃止調査候補 | 旧TRANSPOSE/FILTER数式が残存。現行の要確認Lは大｜小のONE_OF_LIST。今回は数式も含めて保持 |

シート名、既存sheetId、ヘッダ、列の物理順序、業務値、判断値、既存入力規則・共有・保護は変更しない。
未知のタブやIDが違うタブにはUI設定を送らない。ホームの数式源3シートの契約違いは適用を止める。
`ensure_schema`による既存ヘッダの更新は、このコマンドでは実行しない。

## 集計定義

- 対象月: ホームB3。初期値はTODAYから作る当月1日。ユーザー変更を再実行時も保持。
- 計上済み支出: **支出一覧だけ**を参照。元台帳・取込・Amazon・銀行金額を加算しない。
- 未分類: 有効な支出IDがあり、対象月に該当し、大カテゴリまたは小カテゴリが空白/`未分類`。
- 通常review: 取込データの非空IDで、状態が `要確認`、`needs_review*`、`*_needs_review`、`amazon_unmatched`。既存`is_reviewable_status`と同じ。保留・未反映を含む全期間。Amazonカードの通常照合もこちらに含む。
- Amazon review: Amazon要確認の非空Review IDのうち、確認状態が`反映済み`以外。未確認・選択済み・保留・エラー・空/未知の状態も未解決として数える。専用UIに未登録のGmail取消等は数えない。
- 両reviewは別経路の件数であり、二つを合計した「全未処理件数」は作らない。
- 0件は未取込・未対応sourceまで完了した意味ではない。収入・貯蓄率・残高・推定取込時刻は追加しない。

ホームD:Hは非表示の正規化補助領域。1個のARRAYFORMULA/LETで日付serial・文字列日付・桁区切り/円記号付き金額を扱い、SUMIF(S)/COUNTIFS/QUERYが参照する。カテゴリ表は全カテゴリ、横棒グラフは上位12のみ。
空白ID行を除外。金額不正や日付不正は0に隠して済ませず、確認件数と「要データ確認」を表示する。

配列の上限は各source **5,000データ行**。既存appendのINSERT_ROWSで参照範囲が膨張してspillを壊さないようINDIRECTで範囲を固定し、現行gridの上限内に収める。
3つのID列のCOUNTIFによる超過検知がある。上限超過時は不完全な支出合計を表示せず、参照上限の確認を求める。通常の再生成でgridが増えても、範囲内の新規業務行は数式で反映される。

## 書式とrefresh

支出一覧はヘッダ固定、日付・円書式、A:D合計345px、本文56px。Jの支出IDだけ非表示。
本文の書式変更とその復元は先頭5,000データ行以内に限定し、大量の未使用gridを毎回書式更新しない。
要確認とAmazon要確認は自動項目を白、入力項目を淡い黄色にする。判断値を増減せず、入力列を移動/非表示にしない。
ホームから取引情報（要確認C1）と判断欄（J1）、元画像（レシートF1）、統合先ID（取込データA1）へ直接移動できる。元画像や統合先を示す既存画面を隠すと確認導線が弱くなるため、この2台帳の非表示は今回は行わない。要確認全20列の同時表示は狭い画面ではできないため、既存の横スクロールを使用する。
長文の全量確認にはセルを開く。ホームの集計とリンクは横スクロールなしを想定する。

`SheetsDB.configure_review_validation()`と`format_date_column()`に小さな書式補正だけを追加した。
ホーム上の`kakeibo_daily_ui=1`を検出した場合だけ、追加行にも書式を補正する。
既存の取込・reconcile・auto-expense・review applyロジックは変更しない。
本番runtimeは既存のGitHub Actionsが取得するmainのUI関連差分を利用する。今回の承認対象には、この書式補正の既存コード基盤への導入も含めた。workflowや業務処理の実行条件は変更しない。

## 実行

既存の環境変数/サービスアカウントを利用する。新しいキー、OAuth認可、権限は不要。
ローカルに認証がない場合は既存Google Driveコネクタのread/write接続を使って同じ限定requestを実行できる。新規認証を作らない。

```powershell
# live read-only preview（既定。ensure_schemaは呼ばない）
python -m app.sheets_ui_cli --output .private/ui-plan.json

# connectorから収集したmetadataでのoffline previewのみ
python -m app.sheets_ui_cli --observed-metadata .private/ui-observed-metadata.json

# 利用者承認後だけ。<sha>は実際にレビューしたplanのsha256。
python -m app.sheets_ui_cli --apply --approve-plan <sha> --backup .private/ui-before-<unique>.json

# 数値を出力しないnative read-only照合
python -m app.sheets_ui_cli --verify
```

applyは再読取で契約/配置が変わっていないことを確認し、UI復元情報をGit除外の`.private`へ排他的に保存してから、限定batchUpdateを1回送る。送信の自動retryは行わない。
返答不明時は再送前にnative状態を読み、ホーム・chartId・marker・ヘッダを確認する。
機密値を含むHTTP例外本文やレスポンスをログへ追加しない。

## 復元

```powershell
# 初回はrestoreもpreview
python -m app.sheets_ui_cli --restore .private/ui-before-<unique>.json

# 復元previewの確認後
python -m app.sheets_ui_cli --restore .private/ui-before-<unique>.json --apply --approve-plan <restore-sha>
```

復元対象は元のタブ順/表示、日常3シートの今回変更した書式・幅・行高・固定行だけ。
**業務データをバックアップで全体上書きしない。** 入力規則、共有、保護には書き込まない。
初回新設のホームは削除せず右端で非表示にし、markerを`restored:1`にしてrefresh補正を無効にする。再導入時は同じホームを再利用する。
後続rerunの復元では、UI所有のホームの値・数式・月選択・書式・グラフも保存時点へ戻せる。
復元前にsheetId/ヘッダを検査し、別Workがschemaを変更した場合は自動復元しない。

## 検証と限界

fixtureで、requestの対象範囲、API schema/field masks、再実行、ホーム・グラフの重複防止、月選択の維持、復元、refresh後の手入力/候補選択/入力規則維持を確認する。
独立したPython計算で日付serial/文字列、円文字列、負数、0件、不正値を検証し、承認後のnative readbackと照合する。
手元のfixtureはGoogle Sheetsの数式エンジンではない。実施済みのnative数値照合と、fixtureだけで確認したrefreshは区別する。
本番review-apply/reconcile/auto-expenseをUI検証のためには実行しない。

## 2026-09-14 適用記録

利用者が変更プレビューを確認し、シートUIと書式維持コードの導入を承認した後、161 requestsを本番へ適用した。適用直前に元台帳の行追加を観測したが、承認された変更requestsは同一で、業務値には書き込んでいない。

- 既存20シートの全grid範囲を対象に、業務値・数式・判断を含む入力規則・メモ・rich textが適用前後で一致。既存sheetId/名前/ヘッダ/列順を維持。
- Googleの計算結果を独立計算と照合し、当月支出・未分類件数/金額・通常/Amazon review件数・全カテゴリ合計が一致。ホームの数式エラー、不正日付/金額、参照上限超過は0。
- グラフと所有マーカーは各1つ。適用後のlive metadataで147 requestsの再dry-runを生成し、新規ホーム/グラフ/結合/条件付き書式は0、月選択の再書込みなし。
- 全体fixtureは1084 passed、最終UIテストは17 passed。実際のExpenseViewPipeline/ReviewPipelineをfixtureで通し、判断・統合先・カテゴリ・候補選択・入力規則・書式の維持を確認。
- 適用前の書式/寸法/固定行/タブ順を`.private/ui-restore-before-apply.json`へ保存した（122復元requests）。実際の寸法を取得して保存し、業務値は含めていない。復元は上記CLIまたは既存コネクタでUIだけに適用する。
- nativeブラウザーを390px幅へ切り替えた。ホームの列幅合計320px、グラフ320px、補助列非表示、既存review入力列の表示維持をAPIで確認。ブラウザーの幅は検証後に元へ戻した。

**未確認:** 実画面画像の目視検証は、金融情報を十分に除去できると保証できないため自動承認レビューに拒否された。画像は送信していない。架空データの390pxプレビュー検証と表示寸法のreadbackは、native画像の切れ/重なりの目視確認を代替しない。画像確認の承認または利用者によるスマホ確認が必要。

元台帳と支出一覧には別Workの反映時点の差が残る。ホームは支出一覧の値を示す。本番refresh等はUI検証のためには起動していない。廃止調査候補は旧`_要確認カテゴリ候補`だけで、今回削除していない。

API仕様確認: [Sheets requests](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request)、
[LET](https://support.google.com/docs/answer/13190535?hl=en)、[QUERY](https://support.google.com/docs/answer/3093343?hl=en)。
