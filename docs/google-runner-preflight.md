# 実Google合成試験の接続前検証

基点は #161 final head `62173ead3d7a1e1e58e27eb0a2ee9c16f3bbe331`。
本番4処理・R1はNO-GO。今回の追加は固定manifest検証と、承認済みserviceを注入した
メタデータGET診断だけ。認証、token探索、初期化、Firestore RPC、Drive本文取得、
Sheetsセル取得、write、workflow dispatchを行うfactory/CLIは追加しない。
実資源のID・ACL・IAM調査結果はローカルprivate報告で管理し、公開CIへ渡さない。

## #161の安全境界

既存FirestoreConditionalStateのupdate_time条件付き更新が唯一の共有CAS。
Driveのread/compare/writeはCASではない。暗号化本体の別GETによる完全read-backが
成功してからCASへ世代/hashを確定する。復元は空directoryに限定し、CAS再読、
世代/hash、両journalのreceipt/history、pendingを照合する。
RUNNING/PENDING/UNKNOWN、ATTEMPTING、結果不明のpublishは時間経過で解除・再送しない。
保存応答喪失で同じinstanceの追加更新を止め、孤立slotを保持する。
128制御版・累積ciphertext 8MB・全worker最大180秒の制限を維持する。
CAS全体の管理者巻戻しやprotocolを迂回するwriterはコードだけでは防げない。
実効IAMと書込み主体の隔離は実接続前の必須条件。

## 新しいread-only診断

`staging_shadow/google_runner_preflight.py` は既定でDISABLED。
`validate_manifest` は資格情報を含まないprivate JSONを検証する。
candidate SHAは別のtrusted入力との一致が必要。book ID/親/sheet ID、原本の
case/file/親/MIME/hash/version、専用OAuth client、owner email/permissionId、slot固定ID、
明示project/named database/document/runtime principalを固定する。
default database、ADC projectへのfallback、ID重複、入力と保存先の重複、未知flagは拒否。
最初の1件試験は8空slot（初期＋最大5回の変化＋予備2）を準備する。
実runnerの共通上限は引き続き2〜128slotであり、診断の8slot要件は今回の試験計画用。

`ReadOnlyPreflight.run(..., enabled=True)` に渡すserviceは承認後に別途構築する。
実際の認可結果から取得したclient IDとgranted scopesを渡す。自己申告manifestを
認証証拠にしない。Drive about.userのpermissionId/emailとowner-only ACL、
固定親/MIME/未削除、bookのcanEdit、唯一の「確認」sheet IDをmetadataで照合する。
slot検査は既存DriveCheckpointSlots/DriveStateTransportを再利用する。
API失敗・権限失効・構造差替えは固定エラーで停止し、生SDKエラーを公開しない。

成功時も必ず `METADATA_VERIFIED_NO_GO` / `runner_write_allowed=False`。
Firestore実効隔離・保存復元試験が未証明のままGOへ変換する経路はない。
原本hash/version未取得・storage未作成・初回owner permissionId未確認は明示blockerとする。
初回観測のpermissionIdはprivate返却値をレビューしてpinし、同じemailでもIDが変われば停止する。
返却する観測値もprivateなローカルファイルに限定し、ログ/artifactへ出さない。
同意・scope・所有者照合とFirestore IAM検証は異なる境界である。

## OAuth計画の更新

第一候補は専用Desktop OAuth clientと **drive.file単独**。
#161の `openid email` 併用案は、desktop/mobile Pickerフローでは使用しない。
公式desktop/mobile Pickerは他scopeとの併用を認めないため、本人照合は
Drive about.userで行い、既存checkpointのowner表現との対応を明示レビューする。
connector profile IDをDrive permissionIdと同一視しない。
[公式Picker仕様](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)。

