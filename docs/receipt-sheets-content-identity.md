# Sheets正本：version任意化と専用コード配置の準備

2026-11-16。基点 `77074a615d54eebcd1c34f912242cdeb3e0f49c6`。
ユーザーが新mode限定の原本identity変更を明示承認したため、オフラインだけで修正した。
**配置・認可・trigger作成・登録・Actions実行は未実施。実機・本番NO-GO。**

## 新modeだけの契約

対象は `sheets-authority-v1`。旧5件のschema、旧private adapter/journal/cache、
旧worker、旧Apps Script Connection.gs/Code.gs/manifestは変更しない。
`receipt_sheets_source/context/decoder` を専用に分離し、旧decoderへ架空versionを補って通さない。
純粋な既存 `preview_item_corrections`、候補/import/policy digest、本人確認済み合計を再利用する。
本人確認済み合計のsourceとの一致・金額・actor digestはcontextで必須検査する。

sourceには `source_id`, `sha256`, `mime_type`, `classification=normal`,
`transaction_kind=purchase` が必須。`version` は欠落または空文字を許す任意の監査文字列。
存在する監査文字列は受付時snapshotへ固定し、その後書き換えない。
Driveの現在versionとの一致は判定条件にしない。

管理tabのA:J header・列順は維持する。D `source_version` は空欄可。
snapshotに監査文字列があればDもその文字列に一致し、なければ空欄とする。
確認タブのAMも空欄可で、他の内部IDとともに承認済みmachine vectorへ固定する。
Python/JSのcanonical JSON・snapshot digestはversion欠落/空文字/監査値の各場合で一致を検査する。
既存の受付行を移行・修復・再利用する変更ではない。

G列式はD/AMを原本identityとして比較しない。UUID一意、receipt ID、原本ID・hash、
snapshot digest形式、result内UUID/digest/state、posting_authority=false、financial_write_count=0の条件を維持する。
式そのものはsnapshot hashを再計算しないため、管理行の正規性・変更排除条件は引き続き必須。
新しい式はオフライン構文・契約試験のみ。実Sheetは変更しておらず、旧式の24条件PASSを流用しない。

## 原本を確認する順序

承認済みbindingのsource、owner_email、service_email、folder_idへ固定して、workerは次を行う。

1. Drive metadata GET：ID、trashed=false、MIME、所有者、ACL、親を確認。
2. 同じIDのmedia GET：実bytesのSHA-256を固定値と比較。
3. metadataを再GET：上記のsecurity属性が一致していることを確認。
4. 固定snapshotの純粋な商品検証を実行。
5. 1〜3を再度実施し、検証後の実bytesを再取得・照合。
6. 管理行のUUID・snapshot全文/digest・状態を再照合し、H:Jだけを一回更新・独立読戻し。

ID・内容・MIME・所有者・ACL・親・trashの不一致や不明応答は、終端結果を書かず停止する。
raw metadata全体は比較せず、version/name/modifiedTime/descriptionの変化だけでは停止しない。
ACLはactive permissionのtype/role/emailを比較し、順序は無視する。
現在のbinding契約はSheetではowner本人＋worker writer、PNG原本ではowner本人＋worker readerの
2権限を要求する。新modeだけの最小権限修正で、旧経路は変更しない。
追加・欠落・重複・role変更・有効期限・pending ownerを拒否する。
PNGのworker roleだけは旧writer要求からreaderへ変更した。実共有の追加は本人の別承認を要する。
このコード修正が共有追加を実施したり、承認済みとみなしたりするものではない。
親は固定folder ID一つ。所有者・原本ID・MIMEも各GETで必須。

これは各観測点での検証であり、複数GETとSheets更新の間の原子的な原本固定ではない。
観測間に変更して元に戻された事象をすべて検出できるとは扱わない。
原本・管理行への同時編集排除ゲートと、金融write不可の境界を維持する。

## 専用ZIPとscope

`scripts/build_receipt_sheets_bundle.py` がローカルで次の4ファイルだけを生成する。

