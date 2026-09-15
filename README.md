# 家計簿AI — 無料運用を前提にした初期実装

銀行入金だけを家計簿収入へ記録する実装とPayroll分離については
[銀行収入・Payroll分離](docs/bank_income_payroll_separation.md)を参照してください。
`python -m app.cli bank-income-preview`は保存済み取込行のread-only計画を表示します。
固定backfillは反映済み。収入recurringは既定OFF、ホームUIは未変更です。
既存銀行runnerへの接続・再開・有効化に必要な別承認は
[銀行収入recurring接続](docs/bank_income_recurring.md)を参照してください。

一般レシートのoffline read-only MVPは [GENERAL_RECEIPT_MVP.md](GENERAL_RECEIPT_MVP.md)
を参照してください。画像/PDFから購入日・店舗名・支払総額を抽出し、既存transaction
形式とduplicate/reconciliation結果をpreviewします。外部AI送信とSheets書込みは行いません。

## 現在実装済み
- Google Sheets 7シートのヘッダー/カテゴリマスタ初期化
- レシート画像 → Gemini structured output → カテゴリ検証 → Sheets登録
- レシート明細合計と総額が合わない場合は「要確認」にして支出へ自動計上しない
- Google Drive のレシート受信フォルダをPythonで巡回
- Amazon Order History.csv の全履歴CSVから差分抽出
- Amazonは `Order ID + ASIN` を商品行キーに使用し、quantity=0の取消/調整行を除外
- Amazon商品マスタ（ASIN）を再利用し、未知商品だけGemini分類
- 同じAmazonキーで内容が変わった場合だけ更新
- baseline後のAmazon差分商品を固定支出IDで「支出明細」へ商品単位計上
- au PAY利用通知メールを伝票番号で重複なく「取込データ」へ登録
- レシートとau PAY／カード利用を金額・日付・店舗で安全側に照合
- GitHub Actionsで3時間ごと＋手動実行
- 処理済み、要確認、既取込のレシート画像を `receipt_processed` へ退避

## シート名
支出明細 / レシート / カテゴリ / 店舗 / 取込データ / Amazon注文 / 商品マスタ

## 1. Google Cloud
1. Google Cloudでプロジェクトを作成。
2. Google Sheets API と Google Drive API を有効化。
3. サービスアカウントを作成しJSONキーを発行。
4. 作成済みの家計簿スプレッドシートをサービスアカウントのメールアドレスへ「編集者」で共有。
5. Google Driveに `receipt_inbox` と `receipt_processed` フォルダを作り、同じサービスアカウントへ編集権限を付与。

## 2. Google AI Studio
Gemini APIキーを発行。`.env` の `GEMINI_API_KEY` に設定。APIキーはGitHubへコミットしない。

