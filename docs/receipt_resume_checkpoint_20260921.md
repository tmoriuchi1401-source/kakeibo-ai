# 領収書確認の再開調査（2026-09-21、途中経過）

- 開始時 main / origin/main: `52164898475ccd2362255770d2c85e7e827a7c20`。専用clone、branch `fix/receipt-review-resume`、開始時clean。既存作業コピーは変更していない。
- 本番Variablesのproduction/schedule/legacy-disabledはいずれもtrue、validated SHAは上記mainと一致。定期はUTC 09:17/21:17。直近schedule run `35513920241`はSHA検証成功後、receiptsで`state_drive_read_failed`。親ledgerにpendingが残り、後続会計はdependency_failed。原因を「承認SHAが古い」として解除してはいけない。
- 管理シート全領収書確認42行のうち未解決12（一般2、医療2、受付保留8）。古い履歴は待機件数に含めない。一般2件はすでにprocessed/記帳済み、残り10件はinboxで未記帳。個別ID・入力・照合表はGit対象外`.private/row-audit.json`。
- 主原因の暫定分類: 人間確認待ち9（種類未回答7、医療必須項目未入力2）、回答を利用する処理がない1（医療の種類回答）、原本versionにより既存値維持まで停止1、重複候補の判断待ち1。全件に親receipts pendingという運用停止も重なる。
- `receipts -> receipt_confirmation -> capture_inputs/apply_confirmations`は既存定期入口に接続済み。手動workflow増設は不要。ただしpreviewは件数のみで入力の処理可否を検査せず、受付種類メモを利用せず、手動確認後のprocessed移動も不足している。
- 初回修正: 医療/対象外の完全一致する種類回答を、同一原本・最新UI入力の検査後に次工程へ渡す。種類回答で外部AI送信/会計確定はしない。既存値維持は現在の台帳全snapshotと表示/入力を照合して、会計変更なしで旧versionの質問も閉じる。
- 実データsnapshotのread-only評価で、既存値維持1件は安全に閉じられる。医療確定1件は必要項目不足。会計書込0。関連87テスト成功。

- 追加修正: 確認writerの会計planを読戻した後、元identityを保持してprocessedへ移す。移動intent/完了を確認storeへ記録し、応答喪失後は同じIDの親と内容hashを照合する。未確認・会計反映未完了・原本変更は移動しない。既存operator_import履歴は別経路のため除外。
- previewは実際の本人入力と本番validatorで処理可否を検査し、実store/Sheetsへ書かない。既存receipt recovery auditのID整合とpendingを同じread-only scopeで出力する。
- Driveの読取は既存の3回までのretryへ、明示的な403 rateLimitExceeded/userRateLimitExceededと切断を追加。権限403は再試行せず、write再試行は増やさない。根拠: [Google Driveのエラー処理](https://developers.google.com/workspace/drive/api/guides/handle-errors)。失敗は固定HTTPコードを付けて出力し、従来の不明なread_failedのまま原因を推定しない。
- 実snapshotのID監査: レシート56／receipt取込56、対応欠落0・孤立明細0・確認writer pending0・解析requested0、親receipts pending1。固定ID重複0は会計意味上の重複不存在の証明ではない。一般の1件には同日同額同店舗のau PAY支出候補が存在するため、勝手に削除・統合しない。

- Windows full pytest 2,553件成功（119.99秒、既存警告2件）。最終関連154件成功。`git diff --check`成功。実Google会計変更0。

未完了: Drive失敗の原因詳細、pendingの実書込照合と証拠付き復旧、main統合・本番canary・replay・定期入口の最終検証。Goalを完了扱いしない。
