# 銀行収入とPayrollの分離（本番切替前）

## 方針と現行接続

家計簿収入は、収入と確認できた銀行入金の**入金日・実入金額**で記録する。
給与明細の総支給額・控除額・差引支給額を家計簿へ加算しない。
Payrollとの照合は今回実装しない。給与天引きの税・社会保険料を支出に自動計上しない。

開始mainは `af80174e6bb35e60a763f180858465b7cf075f16`。
報告済み `73ff2ff` より進んでいたため、最新mainを独立cloneした。
開始時dirty/stashなし。専用branchは `feature/bank-income-payroll-separation`。
リポジトリ内にAGENTS.mdはない。

- mainのPayroll入口は `payroll-file-preview` / `payroll-drive-preview`。
  Windowsの既存Payroll scheduled Taskは別checkoutの
  `scripts/run-payroll-scheduled.ps1` → `payroll-production-scheduled` を参照する。
- その保存adapterは `payroll_google_sheets_adapter.py` の
  `append_header_rows` / `append_item_rows`。書込み先は
  **給与明細ヘッダ / 給与明細項目**だけで、金銭項目を維持する。
  勤怠・有休・時間などの保存対象は追加しない。既存checkoutやTaskを変更しない。
- 現行ホームは `支出一覧` に基づく支出集計。Payrollを参照していない。
- `bank_finalization.py` は `bank_income` を保持するだけで、収入writerを持たなかった。
  接続されていないPayrollを「切断」するスイッチや移行機構は作らない。

## 最小実装

`app/bank_income.py` に判定、保存済み取込行からのpreview、batch writer、月次集計を置く。
`resolve_transaction_identities` と既存bank stable ID/hash、
`parse_import_rows`、確認済みルールの読み込み・評価、SheetsDBを再利用する。
支出IDと同じSHA256方式に `I-` を付け、収入IDを元取込IDから決定する。

### 判定

- 口座alias・方向・正規化した摘要が完全一致する既存確認済みルールを再利用。
  transfer / reimbursement / other_nonwrite / needs_review は収入にしない。
  矛盾する複数ルールは要確認にする。
- ルールがない場合、銀行摘要先頭の取引種別「給与」「賞与」
  （終端・空白・`*`の境界付き）と明示された利息種別だけを収入にする。
- 入金方向、`bank_income`ラベル、同日同額、銀行名、勤務先名だけでは確定しない。
  「給与振込特典」「振込 勤務先」や広い特典キーワードは要確認。
- 明示された返金・立替精算・借入は非収入へ分類。
- 保存済み行の支出リンク・review等の処理状態は自動変換しない。
  `bank_income`は選択の制約であり、収入確定の根拠ではない。
- 日付、正の整数金額、bank source/alias/identity/hash、parser受理状態を確認。
  同じIDで内容が衝突するものは要確認。同日同額の別IDとparserのoccurrence suffixを保持する。

### 保存先案：収入明細（1シート）

| 列 | ヘッダ | 内容 |
|---|---|---|
| A | 収入ID | `I-` + 元取込IDのSHA256先頭24桁 |
| B | 入金日 | `YYYY-MM-DD`、銀行入金日 |
| C | 金額 | 実入金額、正の整数円 |
| D | 分類 | 給与 / 賞与 / 利息 / その他確認済収入 |
| E | 入金元 | 銀行の元摘要（勤務先マスタとの照合なし） |
| F | 口座alias | 既存の非機微alias |
| G | 元取込ID | bankpdf stable identity |
| H | データ元 | 既存3銀行のsource |
| I | 判定根拠 | 銀行種別または確認済みルールのreason |
| J | 元データハッシュ | 既存取込行のbank hash |

本番シートはまだ作らない。`INCOME_HEADERS`を共通`HEADERS`へ登録しないため、
既存 `ensure_schema` の実行で勝手に新設されることもない。
承認後は新設1シートのA1:J1へこのヘッダを配置する。