## 3. ローカル/Codespaces設定
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```
`.env` に `SPREADSHEET_ID` 等を設定し、サービスアカウントJSONを `service-account.json` として置く。

## 4. Sheetsを初期化
```bash
python -m app.cli init
```
既存シートは維持し、必要なヘッダーと確定カテゴリを書き込む。

## 5. レシート1枚でテスト
```bash
python -m app.cli receipt /path/to/receipt.jpg
```
合計整合性が取れれば「支出明細」に商品行が入る。不整合なら「レシート」「取込データ」に要確認として残り、支出には入らない。

## 6. Amazon履歴を初回投入
```bash
python -m app.cli amazon "/path/to/Order History.csv"
```
毎回フル履歴CSVでもよい。同じ `Order ID + ASIN` は再追加されず、内容変更だけ更新される。

### 今回の実CSVで確認した点
- 880行
- `Order ID + ASIN` は879通り
- 重複1組は quantity=4 の購入行と quantity=0 の調整行
- quantity=0を除外すると836行すべて `Order ID + ASIN` が一意
- 688注文ID、複数商品注文127件、最大6商品/注文

## 7. iPhone運用

iPhoneへGoogle Driveアプリを入れてログインする。Driveアプリの右下にある追加ボタンから
「スキャン」（カメラアイコン）を選び、レシートを撮影する。撮影後の保存先として
`receipt_inbox` を指定する。Driveアプリが作成するPDFはそのまま取込対象になる。

ファイル名の変更は必須ではない。GitHub Actionsが3時間ごとにDriveを確認し、正常取込、
要確認、既取込の画像またはPDFを `receipt_processed` へ移す。解析に失敗して記録も
できなかったファイルは `receipt_inbox` に残るため、原因を確認して再処理できる。

初回テストでは1枚撮影し、Driveアプリで `receipt_inbox` への保存を確認してから、
GitHub Actionsの `Process receipt inbox` を手動実行する。完了後、ファイルが
`receipt_processed` に移り、Sheetsの「レシート」「取込データ」と、正常時は
「支出一覧」に反映されることを確認する。

参考: [Google: iPhoneで書類をスキャンする](https://support.google.com/drive/answer/3145835?co=GENIE.Platform%3DiOS&hl=ja)

## 8. GitHub Actions

### 統合・切替準備（2026-09-15、実切替未実施）

この節をWindows開発 / Actions本番 / Drive状態保存の切替手順の集約先とする。
現状のscheduleと実績を下表で分離する。新しい親Workflowと既存CLIの接続は
mainへ統合済み・新入口無効。本節の切替後の移行目標は現行運用ではない。

#### 現在地: 新入口無効でmain統合済み

- 09-15、mainを`73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2`から
  `002a112bcbfba2fcee5b9b6abf2e63f0d28e7cca`へfast-forwardで通常pushした。
  後続の完了記録commitは文書だけで、実行コード/Workflowはこの検証済みSHAと同一。
- [修正後Linux CI](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34925859316)
  は同SHAで成功。Ubuntu 24.04.5 / Python 3.12.14、一般1183 passed（13.47秒）、
  Payroll/Medical合成51 passed（4.30秒）、合計1234。両jobのcompileall/diff-checkも成功。
  本番/AI Secrets・実帳票なし。Windows全体1234 passed、集中回帰42 passed。
- GitHub Actions Variables設定画面でRepository Variablesなしを統合直前に確認。
  `KAKEIBO_PRODUCTION_ENABLED` / `KAKEIBO_SCHEDULE_ENABLED` / `KAKEIBO_LEGACY_DISABLED`
  はすべて未設定。前後のAPIで旧25 Workflow activeを確認した。
  統合後、新親のWorkflow登録状態はactiveだが、jobのVariable条件を満たさず本番処理は開始しない。
  旧日常4入口はlegacy停止条件に該当せず、従来scheduleを継続できる。
- 専用branchは`integration/production-orchestration-20260915`、upstreamは同名の`origin`。
  通常pushのみ、force push/保護ルール迂回なし。他のworktreeを変更していない。
- 今回の直接外部変更はbranch/mainへのコード・文書pushとSecretsなし合成CI。
  Google write/move/delete、state作成・upload・移送、Variables/Secrets/OAuth、Task、
  Workflowの手動起動/enable-disable設定、公開設定/課金は変更していない。
  旧自動運用のGoogle操作とは区別する。本番切替完了・L4確認済みではない。

#### 開始時のGit・Windows確認

- 対象: `tmoriuchi1401-source/kakeibo-ai`。取得した最新main:
  `73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2`。
- 今回の作業先は `Documents/Codex/2026-09-15/goal-kakeiboai-windows-github-actions-google`。
  開始時は空・Git管理外。独立clone後に
  `integration/production-orchestration-20260915` を作成。開始時dirty/stashなし。
  専用branchのupstreamは未設定。mainへの変更なし。
- 既存Windows mainは `2026-08-31/kakeiboai-github-codespaces-kakeiboai-windows-pc/work/kakeibo-ai`、
  HEAD `af1ff3a`、`main...origin/main`、dirty/stashなし。既存worktreeは保持した。
  他のworktreeのdirty差分は未調査・未変更。
- `PROJECT_STATUS.md` / `RECEIPT_ROUTING.md` / 実コード / 全25 Workflowを読んだ。
  最新mainのtracked tree、今回の作業先と親ディレクトリ、既存mainに
  `AGENTS.md` は見つからなかった。古いチャットを実行authorityにはしていない。
- Task Schedulerをread-only照合。関連Taskは `KakeiboAI Payroll Scheduled Scan` 1件。
  毎日06:00 JST、Ready、最終実行2026-09-15 06:00:01 JST、結果0、次回09-16 06:00。
  launcherは `2026-09-04/kakeibo-ai-payroll-materialization-adoption/scripts/run-payroll-scheduled.ps1`、
  同worktreeを作業先に `python -m app.cli payroll-production-scheduled --config-file ...`。
  config/state/logはユーザーの非公開ローカル領域。06:00:20の最新logで
  `scheduled_read_only_scan / read_only=true / writer_invocation_count=0` を確認。
  原本・金額・認証内容は出力していない。新規明細件数は未確認。
- Documents/Codex内の `.ps1/.cmd/.bat` を探索し、関連launcherは上記1件。
  依存環境のactivate類はlauncherから除外。一部過去pytest領域はアクセス不可。
  ユーザーStartupはOllamaのみ、Desktopにkakeibo名のファイルなし。
  全マシン・他ユーザー・他ツールの直接起動まで確認したという意味ではない。

#### 現行 → 移行後の実行責任

Actionsの時刻はcron設定であり、実際の起動保証時刻ではない。
全25 WorkflowはGitHub API上active。下のrunメタデータは2026-09-15確認、
successは処理件数や実write成功の証明と区別する。Secret/Variableの値は未取得。

| source/責任 | 現行入口・設定JST | 現行コマンド / 書込み先 / authority | state | 直近run実績 | 移行後の責任（branch準備） |
|---|---|---|---|---|---|
| Amazon通常購入 | `amazon-daily-import.yml`、05:23 / manual | `amazon-gmail-recurring --apply`はschedule。manual既定preview、canaryはexact条件。Amazonイベント/ヘッダ・取込・支出、最大100メール/3購入/3日、2h overlap | cache `recurring.sqlite3` | [#133](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34786888299)、schedule success、09-14 07:28 JST | 親のAmazon段階、既存runner + Drive adapter |
| au PAYカード | `aupay-card-recurring-production.yml`、05:23 / manual | `card-gmail-recurring`、取込データ、既存protected policy・一回限りcapability・exact read-back。manual既定dry-run | cache checkpoint/manifests/capabilities/journal/leases SQLite | [#9](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34786937936)、schedule success、09-14 07:29 JST | 親のカード段階、Amazon/receipt成功後 |
| au PAY残高 | `process-receipts.yml`、00/03/06/09/12/15/18/21:17 / manual | `aupay-gmail`、取込データ、wallet通知P1002、既定30日/100件・伝票ID dedupe | Sheets identity、独立local checkpointなし | 同共通[#205](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34889329589)、schedule success、09-15 04:50 JST | 親の残高段階。カードとは別sourceのまま |
| 一般レシート | 同共通入口 | `drive-receipts`、privacy gate→normalのみGemini→レシート/取込/支出→processed、stable IDs | Sheets marker / Drive inbox・processed。Medical shadowは別 | 同#205。今回runメタデータのみ、個別件数未確認 | 親のreceipt段階。既存AI解析を再利用 |
| PayPay通常支払い | 同共通入口 | `drive-paypay`、取込→processed、CSV stable identity dedupe | Sheets / Drive processed property | 同#205。今回個別件数未確認 | 親のPayPay段階 |
| 銀行PDF | `bank-pdf-recurring.yml`、06:47 / manual | **schedule実コードは`--dry-run`**。manual applyのみprotected expense authority、20 PDF/100 rows上限、取込/支出/Drive processed marker | cache checkpoint + per-file manifest/journal/capability/lease | [#4](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34895047512)、manual success、09-15 05:47 JST。#1/#2 startup_failure、#3/#4 success。入力apply値・write数は今回未確認 | 親の銀行段階。scheduleのpreviewを勝手にapplyへ変更しない |
| review判断反映 | 共通入口 + `amazon-manual-review-apply.yml` | `review-apply`、既存判断・取込/支出更新。専用手動入口は8件固定の古い検証条件あり | Sheets | 共通#205 / 専用manual #1 success 08-21 | 日常は親に一意化、修復手動は共通ロック内 |
| reconcile / auto-expense | 共通入口 | `reconcile` → `auto-expense`、取込/支出。現行always()で前段失敗後も起動し得る | Sheets | 共通#205 | 全依存取込成功後に一度。書込み結果不明ならskipしrun failure |
| review/表示更新 | 共通 + `amazon-manual-review-refresh.yml` | `review-refresh` / `expenses-refresh`。ホーム変更は含めない | Sheets | 共通#205 / 専用manual #3 success 08-21 | 親の後処理。失敗をrun successへ変換しない |
| 月次backup/retention | `monthly-maintenance.yml`、毎月2日03:37 JST / manual | `backup`はDriveコピー、`receipts-cleanup`は恒久削除。既存OAuth/SA。現在は画像/PDFと時刻で候補抽出 | Drive backup/processed metadata | [#1](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/33545494331)、schedule success 09-02 03:45 JST。個別write/delete数未確認 | 日常親とは別の最上位保守入口で同一ロック。branchではnormal provenanceのある原本だけ候補化 |
| Payroll | 上記Windows Task | read-only scan、Sheets/Drive読取、local非公開log/state | 既存JSON/journal/HMAC（今回移送しない） | 本日終了0 + log read-only/write0 | 実データは現状維持。将来AI鍵なし独立job、別承認 |
| Medical | 共通inbox privacy分岐、local opt-in shadow | 外部AI禁止、Sheets/Drive移動authorityなし。`medical-review list/show` read-only | user-local versioned shadow JSON、key別管理 | local shadow有効状態・最新store未確認 | 一般inbox共用維持。実データ移行とwrite拡大は対象外 |

設定と文書の食い違い: `PROJECT_STATUS.md` の銀行L4 recurring記述と
commit subject「Enable daily bank PDF recurring production」に対し、最新mainの
schedule分岐はdry-run。Amazon文書のmanual-onlyやカード文書のcache欠落時fallbackも
古い説明を含む。移行では実コードを起点にし、missing stateのfallbackは使用しない。
branchではau PAY残高の100件超過をwrite前に検出する。相対30日検索そのものは維持し、
30日超の起動欠落では自動的に検索期間を広げず、期間を確定した別の復旧承認が必要。

追加のmanual入口（全てActions/現行mainのworkflow_dispatch、個別時刻設定なし）:

| Workflow (`.yml`) | 実CLI / source / 書込み | 直近実績（API） |
|---|---|---|
| amazon-cancellation-order-id-diagnose | `amazon-cancellation-order-id-diagnose`、Gmail/Sheets read-only | #3 success 08-27 |
| amazon-cancellation-quantity-ambiguity-diagnose | 同名CLI、Gmail/Sheets read-only | #1 success 08-27 |
| amazon-cancellation-quantity-preview | 同名CLI、Gmail/Sheets read-only | #3 success 08-27 |
| amazon-cancellation-return-preview | 同名CLI、Gmail/Sheets read-only | #7 success 09-12 |
| amazon-cancellation-scope-diagnose | 同名CLI、Gmail/Sheets read-only | #1 success 08-27 |
| amazon-email-preview | `app.amazon_gmail_preview`、Gmail read-only | #7 success 09-12 |
| amazon-event-reparse-apply | `amazon-event-reparse-apply --apply`、Amazonイベント再解析更新、manual authority | #1 success 08-24 |
| amazon-event-reparse-preview | 同名CLI、Gmail/Sheets read-only | #4 success 08-24 |
| amazon-gmail-search-preview | `app.amazon_gmail_search_preview`、Gmail read-only | #1 success 08-21 |
| amazon-manual-review-preview | `review-apply-preview`、Sheets read-only | #1 success 08-21 |
| amazon-reclassify | `card-amazon-reclassify` + preview群、取込分類更新、manual authority | #1 success 08-20 |
| amazon-review-preview | 同名CLI、Gmail/Sheets read-only | #4 success 09-12 |
| amazon-review-schema-install | 同名CLI、Sheets schema更新、manual authority | #3 success 08-27 |
| amazon-shipping-backfill-preview | `amazon-shipping-backfill-drive-preview`、Drive/Sheets read-only | #3 success 08-21 |
| amazon-shipping-backfill | `amazon-shipping-backfill-drive-apply`、注文出荷日/件数更新、confirm=APPLY | #1 success 08-21 |
| amazon-status-sync-preview | 同名CLI、Sheets read-only | #2 success 08-27 |
| amazon-unmatched-export | 同名CLI、匿名診断JSONをActions artifactへ保存（retention 1日）、取引writeなし | #1 success 08-20 |
| amazon-unmatched-preview | 同名CLI、Sheets read-only | #5 success 08-21 |

上表のmanual系は全てSheets/Drive/Gmailと既存Secretを必要に応じて使用し、
独立durable stateは持たない。現行にはmain限定job guardがない入口がある。
branchでは日常/保守/手動を含む全25既存入口と新親を共通concurrencyへ統一:

```yaml
concurrency:
  group: kakeibo-production
  cancel-in-progress: false
  queue: max
