# 実Google Sheets A方式・合成限定接続

## 範囲と採用構成

基点は Draft #158 head `375e9d91480eb9ce8d4717af21663c77523c4ddf`。
#158をレビューし、合成・金融境界停止の範囲では先へ進めると判断した。
本番gateを外す変更はない。Google transport、SheetsRegistration、下書きadapter、
RawReader、原本revision照合、PersistentJournal、CAS、登録番号・重複・UNKNOWNの判定を再利用する。

正式1タブをコピーした**完全合成・owner限定の専用ブック**に、B「操作依頼」とC「登録・結果」を配置した。
入力・原本リンク・旧保留・医療完全手入力を保持する。標準Tableプルダウン「入力中／登録 1〜9」を使う。
取引ごとの再認証・比較・本人承認は不要。Safari確認ページ、Apps Script、DB、認証基盤は追加しない。

今回の経路は、既存Google Drive connectorでfresh Sheet・Drive revision・profile・ACLを取得し、
credentialを持たないローカル合成backendへ渡す**有限の試験セッション**である。
既存pollingを明示的に呼び、結果だけを同じconnectorで書き戻す。
**常駐worker、スケジュール、無人運用は未稼働。会話終了後の自動処理を保証しない。**
PythonへGoogle tokenを渡していない。新しい認証情報・共有ACL・IAMの追加や変更は行っていない。

## 実Googleで使った資源

