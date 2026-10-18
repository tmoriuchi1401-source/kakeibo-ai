# Phase 5B-2B-1：native入口の棚卸しと安全停止

状態：**未完了・安全停止**。#131のhead `d781d1863cfabb57f08640b76b5fed0927c641f2` から独立分岐した調査成果。共有CASを全入口へ統合したという成果ではない。新しい実行composition、金融authority、writer接続、HTTP入口は追加していない。

ユーザー指定の「共有CASを通らない入口が見つかった場合は、安全に統合するか、統合不能として停止」「重大な未統合入口や競合を発見した場合は原因と対策を記録して停止」に該当したため、下記の未解決条件を残して実行経路の実装を停止した。CI成功も、この停止判断と既存回帰が再現できるという意味である。

## 停止理由

1. **定期処理には、金融claimを共有しない全原本単位の経路がある。** `production_flow.invoke → cli.make_receipt_pipeline / drive_receipts.process_inbox → ReceiptPipeline.process_bytes → _materialize_result`。`process_bytes` はsource IDとbytes等を受け取るが、native source version、page/Unit/index、authority revision、request ID、expected CAS revisionを受け取らない。通常のprivacy/明細validatorと既存import ID確認は存在するが、`ConditionalRequests` は呼ばない。`_materialize_result` は既存の会計appendへ到達し、買取では収入記帳も行う。
2. **旧領収書確認のgateは任意設定。** `receipt_confirmation_production.execute` は `ReceiptConfirmation(store, db, metadata)` を作り、共有gateを設定せずに `apply_confirmations()` を呼ぶ。`ReceiptConfirmationPort.bind_shared_gate` は将来ホスト向けの注入契約であり、このrunnerへの接続証拠ではない。未設定の実装を合成データで呼ぶと、共有CASの操作0件のまま `_write_accounting_plan` 直前へ到達した。実writerは呼んでいない。
3. **PDFには独立したnative claimがある。** `pdf_intake_production.process → PageIntake.post_normal / post_manual → _post` はRegistryStore内のUnit intentをclaimする。`PDFManualUnitPort` が外側で共有CASを使うことと、直接呼ぶrunnerもそのCASを通ることは別である。合成の有効な既存proof/snapshotを使った直接 `post_manual` は、共有CASの操作0件のまま `_post` 直前へ到達した。そこで遮断し、Unit intentも会計も変更していない。
4. **identityを推測で統一できない。** 旧画像の `R-<source_id>`、登録PDFの `R-pdf-receipt-<Unit suffix>`、明示Unitの `receipt-unit-posting/context/request`、複数ページの `pdf-unit-processing/Unit` は別の既存名前空間。旧画像recordのsource/versionだけから、全原本と各Unitとの所有関係を確定できない。`legacy_shared_claim=True` の合成検査結果を作るだけでは、この欠落を解決しない。source全体とUnitの対応、同一hashでのversion変更、旧pendingの既存ledger所有権を、信頼済みreaderで固定する条件が不足している。

これは既存本番で二重記帳が発生したという観測ではない。本番データを読んでおらず、実稼働・設定の有無も検証していない。**全入口で二重の実行権限取得が0という今回の完成条件を、現コードからは認定できない**という判定である。別のfake storeを並べたり、不明なUnit/version/authorityを補って「共有済み」とする修正は行わない。

## 入口棚卸し

対象はreceipt/PDF確認とそれに到達するrunner、writerおよび認証ホスト。現在のツリーと、既存composerが固定する歴史的baseline `c29ecc44e1fb9fcc854cde1da5cce940e633e8fa` ＋ `auth-session/existing-service.patch` を区別して調査した。以下の「共有」は本番接続済みという意味ではない。