```

[GitHub公式仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
で`queue: max`を確認済み。最大100待機を超える起動は取消され得るため、
キューだけで未処理全件の取得を保証しない。長期未実行や上限超過時は既存window
authorityで停止し、checkpointをnowへ飛ばさず、承認された期間分割復旧を行う。
親だけがロックを取得し、子に同じロックを重ねない。

#### 実装済みstate adapterと親接続

`app/drive_run_state.py` は既存SQLite/manifestをsource単位で保存する薄いadapter。
`app/production_run.py` は固定の段階順序・依存失敗skip・安全な件数summaryを担当する。
`app/production_flow.py` が既存CLIを子プロセスとして起動し、従来のstdout/stderrは
メモリ内で受け取って件数だけに絞る。レシート/PayPayだけは既存pipelineの小さな
`production_source.py` adapterを使う。非レシートapply/全previewの子からAI鍵を除く。
原本名/金額/旧CLIの例外全文をlog/artifactへ出さない。親はWindows直接起動を拒否する。
`kakeibo-production.yml`はmain SHA、旧入口停止、新入口許可、schedule許可を別々に検査する。
本番への統合・外部state設定なしでは現行cacheを置き換えない。

さらに`production_ledger.py`の小さな運用JSONを一つ置く。transaction原本や新規DBではなく、
11段階それぞれのpending/最終成功/件数/所要時間/固定error codeだけを持つ。
レシート・PayPay・共通後処理もwrite前にpendingを保存し、結果不明の段階は次回自動再実行しない。
独立段階は続行できるが、ledger自身の保存結果が不明なら後続writeも止める。
同じ本番ロック下でのみ使用し、分散ロックとしては使わない。
最後に再読込し、読取不明を`confirmation_pending=null`としてrun failureで報告する。
手動previewはledger/native stateともremote更新0。通常親applyの銀行previewは取引write0のまま、
親所有の運用JSONには成功したscanの時刻を記録する。新規write0のno-opと新規取込試験を分ける。

| source binding | 移送対象（元の形式を維持） | 移送しないもの |
|---|---|---|
| `amazon_gmail` | `recurring.sqlite3` | authority/Gmail token/原メール |
| `au_pay_card_gmail` | `recurring.sqlite3`, `manifests.sqlite3`, `capabilities.sqlite3`, `journal.sqlite3`, `leases.sqlite3` | audit key/authority/OAuth |
| `bank_pdf_drive` | `bank-recurring.sqlite3`, `bank-pdf-batch-<hash>/bank-steady-state.sqlite3`, `exact-steady-state-manifest.json` | PDF/authority/audit key |
| `production_run` | source別の運用JSONを固定file IDへ保存。既存native stateと別 | 原本・金額・source IDs・例外全文 |

状態は家族inboxとは別の非公開Drive管理フォルダに、固定file IDでsourceごとに保存する。
envelopeはsource/Spreadsheet/フォルダ/file/schemaをhashで束縛し、file allowlist・checksum・
SQLite table/column schema・checkpoint日時を検査する。raw stateは機密扱いであり、
base64は暗号化ではない。Git/log/artifact/cacheへ出さない。依存ライブラリのpip cacheとは別物。
SQLite backupでcommitted WALも含め、既存形式へ復元する。秘密鍵の同梱は禁止。

通常restoreは固定ファイルの欠落・破損・binding不一致・pendingで停止し、新規初期化しない。
新しいlocal一時ディレクトリに全件検証後だけ配置する。previewはlocalコピーのみ使い、
remote stateを書かない。apply前にpendingをremoteへ保存・read-backしてから既存runnerを呼ぶ。
既存runnerの成功・既存read-backが成立した場合だけsource別の成功stateを保存・再読込する。
API writeは自動retryなし。途中中断・source failure・保存結果不明は依存計上を止める。

初回移送と障害復旧を分ける:

1. 初回移送: 全旧write入口停止・run終了後、最後のcache/Windows stateの出所・source・target・
   最終成功窓を照合し、秘密鍵を除いた閉じたstateだけを`snapshot`→`envelope`で梱包する。
   `validate`で再検査し、承認済み非公開フォルダ/既存認証/固定file IDへ移送する。
   **実Driveフォルダ作成・state upload・初期state読取は今回未実施**。
2. 新規source初期化: 履歴のあるsourceのstate欠落とは別。
   承認された`initial_start`と空の既存形式stateを明示的に作る必要がある。
   空の既存形式stateの作成はoperatorが明示的に実施する。移送CLIの`--bootstrap`は
   checkpoint未設定のbundle作成を明示許可するだけで、空stateを自動作成しない。
   missing stateを理由に使用しない。
3. 障害復旧: pending envelopeのdigestを取得し、旧checkpointからの候補について
   Sheetsのstable ID・全行read-back・Drive processed markerを確認する。
   確認証拠を非公開で保持し、digestに束縛した承認後のみ
   `release_after_reconciliation(observed_digest=..., evidence_reference=<証拠SHA256>)`を使う。
   このAPIは証拠の真偽を自動判定するものではなく、日常runnerから呼ばない。
4. 旧checkpointを保持したまま既存runnerのpreview→限定replayを行う。
   Sheets書込み済みなら既存IDでduplicate/repairを確認し、無条件append/rollbackをしない。
   bankの中断manifestが再利用を拒否する場合も勝手に消さず、別途整合確認する。

既存Google認証を再利用する。現在のSAと既存backup OAuthを勝手に交換しない。
切替前に所有者・共有範囲・親フォルダ・`canEdit`・ファイル作成可否を同じ認証で確認する。
adapterは既存fileの親/種別/更新可否を検査するが、private共有範囲や作成権限の実確認は未実施。
権限不足は外部確認の未達として分け、実装・fake Driveテストを止めない。

09-15の追加read-only確認: 現在設定されているSAでDrive `about.get`に成功。
`storageQuota.limit=0`、`canCreateDrives=false`だった。新しいstate folder/file IDは未設定。
Google公式もSAは保存容量を持たずファイル所有者になれないとしている。
[Driveの所有・保存容量の制約](https://developers.google.com/workspace/drive/api/guides/about-shareddrives)
したがって初期配置は、切替承認後に既存の所有者が非公開管理フォルダと4個の固定JSONファイルを作成し、
その所有権を維持する方式とする。必要なSAへの共有は別途明示承認し、公開リンクは作らない。
日常runnerは現在のSAでその固定file IDを更新し、所有者のOAuthへ自動切替しない。
実ファイルの所有者/共有範囲/`canEdit`/更新read-backは、対象IDが確定した後の未実施確認として残す。
`canCreateDrives=false`は共有ドライブ作成の値であり、既存ファイル更新可否の証明には使わない。

offline移送CLI（すべて実Googleへの通信なし、絶対パスかつrepository外を要求）:

```text
python -m app.state_transfer export --binding-file C:/PRIVATE/binding.json --state-dir C:/PRIVATE/native-state --bundle C:/PRIVATE/state-bundle.json
python -m app.state_transfer inspect --binding-file C:/PRIVATE/binding.json --bundle C:/PRIVATE/state-bundle.json
python -m app.state_transfer restore --binding-file C:/PRIVATE/binding.json --bundle C:/PRIVATE/state-bundle.json --state-dir C:/PRIVATE/new-restore-directory
```

bindingファイルのexact fieldsは`source, spreadsheet_id, folder_id, file_id, schema=1`。
export先の既存ファイルは上書きしない。inspectはsource/phase/generation/件数/最終成功窓/digestだけ表示。
`--bootstrap`は初回境界の明示承認後に限る。pendingをrestoreで解除する機能はない。
新しい運用JSONの初回準備は、`source=production_run`のbindingを使った
`python -m app.state_transfer ledger-init --binding-file C:/PRIVATE/ledger-binding.json --bundle C:/PRIVATE/ledger.json --bootstrap`。
これはlocalだけのexclusive createであり、Drive uploadではない。
障害時は運用JSONと該当native stateの**両方**を整合確認する。
`ProductionLedger.release_after_reconciliation`も対象source・観測digest・証拠SHAへ束縛したoperator専用API。

#### 切替順序・承認境界

目標: 親入口一つ、06:17/18:17 JST（`17 9,21 * * *`）、manual既定preview、新入口既定無効。
既存CLIを再利用し、独立sourceは継続、依存元失敗・write不明時は後続計上をskipしrun failure。
本番Secretsは検証済みmainのみに渡し、branch/PR合成テストへ渡さない。

親の制御Variable（すべて未設定で無効）:

| Variable | 用途 |
|---|---|
| `KAKEIBO_LEGACY_DISABLED=true` | 旧日常4入口を停止し、新親への前提とする。月次/手動保守は共通ロック内に残る |
| `KAKEIBO_PRODUCTION_ENABLED=true` | 新親の手動preview/applyを許可。これだけではscheduleを許可しない |
| `KAKEIBO_SCHEDULE_ENABLED=true` | canary/read-back/replay確認後に限り定期実行を許可 |
| `KAKEIBO_VALIDATED_MAIN_SHA` | その時点で検証・承認されたmainの完全SHA。main更新後の無審査実行を拒否 |
| `KAKEIBO_STATE_FOLDER_ID` + `AMAZON_STATE_FILE_ID` / `AUPAY_CARD_STATE_FILE_ID` / `BANK_STATE_FILE_ID` | 承認・作成・移送済みの非公開管理先。名前検索で代替しない |
| `KAKEIBO_RUN_LEDGER_FILE_ID` | source別pending/最終成功を保持する運用JSONの固定ID |

manual既定`mode=preview`、applyには`mode=apply, confirm=APPLY`が必要。
銀行はさらに`bank_apply=true`を明示したmanualだけapply可。scheduleでは常に銀行preview。
レシートpreviewは既存IDとlocal privacy gateの確認までで、AI解析・支出write予定件数の証明とは区別する。
共通後処理5種のpreviewも読み取り専用Sheets接続を使う。
checkoutはmainのtracking refを取得し、実HEADと承認SHA/実行イベントSHAの一致を検証する。
待機中にmainが進んだ場合は、更新後のコードを無審査で動かさず停止する。

**会計条件の分離（09-15）:** canonicalカードwriterの`auto_expense`・統合先空欄行を
新たに共通計上する変更は、今回の統合対象から除外した。`app/auto_expense.py`は統合前mainと同一。
旧CLI・旧定期入口・新親のすべてで従来の計上条件を維持し、canonical ID/hash/取込日時が
揃っていても、この状態の行はpreview/applyとも計上対象にしない。新しいオプトインも追加しない。
従来の`unclassified_paypay` / `unclassified_aupay` / `unclassified_card`と銀行資産形成条件、
stable支出ID、返金・transfer・照合待ちの扱いを維持する。
未反映解消は別の会計変更として残す。新着と過去未反映を区別した対象・上限・preview/apply一致の
検証と承認が必要であり、親有効化を過去分の無制限一括計上承認にはしない。実データ修復は未実施。

統合によって旧入口にも適用される変更:

- 全25既存Workflowの共通concurrency/main限定guard、日常4入口のlegacy停止Variable条件。
  cron、sourceコマンド、銀行scheduleの`--dry-run`は統合前mainと同じ。
- au PAY残高は全取得結果を確定してからwriteし、100件上限/不完全paginationでwrite前に停止。
  receipt/PayPay inboxも100件の取得で次ページがあれば処理前に停止。取得範囲は拡大しない。
- 共通preview 5種をread-only Sheets接続へ変更し、残高CLIに明示dry-runを追加。
- 一般receiptの新規処理結果にnormal provenanceを付与し、retentionはnormalと処理日時が
  両方ある原本に限定。従来の日時だけによる削除対象は縮小する。

新親OFFでもこれらは旧運用で有効になる。今回の作業でGoogle操作を直接行わなくても、
従来の自動運用は継続するため、環境全体のGoogle write=0とはしない。

##### 最初の限定canaryと確認記録

親のmanual `scope=amazon_canary`は**Amazon通常購入1件だけ**を実行する。
他source・銀行・共通後処理・表示更新は起動しない。`scope=all`が通常運用で、scheduleは常にall。
初回canaryは次の入力・確認を揃えてから行う（ここでは実行していない）。

1. 承認するmain SHA、非公開管理先の所有/共有/作成/更新権限、native stateと運用JSONの
   binding/digest/ready状態を確認。旧日常起動を止め、進行中/待機中のwrite runがないことを確認する。
   `KAKEIBO_SCHEDULE_ENABLED`は無効のまま、承認済みmainの親previewを許可する。
2. `mode=preview, scope=all, bank_apply=false`で全stageのread-only結果を確認する。
   source別の件数と確認待ちを記録し、state/Sheets/原本に変更がないことを確認する。
3. 移送済みAmazon stateのローカル使い捨てコピーと同じauthorityを使う既存
   `amazon-gmail-recurring --dry-run`で、対象の`amazon-order:<16hex>` referenceを私的に確認する。
   対象注文・金額・日付・新規イベント1行・ヘッダ1行・取込1行・支出1行を確認し、
   その対象reference、承認SHA、state digest、4表の予定を承認記録に残す。
   明細を含む元preview出力はGit/Actions log/artifactへ載せない。
4. `mode=apply, scope=amazon_canary, confirm=APPLY, bank_apply=false,
   amazon_target=<承認reference>`で実行する。
   親は既存CLIへ`--apply-limit 1 --approved-target ... --expected-event-rows 1 --expected-header-rows 1`
   を渡す。対象が一意でない・件数が変わった場合は停止する。銀行applyとの併用は拒否する。
   native/運用JSONのpending→write→read-back→readyを通し、限定実行なので処理窓checkpointは進めない。
5. summaryの4表のwrite件数が各1、write requestsが4、確認待ちfalseであることを確認。
   既存Amazon read-backは4表のID存在確認であるため、操作者が実4表の重複数・金額・日付・関連IDを
   承認記録と照合する。referenceは注文ID由来で、全セル値を固定する承認hashではない。
   Drive側の更新digest/readyとAmazon以外のstate・原本・表が不変であることも確認する。
6. `mode=preview, scope=amazon_canary, bank_apply=false`（amazon_target空欄）で読み取り専用replay。
   対象の新規購入/event/headerが0であることを確認し、state/Sheets write 0を記録する。
   これはread-only replayであり、本番apply再実行の確認とは区別する。合成統合テストでは通常apply再実行も検証済み。
   新着が入った場合は0件判定をせず、新しい私的previewを確認する。

canary不一致/不明結果ならscheduleを有効化せず、pendingを人が照合する復旧手順へ進む。
初回canaryの成功だけで他sourceの本番確認済みとはしない。続く`scope=all` applyは、
各既存authorityの件数/期間、新着と過去未反映行、共通後処理・原本archiveの変更予定を別途承認し、
sourceごとの実read-back/replayを確認してからscheduleを有効化する。

切替時は **旧起動停止 → 実行中run終了確認 → state移送 → 新入口preview →
限定canary/read-back/replay → 新schedule有効化**。新旧write並走は禁止。
新入口停止とstate/Sheets整合性確認後にだけ復帰を検討し、無条件rollbackをしない。
Windows直接writeはActionsロックの対象外。移行後はlocalテスト/previewに限定し、
本番修復は定期運用を止めた保守手順で実施する。現在のTaskは停止していない。

09-15の新Goalで新入口OFF・旧運用継続を条件とするmain統合は承認済み。
次の実作業はDrive固定stateファイルの準備・権限確認・移送。準備前に旧運用を止めない。
今後の外部操作承認は最後にまとめる: 旧起動停止と新入口切替、
既存認証での非公開Drive folder/file作成・初期state移送、対象を限定したcanary/replay。
銀行scheduleのapply拡大、機微実データ移転、ホーム反映、公開設定/課金はこれに含めない。
実装・テスト・文書・checkpoint commitは本Goalの許可内で継続する。

#### 次段階・公開設定

- Payroll/Medical実データはActionsへ移さない。修正後branchのLinux合成CIは09-15に成功。
  一般系1183件とPayroll/Medical合成51件を別jobで実行した（合計1234件）。
  ローカルWSLは未インストール、Dockerなし。新しいOS環境は導入していない。
  「外部AIへ送らない」と「GitHub計算機内で処理する」は別の承認事項。
  将来の機微jobはAI鍵なしで分離し、原本/OCR本文をartifact/cache/logへ出さない。
- Payroll保存対象は金銭項目のみ。出勤日数・時間外労働時間・有休日数・出勤時間・有休残・
  差引不足額は保存しない。現在のWindows scanは維持し、追加帳票/税区分開発はしない。
- 一般/Medical共通inboxと既存privacy分岐を維持。branchではnormal gateを通った新規結果にだけ
  `kakeiboReceiptClass=normal`を付け、retentionはそのpropertyと処理日時の両方を要求する。
  Medical/Payroll/分類不明/legacy markerのみの原本は候補にしない。削除を伴う試験は行わない。
- mainのPayroll preview parserには勤怠候補と`差引不足額→net_pay`の旧対応が残る。
  これは移行後の保存許可ではない。既存Windows給与write branchは本Goalで変更せず、
  将来移行時に金銭項目だけの保存projectionで禁止項目を除外することを条件とする。
- 現在のGitHub visibilityは**public**（09-15 API確認）。private化は提案のみ。
  09-15にログイン済みBilling Overview / Budgetsをread-onlyで確認し、契約・残枠・支出停止設定を照合した。
  アカウントの具体的な利用額・残量はGit除外の私的確認記録に保持し、公開用文書には転載しない。
  今回visibility/課金/Secrets/OAuthは未変更。

private化の提案:

- 標準runnerのpublic無料とprivateのアカウント枠を区別する。公式のGitHub Free枠は
  2,000分/月、artifact 500 MB、cache 10 GB/repo。artifactとPackagesの保存枠は共用し、cacheは別枠。
  原本/stateはDriveへ保存し、Actionsには依存物cacheだけを置く。
  [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- 2回/日×30日=60回。平均10分なら600分/月、30分なら1,800分/月という推計になり、
  synthetic CI・保守・手動再実行・他repoの利用は別途加算する。親の実測前なので無料枠内と断定しない。
  既存の支出停止設定を維持し、枠不足で起動が止まる場合はcheckpoint回復と合わせて判断する。
- 既存public fork/取得済みコピーはprivate化で回収されない。GitHub Freeではprivate化後に
  一部機能が使えず、公開Pagesは非公開化、code scanning等も契約により利用不可となる。
  共有相手・連携アプリのprivate repo access、Pages/保護ルール利用の有無を変更前に確認する。
  [公開範囲変更の影響](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
  privateでのprotected branches/必須PR reviewer等はProの機能として案内されているため、
  現状の保護設定を維持できると仮定しない。[プラン別機能](https://docs.github.com/en/get-started/learning-about-github/githubs-plans)
- 推奨順序は「利用枠と必要機能確認→private変更の明示承認→設定変更→既存接続のread-only確認」。
  本Goalでは設定変更・権限追加・有料プラン契約を行わない。

#### 要件別の準備監査（本番切替は未実施）

| 要件 | 現在の証拠 | 判定 / 残件 |
|---|---|---|
| 最新main・入口・Windows・文書の照合 | 本節の25 Workflow表、Task+log、前後fetch `73ff2ff`、Billing画面 | 調査実施。契約利用枠/支出停止設定も確認済み。個別runのwrite件数、他ツール直接writeは未確認 |
| 親一つ・06:17/18:17・manual既定preview | `kakeibo-production.yml`, `production_flow.py`, `test_production_workflows.py` | main統合済み。新親jobはVariable未設定で無効・本番未起動 |
| 直列/失敗伝播/依存skip | `test_production_integration.py`で共通fake Google transportから既存CLI/parser/SheetsDB/後処理を通す | 5 source新規取込、4 source支出反映、canonicalカード未反映の維持、通常apply再実行の会計append0を確認。銀行は現行どおり空folder preview。state破損/receipt書込み後の応答消失も確認 |
| 共通排他・Secrets/main guard | 全25既存 + 親にtop-level共通lock。synthetic CIは別lock/Secretsなし | YAML/trigger/guard/依存テスト済み。GitHub実行キュー上の競合は未実行 |
| native state保存/復旧 | `test_drive_run_state.py`, `test_state_transfer.py`, 既存Amazon writerを使う保存失敗/replay、既存SAのDrive about読取 | 合成検証済み。SA容量0/共有ドライブ作成不可を実確認し、所有者による初期配置を手順化。対象ID未設定のため実state移送/実file所有/共有/更新確認は未実施 |
| stateless writeの中断と最終成功保持 | `production_ledger.py`, `test_production_ledger.py` | 合成検証済み。運用JSONの初回作成/実接続は未実施 |
| 上限/欠落/破損/長期未実行/部分失敗 | state/production/Amazon/card/bankの既存・追加pytest | native windowを飛ばさず停止。残高30日超の回復は別の期間承認が必要 |
| 機微境界/retention | 既存privacy gate維持、normal provenance selector、削除0のpreviewテスト | branch準備済み。Payroll/Medical実データ移行なし、保存禁止項目は次段階条件に明記 |
| Linux互換 | `synthetic-tests.yml`、[修正後CI](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34925859316)、検証SHA `002a112` | Ubuntu 24.04.5 / Python 3.12.14、一般1183件/機微合成51件成功。本Goalで実データをLinuxへ移していない |
| テスト・compile・diff | PROJECT_STATUS最新検証欄、上記CIの両job | Windows/Linuxとも合計1234件成功。両Linux jobのcompileall/diff-check成功。本番親Workflowの手動起動なし |
| 運用summary・ホーム | count-only JSON、source別last_success/確認待ち/error/duration | 実装/合成検証済み。ホーム反映は未実施・別承認 |
| 切替・canary・復帰 | 本節の順序・具体入力・承認記録、親のAmazon限定scope、既存authority/dedupe/read-backを再利用 | 合成canaryで4表各1行と他source非起動、read-only replay、対象不一致時のwrite0を確認。実対象reference/承認/切替/他source本番read-backは未実施 |

公開repoへの通常pushはcheckpoint `20452dc` 時点で自動承認レビューが拒否した。
理由は新規コード/運用文書のpublic公開先とpayloadへの明示承認不足だった。
09-15にユーザーが公開pushとSecretsなしLinux合成CIを明示承認し、`943e477`のpushとCI #1が成功した。
その後の09-15新Goalで条件付きmain統合まで明示承認された。本番起動・実データ移送・公開設定変更は含まれない。

Secretsに以下を登録:
- GEMINI_API_KEY
- SPREADSHEET_ID
- GOOGLE_SERVICE_ACCOUNT_JSON（JSON全文）
- RECEIPT_DRIVE_FOLDER_ID
- PROCESSED_DRIVE_FOLDER_ID（推奨。`receipt_processed` のフォルダURLまたはID）
- BACKUP_DRIVE_FOLDER_ID（月次バックアップ先のフォルダID）
- GOOGLE_DRIVE_BACKUP_TOKEN_JSON（Driveバックアップ用OAuth authorized-user JSON）
- GOOGLE_GMAIL_TOKEN_JSON（任意。Gmail読み取り専用OAuthのauthorized-user JSON）

`GOOGLE_GMAIL_TOKEN_JSON` が未登録なら、定期処理は従来どおりレシートだけを処理し、
Gmail取込ステップはスキップする。Gmail用トークンは個人Gmailを読めないサービス
アカウントJSONとは別にする。権限は `gmail.readonly` のみに限定する。

au PAY通知の検索条件を絞る場合は、GitHub Actions Variables の
`AUPAY_GMAIL_QUERY` に Gmail 検索式を設定する。未設定時は直近30日の
`info@wallet.auone.jp` から届いたメールコードP1002の利用通知だけを検索し、
種別が「支払」で必須項目を抽出できる通知だけを登録する。

ローカルに保存した実メール（`.eml`）で、GmailやSheetsへ書き込まずパーサーだけを
確認する場合:

```bash
python -c "from app.aupay_mail_pipeline import parse_eml; print(parse_eml('notice.eml'))"
```

Sheetsへ1件登録する場合:

```bash
python -m app.cli aupay-eml notice.eml
```

Gmailから取り込む場合:

```bash
python -m app.cli aupay-gmail --max-results 100
```

Gmail OAuthの初回認証は、OAuthクライアントJSONを保存したPC上で実行する。
どちらのJSONもリポジトリ外へ置き、内容をターミナルへ表示しない。

```bash
python -m app.cli gmail-authorize "/path/to/client_secret.json" "/path/to/gmail-token.json"
```

ブラウザで自分のGmailアカウントを選び、読み取り専用アクセスを許可すると、
指定先にGitHub Actions用のauthorized-user JSONが生成される。

件名が「【ご利用詳細】au PAY カード」のメールは、au PAY残高決済通知とは形式が
異なり、1通に複数のカード利用が含まれる。ローカルの `.eml` を取り込む場合は:

```bash
python -m app.cli card-eml-import "【ご利用詳細】au PAY カード.eml"
```

この形式には伝票番号がないため、RFC Message-IDのハッシュとメール内の明細番号を
一意キーにする。オートチャージ、Amazon、その他利用の判定はカードCSVと同じ
ルールを使用する。

カード利用詳細をGmailから読む場合は、残高通知用の `AUPAY_GMAIL_QUERY` とは別の
`AUPAY_CARD_GMAIL_QUERY` を設定する。まず書込みを行わない確認を実行する:

```bash
python -m app.cli card-gmail-preview --max-results 100
```

旧raw Gmail import経路は安全境界で無効化されており、次のコマンドは書込み前に
明示的に拒否される。raw transactionはproduction apply authorityではない。

```bash
python -m app.cli card-gmail-import --max-results 100
```

伝票番号を `aupay:<伝票番号>` という取込IDにするため、同じメールを何度検索しても
二重登録されない。必須項目が欠けるメールは自動登録せず `needs_review` として集計する。

次の保存候補を、GmailとSheetsの読み取りだけで評価するには:

```bash
python -m app.cli card-gmail-write-plan-preview --max-results 5000
```

write-planのtransaction schemaは `schema_version`, `source`, `source_record_id`,
`transaction_date`, `merchant`, `amount_yen`, `payment_method`, `identity`,
`business_fingerprint`, `memo` である。identityはRFC Message-IDのハッシュと明細番号
からなるカードメール固有キーで、同じメールの再処理と期間重複取得を吸収する。
日付・店舗・金額・支払方法・会員・明細番号から作るbusiness fingerprintは監査と
collision検知に使い、同日同額の別取引を自動重複扱いしない。不明な必須項目やidentity
collisionはinsertせずreview/rejectedとして停止する。

別Message-IDで同じ明細が再送された場合もsource identityは変更しない。日付・金額・
merchant・会員区分・メール内明細番号を含むbusiness fingerprintが完全一致し、
Message-ID由来部分だけが異なる明細を `probable_resend` として束ね、全source identityを
evidenceとして保持しながらcanonical 1件だけをwrite-plan候補へ投影する。明細番号が
異なる同日同額同merchant取引は別取引のまま保持する。

既存カードCSVとは日付・金額・正規化merchantで比較し、一意なら
`cross_source_strong_match`、候補が複数またはmerchant不一致なら
`cross_source_ambiguous`、候補なしなら `cross_source_no_match` とする。これはpreview
evidenceであり、この段階では自動duplicate authorityではない。Gmail読み取りは間隔を
空け、429、一時的rate-limit 403、一時的5xxだけを指数backoff付きで最大6回試行する。

reconciliation済みcanonical projectionのproduction apply候補を安全に評価するには:

```bash
python -m app.cli card-gmail-apply-plan-preview --max-results 5000
```

このapply-plan previewもGmail/Sheetsの読み取りだけを行い、writerは呼ばない。
collection途中終了、Gmail list/read failure、identity collision、reconciliation不整合は
global blockerとしてplan全体のcandidateを空にする。return、CSV ambiguous、exact既存Gmail
identity、item-level reviewはcanonical item単位でwithholdし、安全gateを通過したpurchaseと
同じplan内で監査できる。strong matchはevidenceのままでduplicate authorityにはせず、
no-matchと同様にcandidateになれる。candidate、withheld、duplicate、reviewの排他的statusと
canonical総数のaccounting invariantもsummaryへ出力する。将来のexecutor境界はsafe plan型と
明示的なapply指定を必須とする。

previewはplan作成後にSheets identityをもう一度読み、executor直前の状態を
`still_new`、`already_present_exact`、`conflict`、`revalidation_failure`として再検証する。
このPhaseのexecutorにはwriterが存在せず、`apply=True`でも
`apply_blocked_writer_unavailable`となる。timeout等で前回結果が不明な
`outcome_unknown`はblind retryせず、再読で存在・不在・判定不能を確定する。
詳細な状態遷移、鍵付きaudit reference、deterministic canary contractは
[`docs/aupay_card_executor_contract.md`](docs/aupay_card_executor_contract.md)を参照。

production writerのwrite-adjacent safety layerは固定source window、target binding、
persistent HMAC key、attempt journal、exclusive lease、最大100件のbounded batch、
write前journalとwrite後exact read-backをcontract化している。repo外protected key adapter、
SQLite durable journal / immutable manifest / cross-process lease、fixed-range Sheets adapter、
repo外承認fileと短命one-shot capabilityも実装済みである。人手承認を要するhistorical
one-shot canaryは引き続きCLIへ公開しない。一方、通常の新着だけを対象とするbounded recurring
authorityと専用CLIは `card-gmail-recurring` として分離実装している。前回成功endからJSTの
absolute epoch windowを作り、overlapを既存identityで除外し、new=0ではmanifest/capabilityを
作成しない。scheduled production、必要secret、failure recoveryの詳細は
[`docs/aupay_card_recurring_production.md`](docs/aupay_card_recurring_production.md)を参照。
永続化とsealの詳細は
[`docs/aupay_card_production_persistence.md`](docs/aupay_card_production_persistence.md)、基本contractは
[`docs/aupay_card_writer_safety_contract.md`](docs/aupay_card_writer_safety_contract.md)を参照。

Amazon注文確認メールの本番候補は、書込み前に次の読み取り専用コマンドで確認する。

```bash
python -m app.cli amazon-production-preview --lookback-days 30 --max-results 100
```

取消・返品・返金・曖昧金額・parser failureは自動支出へ進めず、注文IDと固定支出IDで
期間重複を排除する。canary、bounded authority、JST checkpoint、CSV商品明細への昇格時の
二重計上防止は
[`docs/amazon_recurring_production.md`](docs/amazon_recurring_production.md)を参照。

カードメールの明細parseはmail-level resultの中でaccepted itemとprivacy-safeなreview
itemを分離する。正額は `purchase` として正数を保持し、対象明細block自身が
`-<金額>円(返品)` の形式と返品evidenceを持つ場合だけ `return` として負号を保持する。
メール内に返品という語があるだけでは、同居する正額明細をreturnへ変更しない。
未知の負額形式や必須field欠落はitem番号・reason code・field reasonだけをreview evidence
として保持し、merchantや本文は保存しない。正常な兄弟明細はreconciliation inputへ残る。
partial parse reviewは別manifestへ隔離し、return canonicalは全件 `withheld_return` として
candidateから除外するため、いずれもproduction writerへは到達しない。

## 取込データの統合・重複排除

書き込まずに候補件数だけ確認する:

```bash
python -m app.cli reconcile-preview
```

判定結果を「取込データ」へ反映する:

```bash
python -m app.cli reconcile
```

照合対象は標準で直近6か月、全未処理行、最近取り込んだ過去日付のデータ、
およびそれらに一致し得る過去候補とする。期間は
`RECONCILIATION_LOOKBACK_MONTHS` で変更できる。

解析済みレシートを正本とし、au PAYまたは通常カード利用が金額・日付・店舗で
一意に一致した場合だけ決済側を `matched_receipt` にする。候補が複数ある場合や、
同じレシートを複数決済が参照する場合は `needs_review_duplicate` とし、自動統合しない。
au PAY残高オートチャージとAmazon照合済みカードは対象外とする。

店舗名に表記揺れがある場合は、「店舗」シートへ1行ずつ登録する。
「店舗名」に取込データ上の表記、「標準店舗名」に統一後の名称を入力すると、
照合時に両者を同じ店舗として扱う。店舗IDと備考は管理用の任意項目。

## 銀行PDFをread-onlyで確認する（auじぶん銀行 / ドコモSMTBネット銀行 / 千葉銀行）

native text layerを持つ普通預金取引明細PDFを、Sheetsへ書き込まず確認する。
発行元marker・header・geometryから対応adapterを自動選択し、未知形式はfail-closedする。

```bash
python -m app.cli bank-pdf-preview statement.pdf --account-alias jibun-primary
```

各ページの見出しと縦罫線から日付・内容・出金・入金・残高のcolumn boundaryを
復元する。出金は負、入金は正のsigned amountとしてcanonical transactionへ投影し、
残高は取引金額や通常出力にせず、identityと整合性検証だけに使う。同じPDFの再処理はstable source row identityでduplicateに
なる。出力は件数とreason taxonomyだけで、取引本文や個別金額を表示しない。
OCR、Sheets書き込み、production applyはこのコマンドから実行されない。

既存の `取込データ` をread-onlyで参照し、銀行rowの分類と照合状況を集計する:

```bash
python -m app.cli bank-pdf-shadow-preview statement.pdf --account-alias jibun-primary
```

分類と照合は分離される。au PAYカード引落は `card_settlement` のまま、明示的な
statement-total authorityがなければ `identified_unlinked` とし、通常支出へ落とさない。
PayPayは摘要に明示される場合だけ照合対象にする。日付・同額だけの既存購入row、
個別カード購入の合算、摘要の部分一致は照合根拠にしない。給与・賞与・利息・銀行の
特典／金利優遇、明確な
口座振替、確認済みの資金移動以外は安全側に `needs_review` とする。出力は分類・照合・
review reasonの件数のみで、取引内容、個別金額、残高、照合identityは表示しない。
`loan_repayment` は `expense` / `住まい／住宅ローン` へ投影できるwrite候補とし、
`cash_withdrawal` はwriteしない。`income`、`expense`、`loan_repayment`だけを
production preview候補とし、classificationと
write eligibilityを別フィールドで集計する。

canonical identity resolverを使ったproduction-equivalentのwrite-free planだけを確認する:

```bash
python -m app.cli bank-pdf-production-preview statement.pdf --account-alias jibun-primary
```

既存取込データはread-onlyで再取得し、既存identity・identity collision・分類withholdを
集計する。`write_attempted` は常に0で、Sheets writerへは到達しない。

production canaryの準備では、まずwrite候補のstable source identityだけを列挙し、
operatorがexact identityを1件指定してread-only preflightを行う:

```bash
python -m app.cli bank-pdf-canary-candidates statement.pdf --account-alias jibun-primary
python -m app.cli bank-pdf-canary-dry-run statement.pdf --account-alias jibun-primary --source-identity '<stable-source-identity>'
```

選択は日付・金額・row番号では行わず、`--source-identity` の完全一致だけをauthorityとする。
`income` / `expense` 以外、既存identity、collision、0件または複数件一致、target spreadsheet・
`取込データ` headerの不一致はすべてfail closedになる。dry-runは既存のtarget bindingと
canonical identity readerで事前読取し、共通12列schemaへ1行だけ投影するが、writerや
production capabilityは呼び出さず、`external_write_count` は常に0である。

operatorが所有関係を確認した自口座transferのexact descriptionとdirectionは、Git管理外の
`.env` にJSON配列で設定できる。名称の部分一致や同姓名・同額・反復回数はauthorityにしない:

```dotenv
BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON=[]
```

### 日常運用（銀行PDF）

対応銀行（auじぶん銀行、ドコモSMTBネット銀行、千葉銀行）はPDFから自動判別します。未知形式は安全に停止します。

通常は次の2操作だけを使う。previewはSheets/Gmailをread-onlyで参照し、
新しい `income` / `expense` / `loan_repayment` だけを外部manifestへ固定する。
カード引落、確認済み自口座transfer、ATM、reimbursement、non-own review、
true unknown、duplicate、collisionはwrite対象外である。

```bash
# preview（write_attempted=0）
python -m app.cli bank-pdf statement.pdf