別承認後のローカルbootstrapでPKCE、state、loopback redirect、offline access、
`prompt=consent`、`trigger_onepick=true`、`allow_multiple=true` を使う。
`file_ids` は承認済み固定allowlistのみ。callbackの`picked_file_ids`を照合し、
不足・余分なIDで停止する。folder選択が既存の全子ファイルを許可するとは扱わない。
既存book/原本/各slotそれぞれの到達性を実測する。403/404でscopeを自動拡大しない。
drive.fileには選択ファイルの編集能力が含まれるため、最初のGET限定試験の承認にも
この能力を明記する。実書込みは別承認まで行わない。

External/Testingでdrive.fileを要求するrefresh tokenは通常7日で失効する。
単発試験と継続運用は分け、公開状態や検証要件の変更を自動実施しない。
既存Gmail/HGAアプリ、connector token、本番Secretsを流用しない。
[OAuth有効期限](https://developers.google.com/identity/protocols/oauth2/)。

## Firestore、費用、失効

named databaseを明示した既存SDK/CASを使う。Consoleはdatabase IAM条件を
適用しないので、Console表示をruntimeの隔離証明にしない。
継承role・group・SA impersonation・他project付与・迂回writerを含めて確認する。
金融本文の取得や本番へのnegative readで権限を試さず、IAM評価と合成対象だけの
SDK診断で本番到達不能を証明できなければNO-GOを維持する。
新規server/DBは不要な案を優先するが、共有runtimeへActionsのimpersonationを
付ける前に既存利用者と干渉を評価する。安全なkeyless経路がなければ別承認で
専用principal/WIFを準備する。SA秘密鍵を暫定策として作成しない。
[database IAMの仕様](https://cloud.google.com/firestore/docs/manage-databases)。

Firestore無料枠はprojectにつき1 database。対象named databaseの無料枠適用と
既存使用量は実測が必要で、追加0円を保証しない。Driveはアプリ上限8MBに加え
サービス側revision等の使用量があり得る。TTL/PITR/backupを新規有効化しない。
[料金](https://cloud.google.com/firestore/pricing)。

401/403/invalid_grantやconfig/allowlist変更時は停止。全workerを止め、
専用grant/専用Secrets/新設trustだけを承認の範囲で失効する。
既存共有SA/アプリを無断で失効しない。CAS・全ciphertext/orphan・鍵・receiptは保持。
UNKNOWN解除、再送、自動初期化、世代上書きによる修復は行わない。

Cloud Consoleには閲覧時に関連APIを自動有効化するページがある。
API状態が未確認のサービス画面をread-only調査先として開かない。
変更が起きた場合は対象service/project・表示根拠・前状態の不確実性を報告し、
依存を確認せずAPIを無断で無効化しない。

## 承認と残工程

1. private報告のexact IDを承認し、必要なら専用OAuth app/client/API設定を別承認。
   OAuth同意・file選択・tokenのprivateローカル保存後、GET限定診断を実施する。
2. IAM実効隔離を証明してから、専用document 1つ、private folder 1つ、空JSON slot 8個、
   必要な合成bookコピーを別承認で作成。固定IDを再提示する。
3. freezeした合成checkpointを明示初期migrationし、CAS/暗号化保存/独立GETを試験。
   明示期限・予算・復旧担当を決め、既存認証stateと同じdocumentを使わない。
4. 専用protected environmentの `SYNTHETIC_GOOGLE_OAUTH_JSON`、
   `SYNTHETIC_CHECKPOINT_KEY` とkeyless Firestore認証を別承認で接続。
   manifest検証だけで実port選択やscope/初期checkpoint生成を済ませたと扱わない。
   実Google port compositionとowner mapping、SDK timeoutをレビューする。
5. 固定SHA・手動のみworkflowを別PR/別承認でmainへ登録し、合成1件を1回実行。
   公開PR CIへtoken/鍵を渡さない。scheduleは無効のまま。
6. 別の空実行環境から復元し、receipt/history/既存表示を照合、重複送信0を確認する。
   障害時は照合のみ。1回の成功を全障害・全本番接続の証明にしない。

新CIはUbuntu・外向き通信不可の合成worker試験、core/既存回帰を実施する。
実Google値・token・鍵は入力せず、artifactは完全合成JUnitのみ。
