# 一般レシート・core・PayPayの日次再開（有効化前）

既存 unified production の経路を再利用する。新writer・authority・Secretsは追加しない。本PRはscheduleの本番有効化、金融Apply、原本移動、外部AI送信の実行承認ではない。

| 経路 | JST / UTC cron | 専用Variable |
|---|---|---|
| core | 05:23 / `23 20 * * *` | `KAKEIBO_CORE_SCHEDULE_ENABLED` |
| 銀行（既存どおり） | 06:47 / `47 21 * * *` | `KAKEIBO_BANK_SCHEDULE_ENABLED` |
| 一般レシート | 07:31 / `31 22 * * *` | `KAKEIBO_RECEIPT_SCHEDULE_ENABLED` |
| PayPay | 08:11 / `11 23 * * *` | `KAKEIBO_PAYPAY_SCHEDULE_ENABLED` |
| カード（PR #187の既存専用経路） | 09:23 / `23 0 * * *` | `KAKEIBO_CARD_SCHEDULE_ENABLED` |

新しい専用flagは未設定/falseで停止する。全体 `KAKEIBO_SCHEDULE_ENABLED=false` は維持。PayPayは専用flagの既存 `drive_paypay` 経路でのみ起動し、全体flagをtrueにしても起動しない。legacyは停止のまま。手動の既存入口と確認条件は維持する。

## 最小変更

- coreから `aupay_card` を除外。Amazon・au PAY残高と既存review/reconcile/auto-expense/表示更新だけを実行する。カード・銀行・PayPay・レシートは起動しない。省略sourceのready検証を維持する。
- core/drive_receiptsは親ledger全sourceとnative 3種をread-onlyで検査し、pendingなら金融処理前に停止。core内で失敗が発生した場合は次の独立sourceも起動しない。UNKNOWNの自動retryやpending解除は行わない。
- レシートの事前スキャンとpreviewを原本単位で既定1件に制限。既存 `receipt_limit` 入力で手動時のみ1～3件を選べる。scheduleは1件。通常workerはスキャンが許可した原本だけを受け取る。
- 同一版の既存医療/未回答受付保留は再走査枠から除外する。未走査原本のreviewを閉じず、既存入力を保持する。未完了原本は残り、成功/既取込原本の移動規則、版/hash検証、重複防止は従来どおり。
- `RECEIPT_CONFIRMATION_BINDING`、医療手入力、privacy gate、PDF全ページの境界は維持。医療・給与・sensitive_unknownは通常AI planへ入らない。件数はPDFのページ数ではなく原本数であり、単一原本が900秒以内に必ず完了する保証ではない。既存timeoutは延長しない。
- repository/main/validated、schedule payloadとscope、専用flag、workflow SHA、attempt1を検証。全経路の既存 `kakeibo-production` concurrencyを維持。

## 実状態のread-only確認（2026-11-03）

- mainは `2c21448d9aee9dcacb23700f3ab0e29a91c5affe`。main CI #41583138984 は成功。直近productionログ #41605770577で同一validated SHAと全体schedule=falseを観測した。Variables画面への直接読取はアプリ承認timeoutで未完了のため、有効化直前に現値を再確認する。
- 親ledger全11sourceはready/attempt空/error空。Amazon・カード・銀行nativeもready/attempt空。receipt再解析record10件はcomplete。過去の銀行SQLiteには既存復旧済み試行の履歴が残るため、古いpending行を新規未処理とみなして再送しない。
- Amazon checkpoint `2026-10-31T16:53:32+09:00`。2時間overlap込みの通常取得窓は11/03 14:53:32 JSTで72時間に到達し、今回の確認時点で超過している。**初回coreの前に既存amazon_catchupが必要**。新policy、切捨て、checkpoint巻戻しは使わない。
- カード checkpoint `2026-11-03T14:45:43+09:00`、銀行 checkpoint `2026-11-03T12:06:14+09:00`。残高の前回親ledger結果は新規0/既存18。残高専用native checkpointはなく、既存伝票番号IDで重複を除く。
- 10/31 #41164075582 は `preview / drive_receipts` が900.116秒でtimeoutし、最終ledger readも失敗したrun。Apply成功や新しい取込checkpointとして扱わない。現在親ledgerはreadyだが、一般レシートの直近成功日時は10/15のまま。
- 現在のreceipt inboxは19原本。既存未完了reviewは医療1・受付5。その他の原本を未分類のまま一般と推測しない。初回のローカルprivacy gateと既存取込ID照合が必要。

