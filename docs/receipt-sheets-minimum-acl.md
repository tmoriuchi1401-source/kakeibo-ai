# 新modeの最小ACLと共有変更前の停止

2026-11-17。基点 `03652d205a25e1eebd6ecfc3426c58a062ebde65`。
対象は `sheets-authority-v1` のPython workerだけ。
共有権限の変更、認証scope/Secret変更、Apps Script配置・認可、trigger、登録、Actionsは未実施。

## 実装修正

`GooglePort.metadata(fid, *, worker_role)` のroleを呼出箇所で明示する。
Sheetの接続前検査は `writer`、PNG原本のbytes読取前後は `reader` を要求する。
必須引数なので、roleを指定しない既定値で誤った資産へ適用しない。

|資産|許可するactive ACL（完全一致）|
|---|---|
|独立Sheet|本人owner＋binding.service_emailのuser writer|
|合成PNG原本|本人owner＋同じservice_emailのuser reader|

所有者・worker以外のuser/group/domain/anyone、重複・欠落・別worker・role変更を拒否。
Sheetのreader不足、PNGのwriter過剰も停止する。有効期限・pendingOwner拒否は維持する。
ID、MIME、親、SHA-256、検証前後のbytes取得とmetadata再照合も維持する。
旧worker、private adapter、journal、旧Apps Script code/schema/manifestには差分を作らない。
新modeの旧合成テストfixtureだけはPNG readerへ更新し、既存の失敗5件のデータには触れない。

## identity確認の限界

既存資格情報からclient_emailだけを取り出し、既存workerの保存済みlocal bindingの
service_emailと照合する。秘密鍵・token・全configを出力しない。
GitHub Secretのplaintextや稼働中User Propertiesを取得した証明ではない。
独立環境の新bindingは未投入。共有計画の付与先は、照合済み既存workerと同じ主体に固定する。

## readerからの完全ACL取得は別ゲート

Googleのfiles resource仕様では `permissions` 一覧は、要求主体がそのファイルを共有できる場合だけ
返される。PNG readerのworkerから一覧が取得できることは、オフラインfixtureでは証明できない。
一覧が欠ける場合は今までどおり停止し、共有が成功してもworker検証PASSとはみなさない。
readerをwriterへ引き上げたり、余分な共有先の検査を省略したりして回避しない。
完全ACL照合が成立しなければ、ownerによる独立ACL確認の位置付け等を別途設計・承認するまでNO-GO。

公式根拠：[Drive files.permissions](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)、
[Drive roleとUI名の対応](https://developers.google.com/workspace/drive/api/guides/ref-roles)。

## 承認前の共有計画

私有workspaceの計画書に、既存Sheet IDとPNG ID、現在owner、付与先worker email、
Sheet writer / PNG readerの2件だけを明記する。親フォルダ・Script・本番・旧5件は対象外。
current_user_can_shareと現在ACLを独立GETで確認してから本人へ提示する。
同意前にshare APIを呼ばない。追加認証・scopeが必要になった場合も停止する。
承認後に応答不明となった場合、再送せず独立GETで照合する。
期待した2主体以外が現れた場合、無断で削除・修復せず停止する。
共有後の読取検証は金融writeやH:J書込試験では代用しない。

## オフライン試験

既存419件＋追加30件＝449件PASS（警告0）。最小roleの受理、Sheet不足/PNG過剰、余分な共有先、
ownerのみ、重複、worker不一致、pendingOwner、download中の昇格、ACL一覧欠落を追加検査する。
実connect関数のSheet writer検査と原本reader検査、既存scopeの不変、credential不一致時に
token要求前に停止することも、Google通信を置き換えたstand-inで確認する。
socket接続・金融SheetsDB・旧private adapterを試験で禁止する。
これは共有成功や実workerのACL取得成功を認定するものではない。
JUnit証拠は私有workspaceの `acl-minimum-results.xml` に保存する。

修正commitはlocal review branchに保存し、今回の準備ではpushしない。
共有前の本人承認を待ち、実機・本番NO-GOを維持する。