# 明示承認後のみapply（同一PDF・同一manifestに限定）
python -m app.cli bank-pdf statement.pdf --apply
```

`--apply` を使う場合は、Git管理外のruntime領域と既存transportの承認材料を
次の環境変数で指定する。未設定、target/header不一致、candidate集合の変化、
上限超過はすべてfail-closedする。

```dotenv
BANK_STATE_DIR=/path/outside/repository/bank-runtime
BANK_AUDIT_KEY_FILE=/path/outside/repository/bank-runtime/audit-key.json
BANK_APPROVAL_FILE=/path/outside/repository/bank-runtime/approval.json
```

同じPDFを再previewした結果が全件duplicateなら、manifest・capability・lease・
appendを作らないsafe no-opになる。日常操作では過去backfill用のcanary/batchコマンドを使わない。

## Google DriveからPayPay CSVを取り込む

`PAYPAY_DRIVE_FOLDER_ID` に専用受信フォルダのIDまたはURLを設定する。
書き込みなしの確認と取込は次のコマンドで行う:

```bash
python -m app.cli drive-paypay-preview
python -m app.cli drive-paypay
```

PayPay CSVとして解析できる `.csv` だけをファイル単位で取り込む。成功後は
`PROCESSED_DRIVE_FOLDER_ID` があればそこへ移動し、未設定ならDriveのファイルプロパティに
処理済みを記録する。ファイルは削除しない。壊れたCSVはエラーとして残し、他ファイルの
取込は継続する。

## スマホ用「要確認」シート

未分類のPayPay・au PAY・au PAYカードの明確な支払いは、先に書き込みなしで
件数を確認できる:

```bash
python -m app.cli auto-expense-preview
```

確認後に `python -m app.cli auto-expense` を実行すると、明確な支払いを
`その他 / 未分類` または限定的な高信頼ルールのカテゴリで支出化する。
Amazon分割払い、返金・取消、資金移動候補、既存レシート候補は自動計上しない。
後から一意に一致するレシートが取り込まれた場合は `reconcile` がレシートを
正本とし、自動支出行を `duplicate_excluded` にする。

## 取込済み銀行行の最終反映

銀行PDFからすでに `取込データ` へ登録された3銀行共通の行は、まずread-onlyで
最終分類を確認する:

```bash
python -m app.cli bank-finalization-preview
```

previewは `new_expense` / `new_income` / `excluded_link` / `non_expense` /
`review` / `duplicate` と理由別件数を返す。`bank_income` は支出化しない。
カード請求の口座振替は非支出、所有関係が不明な金融機関相手の振替はreviewとし、
摘要だけで広く支出化しない。既存支出と日付・金額・店舗が一意に一致する場合だけ
link候補にする。

canary候補1件の投影もwriteなしで確認できる:

```bash
python -m app.cli bank-finalization-preview \
  --source-identity '<stable-bank-import-id>'
