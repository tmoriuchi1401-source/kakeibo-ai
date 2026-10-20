# R2-3 A01〜A13の最小対応方針

方式は **A=共通gateへnative固有adapter、B=承認済み短時間窓でdrain/直列、C=旧入口閉鎖後に既存新経路へ、D=証拠不足で保留**。選択は設計上の第一候補であり、本番接続の完了・承認ではない。#149のM6金融接続は引き続き0件。現実の実行対象・保存先・credentialに未確認があるため、Aも実装/有効化前にその不足を閉じる。

元の関数・固定ref/行・更新対象・nativeは [#149 inventory](r2-2-m5-m6-inventory.json) の各A行、今回の稼働根拠は [39入口](r2-3-ingresses.md) と [Actions](r2-3-actions.md) を参照。以下に記載する関数は既存関数への将来の境界接続点で、今回変更していない。

| Group / 方式 | 実行証拠と保存先 | 最小の変更関数・境界 | 実行/権限/完了条件 |
|---|---|---|---|
| A01 **A** 通常review | 親active、最新job skipped。過去process-receiptsのApply mobile review step成功。支出/取込/要確認 | `ReviewApprovalPipeline.apply` の計画固定後・最初のappend/update前にnative request/固定ID/before全値式と共通claimをbinding。既存duplicate/decision/append/updateは再利用 | 親`review_apply` stageを同一laneで実行。要確認を非金融表だからと無条件更新しない。既存行＋全table read-back→native/共通完了 |
| A02 **A** 自動照合 | 親経由/旧runnerの過去step成功。取込link/statusと支出除外を更新 | `ReconciliationPipeline.apply` に全更新planとbefore snapshotを渡すadapter。receipt IDだけのlockでは不足 | A01完了後、A03より前。異source/IDも同じledger snapshotを変える。両表/式の独立一致までlaneを渡さない |
| A03 **A** 支出・銀行最終反映 | 親経由のコードと旧auto-post step成功。現在の銀行確定実行/flagは未確認。支出/取込 | `AutoExpensePipeline.apply` / `BankFinalizationPipeline.apply` の既存validation/transaction ID/authorityを残してterminal write直前へ共通境界。銀行原本をreceipt nativeへ変換しない | A02後、bank固有の承認/duplicateを追加条件のまま維持。native/stateがpendingなら追加writeしない |
| A04 **C** 旧Amazon展開 | legacy workflowはdisabled/retiredだがCLIが残る。親のmode実値不明。注文/商品/取込/支出 | `AmazonPipeline` / `AmazonInstallmentPipeline` の旧金融launchを閉鎖。parser/商品補足の再利用を維持し、金融反映は既存`amazon_production`/confirmed-money経路A05へ限定 | 旧CLI/旧SHAをcredential側でも閉鎖。注文展開が金銭計上に自動fallbackしない。A05 mode/対象mapping未確認なら移行せずD扱いでR1を保留 |
| A05 **A** Amazon money | 親直近successでsource step実行、Amazon対象/金銭件数は未確認。canonical行＋money/native | `amazon_production` のcanonical append、`amazon_money_runtime.money_writer` / `MoneyLedger.append` へ既存money authorityのadapter。別storeを単一transaction扱いしない | Amazon解析→native既存intent→共通claim→sealed transport→両保存先read-back。既存定期/金銭modeを維持。old money/UNKNOWNを解除しない |
| A06 **A** au PAYカード | 専用workflow disabled、親core候補。過去専用run成功。取込＋後続支出/native | `SealedSheetsCandidateTransport.write_once` / `SealedSheetsBatchTransport` のSDK呼出しに同じ実行capabilityを必須化。既存sealed authority・batch重複を再利用 | 親のAmazon/receipt依存の後。SDK直呼び/旧専用workflowから同じfinancial secretを得られない境界が必要。batch部分成功はUNKNOWNのまま |
| A07 **A** 銀行/収入/過去分 | 専用recurring/backfillはdisabled、親銀行cron定義あり、当該stageの現在実行は未確認。取込/支出/収入/native | `SealedCanonicalOneRowTransport`、`BankRecurringIncome` のappend、`BankIncomePipeline.apply` / `run_backfill` に各native authorityを保つadapter。parserはそのまま | 通常銀行定期を親に維持。backfillは同adapterを使う承認manualに限定しB窓も選択可。銀行収入flag・対象範囲の拡大をしない |
| A08 **A** PayPay/au PAY | 親Drive PayPay cron、旧runnerのPayPay/au PAY step成功。現在親最新はskipped。取込と後続支出 | `PayPayPipeline` append、`AuPayMailPipeline` append、`AuPayCsvPipeline.import_csv`、`AuPayCardPipeline` の金融stageに同一claim/terminal境界。CSV/mail duplicateを保持 | PayPay取込→次coreのA01/A02/A03という既存順を維持。異runでnative完了と全ledgerを照合し、取込行だけを別ledger扱いしない |
| A09 **D** 手入力/取消 | manual-entry activeだが最新job skipped。Apps Script配置/trigger/実行者は未確認。受付/支出 | 承認前の変更なし。確認後の候補は `manual_entry.execute` と取消の`set_raw_range`を共通gateへ、`Code.gs`は受付専用のまま。既存request UUID/expense IDを使用 | Script deployment/version/実行者/dispatch権限、直接Sheets編集、取消nativeとbeforeを確認してからAへ確定。未確認のままR1を有効化しない |
| A10 **A** 個別修正/money review | 親daily/manual側、実request未確認。固定ID支出、correction/projection/money native | `DailyCorrections.apply_pending/apply`、`daily_runtime.run_daily_requests`、`daily_money_review.run_money_reviews`。#149 `inspect_daily_correction` の既存 `_requests/_read_target` とbefore fingerprintをそのまま拡張 | 固定ID・入力revision・before values/formulasを独立観測→native固有pending/start→共通境界→既存writer→両完了。古いrow番号をidentityにしない |
| A11 **B** 分類/backfill/restore | category-backfill activeだが最新job skipped、category requestの過去実行成功。支出F:G/rules/history | `CategoryBackfillPipeline.apply/restore`、`category_sheet_requests.execute_request`、`category_operations` の起動対象を承認窓に限定。既存before/履歴を再利用 | R1と全financial受付を保留→旧in-flight/native完了→単一承認requestを既存writerで直列実行→全ledger/履歴read-back→新beforeを再取得。変更窓外の旧workflow/Script dispatchを権限側で閉鎖 |
| A12 **B** 整列/保守 | 親`finish_ledger_updates`→`order_ledgers`の定義。実sort対象未確認。全行/式/row配置 | `ledger_order.order_ledgers/run_ledger_order` を全writer完了後だけ実行。`SheetsDB.set_raw_range/update_expense_fields`、raw SDK/Drive native保守は承認された実行context以外拒否 | 定期run末尾の既存sortは同一lane内の短い直列stageとして扱い、R1 pendingがあればsortしない。手動/直接SDKは窓外閉鎖。並べ替え前後のID→値/式対応とUI入力保持を検証 |
| A13 **B** repair/backfill | 専用workflowなし。実CLI/権限未確認。取込status/cell/card関連列 | `execute_bank_status_repair_once` / `SealedBankStatusCellTransport.write_once`、`execute_repair_once` / `SealedSheetsRowRepairTransport.write_once` に窓とterminal拒否を適用 | 固定ID/before proof/対象セルだけを既存sealed writerで1回。R1を保留し、in-flight/native不明があれば実行不可。repairをUNKNOWN自動復旧に使わない |

## 共通の最小接続契約

1. `production_flow.assemble/invoke` を既存の実行順・`ProductionLedger`を保つ合流点とする。R1実行も同じ単一financial laneに参加させる最小adapterを別PRで設計/実装する。独立Web hostから無制限のSheets writerを渡さない。新queue/DB/CAS engine/ログイン方式は作らない。
2. `LedgerExecutionBoundary.inspect_update` / `inspect_daily_correction` のtarget観測、既存ownership/`ConditionalRequests`、native固有validator、既存guarded writerを再利用する。現`execute_update`を`approved=True`等だけで開く変更は禁止。memory限定constructorを単に解除してproduction SDKを注入しても完成としない。
3. native namespaceはAmazon money、銀行/カードstate、manual request、correction/category historiesのまま。各nativeのpending/start/完了証拠と全ledger snapshotをbindingする。すべてを画像レシートv3へ移す計画にしない。
4. 実保存先の同一store・opaque version・強い条件付き更新・不明応答保持をR3で確認する。**Sheetsの一致read-backだけを強いCASと呼ばない。** backendが必要契約を満たさなければAの有効化を止める。単一credential holderによる排他に依存する案も、旧SHA/外部SDKからのwrite不能性の証拠が先で、単なるGitHub concurrencyでは不足。
5. source/native/全ledger beforeの独立観測→元native事前検証→共通claim→元native pending/consume/start証跡→直前再検証→既存writer単回dispatch→全対象と無関係行/式read-back→元native完了→共通完了。別store間はatomicでない。途中/応答喪失はUNKNOWN/PARTIAL保持、追加write/自動retry/解除なし。
6. Bを使う際は新R1の受付だけを止めて旧処理を並行実行してはならない。共通laneの未完了が0と証明できるまで待ち、R1 financialは窓中閉鎖。承認対象artifact/principalだけを実行可能にして、終端writer完了と独立照合後にR1のbeforeを取り直す。短時間で終わらなければ中止しread-onlyを維持する。窓後に無統制writerを再開しない。

## 定期取り込みを維持する実行順

既存 `production_run.DEPENDENCIES` の順序を変えない。Amazon/receipts→card、全取込（au PAY balance/PayPay/bankを含む）→review_apply→reconcile→auto_expense→projection。Drive source別cronも維持し、PayPayの後続core待ちを勝手に即時記帳へ変更しない。ledger sortはwriterがrow hintを解放し、未解決native/共通operationがない場合だけ。

常時動くA01/A02/A03/A05/A06/A07/A08/A10と通常/買取を先に同じ境界へ接続し、低頻度Bと旧Cの実行権限を閉じる。その後、一般画像R1→医療完全手入力R1を限定有効化候補にする。PDF金融（自動/Unit/canary）を同時導入しない。PDFの現行稼働が判明した場合、その機能を無断停止せず別承認の保留/変更窓を決める。それまで新R1はNO-GO。

現監査では親定期の最新runがskippedで、継続稼働を保証できない。まず安全flag/validatedの現値と意図を所有者が確認し、監査を理由に自動再開・再実行しない。設計上の「維持」は稼働中の定期を追加停止しない方針であって、現在稼働中との認定ではない。