| 入口・コード参照 | purpose / action | native identity・authority | CAS保存先・実行前validator | 更新先・duplicate / replay / unknown | 判定 |
|---|---|---|---|---|---|
| `drive_receipts.process_inbox → receipt_pipeline.ReceiptPipeline.process_bytes` | ACCOUNTING / 通常解析後materialize、買取収入も含む | `receipt:<source ID>` / `R-<source ID>`。privacy＋通常validatorであり明示Unit grantではない | import ID既存確認。共有request CASなし。`evaluate_receipt_privacy`、`validate_receipt_result`、取引種別検査 | 会計3表、買取は収入。既存IDを参照するが、共有claim・全入口でのunknown fenceの証拠なし | **停止要因A** |
| `receipt_confirmation_production.execute → ReceiptConfirmation.apply_confirmations`（一般、医療、旧確認、定期runner） | ACCOUNTING / 確定、既存支出紐付け | `confirmation_operation_identity` = receipt namespace＋source/hash/version。旧recordは全原本単位。本人入力・旧判断 | native reimport/confirmation stateのpending intent。共有gateは任意。`verify_source`、live UI一致、`_parsed`、`_plan`、既存明細照合 | `_write_accounting_plan` の既存append/update。pendingは `_complete` の正確なread-backのみ | **停止要因B**。gate未設定を合成再現 |
| `ReceiptConfirmationPort.bind_shared_gate / submit_legacy → UnifiedWriteBridge.submit` | ACCOUNTING / post | 上記native identity。trusted NativeInspection＋既存owner確認 | 注入されたConditionalRequests＋旧native pending。`_current`、既存明細validator、既存plan、eligibility | native ReceiptConfirmationへ委譲。replayは再executeなし、unknownはread-backのみ | 明示接続したfixtureでは安全性あり。全runner接続は未実施 |
| `pdf_intake_production.process → PageIntake.post_normal` | ACCOUNTING / 自動Unit post | `pdf_operation_identity(page, Unit)`相当の対象。native page AI gate＋自動正常候補 | RegistryStore（strong ETag）のUnit intent。page privacy、untouched候補、evaluate、カテゴリ、重複、ledger | ScopedWriter＋既存materializer。intent既存ならrecover、unknownでappend再送なし | **停止要因C**。外側共有CAS未接続 |
| 同process → `PageIntake.post_manual` | ACCOUNTING / 明示Unit post | page/Unit/index。別途sealed posting authorityとConfirmedItems | 同RegistryStore。page gate、posting proof、actor、exact snapshot、既存item validator、plan digest | `_post` →ScopedWriter。intent既存ならrecover、正確なread-back | **停止要因C**。直前遮断の合成再現 |
| `PDFManualUnitPort.inspect / execute` | ACCOUNTING / post | `pdf_operation_identity`。独立のnative posting proofが必要 | 注入ConditionalRequests＋RegistryStore。`post_manual(apply=False)`の既存validator、snapshot/Unit/source照合 | native `post_manual(apply=True)`。bridgeのUnit fence、unknown read-back専用 | 外側adapterは存在。直接runnerの共有を証明しない |
| `GroupedUnitPort` | ACCOUNTING / grouped Unit post | `digest(['pdf-unit-processing', unit_id])`。trusted GroupedPlanと既存grouping/privacy/owner | ConditionalRequests＋歴史的 `pdf_unit_processing.DriveUnitProcessingStore.reserve`。native `_plan`、現Unit、ledger、plan照合 | ScopedWriter＋materializer。reserve既存ならread-backのみ、partialで再dispatch不可 | adapter契約。全legacyのreserve入口接続は未証明 |
| `receipt_plan.posting_live.main → posting.execute` | ACCOUNTING / apply、replay（固定canary） | 既存承認grantとrequest、page/snapshot/plan | 専用PostingStore（Drive v2 If-Match）。fresh plan、owner、TTL、grant、ledger、projection preflight | ScopedMaterializerDB＋既存materializer。unused→claimed/unknown/complete、既claimはoutcomeのみ | 既存native CASはあるがConditionalRequestsとは別 |
| `receipt_plan.unit_posting_live.main → unit_posting.execute` / `ExplicitUnitPostingPort` | ACCOUNTING / Unit apply、replay | `digest(['receipt-unit-posting', context, native rid])`。独立の承認grant | 専用UnitPostingStore＋（adapter時のみ）ConditionalRequests。`u.prepare`、bound、plan_receipt、native request ID固定、fence/budget | 同ScopedMaterializerDB。既claimはread-back、appendを繰り返さない | 全入口共有は未完。p4/p10の既存制限維持 |
| 歴史的 `pdf_grouping_authority.DurablePdfGrouping.review` / `ExistingReviewPort` | STRUCTURE / confirm_grouping（confirm/edit/reject/hold） | source/hash、proposal digest、grouping revision、partition。金融grantではない | DriveGroupingStore If-Match＋read-back。fresh original/proposal/partition、audit request digest | grouping confirmation/Unit定義。replayは同結果またはstale、会計appendなし | nativeの直接入口とPurposeBoundaryの共有接続は未完 |
| 歴史的 `pdf_page_kind.PageKindConfirmation.confirm` | METADATA / confirm_page_kind | source/page/proposal revision、selection。会計許可なし | grouping store。native expected proposal、全page kind検査、既存敏感分類保護 | page_kinds/audit。旧grouping権限を必要に応じ無効化、再送で同値 | 共有接続は未完 |
| 歴史的 `human_general_authority.HumanGeneralConfirmation.confirm / hold` | METADATA / confirm_privacy | source/page/review identity、authority revision。限定AI consentであり会計許可ではない | HumanGeneralAuthorityStore。native actor/eligibility/source freshness/revision/TTL | 専用grant/audit。request再利用拒否、同grant再照合 | 共有接続は未完。今回grant実更新なし |
| 歴史的 `receipt_reconciliation_authority.ReconciliationConfirmation.confirm` | RECONCILIATION / link_existing（同一・別・判断不可） | current page-local identity（Unit空も合法）、ledger candidate＋snapshot、native request | AuditRepositoryの既存revision/request境界。protected binding、VerifiedActor、Google owner、TTL、原本hash、ledger read-back | reconciliation event/current。sameはlink、differentでも記帳許可にはしない、unknownは保留 | 空Unitを会計Unitへ捏造しない。共有接続は未完 |
| 歴史的host `web.confirm → gateway.confirm`、`receipt_review_runtime`、`reconciliation_runtime`、`real_runtime` | 既存各purpose | Google issuer/sub、session/browser ticket、purpose別native binding | backend FirestoreConditionalStateは認証state用。receipt item queue、audit、native registryは別境界 | 確認proof/queue/metadata。金融writerに自動変換しない | 認証stateのCASを金融CAS共有済みと扱わない |
| `PurposeBoundary.prepare / submit / recover_readback` | STRUCTURE/METADATA/RECONCILIATION/ACCOUNTINGを分離 | ReviewObservationまたはNativeInspectionからserver側identity | 同一ConditionalRequestsをaccounting controllerと共有する検査あり。既存owner/POST/CSRF/TTL/evidence検査 | native port。accepted→executing→awaiting_readback→complete、partial_unknownは明示read-backのみ | 契約あり。legacy native呼出しを自動で捕捉しない |
| `iphone_confirmation_host → ConfirmationService.post`（新1タブUI詳細） | 各表示purpose / save_draft、acknowledge_snapshotだけ | #131のactor/session/原本/入力snapshotへ固定した**合成受付** | PersistentJournalの暗号化下書きCAS。既存session/CSRF/owner、source observation、明細validator | 合成受付repositoryだけ。request同一内容再照合、異内容拒否、unknown自動再送なし | 金融ConditionalRequests・native authorityへの昇格禁止 |
| 将来の1タブUI確定入口 | 未登録 | #131受付をauthorityとしない。独立したnative authorityが必要 | 未接続。GETにclaim/観測を足さない | writer/operation targetを発行しない | 今回追加なし |
| `receipt_reimport_production.run_reimport` | 再解析/比較 / reanalyze、replay | source/hash/version＋manifest | ReimportStoreの既存CAS。source/model/既request/比較検査 | 解析record・比較結果のみ。直接会計修正しない（次の旧確認が別入口） | 実行入口への前段として棚卸し。AI送信も今回未接続 |
| `receipt_plan.runner / unit_runner`、`validated.accounting_plan`、`pdf_intake_runner.plan`、`GroupedUnitPort._plan` | intake/検証/plan | native schema/identity/token/field marker | 既存queue/capture、またはmemory PlanningDB | queue更新またはmemory plan。materializerという名前だけで実会計呼出しと混同しない | downstreamのpostとは別。直接会計許可なし |
| `medical_auto_posting.apply_automatic` | 廃止入口 | なし | 現実装は常に0、`ReceiptConfirmation._plan(automatic=True)`は医療を拒否 | 実行なし。旧automatic pendingはfreeze | 復活・medical AI/自動記帳の許可なし |
| `ExistingAuthorityPort` | 旧financial形metadata adapter | なし | inspect/execute/readbackすべてpurpose route requiredで拒否 | 実行なし | 復活させない |

