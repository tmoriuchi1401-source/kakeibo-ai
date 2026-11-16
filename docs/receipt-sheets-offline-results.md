# Sheets正本経路：オフライン結果

2026-11-17。**最新472 passed（従来449＋新規23）。実機・本番はNO-GO。**
最新の責任分担と検証制約は [`receipt-sheets-split-verification.md`](receipt-sheets-split-verification.md) を参照。
version任意化・原本bytes前後照合・専用bundleの最新結果は
[`receipt-sheets-content-identity.md`](receipt-sheets-content-identity.md) を参照。
以下は初期試験とG2修正時点の詳細を含む。

実コードの `receiptConnectionEdited` → `receiptSheetsRegister_` をNode VMで実行し、
保存した同じSheets行をPython `SheetAuthority` / 既存レシート検証へ渡した。
Sheets CASはfakeにも実装していない。既存Google IOはモック、socket接続を禁止。
金融writerと旧ReceiptPrivateAdapterは新経路の試験でconstructorを禁止した。
原本は完全合成bytes。実Google credentials・本番データ・既存失敗5件は使用しない。

通常登録の試験は、既存関数名のConfigure → Start → 一回登録 → Stopも実コードで確認する。
停止後のイベントはIO前に拒否される。既存triggerの欠落・重複、停止latch保持、旧binding上書きは
開始前に拒否され、trigger作成・削除と受付writeは0。旧経路の設定・停止も既存回帰で確認する。
受付の初期scope確認とlock取得の間に停止した場合も、lock内で再確認してIO前に拒否する。

|指定された試験群|確認内容|結果|
|---|---|---|
|通常登録と正常完了|一度の登録チェック、受付行一回保存、UUIDだけdispatch、claim一回、結果一回、固定snapshotのまま検証済み・未記帳|PASS|
|金額・カテゴリ修正|730→868円、カテゴリ変更を登録時に固定。登録後のUI編集はworker入力に影響しない|PASS|
|snapshotの途中変更|capture中変更、claim後のsnapshot/digest/state改変、行の削除・重複を検出し、結果writeを止める|PASS|
|request_id不一致|別UUID・欠落はclaim前拒否、途中変更も終端writeを止める|PASS|
|worker失敗|例外後に未終端で停止。成功表示なし。再workerは拒否|PASS|
|結果書込み失敗・応答不明|claim/結果のcommit前失敗、commit後応答喪失、読戻し不一致。workerは結果不明、再送ゼロ。receiverの保存/dispatch異常はlatchを保持|PASS|
|手動・Actions再実行拒否|重複チェック、終端/検証中のworker、attempt=2、別SHA/ref、停止flag、新dispatchの履歴重複（claim未commit時も含む）を拒否|PASS|

応答喪失後の完全な終端行は、別の独立読取が結果を確定できることも確認した。
不明応答そのものを成功扱いした試験ではない。詳細な意味は設計書「再実行拒否と不明結果」に記載。
生成されたG列式の正規表現operandを解釈し、実際のUTF-8結果JSONを受理し、別UUIDを拒否することを確認。
初版の式全体の実Sheets評価では構文エラーが見つかった。修正後の独立Sheetでの
24条件の評価結果と、その証明範囲は `receipt-sheets-formula-repair.md` に記録した。

再現コマンド（repository root、既存のtest dependenciesとNodeが必要）：

```text
python -m pytest tests/test_receipt_sheets_authority.py tests/test_receipt_sheet_worker.py tests/test_receipt_sheet_connection.py tests/test_receipt_private_adapter.py tests/test_receipt_sheet_decoder.py tests/test_receipt_sheet_validation.py tests/test_receipt_item_correction_plan.py tests/test_receipt_confirmation_ui.py -q --disable-warnings --tb=short --junitxml=../offline-results.xml
```

変更した共有decoder/baseline抽出の回帰を含む350件と、新経路28件の計378件。
試験はローカルPython 3.14 / Nodeで実行。workflow指定のPython 3.12でのActions実行は未実施。
JUnit証拠は作業workspaceの `offline-results.xml` に保存（実環境の状態証拠とは区別する）。
行位置検査はTOCTOUの解決ではない。管理tabに同時構造変更がない前提の成立証明はオフライン結果に含まれない。

GO：review候補の評価、実機試験計画の作成。
NO-GO：実環境反映、停止中workflow再開、iPhone実機成功の宣言、本番運用、金融write。
