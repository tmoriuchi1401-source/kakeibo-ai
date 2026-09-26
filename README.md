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

### 統合Actions本番運用（2026-09-15切替）

この節をWindows開発 / Actions本番 / Drive状態保存の切替手順の集約先とする。
正式state移送・全source preview・限定実write・通常apply・再実行確認を完了し、
新親の定期運用を有効化した。旧日常4入口は停止し、Drive固定4ファイルを正本として継続する。
銀行はpreview/収入write OFF、Payroll/Medicalは現状維持。cronの実到来は未観測で、L4へ一括昇格しない。

#### 切替実績と現在の制御

実行コードは `8b52c6a758fc69e77c59467aec43938829624697`、
[Linux CI 34968751087](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34968751087)
で合成pytest 1378件・Node 6件、compileall/diff-check成功。後続文書commitも最終main SHAで再検証し、
`KAKEIBO_VALIDATED_MAIN_SHA`を一致させる。実ID・金額・原本・state本文は非公開証跡へ保存する。

| 工程 | 成功run | 確認結果 |
|---|---|---|
| 正式移送 | [34967852055](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34967852055) | 停止後の完全cacheキー、4bundle復元、固定ID更新/GET一致、所有・親・共有維持 |
| 全source preview | [34968092682](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34968092682) | 全11段階成功、state/台帳/原本不変 |
| Amazon canary | [34969011775](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34969011775) | 新規通常購入1件、実4表各1行の値・日付・金額・関連ID/重複0を照合 |
| canary read-only replay | [34969243835](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34969243835) | 同じ購入は既存1件、新規購入候補0、write0 |
| 通常all apply | [34969415764](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34969415764) | 全11段階成功。同じ購入の再計上0、別の未処理イベント1行保存、表示更新 |
| 通常all再実行 | [34969971209](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34969971209) | 全11段階成功、会計/event追加0・既存行変更0・原本移動0 |

- Amazon以外の新規計上は0。au PAY残高は22件不変、カードは通常applyで既存10件・withheld1、
  再実行では処理窓が進み既存2件・withheld0。PayPay入力0、銀行preview対象0/収入更新0。
- レシートはpreview後に投入フォルダへ新着1件が追加された。通常apply/再実行とも計2件保留、
  取込・支出write0、processed移動0。新着追加を重複や今回の原本操作として数えない。
- 過去のcanonicalカード未反映436行は計上対象外のまま。共通自動計上は作成/更新0。
  支出一覧はcanaryの1件を反映し、再実行後の実値は不変。
- 固定stateはAmazon generation6、カード4、銀行0、共通ledger46でready。
  全11段階に最終成功がありpending0。銀行はpreviewのため元checkpointを進めない。
  既存の処理窓上限を超えた場合は停止し、窓拡大やcheckpoint繰上げで回避しない。
- 実Variablesでproduction/legacy_disabled/scheduleの3フラグtrue、新定期 `17 9,21 * * *`
  （06:17/18:17 JST）を確認。旧日常4入口と旧writerを直接使うmanual5入口はdisabledを維持する。
  月次backup/retention・レビュー表示更新・レビューschema保守の3入口は共通排他/main guardを確認して元のactiveへ戻した。
- GitHub設定変更は固定ID暗号文5値・検証SHA・切替3フラグと上記Workflow状態だけ。
  Secrets/OAuth/鍵/共有・Task・公開設定・課金は変更していない。原本削除・過去一括修復は行っていない。
- 復旧時は新親を止めてDrive正本とSheetsを照合する。実write後に古いcacheの旧運用へ自動復帰しない。
  元状態・各run・非公開binding・読戻し・比較証跡は `%LOCALAPPDATA%/KakeiboAI/production-state-setup` に保持する。

以下のOFF統合・保存先準備は切替前の履歴。現在の制御はこの節の冒頭と実Variablesを確認する。

#### 履歴: 新入口無効でmain統合

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

#### Drive保存先の準備・実接続確認（2026-09-15）

本人の既存Drive接続と既存Gmail認証の本人を照合し、本番SAは既存設定とDrive認証応答を照合した。
本人My Drive直下（家族共有フォルダ外）へ`KakeiboAI_system_state`を1個作成。
本人所有のまま、指定した既存本番SA 1つだけにフォルダのwriterを付与した。
以下の4ファイルはその権限を継承する。anyone/domain/group/家族への共有はない。
親のMy DriveはSAから見えないため、フォルダのroot直下配置は本人認証のmetadataで確認した。

| 固定ファイルの用途 / 名前 | 本番SAの読取 | 同じfile IDへの更新・読戻し | 更新後の所有者・親・共有 |
|---|---|---|---|
| Amazon / `amazon_gmail.json` | 内容一致 | 成功・送信bytes一致 | 本人所有・専用folder・本人+指定SAのみ |
| au PAYカード / `au_pay_card_gmail.json` | 内容一致 | 成功・送信bytes一致 | 同上 |
| 銀行 / `bank_pdf_drive.json` | 内容一致 | 成功・送信bytes一致 | 同上 |
| 共通運用記録 / `production_run.json` | 内容一致 | 成功・送信bytes一致 | 同上 |

内容は個人情報を含まない接続試験JSONで、`status=UNINITIALIZED_NOT_PRODUCTION_STATE`。
試験値だけを`owner_created`から`sa_update_verified`へ1回ずつ変更した。
既存`DriveStateTransport`と本番SAを使用し、実ID/親/JSON形式/本人所有/canEdit/canDownloadと
権限一覧を前後照合。本人認証でも更新後の4ファイルの共有範囲を再確認した。
現行native/ledger validatorは準備用JSONを拒否する。validatorを緩めず、ready/checkpointを作っていない。
試験済みファイルを削除・再作成せず、正式移送時に内容を置き換える固定先として残した。

