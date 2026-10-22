# 合成runner：共有CASと永続checkpoint

基点は #160 final head `d109c849655c13663a880ab3f5dcbcf43fef0a56`。
独立Draftの追加範囲は保存・復元の接続、オフライン故障試験、資格情報を使わないUbuntu CI。
従来4処理・新R1は本番NO-GO。金融write、authority発行、原本移動、医療AI送信、
本番gate/Variables/Secrets/IAM/validated SHA/main/既存PRは変更しない。
日常操作は既定のGoogle Sheets「確認」1タブ。取引ごとの本人承認は追加しない。

## 選択と既存資産

| 方式 | 安全性 | 費用・保守 | 判断 |
|---|---|---|---|
| Actionsローカル/cache/artifact | 次回の正本と最新世代を保証できない | 簡単だが秘密データ公開・巻戻しの懸念 | 不採用 |
| Driveだけのread→compare→write | 独立worker間の原子的CASではない | 既存保存先は再利用可能 | CASとして不採用 |
| checkpoint全体をFirestoreに保存 | ドキュメントサイズと履歴増大に抵触 | cipher・保存基盤の重複も不要 | 不採用 |
| 既存Firestore CAS＋既存Drive transport | 条件付きtipと暗号化本体を分離できる | 新サーバー・新DB不要、少量の既存API利用 | 採用 |

CASは `auth-session/baseline.json` のhash固定backendの
`FirestoreConditionalState.read_versioned/replace_versioned`。
`LastUpdateOption(update_time)`、`retry=None`、10秒timeout、既存payload/ETag照合を再利用する。
新しいCASエンジンやFirestore SDKの独自更新は追加しない。
`ConditionalControl` はrun claimと小さいpointer履歴を暗号化して保持する。
`DriveStateTransport` は固定ID・親folder・JSON MIME・編集可能性の検査と
`num_retries=0` の単発uploadを再利用する。通常のread/compare/writeをCASと呼ばない。
`DurableState` の金融処理用envelope/restoreは用途・ファイル形式が違うため流用しない。
`EncryptedDraftIO` を240KB単位で再利用し、既存700KB制限を緩和せず大きいcheckpointを暗号化する。
native/deliveryの両 `PersistentJournal._state()` が履歴・receipt・scopeを検証する。
既存probeのcheckpoint復元と既存runnerのpoll/ready/publish/read-backを引き続き使用する。

