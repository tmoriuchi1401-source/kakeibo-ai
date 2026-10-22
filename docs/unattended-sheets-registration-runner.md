# 無人登録runner：既定OFF・オフライン合成専用

## 今回の到達点

#159 head `8aa665f1903df3548a6efe9fb34f91c53a7474a9` から独立開発する。
日常操作は **入力 → 登録 n → 自動処理 → 同じ確認タブで結果確認**。
Safari確認画面や取引ごとの本人承認は追加しない。
今回は無人実行の制御・復元・publisher接続を合成環境で実装する。
**実Google認証、実Google書込み、schedule設定、有効化、main mergeは行わない。**

PR #159の説明だけに、次のユーザー報告を正式記録した。同PRのコード/head/CI 1,072件成功は変更しない。

| iPhone実機受入項目 | 判定・証拠 |
|---|---|
| Google Sheetsアプリを完全終了して再起動 | 合格・ユーザー報告 |
| SYN-M101の金額135と登録2の保持 | 合格・ユーザー報告 |
| 登録検証済み・未記帳の結果保持 | 合格・ユーザー報告 |
| 合成原本を開いて戻った後の内容保持 | 合格・ユーザー報告 |

既確認の入力・原本往復・登録操作と合わせ、今回の対象範囲は受入完了。
通信断からの復帰・2台同時編集・実金融記帳は未検証。
端末ログ、機種、OS/アプリバージョンは未取得であり推測しない。

## 推奨：既存GitHub Actionsで30分ごとの短時間起動