`rg`で全現行 `app` の `_materialize_result` / `_write_accounting_plan` 呼出しを確認し、plan専用の呼出しも上表へ区別した。歴史的native実装は既存composerのhash固定コードを調査した。テストは主要6本の実call edgeをASTでも固定する。これは動的dispatchや外部の実稼働compositionまで網羅した形式証明ではない。**列挙漏れ0・全native入口統合済みの認定は停止中**。本番データ/設定を見ずに実稼働入口一覧を断定しない。

## 条件付きCASの既存再利用と限界

- `ConditionalRequests.read/replace/transition` はstrong ETag、原文一致、条件付き更新、正確なread-backを再利用する。新CASエンジン/DB/サービスは不要。`UnifiedWriteBridge` はcanonical native target、request内容、source/page/Unit/index/snapshot/authority revisionを検査し、同一Unitのfinancial operationを再所有させない。
- `PurposeBoundary` は同じConditionalRequestsオブジェクトをaccounting bridgeと共有する条件を検査する。purposeの違いを会計権限の同義語にしない。reconciliationの空Unitとaccounting Unitを結合しない。
- `PersistentJournal` はPhase 5B-1の下書きCAS。認証state、合成本人確認受付、金融claimの3者を混用しない。
- 今回の最大16インスタンス試験では、各bridgeが別ConditionalRequestsインスタンスを持ち、**単一の同じatomic Memory backendオブジェクト**を参照する。別々のfake storeではない。生成するのは実行probeのカウンタだけで、実writer、native execute、authority、会計行を作らない。
- この限定試験で実行probe最大1回、lost response/partial_unknownで別入口から再dispatch 0を検証しても、上表の未接続legacy経路への保証にはならない。実Firestoreの耐久性/IAM/保存期間/鍵/backup/復旧/監査/費用は未検証。