Firestore公式上限は1 document 1MiB。既存backendは750,000 bytes、暗号化IOは700,000 bytesで先に停止する。
CASにはcheckpoint本体・鍵を入れず、run_id/status/pending/制御revisionと
checkpointのgeneration/plaintext bundle hash/ciphertext hash/checkpoint hash/size/累積sizeだけを保持する。
[Firestore上限](https://docs.cloud.google.com/firestore/quotas)。

## 保存順序と復元

1. 初回は別承認でfreezeした既存checkpointを明示migrationする。runtimeにはcreate/set/initializeはない。
   checkpointなしの `UNINITIALIZED` を共有正本にしない。欠損時に新しいjournalを作らない。
2. 空の一時directoryにのみ復元する。CAS tip、指定世代のcipher hash/size、暗号化scopeとchunk順、
   bundle/config/checkpoint hash、native/delivery journalのschema・receipt履歴を検証する。
   本体読取後にCAS tipを再読し、一致しなければ停止。ディスク上の旧コピーを正本にしない。
3. 起動時のRUNNING/PENDING/UNKNOWN、native UNKNOWN/ATTEMPTING、未送信/送信中outboxは自動解除しない。
   UNKNOWNは経過時間・次のslotでも復帰しない。結果照会は復元コピー上の既存inspectで行う。
   元のCASとciphertextは変更しない。結果が見つからないことを未実行の証拠にしない。
4. 同じCASを使う全workerがrun全体をclaimする。期限で盗まず、phase間もownerを維持する。
   各backend/publisher呼出し前に既存runnerがPENDINGをCAS確定する。
5. backend後、新checkpointを次の未使用Drive slotへ暗号化保存する。
   新しいGETで完全なciphertextを独立read-backし、exact一致を確認する。
   旧receiptの消失、履歴prefixの巻戻し、journal鍵/scopeの差替えを拒否する。
6. そのACKが得られて初めてCASのcheckpoint generation/hashを更新し、次phaseへ進む。
   readyの保存ACK前にはpublishしない。publisher応答喪失時にpublishを再送しない。
7. Drive保存後の死、保存応答喪失、CAS直前/直後の応答喪失ではRUNNING/PENDINGを残す。
   同じworkerも不確かな書込み後はread-only latchで追加CAS書込みを禁止する。
   未確定slotはorphanとして保持し、自動採用・上書き・削除しない。

Drive slotはあらかじめ固定IDで2〜128個用意し、初期内容は空bytes。
実行時に新規ファイルを作らない。世代gはallowlistのg番目だけを使う。
空slot検査の安全性は共有run claimに依存する。全workerがこのprotocolを守ることが条件であり、
Drive自体のcreate-only/immutable APIを実装したという主張ではない。
slot/folderはowner-onlyのMy Drive、effective permissionsもowner一人、共有Drive/anyone/group/追加userを拒否。
親folder・MIMEの検査は既存transportが行う。権限検査の欠落・失効も停止する。
外部管理者がCAS自体を古いbackupへ巻き戻す操作までは検出できない。
CASへの管理者restore/deleteと旧writerによる迂回を運用上禁止し、災害復旧はfreeze＋別承認が必要。

## 上限、監督、接続口

- 制御履歴128版、累積ciphertext 8,000,000 bytesで停止。checkpoint 1,600,000 bytes、
  config 500,000 bytes、chunkは既存暗号化IOの700,000 bytes以下。
  全世代を残すので容量が来れば停止し、自動prune/archive/鍵更新で継続しない。
- 既存runnerは最大180秒/3cycle、既定120秒。`supervise(command, max_seconds=120)` が
  **保存・復元を含むworker全体**を別processとして監督し、180秒以下で子process treeを終了する。
  終了時にparentがclaimを解除する処理はない。SDK HTTP timeoutも10秒以下に設定する。
  `run_persistent` 単独ではblocking SDKを中断できないため、実hostは必ずsupervisor下で起動する。
- 新しい `staging_shadow/persistent_registration.py` は認証・ADC・env token探索を行わない。
  認証済みの既存FirestoreConditionalState、Drive service、鍵、trusted scope、固定slot allowlistを注入する。
  scopeには既存mode/purpose/instance/ownerのほか `config_sha256` を固定する。

```python
# 接続例。資源作成/認証/実行を行うスクリプトではない。
slots = DriveCheckpointSlots(drive_service, reviewed_slots,
                            owner_permission_id=reviewed_owner_permission_id)
custody = PersistentControl(existing_firestore_conditional_state, slots,
                            checkpoint_key, trusted_scope, empty_private_directory)
result = run_persistent(custody, bounded_synthetic_port_factory,
                        enabled=True, max_seconds=120, max_cycles=2)
# このworker全体をsuperviseで監督する。実Google client factory/dispatchは未選択。
```

`port_factory` は既存probe/native adapterを呼ぶboundedな合成port。
本体保存ACKは `persist_checkpoint`、復元pending検査は既存portと新journal検査の両方へ接続する。
Google Sheets通信は既存のraw readと `validate_writes` の結果C列/依頼B列validationだけ。
既存 `GoogleResultTransport.publish()` はreadyと送信を連続実行するので直接呼ぶとcheckpoint barrierを挟めない。
実portでは既存readyで得たrequestを保存ACK後に単発batchUpdateし、再読取する構成を使う。
金融writer/authority/原本move/外部AIのportは注入しない。

## 独立workerの証拠と限界

`tests/persistent_runner_worker.py` の各workerは独立process・別の空directory。
旧実行directoryをコピーせず、同じ合成serverのCAS/Drive slotを読んで復元する。
合成serverのSQLiteはテスト用のatomic update_timeモデルであり、新しい製品DB/保存基盤ではない。
実際に呼ぶCAS実装はhash固定のFirestoreConditionalState。

| 試験 | 確認内容 |
|---|---|
| 正常→新process | 処理成功、次回backend呼出し0、checkpoint/native・delivery ciphertext同一、receipt/history保持 |
| 同時claim | 2 processをbarrierで同期、更新条件を通るworkerは1つ、敗者は処理しない |
| 保存直後の死 | os._exitで即死、orphanは残る、CASはPENDING、次回呼出し0 |
| CAS直前/直後の応答喪失 | 旧世代PENDING/新世代RUNNINGを照合し、どちらも再送0 |
| checkpoint巻戻し/欠損 | CAS tipの世代・cipher hashと一致せず復元前に停止 |
| UNKNOWN/ATTEMPTING | byte単位で保持、時刻経過で解除せず次回呼出し0 |
| 容量/権限/保存失敗 | 超過前停止、削除/再初期化0、既存Drive transportへのfake契約も試験 |
| 再起動receipt/history | 両journalを既存validatorで検査し、古いreceipt/historyの欠落を拒否 |

Ubuntuの新PR専用CIは外向き通信不能なnetwork namespaceで上記と既存runnerを実行し、
core/既存機能回帰も実行する。公開CIに渡すのは完全合成値だけ。
artifactはJUnit XMLのみ。token/鍵/checkpoint/SDKエラー/snapshotを出さない。
実Firestore/Driveのdurability・IAM・quota・OAuth失効・実ACLを証明したとは扱わない。

## 実Google認証の準備（今回は変更しない）

専用OAuthクライアント・専用合成scopeを使い、connector owner権限をworker権限と見なさない。
第一候補は `https://www.googleapis.com/auth/drive.file` と、既存profile検証が必要な最小の
`openid email`。Sheets API自体もdrive.fileを受け入れるため、最初から全Spreadsheet/全Drive権限を要求しない。
[Drive scope仕様](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)、
[Sheets scope仕様](https://developers.google.com/workspace/sheets/api/scopes)。

drive.fileはその**アプリ**が作成/ユーザーが選択したファイルへのアクセス。
既存ブック/原本のIDを知ること、同じGoogleユーザーであること、folderを選ぶことだけで
既存子ファイルまで全て読めるとは仮定しない。別承認後に次を実測する。

1. 新クライアントで対象の合成ブック、合成原本各ID、checkpoint folder/各slotを明示選択する
   （Google Picker等のアプリへのファイル付与）。既存connectorのgrantが移るとは仮定しない。
2. read-only preflightでprofile、files.get metadata/version/hash、permissions、原本get_media、
   Sheetsの書式を含むraw read、checkpoint各IDのreadとcapabilities.canEditを確認する。
   Picker未選択の対象/allowlist外/本番IDを拒否するnegative testも行う。
3. 書込みは後述の合成専用承認後、slot upload/read-backとSheets結果セルの単発反映を別々に試す。
   drive.fileで403/404になる場合はscopeを自動拡大せず停止。原因・代替と追加承認を提示する。

無人refreshは `access_type=offline`。refresh token、client ID/secret、checkpoint Fernet鍵を別管理する。
OAuth tokenは既存本番tokenを流用せず、ローカルprivate保存から承認後に専用Secretsへ登録する。
失効はGoogleのアプリgrant/token失効＋専用Secrets削除、実行停止、CASと全ciphertext保持。
401/403/invalid_grant時に新しい認可や共有変更を自動実行しない。
外部アプリTesting状態のrefresh tokenは通常7日で失効するため、単発試験と継続運用を分けて評価する。
[offline refresh](https://developers.google.com/identity/protocols/oauth2/web-server)、
[token有効期限](https://developers.google.com/identity/protocols/oauth2/)。

FirestoreはDrive OAuthと別の認証/認可境界。既存server SDKはSecurity Rulesを迂回してIAMを使う。
必要なruntime権限は対象状態のget/update。初回createは別のoperator作業で、runtimeには不要。
**ドキュメントID allowlistがIAMのドキュメント単位制限になると主張しない。**
既存の隔離済み合成database/既存principalで本番到達不能を確認する。
本番databaseにしか権限を与えられない場合は不十分として停止し、IAM分離案を別承認に出す。
新DB/新サービスアカウント/秘密鍵発行/WIF設定を今回追加しない。
[Firestore server IAM](https://docs.cloud.google.com/firestore/native/docs/security/iam)。

GitHub接続案は専用保護environment `synthetic-runner` のSecrets
`SYNTHETIC_GOOGLE_OAUTH_JSON`、`SYNTHETIC_CHECKPOINT_KEY`、承認された既存Firestore認証の参照。
trusted manifestはbook ID/sheet ID/source ID＋hash/version、folder/slot ID、owner、
Firestore project/database/document、scope/config hash、検証済みcandidate SHAを固定する。
本番のvalidated SHAやVariables/Secretsは流用・変更しない。
将来のmanual workflowは固定したレビュー済みSHAだけをcheckoutし、PR入力のref/codeを実行しない。
pull_request/pull_request_target CIやforkにはOAuth/秘密鍵を渡さない。
Secretsはログやartifactへ出さず、RUNNER_TEMPのprivate fileへ必要時のみ書き、終了時に削除する。
必要ならenvironment承認を単発試験の起動時に置く。これは取引ごとの本人承認ではない。
今回追加したworkflowにはdispatch/schedule/environment/secrets/vars/id-token権限はない。

## 資源・費用・復旧と次の手順

今回作成したGoogle資源・新サーバー・新DB・OAuth認可・Secrets・IAM・共有変更は0。
別承認で必要なのは、既存の隔離済み合成Firestore内の**新しい専用document 1つ**
（payload＋将来の明示expires_at、runtimeによる延長なし）、private Drive folder 1つと
固定IDの空JSON slot 2〜128個、専用鍵、既存合成book/sourceへのアプリgrant。
既存承認済みブック/原本/本番documentを保存先として上書きしない。
既存の安全なfolder/空slotが既にあるなら再利用前に同じ検査と承認を行う。
正確な資源ID・現在のIAM/ACLは未取得なので、作成や書込み前にprivate manifestとして提示する。

固定の追加hosting費用はない。Driveは既存storage枠を最大8MB（旧revision等のサービス側保持分は別）消費し、
FirestoreはAPI read/writeと小さなdocument保存量が増える。
無料枠内なら追加0円の可能性はあるが、共有project使用量/region/billing未確認のため0円保証はしない。
TTL/PITR/backup等の有料機能は新規有効化しない。
[Firestore料金](https://cloud.google.com/firestore/pricing)。
最初は一回の手動試験のみで、常時pollingや定期workflowは有効化しない。

復旧は全worker停止→CASの現版/ETag、全ciphertext/orphan、鍵、Sheet実表示と既存receiptをprivate退避→
結果照会と独立照合→証拠に結び付く明示承認の順。
同じ世代の上書き、orphanの自動採用、UNKNOWNの自動解除、古いcheckpointへの戻しは行わない。
破損した正本は保持し、復旧が必要なら新しいreview済みscope/document/slotへcreate-only migrationする。
鍵喪失・CAS全体rollback・本番到達不能を証明できない場合は継続せず停止する。

残工程は次の順。コード合格と実Google有効化を混同しない。

1. 独立Draftのオフライン/Ubuntu CI結果をレビューする。
2. privateな実資源manifest、既存FirestoreのIAM隔離、権限、予算上限、復旧担当と手順を提示し別承認を得る。
3. 承認後に専用OAuth/最小scopeのread-only preflightとdrive.file到達性を確認する。
4. 別承認後にdocument/slotの初回provision・read-backとSecrets接続を行う。
5. 既存Google reader/result request契約を使う実port/client compositionと、固定SHAの**手動のみ**workflowを
   private manifestへ接続し、supervisor下で一度実行する。実Googleport選択とmanual workflowは今回は有効化しない。
6. 正常→別Actions実行環境の復元、同時claim、故障・失効、原本/入力/表示保持を実backendで実測する。
   次回の1回の試験だけでは、全実backend故障ケースの合格までは主張しない。

ユーザー承認が必要なのは実Firestore/Drive書込み、対象資源作成、OAuth認可/アプリへのfile grant、
Secrets/IAM/共有変更、実Google manual実行。main merge、定期有効化、金融接続も別承認。
合成の証拠が揃わない限り実Googleへの手動試験へ進めない。
