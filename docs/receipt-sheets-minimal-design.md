# 一般レシート：Sheets正本の最小設計

2026-11-16。review候補のみ。**本番NO-GO、実環境反映・workflow再開は未承認。**
基点は通常workerの `fe90f64b47ffefae1efbca0ae7f646002e50576f`。
最新のユーザー指示に従い、受付・検証状態のDrive JSON CASをSheets管理行へ置換する。
金融authority、writer、原本移動、AI送信への接続は持たせない。

## 既存経路と置換範囲

|役割|旧経路|新経路|
|---|---|---|
|登録前準備|`receiptPrepareDraft` → private journalのPREPARED|不要。既存custom checkboxのUUIDを使い、登録チェック時に固定|
|受付|`receiptValidationEdited` → `acceptPrepared` → Drive If-Match|`receiptSheetsRegister_` → `_receipt_requests`へ1行保存、独立読戻し|
|dispatch|Connection.gsの既存PAT → category-sheet-request.yml、UUIDのみ|同じworkflow・PAT・request_id入力を再利用|
|worker|receipt_sheet_worker → ReceiptPrivateAdapter → ValidationJournal|receipt_sheets_worker → SheetAuthority|
|検証入力|private catalogと編集中の確認行を繰り返し読む|Sheetsに固定されたsnapshotだけを検証。原本は独立GETで照合|
|結果|private journal終端CAS → 別の`_receipt_validation_results` cache|同じ受付管理行のH:Jだけ更新、独立読戻し|
|G列表示|旧cache参照式|同じG列に受付管理タブを参照する式。入力配置・リンク・メモ不変|

旧Drive依存は `receipt_private_adapter.py`、`receipt_sheet_validation.py` のjournal、
`receipt_sheet_connection.py`、旧workerとConnection.gsの旧portsに残る。
これらは既存5件の履歴・旧経路を壊さないため保存するが、新modeからは呼ばない。
新mode `sheets-authority-v1` は既存 `receiptConnectionEdited` の先頭で選択される。
Code.gsから純粋なdecoder/snapshot関数だけを再利用する。
Pythonのbaseline検証 `_expected` はIOのない `receipt_sheet_context.expected_context` に抽出した。