返却IDは作成ごとにrepository外の`%LOCALAPPDATA%\KakeiboAI\production-state-setup\setup.json`へ保存。
同じ場所の`*.binding.json` 4個が既存adapter用binding、`*.probe.json`と
`owner-final-permissions.json`が読戻し/権限証跡、`completion.json`が到達点の記録。
この領域はWindows本人とSYSTEMだけのACL。実ID・認証情報・native stateを公開Gitへ含めない。
再実行は保存済み固定IDの確認から始め、名前による上書きや無条件の追加作成をしない。

今回の直接Google変更は新設folder 1個、準備用JSON 4個、folderのSA writer付与1件、
各JSON内容更新1回だけ。新しいOAuth同意/scope/鍵、Sheets write、既存原本move/deleteはなし。
本番state移送・Actionsからの接続試験・新親preview/canaryは未実施。旧運用は継続し、新親はOFF。
この確認はWindowsからの保存先接続に限り、本番復元成功・切替完了・L4確認済みとはしない。

##### 移送元stateのread-only棚卸し

| source | 現在のActions上の場所 | cache key | 確認した成功run |
|---|---|---|---|
| Amazon | `$RUNNER_TEMP/amazon-production-state` | `amazon-production-state-<run_id>-<attempt>` | [34907742177](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34907742177)、schedule |
| au PAYカード | `$RUNNER_TEMP/aupay-card-production-state` | `aupay-card-production-state-<run_id>-<attempt>` | [34907806301](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34907806301)、schedule |
| 銀行 | `$RUNNER_TEMP/bank-pdf-recurring-state` | `bank-pdf-recurring-state-<run_id>-<attempt>` | [34911236780](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34911236780)、schedule preview |

3 runのrestore/save step成功と、同じrun/attemptを持つmain cacheの存在をAPIで確認した。
cache metadataは非公開`source-state-inventory.json`に保存。本文・SQLite checkpointは取得していない。
稼働中の観測であり、これを最終移送版にはしない。銀行preview成功は取引apply成功の証明ではない。
既存Workflowは`actions/cache/restore@v4`で取得し、`actions/cache/save@v4`でrunごとに保存している。
最終移送時は停止後のrun/attemptに対応する完全キーを選び、cache miss時は停止する。
通常運用のprefix fallbackや空state bootstrapで不足を埋めない。
cacheには認証/authority等が混在し得るため、下記allowlistのnativeファイルだけを梱包する。
後続の移送準備では、取得・検査だけを行う`state-cache-inspect.yml`を追加しmainへ統合した。
manual/main限定、既定かつ唯一のoperationは`inspect`。Linux検証済みの完全SHAと
3sourceの完全cacheキーを指定する。最新run/attemptが成功でない、cache miss、prefix一致だけ、
成功checkpoint欠落、未確定記録/lease、復元不一致では停止する。元cacheを更新せず、WALを含む
allowlistコピーを一時領域で検査。Google Secrets/実IDは渡さず、cache save/artifact uploadも行わない。
検証SHA `21112169ae62c99ac04e66c0b77fde4542fba60b`、
[Linux CI 34960953834](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34960953834)は
一般1304件＋Payroll/Medical合成55件成功。同じ完全SHAをmainへ通常fast-forward push済み。
inspect起動は初回レビュー拒否後、本人の1回限りの明示承認で実施。
[run 34962727963](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34962727963)は
上記main SHA・attempt 1で成功し、run総数1・artifact 0をAPIで確認した。
Amazon `amazon-production-state-34907742177-1`、カード `aupay-card-production-state-34907806301-1`、
銀行 `bank-pdf-recurring-state-34911236780-1` をexact hitで取得。許可nativeファイル数は1/5/1で、
全sourceのcheckpoint検査・未確定記録/lease検査・一時領域への復元一致が成功した。
checkpointは2026-09-15 JSTでAmazon 08:12:38、カード08:13:26、銀行05:48:11。
最新native statusは`noop` / `complete` / `dry_run_noop`。銀行scheduleはpreview成功として扱う。
Google認証を渡さず、cache保存/Google変更なし。旧運用は停止していない。
この候補検査は最終移送版の確定やDriveの本番binding/接続確認ではない。正式移送・Actions親previewは未実施。
次は固定4bundleの移送経路と、実IDをActions logへ出さない受渡しを準備・検証する。

##### 本番切替の実行手順（最新の承認範囲）

1. 移送コード・回帰テスト・SecretsなしLinux CI・検証済みmain統合を旧運用停止前に完成する。
   成功済みinspectと権限試験は反復しない。取得・checkpoint・構造の未解決があれば停止工程へ進まない。
2. 月次/全manual write（銀行収入backfillを含む）/ローカル/他Workを現物確認し、元の設定を非公開保存。
   Payrollのread-only scanは停止対象にしない。main更新と直接writeも保守中は入れない。
3. 新scheduleをfalseにしてから旧日常入口を止め、必要なmanual/月次write入口を一時disableする。
   開始済みrunを強制cancelせず終了を待つ。未開始と確認できる待機runだけ取消し、待機を残さない。
4. 停止後の最新run/attemptと完全cacheキーを再確定。後続失敗を無視して古い成功cacheを選ばない。
   native3sourceと新規ledgerの4bundleをすべて検査・一時復元してから、固定4ファイルへ正式移送する。
   履歴sourceのbootstrap・checkpoint繰上げ・pending解除は禁止。共通ledgerだけ既存の明示初期化を使う。
5. 全4ファイルを同一IDの新規GETで読戻し、bytes/digestと所有/親/共有維持を確認する。
   応答不明ならまず同じIDを読み直し、盲目的に再更新しない。部分移送では親を有効にしない。
6. 旧入口停止・新schedule=falseのまま、親`preview / all / bank_apply=false / confirm空 / amazon_target空`
   を実行。新着・過去未反映・保留・予定を非公開記録へ分離する。上限や計上条件は変えない。
7. 現previewに既存authority内の新規Amazon通常購入があれば1件を非公開固定し、限定canaryと実4表の読戻しを行う。
   適合対象がなければ架空作成・過去再計上・新着待ちはせず、新規write未確認を明記して他の条件を検証する。
