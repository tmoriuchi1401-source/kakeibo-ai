# 登録要求者と修正snapshotの結合（金融writeなし）

2026-11-07。PR #195〜#197を確認し、#197 `a41d8fee962c594b3b05a692ced1b28f5af1360b` から分岐。**今回の新しいユーザー指示により、編集者の識別は要件にせず、認証済み登録者が特定snapshotを明示POSTする契約を合成実証した。** #197の編集者識別を前提とした停止理由は、この登録契約の必須条件ではなくなった。従来の監査記録は変更していない。

## 選定と操作の事前報告

| 方法 | 比較結果 |
|---|---|
| 既存OIDC/shared session | 採用。署名／issuer・audience・sub・owner／nonce・PKCE、session失効、要求UUID、Origin／CSRF、期限、CAS・独立read-backを再利用できる |
| Apps Scriptユーザー実行 | 今回は不採用。アクセス本人での実行方式、openid等のscope、tokenと固定要求との結合・検証を追加する必要がある。設置者実行のtokenを登録者と誤認しない |

Google公式：[Webアプリの実行ユーザー](https://developers.google.com/apps-script/guides/web#permissions)、[getIdentityTokenのeffective userとscope](https://developers.google.com/apps-script/reference/script/script-app#getIdentityToken())。Apps Scriptで実現不能という判定ではなく、既存認証契約を最も多く再利用できるOIDCを選んだ。

**実装前に報告した操作**：Sheetsの対象リンクを開く→既存の認証開始／続行→固定snapshotを見て「この内容を登録」を明示POST。リンクを開いた後に2操作、未ログイン時はGoogle側の認証操作が加わる。shared sessionが有効ならGoogle再ログインは不要だが、当該snapshotの登録POSTは省略しない。

今回は画面HTML、Sheetsのリンク・編集UI、HTTP route、既存hostのrouting／deploymentを変更していない。上記は既存画面を将来接続する場合の操作見積りであり、今回実機検証した操作回数ではない。専用の新しいWebアプリ／サーバーを構築していない。

## 実装と信頼境界

- `auth-session/receipt_correction_registration.py`：既存 `AuthenticatedGeneralConfirmation` の begin/callback/resume_login/verified_actor を継承する登録専用adapter。OIDC署名・Google subject・owner allowlist・session・CSRFを作り直さない。
- `register_receipt_item_correction_validation_v1` という専用purposeと専用store scopeを使用する。HGA、商品確認authority、限定canary posting権限を発行／更新しない。authority factory・writer・dispatchの依存を持たない。
- Sheets編集値は未信頼の下書き。登録者のメール／ID／権限／認証状態をセルから受け取らない。登録者はGoogle署名検証済みactor、またはそのactorから作成した既存shared sessionから取得する。下書きの編集者と同一である必要はない。
- prepare時にtrusted hostの独立readerから案件・原本metadata/bytes・保存済み解析候補・現在の修正下書き・分類マスタ／承認ルール・総額確認記録を読み、PR #196の `review` でsnapshotを構築する。原本bytesのSHA256も検査する。独立readerは本番未接続であり、安定したmetadataの前後読取とアクセス制御は今後のhost adapterの責務である。
- UUID・purpose・案件ID・原本ID/hash/version・修正snapshot全体・作成時刻・600秒の期限を一つのbindingに固定する。署名検証したactorのrequest digestはこのbindingへ結び付く。GoogleのJWTそのものがsnapshotへ署名するという意味ではない。nonce／要求状態／サーバー内のprotected journal／CASを介した結合であり、ブラウザやSheetsからjournalを書ける配置は不可。
- 認証後の読取専用 `details` は登録対象の明細と金額を返す。POST前後・claim後に独立読取を繰り返し、原本版・候補・入力・カテゴリ等が固定snapshotと一致しなければ停止する。ブラウザのhidden入力やhashをbackendの新鮮な値として採用しない。
- POSTはcookie／Origin／CSRF・期限・ownerを検証し、既存 `AuthRequestStore` と `FirestoreConditionalState` のversioned CASで要求を一度claimしてから、PR #196の `registration_plan` へ渡す。結果は「修正が必要」または「検証済み・未記帳」、常に `posting_authority=False`／`financial_write_count=0`。
- 結果はprivate request journalへ正確にread-backし、登録者actor ID・要求UUID・案件ID・原本版・snapshot digest・期限・検証結果を読取可能にする。登録結果checksumは `registration_digest` であり、実authority用のdigestではない。既存validatorの完了checksum欄は一時コピーでのみ再利用し、journalに `authority_digest` は保存しない。他purposeのvalidatorはこの登録記録を拒否する。
- claim済み／完了済みの要求は再実行しない。結果不明時は照合だけ行う。完了後の `result` は読取専用で、検証や送信を再実行しない。初回login・表示・セル編集で金融writeは発生しない。

## 合成試験

`auth-session/run_receipt_registration_checks.py` は変更していない `compose.py` で既存認証のhash固定fixtureを作る。PR #196のvalidatorは別名空間へそのままコピーし、歴史的な認証モデル／validatorを上書きしない。新adapterはtest fixtureだけへ追加し、既存deployment whitelist、baseline hash、OAuth設定、権限を変更しない。

実行結果：

- 新接続契約 **39成功・0失敗**：1,368円／967円、実際のRSA署名検証（合成鍵と合成証明書）、shared session再利用、編集者と登録者の分離、署名改変・不正claims、明示POST／Origin／CSRF、原本bytes/hash/version・候補・数量／金額・分類・承認ルール版・総額確認の変更、別UUID／ブラウザ混同、期限・失効、証拠改変、CAS競合、複数worker、claim後編集、応答喪失、独立reader障害、未解決入力の差戻し。
- 既存認証/session契約 `python auth-session/compose.py --test`：**363成功・0失敗**、live_write 0。
- PR #196の修正plan／reviewと既存bundle回帰：**67成功・0失敗**。
- 初回の新fixtureは旧認証モデルと現行モジュールの配置差でcollection error 1件。別名空間に分離して解消。認証コードの安全条件を緩めていない。
- compileall・diffチェック成功。既存のread-only合成CIへ新runnerの1 stepを追加しただけで、workflow_dispatchや本番workflowを変更／実行していない。Draftがmain以外をbaseとする間は、このCIは自動起動しない。最終CI状態はPR本文に記録する。

再現：既存CIと同じFlask／Firestore等の合成テスト依存を用意し、repository外の許可されたTEMP/TMPで `python auth-session/run_receipt_registration_checks.py` を実行する。ローカルでは前回作成した分離テスト依存を利用した。

**これは認証core→固定snapshot→独立再検証→検証結果保存の合成契約の成功である。** 実Googleのtoken交換／証明書取得、実Firestore ACL、実Drive／Sheets、HTTP routeと画面の接続、iPhone実機は未検証。合成の署名はテスト用RSA鍵であり、実Google認証済みの本番要求を作成したとは主張しない。

## 最小残作業

1. 現行の「確認」1タブを維持し、既存hostへ登録専用purposeをrouteする最小adapterを別途reviewする。原本metadata/bytes、保存済み候補、未信頼の修正下書きを独立読取するportを接続し、同時編集の新鮮性とACLを検証する。今回のreaderは合成fixtureのみである。
2. 既存認証画面に固定snapshotの明細・総額・原本版・登録ボタンを接続する。Sheetsの入力とメモを保持し、編集後は旧snapshotを失効させる。画面変更・配置／操作回数は実装／展開前に報告する。実Google認証とiPhoneで確認する。
3. protected journalの保存先／TTL／read-backを既存運用と整合させる。今回再利用するauth requestは600秒の短期記録であり、期限後の結果表示・長期照合は既存private journalへの専用purposeの保存／読取設計が残る。結果を同じ案件へ返す際はUUID・原本版・snapshotで照合し、受付後の下書きを上書きしない。
4. 実金融writeの接続は別段階。既存authority・validated SHA・NO-GO・重複照合・独立台帳read-backを維持し、登録receiptや限定canary権限を通常の記帳許可へ転用しない。

G103／H301、Google Sheets「確認」1タブ、main、本番Sheets・原本、Variables／Secrets、authority、validated SHA、銀行等の運用は変更していない。本番金融write・workflow dispatch・AI送信・新規Cloud資源・既存host deployは0。**NO-GO維持。**
