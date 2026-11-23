# PR-B claim UNKNOWN：読戻し修正とfinish専用復旧の準備

基準版はPR #205 `ec355eba1eba774032cac6f146d7d58328e314da`。
この変更は読み取り・コード・オフライン合成検証のみ。配備、STOP解除、受付ON、実finish、
再claim、live validate、intake、UUID変更、既存フェンスの変更・削除、認証情報の再投入は実施しない。
既存の実機結果やPR #204の資源・履歴は変更しない。

## 発火箇所と確認できた範囲

基準版の `Review.gs:290`（`receiptReviewCommand_`）はclaim/finishの更新後に次を評価する。

```javascript
if (receiptCanonical_(ports.readRequests()[index]) !== receiptCanonical_(after))
  throw Error('review_transition_unknown');
```

実測したclaim RPCはHTTP 200 / Execution API USER_ERRORでこのエラーを返した。
独立Sheets読取りでは受付1件が「検証中」、結果空欄で、固定snapshotは不変だった。
従ってエラーはclaim書込み後の比較で発生し、validator/finishへは到達していない。

旧経路は `receiptReviewChange_` のSTOP保存 → Sheets REST `batchUpdate` →
`SpreadsheetApp.getLastRow()/getValues()` による読戻し、という構成だった。
SpreadsheetAppが同一実行内で更新前の値を返す合成ケースで、書込み済みなのに比較が失敗して
STOPが残る現象を再現した。claimの合成診断では8列（状態）・10列（更新時刻）が不一致となる。
intakeでも保存後・装飾前の停止を再現でき、finishでも同じ読戻し不整合が起きる。

当時の実機actual値・不一致列は記録されていないため、実機原因がSpreadsheetAppキャッシュだった
と断定する証拠はない。発火比較式と混在経路の欠陥を特定し、キャッシュによる失敗を再現した、
という範囲を区別する。GoogleはSpreadsheetサービスのlook-ahead/write cachingを説明しているが、
外部REST更新とのキャッシュ整合性までは保証していない。