8. `apply / all / confirm=APPLY / bank_apply=false`と再実行を行い、source別の実反映・原本移動・
   state/ledger ready・同一IDの二重appendなしを読戻す。新着追加と重複、state更新と会計更新は別集計にする。
9. 成立後はproduction/legacy_disabled/scheduleの3フラグをtrueにして新定期を開始する。
   旧日常入口は停止を維持。共通排他/main guardがあり旧writer/cacheを迂回利用しない月次・手動保守だけ元へ戻す。
   正式移送済みDrive stateが新運用の正本となる。銀行はpreview/収入write OFF、Payroll/Medicalは現状維持。

本人の最新Goalは停止から本番切替までを連続実行する承認であり、以前の「preview後に旧系へ戻す」を置き換えた。
実write後は古いcacheの旧系へ自動復帰しない。障害時は新親を止めて読戻し・既存復旧手順で照合する。
取引write前で旧state不変・新親停止・結果不明なしが確認できる場合に限り旧運用へ復帰できる。
将来cronを待たず同経路の手動実績と定期ON確認を完了条件とし、schedule未観測は明記する。

##### 一般レシート再取込の実行状況

一般レシート再取込（2026-09-16開始）は、通常の取込dedupeを迂回してwriteする機能ではない。
`app/receipt_reimport.py`は今回の再解析結果と既存のreceipt/import/expense関連を比較する読取専用処理。
一致はno-op、不足候補・差分・既存の確定判断/照合リンクは確認対象とし、AI差分だけで既存値を上書きしない。
対象24ファイル/31ページを固定し、現行Windows gateでnormal10/Medical1/unknown13を確認した。
実原本と比較元、normal10件の送信allowlistは
`%LOCALAPPDATA%/KakeiboAI/production-state-setup/receipt-medical-20260916` に非公開保存する。
再開時はこの固定ID/版/hashを照合し、既存スナップショットを無条件に作り直さない。
更新されたGoalにより、このnormal10件の店舗・日付・商品・金額を含む原本を、
既存Google公式Gemini APIへ本番Actionsから送信することが明示承認された。
本番Secret `GEMINI_API_KEY` の存在と、新親→receipt子プロセス→Settings→GeminiAIの受渡しを確認。
Windowsの鍵設定は不要であり、ブロッカーとして扱わない。Secretの存在だけでは実認証成功とはしない。

新親のmanual `scope=receipt_reimport` は、固定manifestの10件だけを扱う。
`receipt_store`は既存SA鍵で包んだ独立結果ファイルID、`receipt_manifest`は承認manifestのSHA256。
`mode=preview`はAI送信/保存なし。`mode=apply, confirm=APPLY, receipt_operation=reanalyze`で
最初は`receipt_limit=1`、成功結果を読戻した後に残りを最大3件ずつ進める。
通常と同じReceiptPipeline/GeminiAI/モデル/指示/結果検証を使い、Linux gateがnormal以外なら保留。
通常の取込済みスキップと新着scheduleは変えない。銀行/他sourceの実行はこのscopeに含まない。

独立した非公開Drive JSONへ送信意図・対象行の修復前snapshot・正常解析結果・比較結果を保存する。
原本/OCR/結果全文をActions artifact/cache/logに出さず、4本番stateも置場にしない。
`receipt_operation=replay`では鍵を子プロセスへ渡さず、同じ原本版を照合して保存済み結果だけを比較する。
結果不明の送信は自動再試行せず、保存応答不明は同じfile IDをGETして照合する。
比較差分は修復authorityではない。このscopeは解析・比較・専用JSONへの保存のみで、台帳writerを持たない。
独立した訂正根拠がそろった対象への修復接続は別途必要であり、解析成功を台帳修復成功と扱わない。

2026-09-16、本人の明示承認後に専用結果JSON 1個を作成し、本人所有・指定SA writerのみの共有と
SAによる初期内容の読戻し一致を確認した。共有変更・4本番stateの流用はない。
実行SHA `7313742a6b775f072c757ef8ccf087cc99355a62` をLinux CI `35034710518` 成功後にmainへ通常統合。
本番Actionsの初回 `35036471978` で実Gemini送信・応答・構造化結果検証・保存が成功。
続く `35036675727` / `35037017029` / `35037469334` で残り3件ずつを処理し、normal10件すべて成功した。
既存キー・同じモデル/指示/validatorを使用し、認証変更・鍵のWindows移送・別APIへの切替はない。
Linux privacy保留0。全10件で日付・合計金額は既存と一致し、比較は完全一致3件、
内容一致だが照合リンク保護2件、差分確認待ち5件。台帳修復・原本移動は0。
確認待ちのうち1件は既存明細0件で、非AI原本抽出でも商品と各金額の対応を確定できず保留した。
残る4件にも店舗表記等の差分があり、AI結果の変化だけでは上書きしない。
再実行 `35037853486` は保存済み10件を再利用し、AI再送0・会計変更0・残対象0。
対象4表の修復前snapshotとの一致を再び確認した。対象別の原本リンク・既存値・解析値は
非公開領域の `receipt-reimport-review.html` に保存済み。
終了検証で3 native stateのreadyと共通ledger全source ready、4stateのmetadata不変、
専用結果JSONの同一ID・本人所有・指定SAのみの共有維持を確認した。
文書変更も含む最終main/承認SHAの一致後に新定期・共通保守を元へ戻し、旧日常writerはOFFを維持する。
これは固定normal10件の実解析であり、Medical/unknownを含む全24件の再取込完了やL4とはしない。
Medicalの非AI検証は一般の認証問題から切り離して実施した。inbox PDF2件はMedical1/unknown1。
対象Medicalは`needs_review`で、支払日/発行施設/実支払額の確定根拠を得られず確認待ち。
専用Windows storeへpending1件を保存し、別プロセス復元・重複抑止・既存medical-review CLIでの閲覧を確認。
会計確認UI/確定反映の接続は未完了で、運用開始やL4とは扱わない。外部AI/Google writeは0。
実原本・最小候補検査・queue証跡は上記非公開領域の`medical-inbox`配下。既存4stateと他sourceの条件は維持する。

