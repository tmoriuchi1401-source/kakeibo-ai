# au PAYカード第1窓Applyの実装判定

**NO-GO。安全条件を証明できないため、Apply経路・writer・workflowは追加しない。**
このDraftは調査と合成テストの証跡であり、実行可能な固定Applyの成果物ではない。
停止条件「保留4件の回収可能性を証明できなければApply禁止」を適用した。

基準mainは`732d3e0a2a0dcb2f10913e885c10bae87fc28cca`。
成功Previewは[41422298980](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/41422298980)、attempt 1、success。
今回、既存runとmainをGETで再確認した。本番Preview/Applyを起動していない。

| 固定条件 | 値 |
| --- | --- |
| source | au_pay_card_gmailのみ |
| Gmail取得窓 | 2026-10-13T08:55:56+09:00以上、2026-10-27T08:55:56+09:00未満 |
| 現checkpoint | 2026-10-15T08:55:56+09:00 |
| 成功Preview | 22通、新規33件149,218円、既存重複6件110,493円、money保留4件4,334円 |
| 再送除外の補足 | 39件、対応金額未測定。候補総数に加算しない |

窓はGmailの取得対象時刻を規定する。メールに記載された利用日が取得窓と同じ
期間であることを意味しない。将来の仕様でも利用日へ無断で対象を付け替えない。

## 既存機能と不足点

`app/aupay_card_recurring.py`の通常Applyは`build_incremental_window`で
checkpoint−overlapから実時刻まで取得する。現checkpointから現在までの窓は
14日上限を超える。既存の`preview_window`はApplyを明示拒否する。
`--now`の偽装、checkpointの付替え、authority延長は解決策にしない。

再利用対象はcanonical plan・既存行の再照合、`_batch_reader`によるlease取得後の
fresh pre-read、`SqliteBatchManifestStore`のimmutable manifest、既存recurring
authority、one-use capability、lease、append-only journal、
`SealedSheetsBatchTransport`の取込データA:Lへの1回append、全行exact readback、
`run_durable_source`と親ledgerのpending/commit管理である。新writerは不要。
writer直前に既存IDが見つかればbatch全体を停止し、部分除外して続行しない。
既存batchの再実行・capability再使用も許可されない。

通常collector単体では、一覧にあるメールのraw欠落を正常noopとして扱い
checkpointを進め得ることを合成テストで確認した。固定Previewの
`_StrictPreviewGmail`はこれを拒否する。将来の固定Applyも同じ厳密な
listed/fetched ID・raw・完全取得検査を必要とし、通常Applyをそのまま流用しない。
500 messages/100 candidates/14日上限を切捨てや分割で迂回しない。

## 保留4件の回収は未証明

本番出力で分かるのは4件が`money_review`で合計4,334円という事実までである。
各money ID、原本参照、理由code、現money bookへの永続保存有無は公開集計にない。
Driveの名前検索では該当money bookを特定できず、個別理由・保存済みとは主張しない。
検索結果なしは「保存されていない」証拠でもない。新しいGmail取得・診断dispatchはしていない。

既存Previewはread-only money storeを使い、`MoneyWriter.preview`は保存しない。
したがってPreview成功・money digest前後一致だけでは、4件の回収可能性を証明できない。
checkpointを10/27へ進めると次窓のoverlap開始は10/25となり、それより前のメールは
通常取得から外れる。旧第1窓を再び`build_explicit_preview_window`へ渡しても
checkpoint一致検査で拒否される。Gmailに残っているという期待だけでは不十分。

**既存回収手段は、money bookに原本情報付きreviewとして保存・独立readback済みの
場合だけ利用できる。** `MoneyWriter.apply`のreview保存は金融行を作らず、
`record_from_entry`で再構築し、既存`MoneyReviews`で原本を確認できる。
この経路は後続Gmail窓に依存しない。ただし、一般の`MoneyWriter.apply`は
fresh判断がpostへ変わると支出にも書くため、4件を無条件に渡す手順を提示しない。
reviewの保存・再構築・fingerprint・原本・理由・未計上状態を保護して照合し、
書込み結果をreadbackできると証明するまで、固定Applyの実装/有効化を進めない。
保留を新規へ昇格しない。大規模な新保留管理や別の金融writerは不要である。

テスト内の`legacy_unsettled_purchase_possible`は合成データの理由であり、
本番4件の理由ではない。保存前のPreviewと保存後の既存review経路を明確に区別した。

## 33件149,218円の扱いと支出への経路