- Code.gs：既存Code.gsから純粋な通常セルdecoder/snapshot関数だけを抽出。旧文字列parser・prepare/accept journal処理は含めない。
- Connection.gs：`apps-script/receipt-sheets-authority` の新mode専用entry。旧Drive portsは含めない。
- Sheets.gs：新modeの受付・設定・停止コード。
- appsscript.json：新mode専用manifest。

専用manifestのscopeは全て `https://www.googleapis.com/auth/` 以下の4件。

|scope|必要性|
|---|---|
|spreadsheets|確認行読取と受付管理tab保存|
|script.scriptapp|既存onEdit triggerのhandler/対象/種類/件数の検査|
|script.external_request|Sheets RESTとUUID-only dispatch。今回は実行禁止|
|userinfo.email|実行ownerの一致検査|

受付コードはDriveを呼ばないため、専用projectのDrive scopeは不要。
広い `drive` も、未使用の `drive.readonly` も要求しない。
原本を読むPython workerには既存の `drive.readonly` と `spreadsheets` を維持する。
Drive files.getのmetadata/media GETにはdrive.readonlyを使用可能で、Drive write scopeは不要。
scopeの実grant・新OAuth client・script.projects・PAT発行は一切行っていない。
旧manifestのdrive scopeは旧経路保全のため変更せず、専用ZIPへは含めない。

根拠：[Drive files.get](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/get)、
[Drive scope一覧](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)、
[SpreadsheetApp.openById](https://developers.google.com/apps-script/reference/spreadsheet/spreadsheet-app#openById(String))、
[ScriptApp.getProjectTriggers](https://developers.google.com/apps-script/reference/script/script-app#getProjectTriggers())。

## 配置手順（将来、別途許可された工程だけで実施）

1. 修正後のlocal review commitを固定し、clean checkoutで下記を実行してローカルZIPとhash一覧を作る。
   `python scripts/build_receipt_sheets_bundle.py <workspace内の出力先> --commit <40桁SHA>`
   生成物はsource_commit、ZIP SHA-256、4ファイルのhashをmanifest.jsonへ記録する。
2. 本人が作成済みの専用projectと独立Sheetのcontainer対応を独立に再確認する。
   新しいproject/Sheetを作らず、旧5件のprojectへ配置しない。
3. 専用ZIP内のCode.gs / Connection.gs / Sheets.gsとappsscript.jsonだけを配置し、再読取で照合する。
   旧 `receipt-validation/Connection.gs` や旧manifestを混ぜない。
   今回は配置自体が禁止なので、この工程は実施していない。
4. 新baselineを原本ID/hash/MIMEとowner/ACL/親から確認する。versionは取得不能なら欠落のまま。
   旧案件・旧接続設定はコピーせず、未完成draftも投入しない。
5. `mode=sheets-authority-v1 / enabled=false` の新case専用config、修正G式、内部列の整合性を確認する。
   G式の実Sheets評価を別途許可された工程で実施する。登録・workerを代用にしない。
6. 別途認可が許可されたときだけ、上記4 scopeとの一致を本人が確認する。
   新規scopeや共有変更が必要なら停止。scopeが既存別clientと同じでも新project認可済みとは扱わない。
7. trigger作成・登録・Start（enabled=true）・dispatch・worker実行はそれぞれ今回の対象外。

Configureはdisabled configをコピーするだけでも明示実行・既存PATが必要。
Startは有効化する関数なので、配置準備中に実行しない。
現在の独立資産は前回確認時ownerのみ。workerの期待ACL・既存権限の充足は未確認で、共有を追加していない。
versionの必須依存をなくしても、この権限ゲートやG式実Sheets評価を省略しない。

## オフライン結果

既存378件＋新規41件＝419件PASS。外部socket接続と金融SheetsDB・旧private adapterを試験で禁止。
新規試験はversion欠落/空文字/監査値、JS→Python digest一致、metadata-only変更、
検証前後の内容/ID/MIME/所有者/ACL/親/trash不一致、download中のアクセス変更、
表示identity、旧schemaのversion必須維持、専用4ファイル/lifecycle/scopeを確認した。
Junitは私有workspaceのcontent-identity-results.xmlに保存。Actions実行による証明ではない。

オフラインreview準備は完了。配置・認可・trigger・登録・実機・本番はNO-GOのまま停止する。