このrepositoryは公開であることをread-only metadataで確認した。
標準GitHub-hosted runnerの公開repository実行は無償。private化・larger runner・保存料金まで無料と仮定しない。
既存Python backend・CI・運用基盤を再利用でき、新しい常駐サーバーを不要にする。
[GitHub料金の公式仕様](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。

| 候補間隔（未設定） | 30日/24時間の起動回数 | 入力時刻が均等なら通常の平均待ち | 評価 |
|---|---:|---:|---|
| 15分 | 2,880 | 約7.5分＋起動/処理 | 待ち時間短縮を優先する場合 |
| **30分** | **1,440** | **約15分＋起動/処理** | 最初の低保守案 |
| 60分 | 720 | 約30分＋起動/処理 | 依頼が少なく待ち時間を許容する場合 |

これは計算上の目安で、実測SLAではない。GitHub scheduleは混雑時に遅延・欠落し得る。
default branch上のworkflowが必要であり、Draft branchに置いただけでは定期起動しない。
将来承認後は毎時先頭を避ける案とし、欠落時も古いslotをbackfillせず次回に現在の依頼を読む。
[GitHub scheduleの公式仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

比較：Apps Scriptの時間トリガーは軽量だが、既存Python validator/CASをそのまま実行できない。
安全な既存backend endpointがない現状では新しい中継・認証やロジック二重化が増えるため今回は採用しない。
実行時間等の[Apps Script quota](https://developers.google.com/apps-script/guides/services/quotas)も必要となる。
既存PCの定期起動は第3候補。ローカル永続ディスクを使えるが、電源/スリープ/更新/資格情報保管の負担がある。
新サーバー・有料サービスは導入しない。

## 実装の分担と再利用

- `staging_shadow/registration_runner.py`: 既定OFF、時間上限1～180秒（既定120秒）、最大3cycle（既定2）、
  同一slotの重複起動抑止、停止、全runのCAS claim、phaseごとのPENDING、checkpoint保存ACK、結果照会への停止。
- `Control`: 既存OS排他、fsync/atomic replace、EncryptedDraftIOを再利用したローカル暗号化制御記録。
  create-onlyの連番履歴とHEADを照合し、古いHEAD・欠落・中途書込み・改ざんを拒否する。
- `ConditionalControl`: 同じ制御契約を既存 `read_versioned/replace_versioned` IOへ接続する差込み口。
  既存hash固定のFirestoreConditionalState実装をAtomicDocument fakeへ注入して検証する。
  新DB、Firestore Client、IAM、実documentは作らない。
- `tests/offline_registration_runner.py`: #159のprobe、PersistentJournal、polling、adapter、原本版検証、
  publisherのbounded requestsを再利用するオフラインcomposition。標準入力/環境からGoogle tokenを探索しない。
  実Google用のconfigは拒否し、Google clientを作らない。

Sheetsの結果publisherは #159と同じrequest検証を使う。今回はfake Sheetへ適用する。
#159のGoogleResultTransportは保持し、実Google用factoryを追加しない。
既存本番gate、旧writer、元UI、本人認証hostを変更しない。

## 起動・排他・停止・結果の契約

1. `--enable-offline` がなければ、保存先やcredentialを読む前にDISABLEDで終える。
   明示provision済みの合成scope/key/configがなければ初期化せず停止する。
2. 既存のOS lockで単一ホストの複数processを排除する。共有CAS利用時はrun全体をRUNNINGでclaimする。
   phase間もclaimを保持し、別run_idのworkerは現在版を知っていても解除できない。時刻切れによるlock stealingはしない。
3. 起動時にcheckpoint hashを照合する。前回のRUNNING/PENDING/UNKNOWN、backendの未完了依頼、未送信/送信中publisher intentがあれば照会のみ。
   #159にある試験用archive/abandon操作をrunnerから呼べない。pendingを自動解除・再送しない。
4. fresh captureを2回取得して一致を確認する。競合は取込前に保留。
   全入力・原本版・権限・表示が既観測と同じならUNCHANGEDで、backend/journalを書き換えない。
   100個の後続slotで未変更時に制御履歴が増えないことを検証する。
5. backend呼出し前にPENDINGを永続化し、別processで既存検証を実行する。
   backend成功後もcheckpointの保存・exact読戻しが成功するまで次の工程へ進まない。
   **特にready後のcheckpoint ACKが不明ならGoogle publishへ進めない。**
6. 結果intentを単発claimしてbounded publisherへ渡し、同じSheetをfresh取得して既存pollで読戻しを確認する。
   native検証結果とSheets表示の配信結果を別々に保持する。
   完了表示は「登録検証済み・未記帳」であり、記帳完了ではない。
7. worker subprocessへ残り時間を渡し、期限超過は終了させる。遅れて起動したworkerもbackend IO前にdeadlineを検査する。
   強制終了・応答喪失・不明なCAS/保存はUNKNOWNへ停止。job自体が落ちてもRUNNING/PENDINGが残り、次回に再実行しない。
8. STOPファイルを各工程前に確認し、安全な境界でSTOPPEDにする。実Google失効に対応するfake認可検査も各工程前に行う。
   REVOKED/STOPPED/CAPACITY/UNKNOWNは自動復帰しない。UNKNOWN/REVOKED/CAPACITYはCLI exit 3としてjob失敗を通知する。

金融writer/authorityは存在しない。医療は完全手入力、AI送信0。
一般明細カテゴリを推測配賦せず、PDF/旧保留を金融確定しない。

## 耐久性・容量・保持・復旧の証明範囲

ローカルcheckpointは既存のfsync + replaceを使う。controlの旧ciphertextを削除せず128版/8MBで停止する。
既存journalの700k、checkpointの2MBを緩和せず、runnerは80%のhigh-waterで先に止まる。
無変更pollingで履歴を増やさないが、新しい依頼が増えた場合の本番retentionは未完成。
自動prune、archive、鍵更新、UNKNOWN解除は実装しない。

privateな完全コピーを空の合成directoryへ復元し、control/checkpoint/pendingを保持した再起動を試験する。
結果照会は #159 inspectを**コピー上**で実行し、fenceされた元checkpointを変更しない。
古いHEADの復元は新しい連番履歴が残っていれば拒否できる。
**全directoryを古いbackupへ巻き戻す攻撃/事故は、外部の信頼できる最新tipなしには検出できない。**
この点とActionsのephemeral diskはローカル試験で解決したとしない。

実Actionsで使うには、既存のconditional IOへ同一scopeのrun claimを置き、private checkpoint bundleを
そのclaim下で保存・読戻しする必要がある。既存FirestoreConditionalStateまたは検証済みDrive conditional IOを優先し、
既存DriveStateTransportの単純read/compare/writeを分散CASだと扱わない。
`ConditionalControl`だけを接続してもcheckpointがローカルだけなら不合格。
そのための `persist_checkpoint(budget)` 契約を、publisher前の必須工程として実装した。
cache/artifactを正本にしない。鍵・snapshot・journalは公開CI成果物へ出さない。
[GitHub cacheの公式仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)。

## CIと操作例

新しいPR専用workflowだけを追加する。schedule/dispatch/Secrets/Variables/environmentは参照しない。
既存production workflowは一切変更/dispatch/再実行しない。
CI groupは `kakeibo-offline-registration-runner-${{ github.ref }}`、cancel-in-progressはfalse。
合成runner試験はloopbackしかない別network namespaceで実行し、外部通信不能をassertする。
旧writerと共有排他が証明できたとは扱わず、金融NO-GOを維持する。

将来の定期workflowはPR用groupを流用せず、固定したbook/scope単位のgroupと共有CAS claimを両方使う。
Actions concurrencyだけでは、別workflow・手動CLI・旧writerの共有排他は証明できない。

以下は**オフライン合成専用**。新しいprivate directoryだけにprovisionし、既存#159の実Google用directoryを渡さない。

```text
python tests/offline_registration_runner.py run <private-directory>
# DISABLED（既定）
python tests/offline_registration_runner.py provision <new-private-directory> --enable-offline
python tests/offline_registration_runner.py run <private-directory> --enable-offline --max-seconds 120
python tests/offline_registration_runner.py inspect <private-directory> --enable-offline
python tests/offline_registration_runner.py stop <private-directory> --enable-offline
```

正常、再起動、同一slot/未変更slot、複数process、CAS競合、checkpoint保存失敗、応答喪失、UNKNOWN、
権限失効、STOP、同期競合、容量、HEAD rollback、空directory復旧を検証する。
Ubuntu CIの結果は独立Draft PRへ記録する。実Googleの無人成功の証拠には数えない。

## 次の承認対象：今回は実施しない

connectorのowner権限をworkerへコピーできるとは仮定しない。第一候補は、既存OAuth clientの適格性を確認したうえで、
同じ所有者が**初期設定だけ**で許可するOAuth refresh token。取引ごとの再認証は不要。
connector tokenの抽出・流用はしない。

| 対象 | 必要な権限/操作の最小案 | 影響・rollback |
|---|---|---|
| Google認証 | `drive.file` を第一候補に、専用確認ブックと固定合成原本inventoryをそのOAuth appへ個別許可。worker identityも初期設定で固定 | 別appの既存fileへ自動的にアクセスできるとは仮定しない。許可取消・refresh token失効で停止 |
| 対象ブック | #159の合成専用ブック `1Ls3kMi_4Ox0zm2SOI-9QNYwOF01rBvksFymH2YvAmdU` の確認タブ。原本は固定inventory7件のreadのみ | 原本/入力/金融ledgerへのwriterを渡さない。schedule停止と権限失効で止め、結果不明は照合する。成功を装う巻戻しなし |
| 資格情報保管 | 新しい合成worker専用Secretsの候補：OAuth client情報、refresh token、state暗号鍵。既存本番Secretsは再利用しない | 作成前に名前/scope/保管場所を確定して個別承認。追加分だけ失効・削除、状態ciphertextは保持 |
| 永続state | 既存CAS backendの専用DRAFT_ONLY domainとprivate bundle保存先。資源は未作成、IDは承認後に固定 | create-only provision/読み戻し、別scopeからの拒否、stale/応答喪失を合成専用実backendで先に検証。消して再初期化しない |
| Actions有効化 | レビュー済みコードの別承認merge、main専用の新しい合成workflow、固定scope、既定OFF、時間上限、30分案 | 既存production workflow/validated SHAを変更しない。新workflowをdisableし、追加tokenを失効。pendingは保持 |

`drive.file` は選択済みfileへのcreate/edit/delete能力も含む。API scopeだけで結果セル限定にはできないため、
allowlistとbounded publisherが必要。Sheets scopeはタブ/セル単位に狭められない。
原本revision/bytes/ACLとSheets APIが必要scopeだけで取得できるか、承認後のread-only試験で確かめる。
[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)、
[Sheets scopes](https://developers.google.com/workspace/sheets/api/scopes)。
service accountを選ぶ場合は別identityになり、#159のowner-only ACL判定をそのまま通せない。
既存本番SA流用・domain-wide delegation・IAM/共有変更はせず、必要なら別途設計/承認する。

## 実Googleで自動登録できるまでの最小工程

1. 本Draftのrunnerと制御契約をレビューする。iPhone UIの作り直しは不要。
2. 上の認証/保存先/権限/rollbackを確定して個別承認を得る。
3. private checkpointの実保存・CAS claim・失効停止・復元/retentionを合成専用の実backendで検証する。
   暗号鍵をbundleやCI artifactへ同梱しない。bootstrap/再provisionで過去receiptやpendingを消さない。
4. 実Google compositionを別承認で接続し、最初はscheduleなしの短時間手動起動で、入力/原本版/結果読戻し、通信失敗、重複を確認する。
   offline modeを偽装してlive clientを注入しない。
5. 段階4の成功と待ち時間/保守負担を確認後、定期間隔・有効化を別承認する。
   この段階でも合成専用・金融未接続。旧writer排他、native pending照合、実金融記帳は別工程。

従来4処理・新R1はNO-GO。金融write/authority/原本移動/医療AI送信、本番Variables/Secrets/validated SHA変更、
本番workflow操作、main/既存PR head変更、未知pending解除・自動再送は行わない。