writerは `income_write_enabled=False` が既定。明示trueでも別の承認済みtarget IDと
最大100件のバッチ上限・選択IDが必要。実行直前に取込行と収入保存先を再読込みする。
未作成シート・ヘッダ不一致・既存収入の内容衝突は書込み前に停止する。
確定行だけを `SheetsDB.append_raw` で文字どおり保存し、数式として摘要を評価しない。
全列read-backを確認する。既存行のupdate/delete、支出・取込データの変更は行わない。
完全一致の再実行はappend 0。結果不明時の自動再試行やrollbackはしない。
read-backで書込み済みと確認できた行は再追加せず、内容不一致は停止する。
本番利用には後述の共通排他と結果不明時の運用手順が必要。

## 過去分と新着の分離

### Backfill

```powershell
python -m app.cli bank-income-preview
```

読み取り専用のGoogle clientを使う。`--apply`は存在しない。
`取込データ!A2:L`の保存済み行が入力で、PDFを再ダウンロード・再取込しない。
出力は分類別件数/金額、ID・理由・元行番号、収入行案、月次集計、要確認摘要グループ、
既存収入/内容衝突。個人データを含むためコンソールまたはGit除外の私的領域に保存する。

承認範囲案は、指定Spreadsheetの収入明細1シート新設と、凍結したbackfill planの
確定ID集合・件数・金額上限に限る。承認後も実行直前に再previewし、
ID・金額・日付・hash・ルールの変更があれば計画を更新する。
初回は確定1件でread-back確認後、同じ承認バッチの残りへ進める。
候補ごとの毎回承認は設けない。今回の計画にない将来分へ一括承認を流用しない。

### 新着PDF / recurring

`build_bank_daily_preview`が同じ`deposit_decisions`を呼び、
`household_income`に判定結果を追加する。旧`new_income`は取込側の旧分類件数であり、
家計簿の確定収入件数とは区別する。recurring summaryにも確定/要確認件数を出す。
これらは各PDFで観測した入金の件数で、期間重複PDFの合計を月次収入に使わない。
月次収入は収入明細の一意な確定行からだけ集計する。

既存expense-only authorityの対象・上限・状態保存・processed処理・支出判定は維持。
収入writerをrecurringや本番Workflowへ接続しない。income writeはOFFのまま。
既存processed PDFを未処理へ戻す処理はない。

本番有効化の最小承認範囲案：

1. 対象Spreadsheet / 収入明細、既存3銀行・承認済みalias・新着folder。
2. 許可する確定ルール集合、期間・件数・金額上限、発効/失効期間。
3. 新着の確定収入に必要な取込行保存と収入明細append/read-back。
   支出リンク欄は収入IDに転用しない。
4. 現行`kakeibo-production`共通排他内で既存expense処理と直列に実行。
   収入未保存/結果不明のままそのPDFを完了扱いにしない接続を、切替時に追加検証する。
5. writerと全列read-back後の成功記録。失敗・不明時にcheckpointを飛ばさず停止。

これは承認案であり、現行expense-only authorityを拡張する実装・Secrets変更ではない。
スケジュールはmainの現在コードで06:47 JST、schedule経路はdry-run。
既存運用を停止・変更せず、収入scheduled writeを有効化しない。

## 月次集計 / UI追加案（本番未変更）

- `monthly_income` は収入明細だけを受け取り、bank provenanceと一意IDを検証して月別集計する。
  Payroll、支出、取込側の`bank_income`件数/金額を足さない。
- ホームの月選択B4と計算月B3、既存支出B6・カテゴリ集計は維持する。
- 空き領域A26:B30に「銀行確認済収入」「収支」「収入確認待ち」を追加する案。
  B26=月次収入、B27=B26−B6。既存支出B17の異常時は収支を数値表示しない。
- RAW保存の日付はISO文字列なので、例えば収入表示は次の式を使用する。
  認可されたwriterだけが生成する確定行を対象とし、展開前のledger検証も行う。

```text
=SUMIF(ARRAYFORMULA(LEFT('収入明細'!B2:B5001,7)),TEXT($B$3,"yyyy-mm"),'収入明細'!C2:C5001)
=IF($B$17>0,"要データ確認",B26-$B$6)
```