- [Google Apps Script：組込みキャッシュ](https://developers.google.com/apps-script/guides/support/best-practices#use_batch_operations)
- [Sheets REST GET：範囲とfield mask](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get)

## 最小修正

PR-Bのcatalog・受付・UIの値読取りを、同じブックへの独立Sheets REST GETに統一する。
`effectiveValue`をstring/number/boolとして復元し、空セルを補う。0とfalseを空文字へ変換しない。
98列、固定snapshot、登録1チェック、同じScriptLock、before完全一致は維持する。
SpreadsheetAppはsheetIdと装飾に引き続き使う。

読取りは対象タブの必要列・1〜1000行に限定する。REST側のgrid行数も1000以下を必要とするため、
空行だけを含む場合も1000行を超えて拡張されたタブは停止する。対象検証ブックはこの範囲内。
ヘッダ、ブックID、タブ、型、HTTP状態、範囲を検証する。

REST GET自体が古い値・不一致・失敗を返す場合も、STOPを保持してUNKNOWNにする。
read/writeを自動再送せず、後日の独立した読み取り照合だけを認める。
`receiptReviewReadback_` の診断はstaticなevent/code/tableと1始まりの不一致列番号だけ。
actual/expected、セル値、UUID、原本ID、hash、snapshot、例外本文、PAT/OAuthはログ出力しない。

## finish専用経路

`app.receipt_review_finish` は通常の `execute_review` と別の明示呼出しAPI。
通常runner/watchから自動的に呼ばず、claim用フェンスを迂回するresume機能は追加しない。

1. `prepare_finish_plan` をオフライン実行する。10セル・catalog・UUID・原本binding・snapshot digestを
   検証し、基準版の `validate_snapshot` で結果を生成する。状態「検証中」・結果空欄だけが対象。
   artifactはbefore10セル、承認対象ブック/API deployment、validator基準版、結果と各digestを固定する。
   実際のartifactはsnapshotを含むprivateファイルとして保持し、Git/CI/stdoutへ出さない。
2. 実行にはartifactの `plan_digest` を別途承認してpinする必要がある。
   artifactが存在することや自己計算hashだけでは実finishの承認にならない。
3. `execute_finish_only` は現在値を独立 `RestReviewReader` とScript側で照合し、before10セルとcatalogが
   完全一致する場合だけ進む。snapshotやupdated_atの変更を許容しない。
4. 同じ既存DBの消費済みclaim UUIDを確認し、別テーブル `finish_attempts` にUUIDを一度だけcommitする。
   元のattempts/source/hash行は変更しない。別planや再起動でも同じUUIDを再finishできない。
   本準備では実DBへのfinishフェンス作成も行わない。
5. 原本の `source_check` → 独立before再確認 → 既存 `port.finish` を1回 → `source_check` →
   独立Sheets REST GET、という順序。finish応答喪失時も後段の二つの読取りを試みる。
   source_checkは既存の所有者・親folder・version・MIME・bytes SHA256検証を利用する。
6. 実行経路ではclaim/intake/validate_snapshotを呼ばない。結果は承認済みartifactから取得し、
   `financial_write_count=0`、`posting_authority=false` を固定する。
7. 最終10セル・固定先頭7セル・catalog・result JSONを独立照合する。
   不一致/UNKNOWNなら再finishせず、既存STOP・claimフェンス・finishフェンスを保持する。
   callerは成功/失敗にかかわらずfinallyで両受付をOFF、Script内PATコピーを削除する。

## 合成検証

`tests/test_receipt_pr_b_readback.py` は実ScriptコマンドとREST writerに対し、基準版のSpreadsheetApp
readerを再現する。更新前cacheでclaim/intake/finishが失敗し、REST GET版が同じケースを通過する。
古いREST応答、HTTP失敗、行数逸脱で停止し、ログへprivate値を出さず、二重finishも拒否する。

`tests/test_receipt_finish_only.py` は独立REST readerと実Script finishを組み合わせる。
10列それぞれの変更、二重finish/再起動、応答喪失、原本ID/hash/version/所有者/folderの前後不一致、
artifact・実行先差替え、金融authorityの付与を拒否する。実行時validator/claim/intakeの呼出しは禁止し、
金融DB・Gemini生成と実ネットワークを禁止した合成環境だけで検証する。

ローカル結果：全体回帰 **5,191 PASS**、その後の独立読戻し追加2件を含む最終対象 **40 PASS**。
Script構文6ファイル、compileall、whitespace確認もPASS。全体回帰には既存Pillow APIの非推奨警告1件。
実finish・配備・受付変更・認証再投入は0。

## 実finishの実行条件（本作業では実行しない）

- 修正版PythonとREST読戻し版Scriptの確定SHA・source hashを承認し、独立Scriptへ反映・読戻し検証する。
  配備済みの旧PR #205版をそのまま使わない。共通Cloud設定やPR #204は変更しない。
- 承認対象1件の最新10セルがartifactのbefore/digestと完全一致し、受付1件・状態「検証中」・結果空欄、
  catalog・snapshot・UUID・原本bindingが不変であることを再確認する。
- 原本の現在の所有者・親folder・version・MIME・bytes SHA256を実行前後のsource_checkで確認する。
  接続ツールがversion/所有者を返さなかった場合、オフライン照合をその代用にしない。
- 元のclaim/source/hashフェンスを保存し、finishフェンスが未消費であること、実行するartifactの
  `plan_digest`、実行先、1回だけのfinish、対象STOPの限定復旧、認証・受付時間を別途承認する。
  既存環境OFF確認、15分以内の受付、finallyの両OFF・PATコピー削除を具体化する。
- claim、live validate、intake、登録操作、UUID再発行、フェンス削除は実行しない。
  最終状態「検証済み・未記帳」とfinancial_write_count=0/posting_authority=falseを独立確認する。

本準備結果をもってSTOP解除・受付ON・認証情報再投入・実finishの許可とは扱わない。