##### 領収書確認（一般・Medical共通の確認受付）

2026-09-16、Medical確認受付の本番導入への明示承認後、定期・共通保守を一時停止してmainへ反映。
実装SHA `68740435e5f3ac5e8d2512b3a6aadd0891a64ad5` のLinux CI `35041277724` は1440件成功。
本番受付run `35042040514` が成功し、実「領収書確認」タブを作成した。
一般は変更不要6件（機械判断）・確認待ち4件、Medicalは固定inboxの1件が入力待ち。
保存JSON・原本ID/版/hash・表示リンクを読戻し照合し、Medicalの会計4表への追加0件と原本保持を確認。
本人入力は空欄。確認受付開始と、本人確認後の実記帳成功・自動抽出は区別する。実記帳は未確認。
通常定期と同じ `all` のmanual run `35042346137` も全11段階成功。会計の新規追加/修正0、銀行preview。
既存review-refresh後も同じ確認5行・保存11ID・入力欄が一致し、二重追加なし。
3 native state ready / 共通ledger全source ready、4stateと専用結果JSONの所有者・親・共有維持を読戻し確認した。
定期再開は下記のmain更新手順で文書commitも含めた最終main/承認SHAを一致させ、共通保守を元に戻して行う。
受付は本番で開始済みだが、更新後cronと本人確定後の実記帳はまだ未観測。MedicalのL4実績とはしない。

新親の `receipt_confirmation` scope と通常 `all` のreceipt段階は、専用の「領収書確認」シートを使う。
生成元は既存の専用結果JSON内の `confirmation_items`。4本番stateを確認データ置場に流用しない。
`RECEIPT_CONFIRMATION_BINDING` Variableは既存SA鍵で包んだ専用file IDと既存manifest SHA256のJSON。
平文ID・確認内容・原本・OCRをGit/Actionsログ/artifactへ出さない。

- 一般の完全一致/照合保護と、固定原本の明細・金額・カテゴリ等が一致する店舗表記差は、
  本人確認とは別の機械判断で既存値維持として閉じる。商品名・カテゴリ・明細数の差は自動で閉じない。
- 「領収書確認」は1対象1行。原本リンク、既存値、候補、理由を表示。黄色のH:Oが入力欄で、
  refreshはこの列を上書きしない。確認内容は専用JSONへ保存され、シートから消えた行も再表示できる。
- 一般は原本と候補明細を確認してM列の「候補明細で確定」、または「既存値を維持」/「保留」を選ぶ。
  明細0件は既存値維持だけで解決済みにせず、候補確認・重複先指定・保留のいずれかとする。
- MedicalはH:Kへ支払日・発行施設・本人の実支払額・カテゴリを入力し、M列で「医療費を確定」。
  候補と本人確定を区別し、日付/施設/金額の欠損を0円・仮日付で補わない。患者名・病名・診療内容は入力不要。
  カテゴリは既存の「医療・保険｜病院/薬/その他」から選択。支払方法は任意。
- 同日付近/同額の有効支出があれば重複確認を要求する。同じ支払いならN列へ支出IDを指定して
  「既存支出と重複（紐付け）」を選ぶ。別の支払いと確認できた場合だけ「重複候補と別の支出として確定」を選ぶ。
- 次の新親実行で原本版・入力内容・既存照合を再確認し、既存のreceipt/import/expense ID規則で反映する。
  書込み前intent→既存writer→read-back→反映済みを保存。応答不明の再appendとpending強制解除はしない。
  未確認の入力は計上せず、確定後の再実行は重複しない。表示更新失敗は次回再開する。

Medicalの検出は既存receipt_inbox/privacy gateを使うAIキーなしの子プロセスで実行する。
normalと判定した同一bytesだけを既存一般レシート処理へ渡す。Medical/unknownはAIへ渡さず、
Medical原本はinboxに保持し、normal provenanceも付けない。一般原本のretention削除条件を満たさない。
銀行preview、Payroll、旧日常OFFと会計条件は維持する。受付開始と本人確認後の実記帳成功は別に報告する。

##### Medical匿名化支払額画像の候補解析

**2026-09-16 仕様更新：条件付き自動記帳。** 通常のMedicalはActions内だけで原本取得・描画・
候補検出・匿名化PNG検査・同じbytesのGemini送信・会計判定を行う。
`MEDICAL_DERIVED_AI_POLICY=auto-v1:free` が新しい通常経路。本人の切出し確認・保存・M列操作を要求しない。
手動確認記録は保持するが、この経路では参照しない。`reviewed-v1:*` は従来の例外用手動経路、
`prepare-only` は新規送信を止める既存モードとして残る。

**記帳対象 (`medical-current-actual-payment-v1`):** 当該領収書で今回実際に支払った金額を記録する。
「今回入金額」は、同一領収書の実際の受領欄として確認できる場合に採用する。
一部入金でも実際の受領額を記録し、請求総額・保険負担・未収残高を代わりに記帳しない。
請求と入金と残高の加減算や、全内訳の数字の読取り・一致は要求しない。
返金・取消・預り金・別時点/別帳票、今回の受領額の意味を変え得る注記は区別する。

- 金銭語の部分一致・近似・分割文字を候補とし、全候補の囲み・内容・残存画素を個別検査する。
  元の切出し未解決理由と件数は保持し、会計上の役割は別の `medical-accounting-roles-v3` で評価する。
  同じラベル画素からの重複検出、見出し/印欄、負担率、保険者負担、点数、明確な前回入金を区別する。
  非支払欄の数字の正読や送信可能な画像の生成は、会計上の役割を確認する必須条件にしない。
  発見時の役割未確定数と、今回実支払額への影響未確定数を別に保持する。
  1つの領収書内の明確な今回請求・今回未収と、請求内訳と確認できる保険分/内税は受領額の代用にしない。
  完全なラベル・所属する文字画素・領収書の対応を根拠とし、汎用の請求/負担の断片だけでは除外しない。
  OCRパスで矩形がずれても、同じ連結文字画素と確認できる再検出は1群にまとめ、意味不明なら保留を残す。
  別の支払欄、関係不明の別入金、別領収書、未対応の金銭欄や通貨値は会計競合として保留する。
