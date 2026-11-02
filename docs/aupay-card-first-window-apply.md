# au PAYカード 第1固定窓の1回限りApply

この入口は調査・Preview用ではない。既存の成功Preview #41422298980を集計基準に、1回の実行内で完全取得、fresh canonical plan、保留保護、取込、checkpoint、親ledgerの完了まで処理する。追加Preview・独立監査は不要。PR #182の調査のみのDraftはマージ前提にしない。監査準備commit `fecfd8200e8ab1b50c5f65a5983916b0c25b0afb` のstrict取得・原本参照・読取専用document観測を再利用する。

## 固定された対象と承認範囲

| 項目 | 値 |
| --- | --- |
| source | `au_pay_card_gmail` のみ |
| 開始（包含） | `2026-10-13T08:55:56+09:00` |
| 終了（排他） | `2026-10-27T08:55:56+09:00` |
| 開始時checkpoint | `2026-10-15T08:55:56+09:00` |
| 新規の必要件数・上限 | 33件・149,218円（両方完全一致が必要） |
| 重複 | 6件・110,493円、金融行への追加なし |
| 保留 | 4件・4,334円、reviewのみ保存 |
| その他の分類 | 0件（変化時停止） |
| 取得基準 | 22通、分類対象43件、noncanonical resend 39件 |
| 金融write | 既存batch writerから `取込データ!A:L` へ1回appendのみ |
| 支出明細 | 書込み禁止。金額・セル値・数式の変更も完了条件で拒否 |

承認は、この固定範囲・集計・安全条件の下で**実行中に確定するfresh個別IDとfull-plan/batch refs**を対象にする。過去Previewは個別IDの完全な証跡を公開していないため、過去と今回の個別IDが同じだという証明はできない。この点を隠して過去Previewを個別plan承認として扱わない。同額の別IDへの途中差替えはfresh再照合で停止する。

## 実装経路

`.github/workflows/aupay-card-first-window-apply.yml` → `python -B -m app.aupay_card_first_window_apply`。

1. ownerの手動main dispatch、attempt 1、expected SHA＝workflow SHA＝validated SHA、実mainを検証。既存の `kakeibo-production` concurrencyを共有し、他のactive/queued/waiting runを拒否する。このworkflowの全revisionを通じて最初のdispatchだけを許可する。
2. 既存の暗号化binding、Drive native state、親ledger、金融表、MoneyReviewsをGETする。checkpointや未解決intent、queued/pending review要求の変化で停止する。authorityは実時刻で検証し、時刻偽装・期限や14日上限の拡張を行わない。
3. `CapturedGmail` と既存strict collectorを使用し、全list/getの対応、raw・Gmail ID・internalDate・RFC Message-ID・source IDを確認する。原本はメモリ中に保持し、原本hashとURLを照合する。欠落、期間外、上限超過、未解析、未知分類で停止する。API例外を再送しない。
4. canonical planを既存行と再照合し、公開集計を完全一致で比較する。全canonical候補を既存full-plan bindingへ固定し、新規33件のみを既存batch projectionへ選ぶ。
5. 書込み前にfresh既存行・分類・full-plan/batch refs・MoneyReviews・金融表・観測したdocumentの不変を確認する。親ledgerとカードnative stateのpendingを独立readback付きで保存する。
6. 保留4件についてmoney ID、fingerprint、分類理由、レコード全体、原本URLを確定する。既存reviewが完全一致なら保持する。一致しなければ上書きせず停止する。未保存分だけを既存MoneyWriterの `apply(..., review_only=True)` で保存する。
7. `review_only` は全件を保存前に検査し、各レコード直前にも再検査する。post/resume/transfer/supplement等を選択した場合は停止する。専用store制限で既存レコードやaliasの改変を拒否し、MoneyLedgerのappendも禁止する。`MoneyReviews.apply` や `apply_pending` は呼ばない。
8. 別の読取専用store/clientから保留4件を復元して完全一致を確認する。既存review、他のmoney記録、review要求、観測documentの保持と金融表の無変更も確認する。その後もう一度fresh planを照合する。
9. 既存manifest、recurring authority、one-use capability、journal、lease、`SealedSheetsBatchTransport`、`execute_production_batch_once`を使用する。API境界では許可された33行・149,218円のimport appendを1回だけ許可する。
10. writerのexact readbackに加え、append acknowledged、manifest一致、capability sealed、journal final confirmed、lease 0件、金融表全体の正確な33行追加差分、支出・収入・レシート・Amazon注文の値と数式の不変を確認する。一般batch writerが応答不明後のreadbackをexactと判断しても、この入口はUNKNOWNとして停止しcheckpointを進めない。
11. 全条件成功後のみ既存recurring checkpointを終了時刻へ進め、native stateをDriveへcommitする。独立GETでcheckpointとnative内部の完了証跡を確認してから親ledgerをcompleteする。
12. 再度独立GETでnative state・checkpoint・親ledger・金融表・保留bookを確認する。他sourceの親ledger項目も不変であることを確認する。