| 用途 | 専用資源 |
|---|---|
| iPhone受入 | [登録接続・iPhone受入（完全合成・未記帳）](https://docs.google.com/spreadsheets/d/1Ls3kMi_4Ox0zm2SOI-9QNYwOF01rBvksFymH2YvAmdU/edit) |
| 故障専用・日常利用不可 | [通信故障プローブ（完全合成・UNKNOWN保持）](https://docs.google.com/spreadsheets/d/1AsJqRv1dtzyqcis23Uh3K-4mP71BOLMxnk1OXIcefm0/edit) |

既存Phase4合成資源をコピーし、承認済み元ブック・4タブ・本番ブックは変更していない。
G101/M101だけは #158 fixture の既知の合成PNGを専用フォルダへアップロードした。
ダウンロードした実ファイルのbytesとSHA256がfixtureと一致することを確認し、実Drive revisionを固定した。
実IDをfixtureの金融identityに見せかけず、外側のGoogle principal/原本ID/revisionと内側の合成native identityを別domainに保つ。
他6案件の入力・リンク・保留は維持する。実データの転用・AI/OCR送信・原本移動はない。

## 実装と送受信の契約

`staging_shadow/live_synthetic_sheets.py` が実Sheetと既存の合成workerの境界。
`tests/live_google_sheets_probe.py` はテスト専用backendであり、deploy用entrypointではない。
実Google clientを注入する場合も、明示enable・既存intent・fresh読取りが必須。
credential探索や環境変数からの自動接続factoryはない。

1. 独立に2回、Sheetの全対象範囲・Table/保護/注記・profile/ACL・全原本revisionを取得する。
   固定8案件、種類、ID、原本hash/ID/revision、入力、構造、literalな登録番号を検証する。
2. 2snapshotが異なれば取込前に拒否。同じ入力の安定観測を既存pollingでも行う。
   これはモバイル端末の未同期編集が全て届いた証明ではない。
3. backendが既存下書き/CAS/journalへ保存・検証し、金融境界で停止する。
4. 結果intentを暗号化journalへ記録。送信直前にもう一度fresh照合し、単発のSENDING claimを永続化してから送る。
   同じintentのreadyは二度成功しない。結果はCだけ、通常入力や原本・金融ledgerは書き換えない。
5. Googleのexact raw formula/rule読戻しで結果を照合する。
   送信結果不明は照会のみ。別IDを作って再送しない。backend UNKNOWN時は新入力の内部への転写も停止する。

claim必須の新しい未送信intentは、入力/表示が変わればCASでCANCELLEDにできる。
まだ送っていない同一intentの再取得は単発claimを経て送信可能で、SENDINGの再送とは区別する。
旧形式intentは未送信と推測しない。今回、readyが入力変更で拒否された旧表示1件は、
Google write未実行の操作記録を確認し、checkpoint hashを固定した試験保守操作で明示的に取り下げた。
SENDING/UNKNOWNは取り下げ不可。取引の受付済みreceiptは変更しない。

結果は受付待ち／保留／競合／UNKNOWN／登録検証済み・未記帳を区別し、案件IDと受付識別子を表示する。
結果数式に案件・原本・入力のguardを含め、最後のfresh読取り後に入力が変わっても競合を表示する。
Google Sheetsに原子的な入力CASがあるとは主張しない。Drive revisionの表示セルだけを正本にしない。
UNKNOWNは後続の入力・操作変更でも自動的に受付待ちへ戻さない。

実TableのDROPDOWNはCellDataではなくcolumnPropertiesにあるため、既存の列ルールを保持する。
表示列にだけ書き込む。一般複数明細は既存の個別カテゴリを保持し、総額からカテゴリを推測配賦しない。
医療は手入力不足なら保留、PDF・旧保留は登録不可。実金融writer/authorityのportは接続しない。

connectorの読取りAPIではTable/保護が返らないため、専用コピーのA1に絶対一致しないfindReplaceを使い、
更新0件のbatch responseから構造を取得した。これはwrite可能なAPIによるmetadata probeであり、純粋なGETとは区別する。
本番にこのprobeを送っていない。

## 権限の確認結果と限界

実profileと同じGoogleユーザーがowner、共有なし、ACLはownerの1件であることをfresh readで確認した。
同じconnectorで専用コピーの入力・結果書込みと読戻しに成功した。
ACL/profileの欠落・変更・共有追加はfail-closed。
セル編集者を特定したとは扱わない。検証した実行主体は**connectorのownerユーザー**であり、
常駐service workerの権限や事前許可モデルの本番有効性は検証していない。
connector内部OAuth scope/自動retry設定は公開されないため、最小scopeや内部retry 0を証明したとはしない。
注入client用のGoogleResultTransportは `execute(num_retries=0)` を使い、自動再送を抑止する。

## 検証記録：実Googleと合成を区別

| 検証 | 実Googleでの結果 | 自動合成検証／未確認範囲 |
|---|---|---|
| 入力→登録→結果 | G101の登録 1を取得、backend検証、同じCセルへ「登録検証済み・未記帳」、exact読戻し成功 | 金融write/authority 0 |
| 医療の手入力 | M101の金額空欄を保留。その後の実Sheet上の金額135・登録2を取得し、登録検証済み・未記帳を反映・exact読戻し成功 | 編集に使われた端末と原本往復/再起動の実機結果は未確認 |
| プルダウン・再計算 | ネイティブTableの入力中/登録1〜9をPC実ブラウザーで確認。引用符・改行を含む数式が実Googleで計算成功。Google Web画面も390pxで確認したが結果列右端は横移動が必要 | CIの390pxは既存HTML fixtureの視認性試験。Google Web/fixtureのどちらもネイティブiPhone Sheets実機の代わりにはしない |
| 二重取得・再起動 | 再読取り/プロセス再起動後もG101下書きは1回、既存結果を照会 | 連続操作・重複・古い番号は回帰試験 |
| 同期中の編集 | 2回の実読取りの間に合成メモをAPIで編集、取込前にconcurrent_edit拒否。Googleも競合表示。値を条件付き復元 | 同一ownerのAPI編集。2台/2アカウントの同時編集、オフライン復帰は実機未確認 |
| UNKNOWN保持 | 故障専用ブックにUNKNOWNを表示。登録2へ変えて再起動しても下書きwrite0、pending再送0 | 発端は合成native timeout注入。実Googleの回線断を再現した証拠ではない |
| 送信結果不明 | 実処理はintent/claim/読戻しを使用 | response喪失・未反映・再起動を合成試験。実Google outage/connector内部retryは未確認 |
| 原本差替え・ACL・構造・別案件 | 実原本の現revision/2枚のbytes一致・実ACLを取得 | 差替え/ACL変更等は合成負例。本番資源や実ACLを変更して試していない |

CIはGoogle credentialなしで実行し、実Google成功の証拠には数えない。
関連core/回帰/ブラウザーは独立Draft PRのUbuntu workflowで実行する。

## 合成checkpointの容量と安全停止

既存PersistentJournalの700k上限は維持する。フルsnapshot履歴により、有限試験でも容量上限で停止することを実際に確認した。
取込journalと結果配信journalを別のDRAFT_ONLY domainに分け、どちらも既存エンジンを使用する。
既存履歴を削除せず、上限超過を自動リトライしない。

試験harnessにだけ、**明示的なローカル合成checkpointのarchive操作**を用意した。
許可configと現在checkpointのSHA256一致が必要で、旧checkpoint全bytesをcreate-only保存・fsync・hash読戻しし、
現在の全receipt/detail/confirmation/UNKNOWNを新世代の既存migration APIへ引き継ぐ。
再起動時に全archive hashを照合する。自動世代更新・履歴の消去・UNKNOWNのresetではない。
checkpoint 2MB・archive4世代の上限も維持する。
この試験用ファイル/鍵保管は本番の耐久CAS、分散排他、保存/失効管理を解決しない。

## 接続試験を再開する人向け

private config/checkpoint・合成鍵・raw snapshot・profile/ACLはGit/CIに保存しない。
既存connectorを使えるセッションで、以下の手順を実行する。credentialの追加や共有変更は別承認を得る。

1. pinned configのbook/sheet/原本inventory/owner証跡を読み、独立したfresh captureを2回取る。
2. `tests/live_google_sheets_probe.py <private-directory> <request.json>` に
   `op:poll, first, second, sources, second_sources, profile, acl` を渡す。
3. response.errorなら書込みしない。pendingがある場合、もう一度fresh captureして
   `op:ready, ids, raw, sources, profile, acl` を渡す。
4. checkpoint保存が成功したresponse.requestsだけを同じbookへ**一度だけ**送る。
   timeoutなら再送せずfresh readとpollで照会する。Googleの表示だけで成功扱いにしない。
5. `op:inspect` で結果・outbox・合成write数を照会。native/金融writerは接続しない。
   archiveはconfigの `allow_checkpoint_archive:true` と
   `op:archive_checkpoint, expected_checkpoint_sha256` を明示した試験操作のみ。
   `fault_poll` は故障ブックの `allow_fault_injection:true` のときだけ許可する。
   旧形式の `abandon_unsent` は同じ許可config・現在checkpoint SHA・
   `verified_never_dispatched:true` に加え、別途の確実な未送信記録が必要な試験保守操作。
   Googleで結果が見えないことやtimeoutを未送信の根拠にしてはならない。

## iPhone実機の受入手順（今回未確認）

常駐workerはないため、backend試験セッションの稼働中に行う。稼働外に入力しても即時結果は出ない。

今回の入力試験は、金額空欄/登録1で保留→金額135/登録2→登録検証済み・未記帳まで実Googleで完了した。
受付はG101 `3b01166a`、M101 `17e6938b`。現ブックでは再登録せず結果を照会する。

1. 上の「iPhone受入」ブックをGoogle Sheetsアプリで開く。確認タブでSYN-M101を検索する。
2. 金額 `135`、B「登録 2」、C「登録検証済み・未記帳」と受付 `17e6938b` を確認する。
3. 原本リンクから**合成PNG**を見て戻り、入力・案件ID・縮小率が保たれるか確認する。
4. アプリを閉じて開き直し、入力・原本リンク・結果が保持されるか確認する。
   案件ID列の固定、横移動、プルダウンの誤タップ、文字切れも確認する。

入力→登録の新規実機再現は、既存receiptを再利用せず、別途準備した未登録の合成ケースで稼働中のbackendと行う。
必要項目を入力して同期後、標準プルダウンの次の登録番号を選ぶだけで、別の本人承認操作は不要。
保留/競合/UNKNOWNならそのまま報告し、何度も登録番号を進めない。

実機報告がなければ合格にしない。故障ブックのUNKNOWNを通常登録へ戻さない。

## 本番までに残る最小工程

1. iPhone実機で上記を完了し、オフライン復帰/2台編集/Google通信失敗時の体験を確認する。
2. 常駐workerの実行場所・既存Google credentialと最小scope・ACL・失効時停止を具体化し、必要な追加権限を個別承認する。
3. 実backendの耐久CAS・分散lease/旧writer排他・journal容量/鍵/retention/復旧・pending/UNKNOWN照合を検証する。
   Google revisionとnative正本を独立にbindする。今回のmemory-native aliasは本番identityに使えない。
4. 事前許可の対象/保存先/実行主体/上限と既存writerの契約を検証し、既存金融gateを維持したまま接続案をレビューする。
   銀行/PayPay/Amazon自動記帳の実運用試験や新たな自動経路は今回追加していない。
5. main merge、実backend/本番接続、Cloud/IAM、金融write/authority、validated SHA、有効化は別承認。

従来4処理・新R1のNO-GOを維持する。Variables/Secrets/IAM/validated SHA/本番Sheets/原本/main/既存PRは変更しない。
