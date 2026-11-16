# G列式の修正と独立Sheets評価

2026-11-16。基準commitは `e30f3bee1313105210ae020cdfc99a1be00cbd32`。
変更対象はreview branchの式生成・回帰試験・記録と、承認済み独立SheetのG2だけ。
Apps Script配置・認可・trigger作成・登録・worker実行は行っていない。

## 原因と修正

`result_formula()` の終端状態を判定するIF群の後に、LETを閉じる `)` が一つ不足した。
そのためIFERRORのfallbackがLETの引数に入り、式全体も閉じなかった。
LETの直後へ括弧を一つ追加した。末尾へ追加するだけではLETの引数構造を修復できない。
回帰試験は引用符を考慮した括弧対応とIF/IFERROR/LETの引数数を検査する。

## 実Sheetsでの評価

既存独立Sheetの空欄を独立GETで確認後、確認A4:AT27と管理A2:J23に
完全合成の表示試験fixtureを一回だけ配置し、生成式をSheets engineで評価した。
通常の登録チェックもonEditもworkerも経由していない。
fixtureのversionは明示的な試験文字列であり、実原本のversionを代用していない。

|条件|SheetsのeffectiveValue|結果|
|---|---|---|
|整合した正常終端|検証済み・未記帳（登録時の内容）|PASS|
|未登録ready / 空欄、管理行なし|編集中・未登録|2件PASS|
|登録済み形式だが管理行なし|結果不明・照合待ち|PASS|
|受付済み / 検証中|受付確認中・未記帳／チェックを繰り返さない|2件PASS|
|修正が必要 / 保留|各状態＋（登録時の内容）|2件PASS|
|要求ID、receipt ID、原本ID/version/hashの不一致|結果不明・照合待ち|5件PASS|
|snapshot digestの不一致 / 不正形式|結果不明・照合待ち|2件PASS|
|result内の要求ID/digest/state不一致|結果不明・照合待ち|3件PASS|
|posting_authority=true / financial_write_count=1|結果不明・照合待ち|2件PASS|
|結果欠落 / 未知状態 / 要求重複 / 表示IDと内部ID不一致|結果不明・照合待ち|4件PASS|

計24件PASS。独立GETでeffectiveValueに式エラーがないことを確認した。
試験fixtureは配置済み範囲だけを空欄へ戻し、独立GETで管理headerのみ・要求0件と
確認試験行の空欄を確認。実対象のI2はreadyのまま、商品Bは730円のまま保持した。
G2は修正式を保持し「編集中・未登録」と評価される。
詳細のwrite応答・readback・期待値比較は私有workspaceの証拠JSONに保存し、repoへ原本や資格情報を含めない。

この式は表示補助であり、snapshot JSONのhashを再計算する認証器ではない。
正規の管理行をreceiver/workerが検証し、管理行の手動変更や並行構造変更を排除する前提は維持する。
全入力改ざんへの耐性やiPhone登録成功を、この24件で証明したとは扱わない。

## 原本identityの判定

固定原本IDから新たにダウンロードした68515 bytesのSHA-256は固定値と一致。
しかしDrive metadata GETに `version` を明示しても、接続ツールの正規化応答には
versionもchecksumも含まれなかった。revision ID、時刻、hashからversionを推定しない。
原本のidentity全体は未PASSであり、実原本のversion欄とbaselineは未完成のまま。

Driveのversionは内容以外のサーバー側変更でも進む。SHA-256はbytesの同一性を確認する。
bytes固定だけを目的とする設計ならversionは必須ではないが、現reviewのsource schema、
baseline、UI内部列、worker source_checkはversionとhashをそれぞれ要求する。
今回の修正ではこの条件を緩和していない。

別途の設計提案：原本ID＋実bytes SHA-256を内容identityとし、versionを監査情報へ変更する案は検討可能。
採用には別承認、source schema/baseline/digest/decoder/workerの一貫した変更と回帰評価が必要。
現行原本のversion取得の代替として無断適用しない。

公式根拠：[Drive files resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)。

## scopeと次工程

Apps Scriptの4ファイル・manifestは基準commitから変更なし。manifestのscopeは
`spreadsheets`, `drive`, `script.scriptapp`, `script.external_request`, `userinfo.email`
（全て `https://www.googleapis.com/auth/` 以下）の5件。`script.projects` は不要。
Sheets読書き、trigger検査、外部HTTP、owner照合は新modeのコード使用と対応する。
Drive書込みscopeは同梱の旧connectionコードに由来し、新modeのSheets受付には不要。
固定manifestを勝手に変更しないが、最小権限として十分に絞られた新mode専用manifestとは評価しない。
新projectへのgrantが既存の他clientの認証で済むとも扱わない。

原本version未確認のため、Apps Script配置・認可工程への総合判定はNO-GO。
手順書をGOへ進める条件は満たしていない。認可、trigger、enabled=true、登録は未実施。
実機NO-GO・本番NO-GOを維持する。

ローカル回帰378件PASS。Actionsは起動していない。