- 閉じた金銭欄、接続を検証した隣接セル、または空白境界を検証した文字領域を候補にする。
  細い罫線の補正は検出用だけ。送信PNGには元の文字画素を使い、許可文字・配置・全残存画素とmetadata除去を検査する。
  閉じていない領域は別のvalidationとして記録し、閉じたセルと偽らない。分割ラベルの中央に金銭語があっても全体を復元する。
  OCRの数字を正解金額として採用せず、GeminiとのOCR金額一致も要求しない。
  数字の読取信頼度だけで送信を止めないが、ラベル・数字領域・全画素の内容検証は省略しない。
  走査による細い縦罫線のずれは孤立性・連続性を検査して除き、隣の文字画素を保持する。
  日本語ラベルの文字矩形が重なる場合は、範囲を限定した単語全体を追加検査する。
  この扱いを数字へ適用せず、他の文字・未分類画素・複数金額を許可しない。
  患者情報・施設・日付・診療内容・QRなどを残す画像、未検証の画素、原本/OCRは送信しない。
  前処理にAI鍵を渡さず、送信プロセスにはGoogle原本/Sheetsの認証を渡さない。
- 自動記帳は、対応する非AI日付/施設、マスタ内カテゴリ、一意な実支払額、署名済み保存応答、
  原本/ページ/版の対応が揃い、本人入力・保留・既存記帳との競合がない場合だけ。
  支払方法は推測せず空欄とする。Medical自動処理の事前31日検査とwriterの7日検査は、
  `medical-payment-units-v1` の共通処理で支払い単位を比較する。前後の日付窓は維持し、事前検査では日付不明の同額候補も保留する。
  レシート/取込/支出の明示IDとリンクを使い、領収書総額・決済取引額を比較する。
  商品明細だけの同額一致を除外するには、親ヘッダ・取込完了記録・全明細の所属/有効状態/連番ID・合計の整合が必要。
  親総額と関連決済は引き続き比較する。親不明・欠損・リンク/総額矛盾・分割/部分支払い不明は、関連する候補を保留する。
  同じファイル・店舗・日付・カテゴリだけで取引を統合せず、返金/振替を購入へ合算しない。無関係な欠損を全件の保留理由にはしない。
  取込のみ/支出反映済み、支払総額候補/構成明細だけの一致/対応不明を区別し、自動紐付けは行わない。
  一般レシートと本人が明示判断する手動経路の候補抽出・UI契約は維持する。
  安全性を検証した画像の解析は、他候補の切出し失敗や日付不足と分離する。
  今回実支払額への未解決影響が0で、独立した今回支払欄が1つの場合だけ、他の記帳条件へ進む。
  切出し失敗件数を0へ書き換えない。広い検出件数・独立欄数・会計競合数は別に記録する。
  日付は西暦/和暦と隣接ラベルを非AIで検証し、無関係なOCR行の信頼度を使わない。
  生年月日・診療/受診/処方日・期限・期間は支払日に流用しない。発行日は入金欄または領収書の根拠も必要。
  日付なし・役割不明・競合・匿名化不成立・重複不明を、それぞれ確認待ちの理由として保持する。
- `medical_auto_posting.decide` が条件と保留理由を判定し、本人のM列・確認署名を偽装せず、
  別の `automatic_decision` とpending計画を保存して既存writer/stable ID/read-backへ渡す。
  表示は「自動反映済み」。例外は「領収書確認」に日本語理由を集約し、他の対象の処理を続ける。
- 送信最大3件/run、自動記帳最大1件/run。既存成功応答は再利用。応答不明intentは再送せず保留し、
  会計write不明・state破損は停止して読戻し照合する。原本は保持し、4本番stateへ画像や候補を混在させない。
  会計policyの評価は専用storeの `medical_accounting_evaluations` に別途署名して追加する。
  成功済みAI応答・元mapping・来歴を変更せず、その完全な記録と評価をdigestで結ぶ。
  原本/送信PNG/モデル/プロンプト/応答の対応を検証できない場合は流用せず、盲目的な再送もしない。

初回は `auto-v1:free:canary` と専用storeの `medical_auto_canary.source` で既存非公開記録の1件へ限定する。
手動記録/座標なしの同経路で実送信・自動記帳・保留を別々に確認し、成功結果replayまたは安全な保留を確認後、
通常policyへ移行する。対象ID・実画像・実金額をGit/ログ/artifact/cacheへ載せない。
**本番検証（2026-09-17）:** 自動run `35148416339` で本人操作なしのGemini解析1件と候補保存・表示を確認。
候補24・提案58から送信検査を通った派生PNGは1枚。非AI日付/施設とAI金額を読戻し確認した。
一方、未解決の金銭候補10箇所（境界3・数字領域6・内容1）が残り、記帳は0件の保留。
切出し・画像保存・M列確定は正常経路の条件ではないが、この実帳票の無人記帳成功は未確認。
本人入力・既存会計・原本を保持し、例外理由は「領収書確認」に集約する。
同一版のreplay `35149803791` は新規AI送信0・保存応答再利用1・会計追加0。
並行した本人分類6行のF:Gだけを別集計し、それ以外の会計セル不変と確認入力保持を読戻し確認した。