## 停止時の自動検証

`tests/test_native_ingress_safety_stop.py` は26件。未接続の旧ReceiptConfirmation 2件、直接PDF manual 1件はwriter直前の例外probeで遮断する。旧領収書は合成native storeにpendingを保存するところまで進むが、会計3表と共有request storeは変化しない。PDF probeではnative registryも不変。privileged validator/actor/source検査は緩和せず、既存の合成fixtureとそのvalidatorを使う。自動ReceiptPipelineの欠けた引数とwriter到達辺はAST/実signatureで確認し、AIやwriterを実行しない。

接続済みbridgeの参考検証は1/2/16インスタンス×同ID/異IDの6条件。同一backendで最大1実行probe、process側wrapper再生成のread-back、lost acceptance response、partial/lost probe response、別入口replay、read-back専用復旧、version/Unit/page/snapshot/authority不一致、default OFFを検証する。wrapper再生成は実DB耐久性の証明ではない。

専用 `iphone-phase5b2b1-checks.yml` はUbuntu 24.04/Python 3.12、contents:read、write flag false、Secrets/Variables/environmentなし、PRの正確なhead、歴史的git object取得のみ。新規26件と#131までの792件＋新CI境界1件を実行する。既存安全回帰内のwriter試験は従来どおりfake DBであり、本番writer/本番authorityではない。今回の新規probeは既存writerを呼ばない。CI確定結果は新Draft PR本文・報告に記録する。