## マージ後の最短手順（このPR作成中には実行しない）

1. Draftをレビューし、明示的に承認された場合にマージする。PR #182のマージや別の調査runは不要。
2. マージ後mainの既存CIの全3job成功を確認し、その正確なmain SHAへ既存 `KAKEIBO_VALIDATED_MAIN_SHA` を承認の下で更新する。authority、Secrets、scope、scheduleの変更は不要。
3. 本番金融writeについて、固定窓・新規33件149,218円・保留4件review保存・checkpoint更新を一体で1回実行する最終承認を得る。
4. 他のrunがない時に上記workflowをmainから1回だけdispatchする。`expected_commit` は手順2のSHA、source/window/checkpointは既定値のまま、`confirm` は `APPLY_FIRST_WINDOW_33_149218_ONCE` とする。
5. 公開結果の `success=true`, `written=33`, `amount_yen=149218`, `held_protected=4`, `expense_rows_written=0`, `checkpoint=2026-10-27T08:55:56+09:00`, `parent_ledger=complete` を確認する。個人情報・原本・個別IDを公開ログ、artifact、cache、step summaryに出さない。

## 失敗と残るリスク

- この文書と合成テストは本番4件の保存状態や理由を既に確認した証跡ではない。実際の4件が完全取得・照合条件を満たすかは承認後の1回の実行内で初めて確認する。相違があれば金融write前に停止する。
- 外部Sheets・Driveは単一トランザクションではない。途中停止でreviewだけ保存済み、金融行だけ追加済み、checkpoint更新済みで親ledger pending、あるいは最後のGETだけ不明という状態はあり得る。自動再実行・rollback・pending解除はしない。次の操作には保護された実状態による個別照合と別承認が必要。
- 最初のdispatchはguardで失敗しても消費される。rerun attempt 2や別SHAでの再dispatchも拒否する。削除したrun履歴を復旧手段として扱わない。既存checkpoint・pending stateも独立して再実行を拒否する。
- 既存GITHUB_TOKENはrepository Variablesのlive read権限を持たない。validatedとflagsはjob開始時snapshotを使用し、mainと競合runは繰返しlive GETで確認する。実行中にownerがvalidated・flags・bindingを変更しない運用が前提。新PATや新設定は追加しない。
- concurrencyは同じworkflow groupを使う処理の排他であり、ユーザーの直接Sheet編集をロックしない。直前再照合と全金融表の事後差分検証で検出するが、外部編集との原子的排他は保証しない。
- 支出明細への反映はこのApplyに含まない。既存coreのcanonicalカードauto_expense除外を変更しない。
- 公開失敗結果は固定コード `first_window_apply_stopped_no_retry` のみ。API例外や個別データは出さない。失敗時の本番詳細の確認は別途承認されたprivateな手順で行う。

## 検証

ネットワークを禁止した合成mail・fake Sheets/Driveと実際のSQLite manifest/capability/journal/leaseを使用する。既存writerを差し替えた成功スタブだけの試験ではない。成功、既存review保持、missing raw、終了境界、集計差、同額別同一性、既存行変化、review要求競合、post/resume選択、review保存UNKNOWN、append UNKNOWN、欠落・改変行、禁止支出差分、checkpointと親ledger、実行入口全体、workflow境界と公開ログ抑制を検証する。