**会計役割を分離した再評価（2026-09-17）:** run `35156998572` は保存応答再利用1・追加AI送信0・記帳0。
元の切出し未解決10箇所は、重複1・根拠付き非支払3・支払候補0・意味未確定6として保持する。
検証済みの今回入金欄1つは別に数え、元のmappingを変更しない。
広い検出全体の会計上の未解決は7群。保険分負担と今回入金の対応1、請求の対応先不明2、
負担の対応先不明2、欄の種類が不明な金銭語断片2であり、7支出や誤金額が確定したという意味ではない。
今回入金との関係を説明できないため、この対象は保留を維持する。切出しの失敗だけを保留理由にはしない。
元のAI成功記録・以前の役割評価・本人入力・本人分類6行・他の会計行は読戻しで保持を確認した。
replay `35157380836` も追加AI送信0・保存応答再利用1・会計追加0で、会計4表・両版の評価記録が一致。
通常policy `auto-v1:free` に戻し、新親/定期/共通保守を継続する。対象の保留で他sourceを停止しない。
本番の無人記帳成功は引き続き未確認であり、解析成功や合成writer試験だけでL4とはしない。

**今回実支払額scopeでの再評価（2026-09-17）:** run `35163937413` は追加AI送信0・保存応答再利用1・記帳0。
旧7群を保持し、明確な保険分の請求内訳1群を非競合、同じ負担文字の別OCR検出1群を重複として別評価した。
今回入金欄1つに対する独立した第二支払欄は未検出だが、影響未確定5群が残る。
所属不明の請求/前回額付近の候補、負担/税の記述、見出し/印付近の断片は、今回の受領額に影響しない根拠を
確定できていない。全請求内訳の解明を求める条件ではなく、この影響未確定を保留理由として保持する。
さらに既存writer計画が「同日付近・同額の既存支出」を検出しており、同一取引か未確定のため、この別条件も緩めない。
replay `35164305185` は追加AI送信0・保存応答再利用1・追加記帳0で成功し、会計4表・元AI応答・新旧評価・本人入力の保持を読戻し確認した。
通常policy `auto-v1:free` へ復帰し、新親/定期/共通保守を継続する。固定1件は例外保留であり、実記帳成功とはしない。
7群の内訳・物理的所属・根拠はPROJECT_STATUSの最新記録を参照する。

**支払い単位の重複照合へ修正（2026-09-17）:** `medical-payment-units-v1` をmainへ反映し、
固定対象run `35179709578` は追加AI送信0・保存応答再利用1・記帳0で成功した。
実台帳で事前31日検査2件とwriterの7日窓内1件を読取り専用で比較し、親取引と完全な構成明細を確認できる
商品明細額だけの一致は重複候補から除外した。実際の自動writer計画も直接実行し、意味判定より後の条件を確認済み。
親の支払総額・関連決済は比較を継続し、日付窓・匿名化・意味判定を緩めていない。
固定対象は意味未確定5群で引き続き保留。会計4表・本人分類・確認入力・元AI応答・評価・手動記録を保持する。
replay `35180332561` も追加AI送信0・保存応答再利用1・追加記帳0で成功。両検査・writer計画を再確認し、
会計4表・本人入力・AI応答・評価の初回結果との一致を読戻した。通常 `auto-v1:free` に戻して運用を継続する。
同じ許可済みinboxの別帳票はsensitive_unknownで、追加のMedicalは0件。別帳票の正常系実評価は未実施であり、
固定対象の無人記帳成功や全帳票の精度、L4を確認した結果ではない。検証SHA・CIはPROJECT_STATUSへ記録する。

main更新時は、最新mainの変更を保持して統合候補を合成CIで検証する。実行中処理の終了を確認し、
新親/定期/必要な共通保守を短時間停止→検証済みmainを通常push→`KAKEIBO_VALIDATED_MAIN_SHA`を完全SHAへ更新→
mainとの一致を読戻し→共通保守/新親/定期を再開する。文書だけのcommitもSHA不一致を残さない。
停止対象はその時点で有効な共通writer/表示保守を確認する。分類条件の専用runnerも有効なら対象に含め、
元状態を非公開保存して反映後に戻す。分類のUI/preview/apply等、別機能のVariablesは変更しない。
旧日常入口は再開せず、銀行preview/収入write OFF・Payroll・固定4stateを維持する。

導入結果とreplayはPROJECT_STATUSの最新記録を参照。以下は旧経路・導入途中の履歴。

初回自動canary `35081592505` は正常終了したが、実対象は候補19箇所すべてで金額欄の囲みが
確定できず、画像送信0・自動記帳0の保留。日付不足も残り、施設/カテゴリの非AI候補だけを表示した。
本人入力・手動確認記録・既存会計は不変。これは自動保留の本番実績であり、実画像AI・無人記帳の成功ではない。
成功済み手動解析が同じ画像に残る場合も、自動の出典対応へ書き換えて使わず、対象だけを照合待ちにする。

