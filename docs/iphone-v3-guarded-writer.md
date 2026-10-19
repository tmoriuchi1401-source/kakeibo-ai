# R1 writer隔離合成接続

基点は#144固定head `759f444526a9266d2cae49c8758de2fd5ec94a54`。
mainのAGENTS/継続仕様、#139～#144、#143設計を確認して独立分岐した。
今回は修正下書き→既存writer→memory台帳→独立結果照合まで。
確認1タブUI、本人確定UI、PC/390px/実機、本番金融authority/POST、R2/R3は実装・検証しない。

## 実装と旧契約の境界

| 箇所 | 実装 |
|---|---|
| `staging_shadow/synthetic_draft_writer.py:SyntheticDraftConfirmation` | 既存`SyntheticLegacyConfirmation`の境界を継承する新v3専用adapter。`__init__/submit/_current/_native_completed/inspect`を明示分離 |
| 同 `_dispatch_once` | #140の既存`_MemoryLedger`と`_GuardedConfirmation`をそのまま利用。既存delegateの一度限りlease/call-frame/order/scope guardを通す |
| 同 `_claim/_finish_shared` | 既存ConditionalRequests/ownershipを利用。完全read-backとstrong tag、native completion、元のclaimを照合 |
| `staging_shadow/receipt_draft_contract.py` | v3だけに閉じたdispatch/complete_fixtureと既存`complete_fixture`状態を接続。通常呼出しは拒否のまま |
| 同 `validate_operation/IsolatedDraftStore._commit` | completionの全bindingと書込み元contextを検査。trusted parent pin/legacy/旧operationsの不変条件は維持 |
| 新tests/専用Ubuntu CI | 新writer統合と既存#142～#144・安全回帰を別集計 |

**`app/receipt_confirmation.py`のwriter本体、旧v2 contract/validator、
`staging_shadow/synthetic_legacy_writer.py`、金融authority、共有CAS engine、認証host、privacy/duplicate/replayは変更0。**
新しいfinancial sink呼出しも追加せず、既存sink censusの許可一覧を変えない。
v3の新request markerは`receipt-confirmation-draft-inputs-v2`、保存rootは`receipt-reimport-v3`のまま。
completionは#139/#140と同じ7 field（mode/fingerprint/start_digest/pending_digest/snapshot_digest/observation/at）を再利用する。
新schema/authority/DB/CAS engine/保存文書migrationは追加しない。

## 実行順序

1. baseline/draft/原本bytesと版/master/全ledgerを独立再読取りし、新validatorでparsed/planを再計算。
2. 既存PurposeBoundary/VerifiedPostでowner/session/CSRF/purpose/issuer/TTL/exact inputsを照合し、native qualificationをCAS保存。
3. 共有CASでaccepted→executing。全scopeのownership relationとrequest fingerprintを照合。
4. native pending_projection＋fixture consumeを同一CASで保存。legacyは変更しない。
5. source/draft/master/authority/ledgerを再検証し、writer_startedをCAS保存、raw/tagを完全read-back。
6. 新contractのstart ticketを消費し、閉じたcomposition contextで既存guarded delegateのleaseを一度だけ消費。
7. 既存`ReceiptConfirmation._write_accounting_plan`が既存DB APIのupdate/appendを呼ぶ。
8. 各DB writeの前にもsource/draft/master/authority/claim/native証拠とledgerの既知prefix・tagを照合。
9. 本体のsave_itemから来た正確なapplied結果、全DB events、全ledger値/数式/順序/既存行を照合。
10. 実writer call-frameが生きている間だけnative completionをCAS保存。独立read-back後、共有awaiting_readback→completeと照合。

`dispatch/complete_fixture`は明示adapterの正規呼出し元・一度限りticket・実writer contextが揃わなければ拒否する。
偽のapplied戻り値、全ledger行を置いただけのfixture、直接callback、旧apply_confirmations/v1 save、過去pending再開は通さない。
保存pendingはpendingのまま保持し、applied execution projectionを保存済みbaselineへ戻さない。

## 合成記帳の比較

| 項目 | 一般original | 一般proposed/実ledger | 医療original | 医療proposed/実ledger |
|---|---|---|---|---|
| 日付 | 2026-01-02 | 2026-01-03 | 2026-01-02 | 2026-01-03 |
| 店舗/施設 | Synthetic shop | Synthetic edited shop | Synthetic clinic | Synthetic manual clinic |
| 明細01 | 70 | 80 | 120 | 135 |
| 明細02 | 50 | 40 | — | — |
| 合計 | 120 | 120 | 120 | 135 |
| カテゴリ | 食費/食品 | 01日用品/消耗品、02食費/食品 | 医療・保険/病院 | 医療・保険/病院 |
| 支払 | 現金 | Synthetic card | 現金 | Synthetic card |