表示の5000行上限を超えた場合は集計エラーを明示し、範囲拡張を先に行う。
月候補I列には収入のみ存在する月も追加するが、現在の選択B4は保持する。
要確認の入金がある月は「確認済分のみ」と表示し、完全な月次収入と誤認させない。
未取込月を収入0円と断定しない。過去分欠落を一律除外で作らない。

## 実データ監査と切替

指定Spreadsheetをコネクタで再読取りし、全取込範囲と支出明細・Payroll2表・ホーム数式を監査する。
確認済みルールは既存私的設定から読取る。件数や既存計上の有無は推定せず、私的報告に記録する。
既存の家計簿収入がある場合は期間ごとの切替を検討し、一律削除や過去期間の除外をしない。
給与原本・明細と現行支出を保持し、確認済み銀行分だけを収入保存先へ追加する。

金額・摘要・口座alias・取込ID・元行番号・実データmonthly previewは、
Git除外 `.private/bank-income-plan.json` と `.private/bank-income-report.md` に保存。
元read-only snapshotと既存ルールsnapshot、分類件数・既存計上の監査結果も同領域。
実データとそこから得られた診断結果を公開Gitへ入れない。

## 検証と停止位置

- `tests/test_bank_income.py`: Payroll有無と独立した銀行候補、銀行分だけ1回計上、
  確認済みルール、非収入/要確認抑止、再取込・重複期間・同日同額の別取引、
  ID衝突、null空欄、schema/target/default-OFF、全列read-back、結果不明、RAW保存、
  月次集計のbank限定、新着PDFと保存済み行の同じ判定、preview CLIのread-only境界。
- 既存bank expense / transfer / card settlement / recurring authorityとPayroll parserの回帰。
  別checkoutの既存Payroll writer合成テストも17件成功。保存adapterや実行経路の変更なし。
- 最終full pytest / compileall / diff-check結果はPROJECT_STATUSに記録する。
- 実データではread-only snapshotに同じpreviewコードを実行。Sheets/Drive write 0。
  シート新設、main反映、既存行削除、Secrets/authority変更、scheduled income writeは未実施。

上記は初回実装時の停止位置。以下の個別承認済みbackfill入口を追加した。

## 固定集合の手動backfill

`bank-income-backfill.yml` はmanual dispatchのみ。既定はpreviewで、income recurringは接続しない。
既存Secretsを参照し、共通 `kakeibo-production` concurrency内のmainジョブで実行する。
`expected_head` は検証済みmainの完全SHAを渡し、checkout・実行SHA・origin/mainの一致を必須とする。
同一SHAのpreview成功後、今回承認されたapplyを1回dispatchする。

`app.bank_income_backfill` は私的planの全列と対象Spreadsheetを単一SHA-256 commitmentで拘束する。
取込時刻の上限は集合復元の補助で、時刻だけでは計上を許可しない。
元集合・内容・既存ルールが一致しなければシート準備前に停止する。
後着取引を補充せず、既存完全一致分をskipする。

収入シートがなければaddSheetとA:J RAWヘッダのみを作成する。
既存シートの修復や共通ensure_schemaは呼ばない。
既存BankIncomePipeline.applyを使い、未反映1件の全列read-backとreplay 0成功後に残りを追加する。
最終固定集合の再preview、replay 0、全列一致、重複排除、月別合計の一致を検証する。
各段階で保護対象の数式/値をbatchGetで比較し、読取頻度を制限する。
Payroll・支出・取込・要確認・ホームへのwriteやDrive操作はない。

read-back不一致、通信結果不明、保護対象変更では後続を停止する。
自動retry・rollback・成功分の削除は行わない。失敗したapplyを再実行せず、
まずread-onlyで結果を確認し、成否不明の行を推測で追加しない。
公開Actionsログは件数と検証結果だけとし、金額・摘要・対象IDは出さない。
実績と月別金額はGit除外 `.private` に記録する。

追加テストは合成値だけで、固定内容変更、対象外追加、既存一致、衝突、
canary失敗・成否不明、保護対象変更、最終照合とreplay、main実行境界を検証する。