Medical派生画像AIの実装はmainへ接続済み。実対象は匿名化保留で、Gemini実解析は未確認。
原本用gateは維持し、Medicalをnormalへ変更しない。
新親の `MEDICAL_DERIVED_AI_POLICY` は未設定なら既存確認受付だけ、`prepare-only` は非AI準備だけ。
利用中プランを確認した `reviewed-v1:paid` / `reviewed-v1:free` でのみ検証済み派生PNGの送信を許す。
これは課金プランを変更する設定ではない。利用条件/実プランが不明なら送信を開始しない。
2026-09-16、本人より利用中プランはFreeと回答。支払額ラベル「今回入金額」を追加した。
候補検出は金銭関連語の部分一致・近似一致・横/縦の分割文字を広く拾う。
前回/累計入金額・請求額・未収額も検出対象だが、実支払額としての採用とは別。
完全一致ラベルやOCR信頼度による入口での除外をやめ、送信前の画素・匿名化検証は独立して行う。
自動送信側の許可内容検査に通らない候補は、非公開の確認・修正へ回す。
[Google公式利用条件](https://ai.google.dev/gemini-api/terms)では無料サービスへ個人・機密・機微情報を
送らないことを求め、有料サービスではprompt/応答を製品改善に使わないと説明している。
どちらでもこの経路は実支払額・ラベルだけに限定し、利用者の本人確定を代行しない。
実送信結果はPROJECT_STATUSへ別途記録し、合成試験・送信0の保留と区別する。

2026-09-16の固定1件はActions前処理で施設・カテゴリ候補を取得したが、
支払額領域を一意に確定できず `payment_region_ambiguous_or_absent` で保留。
日付・金額は空欄、本人入力H:O/Mを保持。G列にAI/非AIの未確定候補と保留理由を表示する。
新候補だけで本人判断を選択しない。「候補で医療費を確定」は本人の明示選択が必要で、
候補更新時は古い判断を無効にする。同じ原本版/crop/前処理/prompt/modelの保存済み結果は再送しない。
新規解析は最大3件/run。結果不明のintentは自動再送せず照合する。
同日の通常 `all` run `35052380694` でもMedical画像送信0・確認入力保持を確認した。
通常カード新着3件は取込のみで支出追加0。固定4stateのready・所有/共有維持を読戻し確認済み。
この結果はMedicalのGemini解析成功ではない。一般の定期運用を継続し、Medical送信だけ保留する。

自動匿名化が保留の対象は、本人用の一時的なローカル確認画面で範囲を修正できる。
既存SAの読取権限で原本をメモリに読み、127.0.0.1のランダムURLだけに表示する。
AI・公開サーバーへ原本を渡さず、別のサービス/DB/認証は作らない。

```text
python -m app.medical_crop_review_ui --binding <非公開actions-reimport-binding.json> --output <repo外の新しいreview.json>
```

- 未確定Medicalが複数なら非公開記録の `--review-id` で1件を指定する。
- 出力先と同じ場所の `.url` ファイルに本人用URLを保存。画面は1時間で終了する。
- 本人が原本から支払額・印字ラベルだけを選び、**最終PNGを見て匿名化を確認した場合だけ**保存。
  確認をAIで代行しない。保存内容は版/hash/座標/確認署名だけで、原本・PNG・OCR・正解金額を含めない。
- Drive版だけが変わっても、同一ID/親/MIME/内容hashと読取中の版安定を確認できれば、
  UIはその現在版を新たな確認対象にする。旧版の承認は流用せず、本番storeも自動更新しない。
- 保存はローカルのみ。既存保守手順で定期・共通writerを一時停止し、実行中0を確認してから、
  同じ原本を読取・照合し `MedicalCandidateState.install_crop_review` で既存専用JSONへ反映する。
  4 native stateは使わない。本人入力・M列・会計を更新しない。
- Actionsは同一原本からPNGを再生成し、本人が見たPNGのhash/版/範囲/署名と完全一致した場合だけ受理。
  Linux/Windowsの描画差も不一致なら保留。回転・複数ページは依然対象外。
- プラン確認後に派生画像だけを解析し、候補読戻し・同一版のAI再送0を確認する。
  匿名化確認は会計確定ではない。安全に分離できない場合は既存確認シートの手入力を利用する。
- 初回送信対象は、同じ専用storeの `medical_image_send_reviews` に明示登録した確認記録のdigestだけ。
  プラン設定だけでは送信できず、原本版・本人確認記録・最終PNG・前処理版を照合する。
  記録登録だけで本人の会計判断は変更しない。別原本・自動cropは未承認として保留する。
- 自動候補は部分一致・近似・分割文字で広く検出し、各囲み領域を個別検査する。
  一候補が枠不明でも他候補を検査するが、未解決候補を捨てて残りを正解扱いしない。

2026-09-16の保存済み本人確認は照合・専用store反映・対象1件への限定まで完了。
実行コード `05682bc` のLinux合成1610件は成功したが、実行環境の自動承認レビューが
Free送信設定とprepare-onlyでの通常再開をそれぞれ実行前に拒否したため、画像AI解析・候補表示・replayは未実施。
本人の画像確認/保存とFree回答は済んでいる。新親/定期フラグfalse、旧日常と共通保守3入口は停止中で、
Medicalはprepare-only。本人入力/会計不変と4state readyを確認済み。
拒否解消後は保存済み記録を再作成せず、現在状態を読戻して未実施工程から再開する。
Medical固有の問題だけならprepare-onlyで通常運用を再開する方針を維持する。

その後Ask for approvalで通常運用を復旧し、限定run `35058745018` を起動したが、
確認シートの旧版ID重複で送信前に失敗した。解析intent/応答0・会計不変を読戻し、Medicalをprepare-onlyへ戻した。
確認表示は既存行の更新を全て済ませてから新規行を追加する。appendによる行移動後に古い行番号を使わない。
本人入力のない重複表示は、固定原本・保存済み確認・開始前行との一致を確かめ、
対象の管理列だけを現在版の確認IDへ戻す。本人入力・会計表・原本・確認署名は変更しない。

確認表示は全ID照合で復旧済み。run `35062753003` は成功したが、Linuxで保存済みcropの
照合が保留となり、画像送信0・非AI施設/カテゴリ候補のみ表示された。本人入力・会計は不変。
Windowsでの照合成功をLinuxでの一致とみなさない。固定理由コードで署名/原本/寸法/描画/PNGを
切り分け、prepare-onlyで確認する。不一致の画像へ差し替えたり、本人確認を再作成したりしない。
新親・定期・共通保守のONと、Medical画像AI解析の成功は別に記録する。

##### 固定IDの非公開受渡しとmain更新（共通手順）

通常のActions環境変数は処理開始前にログへ出るため、固定folder/4 fileの5 Variablesは
`app.private_state_bindings.wrap`で既存本番SA鍵の公開部分を使ったRSA-OAEP-SHA256暗号文にする。
元のbinding・暗号文設定・本人所有/指定SA共有のmetadata commitmentはrepository外の非公開領域へ保存する。
Actions側は既存SA鍵でPython内だけ復号し、平文をGITHUB_ENVへ出さない。新しい鍵・OAuth・Secretsは追加しない。
canaryの`amazon_target`も同じ関数の`AMAZON_TARGET`用途で暗号化する。平文入力・別用途の暗号文は拒否する。
将来既存SA鍵を正規の手順で交換する際は、5値も再暗号化して同じIDへの復号一致を確認する。

`state-migration.yml`はmanual/main/検証SHA一致・共通排他・既定inspect。最終成功run/attemptの完全キーだけ復元し、
後続の実行済み失敗runは自動で飛ばさない。Gmail/AI鍵・cache保存・artifact uploadは持たない。
正式移送は`operation=migrate`、完全SHA/3完全cacheキー/非公開metadataのSHA256を渡す。
全4準備用JSONの一致と全bundle復元成功後だけ同一IDを更新する。正式ledgerの再初期化は拒否する。

以後mainを更新するときは新scheduleを止め、実行中writeの終了後に通常統合し、更新後SHAのLinux CIと
実行コード/Workflow差分を確認する。文書commitも含め`KAKEIBO_VALIDATED_MAIN_SHA`を最終main完全SHAへ更新し、
一致を読取確認してからscheduleを戻す。コードの未検証変更があれば承認SHAを進めない。

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
   **保存先folder/未初期化4ファイルの作成・接続試験は完了。本番state upload/復元は未実施**。
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
保存先の作成は本人認証、日常更新は既存SAと役割を分ける。
所有者・共有範囲・親フォルダ・`canEdit`と実読取/更新/読戻しは上記準備用ファイルで確認済み。
切替前には固定IDで再確認する。Actions runnerからの接続と本番state復元は未確認のまま残す。

09-15の準備前read-only確認では、現在のSAでDrive `about.get`に成功し、
`storageQuota.limit=0`、`canCreateDrives=false`だった。その後、本人認証で固定保存先を作成した。
Google公式もSAは保存容量を持たずファイル所有者になれないとしている。
[Driveの所有・保存容量の制約](https://developers.google.com/workspace/drive/api/guides/about-shareddrives)
今回の保存先準備Goalの明示承認により、既存の所有者が非公開管理フォルダと4個の固定JSONファイルを作成し、
その所有権を維持した。指定SAへの編集者共有も同Goalの許可内で行い、公開リンクは作っていない。
日常runnerは現在のSAでその固定file IDを更新し、所有者のOAuthへ自動切替しない。
実ファイルの所有者/共有範囲/`canEdit`/更新read-backは確認済み。本番stateへの置換は未実施。
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
   amazon_target=<承認referenceのRSA暗号文>`で実行する。
   親は既存CLIへ`--apply-limit 1 --approved-target ... --expected-event-rows 1 --expected-header-rows 1`
   を渡す。canaryではイベントも選定Order IDに属する行だけに限定する。
   対象が一意でない・限定後の件数が変わった場合は停止する。銀行applyとの併用は拒否する。
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
今回の承認範囲内で各既存authorityの件数/期間、新着と過去未反映行、共通後処理・原本archiveの変更予定を確認し、
sourceごとの実read-back/replayを確認してからscheduleを有効化する。

切替時は **保存先準備完了 → 対象旧入口停止・実行中/待機中処理の終了確認 →
最新の確定state取得 → 固定ファイルへの正式移送・読戻し → 新入口preview →
限定canary/read-back → 通常all apply・再実行検証 → 新schedule有効化**。新旧write並走は禁止。
保存先準備前に旧運用を止めない。今回実行したのは保存先準備・接続確認まで。
移送の保守時間には旧日常4入口だけでなく、月次backup/retention、全manual write入口
（銀行収入backfillを含む）、ローカル/他Workのwriteも入れない。
停止・終了確認後から移送/読戻しまで同じproduction concurrency内で作業し、
Actions lock外のローカルwriteは操作者の保守手順で止める。待機runを残したまま移送しない。
これらの停止・移送・新親起動は今回行っていない。
新入口停止とstate/Sheets整合性確認後にだけ復帰を検討し、無条件rollbackをしない。
Windows直接writeはActionsロックの対象外。移行後はlocalテスト/previewに限定し、
本番修復は定期運用を止めた保守手順で実施する。現在のTaskは停止していない。

09-15の最新Goalで旧入口停止・正式移送・preview/canary/通常apply/replay・新定期ONまで明示承認された。
工程の区切りだけでは再承認を求めず、既存authorityとprivacyを維持して条件成立時に連続実行する。
銀行収入write、認証/共有変更、過去一括修復、原本削除、公開設定/課金変更は承認範囲に含めない。

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
| native state保存/復旧 | 合成pytest、本人による固定保存先作成、既存SAの準備用JSON読取/更新/読戻し | 固定ID/本人所有/指定SAだけの共有/実接続を確認。本番state移送・Actions接続・復元は未実施 |
| stateless writeの中断と最終成功保持 | `production_ledger.py`, `test_production_ledger.py`、共通運用記録用固定JSONの接続試験 | 合成検証済み。準備用ファイルは未初期化のままで、正式運用ledgerは作成/移送していない |
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
レシート行のG列「原本」には、Driveのfile IDで開く「画像を開く」リンクを表示する。
画像とPDFの両方に対応し、原本を開けない行には理由に応じた状態を表示する。

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

「要確認」シートの自動生成列（A〜J）は編集しない。スマホから対応する場合は
K〜O列だけを入力する。

- ユーザー判断: `支出として計上` / `重複として除外` / `レシートと統合` / `保留`
- 統合先取込ID: `レシートと統合` の場合に入力
- カテゴリ: `支出として計上` の場合、M列から `大カテゴリ｜小カテゴリ` を選択
- ユーザー備考: 任意
- 反映結果: システムが結果またはエラーを記録

判断とカテゴリはシート上のドロップダウンから選択する。モバイルでも安定して選べるよう、
カテゴリは `食費｜食料品` のように大・小を一体化して表示する。従来どおりM列とN列へ
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

各Drive inboxの処理済みフォルダ設定と再実行については [inbox と処理済み原本](docs/inbox-processed-folders.md) を参照。