```

production反映は自動workflowへ接続していない。明示承認後に限り、選択したstable
identity 1件だけを `bank-finalization-apply --apply` へ渡す。previewが返す
`target_spreadsheet_id` と `expected_git_head` もapply時に完全一致させる。支出は既存のstable
`M-...` IDで `支出明細` へupsertし、`取込データ` のstatus/targetをread-backする。
途中失敗後のreplayでは同じ支出を再追加せず、未反映の取込statusだけを修復する。

新着銀行PDFのrecurring launcherは、既存の3銀行parserとsteady-state batch経路を再利用する。
`BANK_PDF_DRIVE_FOLDER_ID` のDriveフォルダをoverlap付きでbounded列挙し、source-row identityで
dedupeする。read-only確認は次で実行する:

```bash
python -m app.cli bank-pdf-recurring --state-dir "$BANK_PDF_STATE_DIR" \
  --authority-file "$BANK_PDF_RECURRING_AUTHORITY_FILE" --dry-run
```

1回あたりの上限はauthorityの `max_files`（最大20）と `max_rows`（最大100）。新着0件はsafe
no-opで、review / transfer / card settlement / incomeは支出writeしない。workflowはmanual
dispatchを維持しつつ、毎日06:47 JST（21:47 UTC）のproduction scheduleで実行する。

Amazon baseline注文とカードのAmazon分割払いは、次の専用previewで照合できる:

```bash
python -m app.cli amazon-installment-preview
```

同一会員・同一分割表記の未解決行全体と、単一商品のbaseline注文が期間・合計金額で
双方一意に一致する場合だけ、`amazon-installment-apply` でAmazon商品を正本として
支出化する。分割払い側は `matched_amazon_installment` とし、別支出を作らない。

Google Sheetsアプリでは「要確認」シートだけを開けば、対応が必要な取引を確認できる。
このシートは自動生成専用で、3時間ごとのGitHub Actions実行時に最新状態へ更新される。
日付列はスマホ表示を含め `yyyy/mm/dd` 形式に統一する。

- 高: レシート解析エラー、曖昧な重複候補
- 中: レシート未照合のau PAY、未分類カード利用
- 表示しない: オートチャージ、Amazon照合済み、正常統合済み

書き込まずに件数を確認する:

```bash
python -m app.cli review-preview
```

手動でシートを更新する:

```bash
python -m app.cli review-refresh
```

「要確認」シートの自動生成列（A〜I）は編集しない。スマホから対応する場合は
J〜N列だけを入力する。

- ユーザー判断: `支出として計上` / `重複として除外` / `レシートと統合` / `保留`
- 統合先取込ID: `レシートと統合` の場合に入力
- カテゴリ: `支出として計上` の場合、L列から `大カテゴリ｜小カテゴリ` を選択
- ユーザー備考: 任意
- 反映結果: システムが結果またはエラーを記録

判断とカテゴリはシート上のドロップダウンから選択する。モバイルでも安定して選べるよう、
カテゴリは `食費｜食料品` のように大・小を一体化して表示する。従来どおりL列とM列へ
大・小カテゴリを個別入力した既存行にも対応する。カテゴリマスタに存在しない組合せは
反映しない。入力した判断は次の3時間ごとの
GitHub Actionsで反映される。すぐ反映したい場合はActionsを手動実行する。

手動で判断だけを反映する:

```bash
python -m app.cli review-apply
```

## スマホ用「支出一覧」シート

「支出明細」は監査用として除外行も保持する。「支出一覧」は `計上状態=active` の
支出だけを新しい日付順に表示する最終画面で、`duplicate_excluded` は表示しない。
Google Sheetsアプリでは通常このシートを見れば、二重計上を除いた最終支出を確認できる。
日付表示は `yyyy/mm/dd` に統一し、3時間ごとのActionsで自動更新する。

書き込まずに最終件数・合計を確認する:

```bash
python -m app.cli expenses-preview
```

手動で「支出一覧」を更新する:

```bash
python -m app.cli expenses-refresh
```

## au PAY CSV取込

au PAYサイトから出力した `auPAY_YYYYMM.csv` は、月次確定データとして取り込める。
日常は3時間ごとのGmail取込で速報反映し、月1回CSVを取り込んで不足分を補完する。
既存のメール取込と同日・同店舗・同額の支払いは件数単位で照合し、二重登録せず
既存行の備考へ `CSV確認済=YYYYMM` を記録する。
オートチャージは監査用に保持するが、支出には計上しない。

```bash
python -m app.cli aupay-csv-preview auPAY_202608.csv
python -m app.cli aupay-csv-import auPAY_202608.csv
```

カード明細の `auPAY_Card_YYYYMM.csv` は従来どおり次で取り込む。

```bash
python -m app.cli card-preview auPAY_Card_202608.csv
python -m app.cli card-import auPAY_Card_202608.csv
```

Amazonのカード請求は、商品単価ではなく注文ID単位の合計金額で照合する。
既存のカード行を現在のAmazon注文データで再判定する場合は次を実行する。

```bash
python -m app.cli card-amazon-reclassify
```

一意に一致した請求は `matched_amazon`、候補が複数なら
`amazon_needs_review`、候補がなければ `amazon_unmatched` とする。

## 月次バックアップとレシート保存期限

毎月、Spreadsheet全体を `kakeibo-backup-YYYY-MM` という名前で
`BACKUP_DRIVE_FOLDER_ID` へコピーする。同じ月のバックアップが既にあれば再作成しない。
サービスアカウントには保存容量がないため、バックアップだけは本人のDrive OAuthを使う。

```bash
python -m app.cli drive-backup-authorize client_secret.json drive-backup-token.json
python -m app.cli backup
```

GitHub Actionsでは `drive-backup-token.json` の内容を
`GOOGLE_DRIVE_BACKUP_TOKEN_JSON` Secretへ登録する。

処理済みフォルダへ移動した画像・PDFには処理日時を記録する。1年以上経過した
対象を確認する場合はプレビューを使う。

```bash
python -m app.cli receipts-cleanup-preview
```

次のコマンドは対象ファイルをゴミ箱へ移さず完全削除する。

```bash
python -m app.cli receipts-cleanup
```

`.github/workflows/monthly-maintenance.yml` は毎月バックアップを作成した後、
1年以上経過した処理済み画像・PDFを完全削除する。旧ファイルに処理日時がない場合は
Driveの最終更新日時を基準にする。

## 次の実装
1. 店舗名マスタと照合ルールを拡充
2. 実運用データを使って要確認判定を調整

## 安全設計
- APIキー、サービスアカウントJSONはGitにコミットしない。
- AIがカテゴリを新設できないよう、Sheetsのカテゴリマスタとの一致をPythonで検証。
- 合計不一致は自動計上しない。
- 生データ側のID/ハッシュを保持して再取込による二重計上を防ぐ。


## 5a. 書き込まずに解析テスト
```bash
python -m app.cli analyze receipt_test.jpg
```
Sheetsへ書き込まず、Gemini解析結果だけを表示する。


## Amazon初回基準点（推奨）
AmazonのデータリクエストCSVは毎回全期間になるため、初回だけ次を実行します。

```bash
python -m app.cli amazon-baseline "Order History.csv"
```

このコマンドは過去の有効行を `Amazon注文` に基準点として記録しますが、Gemini分類は行いません。
次回以降は新規・変更行だけを検出し、未知ASINだけGeminiで分類します。

```bash
python -m app.cli amazon "Order History.csv"
```

baselineの行は過去データの基準点として保持し、支出明細へ一括計上しない。
baseline後に検出した新規・変更商品だけを、Amazonキーから作る固定支出IDで
「支出明細」へ追加・更新する。同時に注文単位の合計を「取込データ」へ
`canonical_amazon` として記録し、レシートとの重複判定に使用する。

Amazon注文合計とAmazonレシートが金額・日付・店舗で一意に一致した場合は、
商品単位のAmazon明細を正本とし、レシート由来支出の `計上状態` を
`duplicate_excluded` にする。元のレシート・支出行は監査用に削除しない。


## v3.4: Sheets書き込みクォータ対策
Amazon取込は1行ずつSheets APIへ書き込まず、新規行をまとめて1回のappend、
変更行をまとめて1回のbatchUpdateで反映します。
途中で429になった旧版から再実行しても、すでに登録済みのAmazonキーは検出されるため二重登録しません。
