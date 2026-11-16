# 独立合成試験：workerとownerの原本検証分担

2026-11-17。基点 `c534d19d715b495912e8048bc5cbbfab7afe6178`。
対象はreview branchの `sheets-authority-v1 / synthetic_only=true`、独立した1 Sheet・1 PNG・1 caseだけ。
旧5件・旧worker・本番経路へ適用しない。共有、scope、Secret、Apps Script、workflowは変更しない。

## 確認できた事実

既存worker主体の実GETではSheet管理tab、PNG metadata/mediaはいずれも200。
原本bytes hashとID・MIME・所有者は取得・照合可能だった。
PNG readerではpermissions一覧がなく、Sheet/PNGとも親情報は返らなかった。
一方owner接続の独立GETでは、Sheet=本人owner＋worker writer、PNG=本人owner＋worker reader、
両資産の固定親IDを取得・照合できた。共有や認証を広げて解消しない。

## 検証責任

|対象|worker|ownerによる事前・事後GET|
|---|---|---|
|原本ID・MIME・所有者・非trash|固定bindingと照合。異なる値・取得不能は停止|対象ID・Sheet/PNGの対応も確認|
|原本bytes / SHA-256|検証前後に独立読取し固定値と照合|baselineの固定ID/hashを確認|
|snapshot|管理行とapproved context、UUID、source、digest、状態を照合|事後に同一要求・snapshot・結果を独立確認|
|ACL|必須読取を行わない。変更検出を保証しない|完全なactive ACLが期待した2主体だけかを検査|
|親|必須読取を行わない。変更検出を保証しない|両資産が固定folder ID一つに属するかを検査|

workerは `id,trashed,mimeType,owners(emailAddress)` だけをmetadata GETする。
bytes GETとmetadata再読取、固定snapshot検証、H:Jだけの単回PUT・読戻しは維持する。
`GooglePort`生成と`connect`の入口でも新mode・synthetic_only・review refを検査する。
分担方式を旧mode、synthetic_only=false、main refから使うと通信前に拒否する。
worker結果は常にposting_authority=false / financial_write_count=0。

## owner検査の実装と証拠

`receipt_sheets_owner_check.py` はIOを持たない純粋な観測値検査である。
完全ACLはSheet owner＋worker writer、PNG owner＋worker readerの2件に完全一致させる。
欠落、重複、余分なuser/group/domain/anyone、role過不足、期限付き・pendingOwnerを拒否する。
owner欄は期待したowner permissionとの一致で確認し、metadataのownersは別途workerが照合する。
親不一致・必要なmetadata欠落も拒否する。

`check_owner_assets` はSheet IDと一つのcase/sourceの対応をapproved context/import/machine vectorに結び付ける。
`check_owner_after` は新しいGET観測と、同じbindingの事前検査記録を照合する。
記録のbinding_digestはenabledを除く設定・baselineを固定する（受付の有効化は原本identityではない）。
このdigestは署名・認証・本人承認tokenではない。供給された観測がowner GETであることや取得時刻を
純粋関数が証明するものではない。operatorが既存owner接続で取得・時刻とともに私有証拠へ保存する。
owner記録はworkerが自動取得・検証する仕組みではなく、実施手順上の開始/合否ゲートである。

## 事前確認手順（将来、試験の実施が別途許可された場合）

1. 固定review SHAと新modeの完全なdisabled bindingを選ぶ。既存ownerとworker emailが一致し、
   `validate_binding(c)` が通ることをローカルで確認。未完成draft・旧5件の設定は投入しない。
2. owner接続でSheet・PNGをID指定GETし、raw応答・取得時刻・接続主体を私有証拠へ保存する。
   worker応答にないACL/親を「同じはず」と補完しない。
3. connectorが `mime_type` / `parent_ids` を返す場合は、その観測値を明示して
   `mimeType` / `parents` へ投影する。欠落した値を推定・合成しない。
4. `check_owner_assets(c, sheet_metadata, {source_id: png_metadata}, phase='before')` を実行して記録を保存。
   owner＋workerの2主体、Sheet writer / PNG reader、固定親、case/source対応が不一致なら開始しない。
5. 確認行のcase ID、原本リンク、ALの原本ID、ANのhashが選んだPNGと対応することを独立に読む。
   行番号自体をidentityにしない。Scriptを使う将来工程ではcontainer Sheetも再確認する。
6. 実行中は原本・ACL・親・管理tabの手動変更や並行writerを止める運用条件を本人と確認する。
   これは観測・合意であり、workerが変更不能性を技術的に保証した証拠にはしない。
7. owner事前検査は今回の登録許可ではない。Start、有効化、trigger、登録、Actionsは各別承認の範囲だけ。

## 実行後の独立確認手順

1. 自動再送・再workerを行わず、owner接続で同じSheetとPNGを新たにGETして証拠を保存。
2. `check_owner_after(c, fresh_sheet_metadata, {source_id: fresh_png_metadata}, before_record)` を実行。
   事前記録と同じidentity/baselineであること、完全ACLと親の一致を確認する。
3. 同じUUIDの管理行を独立GETし、receipt/source ID、固定snapshotとdigest、終端state、resultの
   UUID/digest/state、posting_authority=false、financial_write_count=0を照合する。
   owner事後検査が失敗すれば、workerが「検証済み・未記帳」を返していても試験全体はNO-GO。
4. 確認行の原本リンク・下書き・メモ・入力規則の保持も確認する。事前の入力と照合し、別案件へ移さない。
5. 取得不能・不明・相違なら停止し、不確かな結果を成功へ修復しない。旧journal/cacheや既存5件を変更しない。

## 保証しないこと

**実行中のACL・親の変更をworkerは検出できない。**
一時的に変更して元に戻された場合は、ownerの事前・事後GETも一致し得る。
owner確認とworker処理は原子的なlockやCASではなく、連続監視・署名付き承認でもない。
両端の観測が一致しても「実行中に変更なし」を保証済みと扱わない。
検査記録にも `continuous_acl_parent_verification=false` を明示する。
この制約を受け入れた独立合成・未記帳試験にだけ用いる。本番や金融writeへの適格性は認定しない。

## オフライン結果と次工程

既存449件＋追加23件＝472件PASS。旧ACL/親試験はowner側へ移設し、workerが途中変更を検出すると
いう以前の期待値を、この明示承認された分担仕様へ変更した。試験を削除・skipしていない。
異なるID・MIME・SHA・所有者、snapshot/context不一致の拒否は維持・追加確認した。
ACL/親欠落下の正常bytes検証、owner前後記録、対応関係不一致、別binding再利用拒否、
途中の変更/復元が検出できない制約、新mode合成以外の通信前拒否を追加検査した。
外部socket・金融SheetsDB・旧private adapterは禁止して実行した。

分担方式のオフラインreview準備はPASS。コードはlocal review branchへ保存し、配置・認可・trigger・
登録・Actionsを実施せず停止する。固定SHA反映・完全なtrial bindingの直前owner確認を経るまで、
配置/実行の総合GOは出さない。実機・本番NO-GOを維持する。