未実施：全入口の同一composition、全purpose間の同時claim、未統合経路を含む二重実行権限0の証明、全入口のunknown回復、全入口の認証/Sheet並べ替え結合検証。#131までのGET副作用0・認証・validator・下書き/受付回帰を再実行することと、これらの未実施項目を取り違えない。

## 再開時の具体的対策・次工程

Phase 5B-2B-2へは進めない。まず5B-2B-1の次の不足を解決する。

1. **対象の明確化とtrusted reader**：全原本writer（買取収入を含む）のlegacy operationと、新UI対象のpage/Unit操作の重なり/対象外条件を明示する。独立に観測したsource ID/hash/native version、ページ、Unit ownership、ledger ID、入力snapshot、authority revisionを固定し、情報不足はUNKNOWN。全原本を仮のUnitに置換しない。live readerは別承認まで未接続。
2. **全legacy compositionへの強制gate**：ReceiptConfirmationだけでなく、ReceiptPipeline、PDF auto/manual、grouped reserve、固定canary post、metadata/reconciliationのnative入口を列挙した同一隔離compositionで捕捉する。nativeの直接呼出しが共有CAS前にwriter境界へ到達したら合成起動を拒否する。既存のnative intent/authority/CASを残して外側共有claimと結ぶ。実行先の差替えでvalidatorを省略しない。
3. **共有backendの証拠**：すべての新旧インスタンスを同じConditionalRequests保存先へ接続。source全体対Unitの所有競合、purpose/actionの違い、同hash/version変更、旧pendingとの競合を含め16-wayで検証する。各入口で独立fake storeを用意して合格にしない。
4. **unknown境界の接続**：外側accepted、native intent claimed、writer開始前後、ledger部分反映、native/外側完了応答喪失をそれぞれ注入する。別入口での自動再dispatchは禁止。既存native read-backの明示操作だけで照合し、未実行claimを後から補完しない。破損は隔離し手動復旧の条件を固定する。
5. これらが合成で揃って初めて、5B-2B-2の既存authority/writer**境界の設計**、実行直前原本/ledger/入力再検証、共有CASの実運用設計へ引き継ぐ。#131受付はあくまで合成本人確認受付で、資格・operation target・native grantを持たない。

今回の合成adapter調査/テストは既に承認された範囲。再度の本番承認を求めて作業を止めているわけではない。停止理由は上記の安全上の未統合である。本番接続、runner/workflow変更、authority schema変更、live reader/実Firestore/実OAuth、鍵/IAM/保存/復旧運用、追加ユーザーの認可、公開実機環境、実writer、本番切替はそれぞれ具体案と個別承認が必要。これらを今回実施しない。

## PR依存関係とmain統合順序

調査時点ですべてOPEN/Draft：

`main → #123 → #124 → #127 → #128 → #129 → #130 → #131 → 今回の安全停止Draft`

今回のbaseは#131のheadブランチ `codex/phase5b2a-synthetic-confirmation`。既存7件のhead/base/Draftを保持する。将来統合する場合もこの順の依存を維持し、各段のレビュー・回帰・不足解消・個別main merge承認が先。今回の安全停止PRをmergeしても全入口共有CASが完成するわけではない。既存Draftの変更、main merge、Cloud、デプロイ、Spreadsheet/検証ブック/Drive資源/本番Firestore/OAuth/実医療情報/本番会計/authority/原本移動/AI送信は今回0。