receipt `R-synthetic-legacy-stop`、明細`-01/-02`（医療は-01）、import `receipt:synthetic-legacy-stop`を維持。
一般はreceipt更新→既存明細01更新→baseline catalog内の明細02追加→import追加の4 DB write。
医療はreceipt/本人確認支出/importの3 DB append。
一般の既存明細01の`preserve-label`、既存receipt timestamp、無関係行、数式`=1+2`と全順序を保持する。
完成行の直接代入は成功試験に使わず、実writerのDB calls/events/lease消費を計測する。
医療は保存draftの完全手入力だけ。部分入力/AI/OCR由来/旧parsed補完を拒否する。
メモはconfirmed snapshotに保持し、既存writerが会計行へ保存しない項目の新仕様は追加しない。

## 三つの保存先と停止・照会

native、共有CAS、ledgerは別々のversioned IOであり、単一transactionではない。
各CASの412/timeout/応答喪失/read-back不一致は、その場で停止し、追加writeで不明状態を修復しない。
startが不明ならdispatch 0。dispatch後は書込み0件に見えてもlease/startから再実行しない。
部分ledgerはPARTIAL、全行があってもnative completionが無ければUNKNOWN。
native completionがあっても共有completeが無ければUNKNOWN。照会で共有completeへ補完しない。
最後の共有CAS ACKだけを失っても、全証拠が既に保存済みなら再構築後のread-only照合でCOMPLETEになり得る。追加writeは0。
現在のdraft/source/master/ledger tagや証拠が一致しない場合は成功表示しない。
synthetic-only/real_native_complete=false/accounting_permission=falseを維持し、合成資格を実金融authorityへ昇格しない。

再構築はdefault read-only、fresh_attempt/fresh_processは初回の明示fixture compositionに限る。
同じrequestの再送はread-only、異内容・別requestでの同一operation再利用は拒否する。
trusted parent pin、旧v2 operations、legacyの全履歴と既存fenceを保持する。
G103/H301/旧医療p1/PDF・旧pending/applied/UNKNOWNを自動解除しない。

## 検証・完了判定

新規testsは一般/医療の実writer成功、各停止点、CAS 412/timeout/lost response/read-back/tag、
source/draft/master/authority drift、改ざん、auth、同request再送/別内容再利用、2/16 instances、
別process再構築、通常取込/買取/PDF Unit ownership競合、旧状態/保留、直接呼出しの拒否を検査する。
v1 save/applyと実client/networkはfail spy。新規件数・旧証拠473件・既存安全回帰とhead/runはPR本文に分離記載する。
旧#142/#143のdiff guardだけ、今回承認されたv3専用adapter追加を許す。旧writer/validator本体は変更不可のまま。
専用Ubuntu CIが全群を通過した場合に、writer隔離合成接続だけを完了と判定する。
本番Spreadsheet/Drive/Firestore/OAuth/医療情報、Cloud、実金融POST/authority/会計write、原本移動、AI送信は0。
既存main/PR、production runner/workflow、本番保存文書、正式1タブUIは変更0。

## R1本人確定UI統合に残る最小差分（今回は未実装）

1. `app/iphone_item_drafts.py:issue/inspect`：PDF Unitとは別の画像whole-source catalog・保存revision/strong tagを注入。
2. `app/iphone_confirmation.py:ConfirmationService.read/proof/post`：保存draftからv2 inputsを作り、全入力/plan比較表示と本人の明示操作にbinding。既存acknowledge_snapshotを金融authorityにしない。
3. `app/iphone_confirmation_host.py:make_isolated_confirmation_application`：既存認証hostの資格検証を注入し、明示合成adapterへ接続。結果GETは`SyntheticDraftConfirmation.inspect`だけにする。
4. PC/390pxで入力保持・原本往復・session切れ・二重タップ・保留・結果を統合試験。その後iPhone実機受入。

今回の正常/異常系とCI成功後は、この次段の隔離UI統合へ進めると判定できる。
実UIへの着手、本番authority/POST、R2/R3、実backend/migration、本番canary/有効化/main統合はそれぞれ別承認。
