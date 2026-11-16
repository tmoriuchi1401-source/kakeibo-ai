# 独立した合成レシート1件：実機試験準備

2026-11-16。オフライン377件PASS後に作成。**手順のみ。現時点で実機試験NO-GO、本番NO-GO。**
新modeのversion任意化と専用ZIPの最新手順は
[`receipt-sheets-content-identity.md`](receipt-sheets-content-identity.md) を優先する。
停止中workflowの再開、Google資産作成、実環境設定変更はこの文書では許可しない。
本人が変更対象と副作用を確認し、禁止事項のうち必要なものを明示して別途承認するまで実行しない。

## 対象と事前ゲート

新しい合成case `SHEETS-SYNTH-ONE` 一件を用いる。既存失敗5件と異なるUUID、receipt_id、原本を使用。
架空店舗、商品A 500円・商品B 868円、支払総額1368円と印字した完全合成原本を用意する。
保存済み候補は商品Bを730円として、本人がiPhoneで868円に修正し、許可された別カテゴリを選択する。
店舗・日付・合計・候補・カテゴリ・原本ID/hash/MIME・所有者/ACL/親のbaselineは事前に確認し、権限を推定しない。
新modeだけversionは任意監査情報。未取得でも偽の値を補わない。

まず**読取とローカル準備だけ**で以下を埋める。現時点で具体的な利用可能資産bindingは未選定。

|対象|必要な証拠／未充足なら|
|---|---|
|独立した既存の試験workbook / 原本 / folder / Apps Script project|本番、既存5件、承認済み検証資産を変更しないbinding。追加資産作成が必要なら停止|
|`_receipt_requests`タブと正確なheader|既存資産内の承認済み試験領域。タブ新設も未承認なら実施しない。新規資産に該当すると判断される場合は停止|
|Google主体・既存PAT・既存trigger|現在の権限だけで同じ試験Sheetに到達し、既存 `receiptConnectionEdited` 一つだけが該当すること。追加権限・trigger新設が必要なら停止|
|排他|receiverは一project、workerは固定concurrency group。別ツール・ローカル実行・管理tab手動編集・sort・行挿入/削除を試験中排除できること。保護だけを証拠にしない|
|Actions履歴|公開GET可能、同branchのdispatch履歴が全件100件以内、過去runを削除しない。証明不能なら停止|
|workflow実行binding|review branch/SHAが固定でき、main・validated SHAを変更せず実行できること。現行mainのworkflow登録が選択refを扱えるかread-onlyで確認|

これらは「これから新規サービスを作るリスト」ではない。既存資産と既存権限だけで成立しなければNO-GOで停止する。
既存5件のsource/journal/cache/checkboxは読書き・再実行せず、その案件で試さない。

2026-11-16の追加確認：現行mainの `category-sheet-request.yml` にUUID入力のworkflow_dispatchが
登録済みであることをGitHub GETで確認した。main変更が必要とまでは判定していない。
Driveではreceipt/レシートに関係するSpreadsheetとApps Scriptのメタデータだけを検索した。
本番・既存5件・過去検証用の候補が含まれ、今回使える独立bindingは未確定。
Apps Script検索の結果0件は、bound projectやtriggerの不存在を証明しない。
確認タブ本文・旧journal・原本内容はこの検索では取得せず、Google writeは0。

## 承認のために提示する変更表

上のbindingが揃った後、実施前に次を具体値付きで提示する。この段階でも適用しない。

1. review commit、対象試験workbook/tab、合成原本ID/hash/MIME・所有者/ACL/親、UUID、receipt_id。
2. ローカルで作った `make_binding(..., ref=review branch)` のdisabled config。
   `mode=sheets-authority-v1`。既存Secret `RECEIPT_VALIDATION_CONFIG_JSON` と既存User Propertiesへ
   適用する対象、現在値の保全方法を示す。既存5件用bindingと取り違えない。
3. 既存 `RECEIPT_VALIDATION_REF/SHA/ENABLED` を試験中どう扱うかの差分。
   停止workflowを再開する操作が必要なら、その操作も明示した別承認が必要。
   main、`KAKEIBO_VALIDATED_MAIN_SHA`、金融flag、他workflowのscheduleは変更しない。
4. 確認タブの入力・リンク・メモ・配置を維持したまま、合成行の既存Iセルに
   `ready:<UUID>` / `register:<UUID>` custom checkboxを割当て、同じGセルに `result_formula(row)` を設定する差分。
   新しい確認画面やボタンは作らない。別の手動準備操作を本人の登録手順に追加しない。
5. 管理tab、既存trigger、config、停止latchの現在状態と停止手順。latchの自動解除・旧案件の復旧は含めない。

承認された独立projectでは、既存の関数名を使用する。
`receiptConnectionConfigure` はstagingのdisabled新mode設定をUser Propertiesへ移す。
旧journal用のUser bindingが残るprojectへの上書きは拒否されるため、そのprojectを流用しない。
`receiptConnectionStart` は既存のON_EDIT triggerを検査してから有効化する。
triggerがなければ停止し、作成を試みない。
`receiptConnectionStop` は受付を無効化し、trial用PATコピーを削除する。
trigger、受付行、停止latchは変更しない。GitHub側の停止flagも別途承認された手順でfalseへ戻す。

## 承認後に限る一件の手順

1. 他workerがterminalであることをActionsから確認する。管理tabのwriter排除条件を確認する。
2. 承認された差分だけを適用。既存5件は一切触らない。新規権限・資産が必要と判明したら停止。
3. iPhone Sheetsアプリで対象合成行を検索。原本を開き、戻って入力・メモが保持されることを確認。
4. 商品Bを730→868円、カテゴリを指定の許可候補へ変更し、既存の商品原本照合チェックを行う。
5. 登録チェックを**一回だけ**行う。以降はチェックを外さず、再チェックせず、手動dispatch/rerunしない。
6. `_receipt_requests`を別のread-only観測で確認し、同じUUIDの一行、snapshot/digest、原本identityを照合。
   Actionsに送られたinputはrequest_idのみで、実行一件・attempt1・承認SHAであることを確認。
7. G列に「検証済み・未記帳（登録時の内容）」を表示することを確認。
   管理行とresultのrequest_id/digest/state、金額・カテゴリ、financial_write_count=0、posting_authority=falseを独立照合。
   式のsyntax error、未終端、欠落、重複、不一致は成功としない。
8. 入力・原本リンク・途中メモ・行配置が保持され、金融明細・原本移動・AI送信が発生していないことを確認。
9. 承認された停止差分だけを実施し、停止を独立GETで確認。証拠を保存し、勝手に次の合成件を作らない。

エラー・timeout・応答不明・worker異常時はその時点で停止する。
停止flagをfalseへ戻す等、事前に承認された停止操作以外の状態修正はしない。
結果を人がread-only照合し、成功の根拠がなければ結果不明と記録する。
追加GETによる人の照合は処理の再送ではないが、worker再実行・結果再書込みは行わない。

## 採否

一件合格は「独立した合成レシートでiPhoneから一回登録→検証済み・未記帳」を証明するだけで、
本番導入、実データ、医療/PDF、金融write、旧5件の回復の許可にはならない。
既存資産未確定、排他条件未確認、式とiPhone未試験、本人承認未取得のため、現在は**実機NO-GO / 本番NO-GO**。
