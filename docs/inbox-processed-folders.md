# inbox と処理済み原本

各 inbox と同じ親フォルダに、その種別専用の processed フォルダを置く。
フォルダ名ではなく固定 ID で指定する。

|投入先|処理済み|設定|
|---|---|---|
|receipt_inbox|receipt_processed|PROCESSED_DRIVE_FOLDER_ID（既存）|
|PayPay_inbox|paypay_processed|PAYPAY_PROCESSED_DRIVE_FOLDER_ID|
|Bank_inbox|bank_processed|BANK_PDF_PROCESSED_DRIVE_FOLDER_ID|
|Payroll_inbox|payroll_processed|独立 Payroll repository の PAYROLL_PROCESSED_ID（既存）|

新しい2つの設定は GitHub Actions Repository Variables またはローカル環境変数へ設定する。
実行用サービスアカウントに投入先と移動先の編集権限が必要。
Receipt 用の設定を PayPay へ流用しない。親の本番 PayPay apply は専用設定未指定時に停止する。
旧単独CLIの未指定時は、従来の処理済み appProperty のみを保存する互換動作を維持する。

## 移動条件と再実行

- PayPay は CSV 全体の解析、取込、追加 ID の読み戻し後に移動する。
  既存処理済みマーカーだけが付いて inbox に残る CSV は、再取込せず移動できる。
- Bank は既存の bounded recurring apply が対象。支出 writer の確定件数、
  収入を有効にした場合の保存・読み戻しが完了してから移動する。
  既存マーカーがあっても、入金未処理、要確認、衝突、空解析では移動しない。
  同じ run の別 PDF と重なる原本も、その支出書込み完了まで移動を遅らせる。
- 入出力フォルダの同一指定、書込不能の移動先を処理前に拒否する。
  移動時に親を再取得し、投入先だけを除去する。他の親と appProperties は保持する。
  移動後の親を読み戻して検証する。
- 移動失敗は失敗として返す。取込済み ID を利用して、次回の追加記帳を抑止する。
  Bank は失敗時に checkpoint を進めない。
- preview / dry-run は移動しない。Bank の定期 preview 方針・既存 apply authority、
  期間・件数上限は変更しない。期間外の古い Bank 原本は、この変更だけでは移動されない。
- Payroll は別 repository の既存 lifecycle を維持する。
  確認済み保存・確認済み重複は processed、要確認は payroll_review、一時障害は inbox。

既に receipt_processed に入った PayPay 原本を整理する場合は、ファイル単位の種類と
処理済み状態を確認して移す。ファイル名や更新日時だけを根拠に原本を移さない。