既存の `receiptConnectionConfigure/Start/Stop` もmodeを判定し、新経路へ分岐する。
Configureはdisabled設定だけを保存し、旧journal用bindingの上書きを拒否する。
Startは同じSheetを指す既存ON_EDIT triggerがproject内に一つだけあることを確認し、
管理tabをGETで検査してから設定をenabledにする。trigger作成・削除は行わない。
欠落・重複・停止latch保持中は開始を拒否する。
Stopは新modeの設定をdisabledにし、一時PATコピーだけを削除する。
既存trigger・正本行・停止latchを保持し、結果不明を解除しない。
設定・開始・停止・受付は同じScriptLockを使用する。受付はlock取得後にもenabled設定を読み直すため、
停止前に設定を読んで待機していたイベントも、停止後に古い設定で受付を続行しない。
これらは本人承認後の明示操作用コードであり、review branchの保存では実行されない。
[Triggerの識別情報](https://developers.google.com/apps-script/reference/script/trigger)の読取scopeは既存manifestのscript.scriptapp内に収まり、追加scopeは不要。

## 唯一の受付・検証正本

同じworkbookの `_receipt_requests`、A1:J1000。行1は下記の固定header、最大999要求。
空白行、欠落、重複UUID、不正なschema/digestを検出したら停止し、再構築しない。
行番号はidentityではない。各更新前にUUIDで再解決し、前回の行位置と全文も照合する。

|列|項目|保存内容|
|---|---|---|
|A|request_id|custom checkboxに含まれるUUIDv4|
|B|receipt_id|既存case_id|
|C:E|source_id / source_version / source_sha256|原本ID・版・bytes hash|
|F|snapshot_json|case_id、source、operations、context_json|
|G|snapshot_digest|FのPython/JS共通canonical JSONのSHA-256|
|H|status|受付済み → 検証中 → 検証済み・未記帳 / 修正が必要 / 保留|
|I|validation_result|request_id、snapshot_digest、state、issues、posting_authority=false、financial_write_count=0|
|J|updated_at|UTC ISO日時|

context_jsonは既存の保存済みimport、候補商品、カテゴリ、ルール、原本照合根拠を含む。
新しい候補収集・AI解析・金融権限を作らず、既存baselineを純粋関数で検証する。
baselineの既存数値表現（1.0等）を保持するためcontext_jsonはcanonical文字列として格納する。
snapshotはASCII escapeのcanonical JSON、結果は日本語stateをそのまま表示式に照合できるUTF-8 canonical JSON。
1セル40000文字、既存User Property上限に収まるconfig 9000 bytes以内に限定し、超過は停止。

接続設定は既存 `RECEIPT_VALIDATION_CONFIG_JSON` Secret / User Propertiesと既存
`RECEIPT_VALIDATION_ENABLED/REF/SHA` Variablesを再利用する。新規Secret・Variablesは不要。
設定内のbaselineは受付状態の正本ではなく、原本・候補・policyの事前確認済みbindingである。
workerはsnapshot内contextとこのbindingの完全一致を確認する。
一般・normal/purchaseのみ。医療・PDF混在等へ拡張しない。

## 一回の登録操作

1. 本人は既存の商品セルと原本照合チェックを必要に応じて修正する。
2. I列の既存custom checkboxを一度チェックする。`e.value/oldValue`のUUIDを検証。
3. 同じUUIDを持つ確認行を検索し、case・原本・machine vectorを既存decoderで検証する。
   eventの行座標から案件を決めない。入力を2回読み一致した内容を固定する。
4. 管理tabのheader・UUID一意性・空白行・既存receiptを確認して、末尾の空行へRAW PUTを一回。
5. 同じ管理tabを独立GETし、新規行が一意で全文一致することを確認してからUUIDだけdispatchする。
6. workerは固定snapshotを読み、検証中を一回保存・読戻しする。
7. 既存 `review` / `registration_plan` を使用し、固定operations・固定候補・固定policyで検証。
   既存原本GETで版・bytes hash・ACL・親を検証前後に照合する。確認UIは読まない。
8. 結果書込み直前にUUID、snapshot全文とdigest、状態、行位置を再照合。
   H:Jだけを一回PUTして独立GETで確認。同じ管理行が終端となる。
9. 既存G列は管理行をUUID＋receipt＋原本identityで参照し、1件だけで、result内UUID/digest/stateと
   金融write=0が一致するときだけ「検証済み・未記帳（登録時の内容）」等を表示する。
   登録後の入力変更は未登録下書きであり、今回のsnapshotへ取り込まない。

本人が同じレシートを二重登録しない前提。既存receiptへの再受付は拒否する。
エラー後の修正再登録や既存5件の移行は今回の経路に含めない。

## 排他の成立条件：CASは仮定しない

- receiverは既存の一つのApps Script project / ScriptLock下だけで新規行を追加する。
  既存行を編集しない。workerは新規行を作らずH:Jだけを更新するため、通常の受付とworkerの書込範囲は交差しない。
- 旧receiptと新receiptのworkflowは既存の固定group `receipt-synthetic-validation` を共有する。
  `cancel-in-progress:false`。新旧modeはref prefixで排他的に選択される。
  同groupの実行は一つだけとなる。[GitHub concurrency仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- 有効期間中は管理tabのsort、挿入、削除、移動、既存行・snapshot直接編集を全員が停止する。
  外部ツール・別project・ローカルworkerも同tabへ書かない。
  保護設定だけでownerの編集を防げるとは主張しない。
- ScriptLockは同じscriptの実行だけを調整し、Pythonや人の編集はlockしない。
  [LockService仕様](https://developers.google.com/apps-script/reference/lock/lock-service)
- Sheets PUTは無条件のrange更新である。二重readやread-backでTOCTOUを消した、CASを実現したとは扱わない。
  [values.update仕様](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/update)

**これらのwriter・構造変更排除を実環境で確認できない場合は停止し、実機試験NO-GO。**
全writerをfenceする新基盤や複雑なCASは追加しない。

## 再実行拒否と不明結果

Actions rerunはrun_attempt != 1で接続前に拒否。新しい手動dispatchはattempt=1なので、さらに
公開repositoryの既存workflow実行履歴をGET一回で照合する。同review branchの全dispatch履歴が
100件以下で取得でき、run-nameが対象UUIDの実行が現在run_id一件だけの場合に限って続行する。
過去claimが送信前失敗でSheetsに受付済みが残っても、新dispatchは履歴二件となり拒否される。
履歴不達・不完全・100件超過は停止。履歴の削除・消失を許容する運用は不可。
認証なしで公開履歴を読めるためActions権限は追加しない。
[workflow runs API仕様](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)
これは実行の重複検査であり、受付・検証状態をActionsから復元するものではない。

receiverは送信前に汎用停止latch `RECEIPT_SHEETS_STOPPED` を設定する。
保存読戻しとdispatch 204が両方確認できた場合だけ解除する。
latchはrequest内容や受付状態を保持せず、別正本・復旧台帳ではない。
異常・process death時には保持し、以後の登録を止める。自動解除・修復・再送機能はない。

claim/resultのHTTP異常・timeout・読戻し不一致・worker exceptionは結果不明を返す。
新しい状態への推測書込み、rollback、cache append、通常処理のretryは行わない。
token取得も一回、credential refresh replay・HTTP redirects・HTTP retryを無効化する。
未終端の管理状態は画面上「受付確認中・未記帳／チェックを繰り返さない」となり、成功とは表示しない。
本人はActionsの失敗と管理行をread-onlyで確認し、処理を再実行しない。

**応答不明と正本の確認は区別する。** 終端PUTがcommitした後に応答を失う場合、workerの返答は結果不明。
返答を成功と読み替えない。ただし、後から独立readしたSheetsに一意で完全な終端結果があれば、
そのreadを根拠に結果を表示できる。式自体もこの独立readであり、通信ACKの推測ではない。
欠落・重複・不一致・未終端のままなら成功表示しない。
応答不明後は正本が完全でも一切成功表示しない、という追加要件にはこの案は対応していない。
その意味での要件変更がある場合は再設計・承認までNO-GOとする。

## 成立範囲

オフラインの合成検証のみ合格。Google実APIの書込み、式の実Sheets engine評価、
iPhone表示・一回操作の実測、管理tabの全writer排除は未実証。
既存5件の個別本文取得・修正・再実行、Google実環境へのwriteはゼロ。
試験資産候補のメタデータ検索だけを行い、利用可能なbindingは未確定のまま。
新規サービス・Firestore・Drive JSON journal・復旧store・金融writeは新経路にない。
実機試験準備と判定は [試験手順](receipt-sheets-device-trial.md) を参照する。
