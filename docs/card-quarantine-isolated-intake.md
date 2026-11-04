# カードUNKNOWN保持中のPayPay単独取込

基準main：`fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`。カードrun #41825241169のUNKNOWN・親/native pending・旧checkpointを保持する。これはカード復旧や金融要求0回の認定ではない。

## 現在の影響と案の比較

| 経路 | 基準mainのカードpendingによる影響 | 今回の扱い |
|---|---|---|
| CORE | 全source/nativeのready確認で停止 | そのまま停止 |
| PayPay `drive_paypay` | 同じ全体ready確認で停止。source自身にカード依存はない | A：この単独取込だけを隔離 |
| Amazon通常取込 | CORE入口で停止 | B：今回解放しない |
| Amazon catch-up | 全sourceのready確認で停止 | 既存guard維持 |
| au PAY残高 | writer自体は取込行の追加だが、定期入口はCORE | B：今回個別入口を追加しない |
| 銀行 `drive_bank` | 全体ready確認とは別の既存経路 | コード・設定・動作契約を変更しない |
| 共通後処理 | review_applyはカードを含む全取込に依存し、その後reconcile・auto_expense・表示refreshへ進む | C：COREとともに停止を維持 |

Amazon・残高の個別化は理論上別途検討可能だが、Amazonには共有moneyへの処理があり、単にCOREから二つのsourceを抜き出して安全と判断しない。既存Amazon canaryは別承認の限定経路で、今回の自動単独取込案へ転用しない。manual categories/daily/projection等の独立操作も今回の呼出対象ではない。

## 最小の隔離境界

`ISOLATED_INTAKE_SCOPES`は既存の`drive_paypay -> {paypay}`だけを許可する。source選択がこの集合とずれた場合も停止する。元の`verify_source_readiness`、`execute_serial`、`DEPENDENCIES`、schedule対応表は変更しない。

専用ready確認は、選択sourceと`DEPENDENCIES`からたどる必要依存の全てについて、parentのphase=ready・attempt空・error空を要求する。必要依存にnativeがあれば、既存binding/schema検証に加えてnative ready・attempt空も要求する。未知状態・欠落・読取失敗は許可しない。将来PayPayに依存が追加されれば、その依存も検証され、カード依存が追加されればカードpendingで停止する。

PayPayには現時点で依存sourceと独立nativeがない。非依存のカードnativeをready扱いしたり、pendingから変更したりしない。親ledger全体のschema/binding検証は既存通り行う。親は既存`begin/complete/fail`でPayPay要素だけを更新し、カード要素を含む他sourceは保持する。事後の独立GETは、期待した親payload全体との一致を要求する。不一致やGET失敗は成功扱いしない。

取込は既存の`DrivePayPayPipeline`／`PayPayPipeline`をそのまま再利用する。既存の`paypay:<transaction_id>`重複除外と取込readback、processed属性・原本移動を維持する。writer・金融列・分類規則・ID生成は変更しない。

## 共通処理を呼ばない

`drive_paypay`は既存通り取込だけを選択し、最終ledger sortを行わない。今回さらに銀行review refreshの呼出自体もこの経路から除外する。カテゴリ適用、daily要求、review_apply、reconcile、自動支出、表示再構成、カード処理、他source取込は呼ばない。

そのためPayPayの取込行は保存されても、CORE停止中の照合・支出化・カテゴリ反映は先へ進まない。今回のsuccessは「PayPay取込の成功」であり、全会計処理完了を意味しない。

## 停止条件と残る制約

- PayPay自身のpending、error、UNKNOWN、必要依存の異常、binding/schema異常、選択scopeの拡張、親payloadの事後不一致で停止する。
- PayPay writerが結果不明になれば、既存親ledgerのPayPay pendingを維持する。次の実行はそのpendingで拒否し、自動retry・pending解除はしない。
- カードのUNKNOWNや失敗履歴を解決・例外化しない。カードcheckpointと固定証拠を変更しない。
- 他sourceの正当な取込で共有金融表が増えれば、カードの旧固定金融ハッシュと現在値は一致しなくなり得る。失敗時点の保存証拠は保全し、将来のカード復旧でその増分を別途照合する。現在値への安易な固定ハッシュ更新は行わない。
- 既存の共有concurrency、ownerの直接編集を避ける運用、Sheets/Drive間の非原子性という制約は変わらない。この変更は過去のカード要求や副作用を取消すものではない。

## 合成検証

ネットワーク禁止のfixtureで、カードpending・PayPay ready、手動／schedule、既存writerによる取込と重複再読、カード要素/native保持、PayPay自身のpending/UNKNOWN、依存parent/native異常・推移的依存、共通後処理の呼出禁止、親readback不一致、PayPay append応答不明時のpending保持と次回拒否を検証する。Amazon／残高がreadyでもCOREは解放されず、銀行の既存経路は専用gateへ迂回しないことも確認する。

本変更はDraftレビュー用。本番schedule／Variables／Secrets／authority／validatedを変更せず、本番の金融write・dispatch・カードApply・pending解除を実行しない。マージと新main CI・validated整合、本番適用の承認は別途必要。