## 承認後の順序と初回条件

1. PR #187と本PRのレビュー・merge。各merge後の新main CI全3job成功を確認し、最終main SHAへ `KAKEIBO_VALIDATED_MAIN_SHA` を整合する。新しい3つの専用flagはfalse/未設定で保持し、銀行flagは変更しない。
2. 正しいrepository/main/validated、競合なし、親/native ready、最新成功checkpoint、既存authorityの有効期間・窓・件数上限、receipt bindingと非公開ACLを再確認。UNKNOWN/未照合writeがあればここで停止し、再実行しない。
3. Amazonは手動 `preview / amazon_catchup` で既存72時間・100通・3購入上限の固定窓を確認。金融Applyの最終承認後、同じ既存 `apply / amazon_catchup / confirm=APPLY` を使う。各成功後にcheckpointの前進・正確なreadback・重複除外・親/native readyを確認し、`catch_up_pending=0` になるまで次窓を個別確認する。失敗/UNKNOWNは再送せず停止。
4. 一般レシートは `preview / drive_receipts / receipt_limit=1`、続いて最終承認済み `apply / drive_receipts / confirm=APPLY / receipt_limit=1`。一般原本のみ通常AIに入り、新規/重複/保留・review保存・原本残置/移動・親ledger readyを確認。必要な場合だけ次回2～3件へ。timeout後の無条件再実行は禁止。
5. `preview / core`、最終承認済み `apply / core / confirm=APPLY` を確認。Amazon窓がoverlap込み72時間以内、残高既存IDの追加登録0、カード/銀行/PayPay/レシート起動0、必要な後続処理成功を確認。
6. カードはPR #187の手順で既存authority、native checkpoint、復旧証拠、14日窓を確認。手動Apply時のexpected_checkpointはその場で最新値を必須入力する。
7. 初回結果を確認後、`KAKEIBO_RECEIPT_SCHEDULE_ENABLED=true` → `KAKEIBO_CORE_SCHEDULE_ENABLED=true` → `KAKEIBO_PAYPAY_SCHEDULE_ENABLED=true` → `KAKEIBO_CARD_SCHEDULE_ENABLED=true` を個別に有効化し、各初回runを確認する。全体flag、銀行flag、医療、Secrets/authorityは変更しない。問題時は該当専用flagのみfalseへ戻し、pending/UNKNOWNを解除しない。

## 検証の範囲

合成データとネットワーク禁止の既存テストで、1～3件のplan制限、未走査review保持、重複、医療/給与/unknown遮断、timeout非retry、coreのsource分離、UNKNOWN後の停止、各専用flag false、main/validated不一致、native/親pending停止、Amazon72時間とcatch-up重複、残高重複、銀行回帰を確認する。実データの新規金融write、AI送信、原本移動、schedule有効化は本作業で実行しない。

## PayPay再開（別PRで追記）

- 新規日次入口は既存 `drive_paypay`、08:11 JST、`KAKEIBO_PAYPAY_SCHEDULE_ENABLED=true` 時のみ実行。専用flagは未設定で無効。
- 既存CSV・重複ID・移動先・会計writerを変更しない。停止期間の遡及ファイル再投入や無理な再取込はしない。
- 初回は `preview / drive_paypay` で新規CSV 0件を期待し、確認後に既存 `apply / drive_paypay` を1回だけ実行する。対象外の新規CSVが見つかったら止めて報告する。
- 本番有効化前に親ledger全source・native3種ready、main=validated、実行競合なしをread-only確認。UNKNOWN時の再送禁止。