33件149,218円は比較基準であり、固定IDの書込み承認ではない。同額・同件数で
異なるcanonical IDのplanを合成し、公開集計は同じでも既存HMAC
`full_plan_binding_reference_v2`が変わることを確認した。
今後は別途承認した読取り準備でfresh planを作り、既存のkeyed candidate/full-plan/
batch refsとsource window・target・authority・checkpoint・保留回収証跡を結び、
その不透明な参照を承認してApply直前に再照合する設計が必要である。
個別ID・merchant・本文・個別金額や原本URLを公開ログ/artifactへ出さない。
この準備経路は未実装・未実行で、旧Previewの個別ID同一性を遡って証明するものではない。

既存行が増えた合成例では新規が32件となった。33件へ強制復元せず、
fresh再照合と承認参照の不一致で停止してレビューし直す必要がある。
条件がすべて成立した場合の比較上限は取込33行149,218円、支出0行であり、
実際の必要行数・HTTP要求数を承認前に確定したという意味ではない。

既存の普通カードwriterは`auto_expense`状態のcanonical取込行を作る。一方、
`AutoExpensePipeline`はこれを意図的に除外し、既存テスト
`test_canonical_card_pending_is_excluded_from_preview_and_apply`もその仕様を固定する。
coreのreconcileはreceipt一致を扱うが、未一致のcanonical行を自動支出へ送らない。
**取込追加後にcoreを実行すれば33件が支出へ出る、とは言えない。**
このDraftはstatus/authority/auto-expense範囲を変更しない。支出反映は独立の仕様確認・
承認済み工程が必要であり、第1窓Applyへ抱き合わせない。

## 実行・checkpoint・失敗時の手順

現時点で実行可能な本番Apply手順はない。merge/promote/dispatchを行わない。
再開には次を満たす別途レビューが必要である。

1. 4件の実理由・安定ID・fingerprint・原本・永続review/readbackを非公開で照合する。
   保存済みでなければ、金融書込みを伴わずreviewとして保持する最小手段を先に設計・検証する。
2. fresh planのprivate同一性承認、固定窓/実時刻authority/完全取得/現在checkpointの検査を備えた
   au_pay_card_gmail専用の最小経路を別途レビューする。既存writerをそのまま使い、
   money post・支出反映・他sourceを呼ばない。unknownなら停止し再送しない。
3. 成功条件は、保留回収証跡のreadback、取込writerの全行exact readback、
   capability sealed・journal confirmed・lease終了、禁止表/他state非変更である。
   これらを満たした後にのみローカルcheckpointを更新し、既存durable commitと
   親ledger完了・独立GETが成功した場合に限り本番成功と判定する。
4. writer不明/未確認、state保存不明、親ledger不明はUNKNOWN/NO-GOで停止する。
   既存pendingを保持し、再dispatch・rerun・checkpoint操作・自動rollbackをしない。
   原本・exact ID/内容・journal・manifest・state/ledgerを読取り照合し、
   別途承認された既存operatorの証拠拘束reconciliationだけで復旧を検討する。
5. main上のCI、validated整合、authority、全設定、銀行との60分余裕、競合0を再確認し、
   mergeと本番Applyはそれぞれ別途明示承認を得る。今回はその承認を求めない。

## 合成検証の対応

| 要求 | 今回/既存テストの検証 |
| --- | --- |
| 33新規・6重複・4保留、金額整合 | 新しいassessmentテストで43件264,045円を照合。架空43メールで構成し、本番22通の再現ではない |
| 新規だけ選択 | 既存batch projectionが33件149,218円だけ選択 |
| 直前既存行増加 | fresh selection32件、同ID1行。既存batchテストがlease後の既存IDで送信前停止 |
| Gmail不完全 | strict Preview拒否とnormal collectorの不足を別々に実証 |
| checkpoint不一致/固定Apply拒否 | 既存windowテストとassessment。正常な既存経路のcheckpoint更新は合成clock ENDだけで実証 |
| authority不正/期限切れ | expired/wrong target/wrong sourceを送信前拒否 |
| writer失敗/UNKNOWN | timeout/no appendとも1要求で停止、checkpoint旧値、capability sealed |
| 再送/二重記帳 | 既存recurringの重複noopとbatch capability one-useテストを再利用 |
| 保留回収 | 合成4 reviewを保存/読戻しし、checkpoint後にGmailなしで再構築・既存hold要求。実4件の証明とは区別 |
| 正常時のみcheckpoint | confirmed成功時だけ進み、unknown/incomplete時は旧値。durable/親ledger既存テストも実行 |
| 他source影響 | app/workflow/authority/依存物の変更なし。既存銀行/Amazon/PayPay/productionテストと全CIを確認 |
| core経路 | 現行仕様がcanonicalカードを自動支出へ送らないことを新テストで確認 |

ローカルはPython3.14で関連218テスト成功。CIはPRのPython3.12全3 jobを確認して
最終PR説明へ記録する。新テスト15件はすべてsocket接続禁止の合成環境で実行する。
変更ファイルはこの文書と`tests/test_aupay_card_first_window_assessment.py`のみ。
