# R2-1 全記帳入口の棚卸し・安全な切替/drain設計

調査日: 2026-10-20。基点はPR #146固定head `7e13f6034b20ad75719ac553e031ae0813e8a3d4`。
独立branch `codex/r2-1-writer-inventory-drain`、新Draftのbaseは `codex/iphone-r1-confirmation-ui`。
main観測値は `d62e1e254692e676f4e4a72dc593524a80fbea08`。

**結論: 現行production経路の共有mandatory gateは未成立。旧通常取込・買取・確認・PDF・canary・直接SDKを統制する必要がある。**
このPRはread-onlyコード調査、設計、静的検査と既存の隔離合成回帰だけである。
production constructor/writer/runner/workflow、R1実装、native文書は変更しない。
本番Spreadsheet/Drive/Firestore/OAuth、実医療情報へのアクセス、実金融POST、authority発行/消費、原本移動、AI送信、Cloud/deployは実施しない。

**運用全入口の完全棚卸しとは報告しない。** リポジトリとその固定参照先は調査したが、実Cloud、旧host、local scheduler、配布済み資格情報、直接所有者編集、非追跡の管理scriptの実態は本番未確認である。
この不明範囲が閉じるまで本番切替の承認条件は満たさない。合成Aを本番Aへ読み替えない。

## 1. 基点・依存関係と再利用する実装

mainの `AGENTS.md` と `docs/iphone-receipt-review-mvp-spec.md` 全文（第10節を含む）、`PROJECT_STATUS.md`、`NEXT_SESSION.md` を確認した。
PROJECT_STATUS/NEXT_SESSIONの過去運用記述はlast verifiedの記録であり、現在の稼働状態の証拠にしない。
採用済み「確認」1タブ/画像whole-source/医療完全手入力/既存writer/保留の契約を変更しない。

[PR #123〜#146の開始時API記録](r2-1-pr-baseline.json)にはhead/baseのbranchとSHA、Draft/state/mergedを保存した。
各base→headのchanged pathsと固定headに継承された実装を対照した。

| PR | 継承する成果・本調査での意味 |
|---|---|
| #123/#124 | read-only eligibility/legacy quarantine、purpose分離、ConditionalRequests、native ports。旧確認gateは任意注入のまま |
| #125/#126 | mainへmerge済みの継続仕様。その他のPRのmerge/切替許可ではない |
| #127〜#131 | 1タブidentity/下書き/raw reader/観測/認証受付。下書き保存やacknowledgementは金融許可ではない |
| #132 | 裸のnative入口の共有CAS迂回を発見した停止証拠。現在も有効 |
| #133/#134 | TrustedOwnershipReader/relation、共有ConditionalRequests、隔離mandatory probe、crash fence/read-only recovery。production構成ではない |
| #135/#136 | 通常/買取のnative evidenceとmemory実writer。共有claim/start/単回dispatch/readbackの再利用候補 |
| #137/#138 | 旧確認v1の停止根拠とwriter-free契約PoC。read-then-writeをCASと呼ばない |
| #139/#140 | memory-only native v2/guarded旧writer。v1の無条件save・production constructorは未変更 |
| #141 | PDF auto/manual/grouped/canary調査。金融provider/source全体partition/start/consumeの不足を維持 |
| #142/#143 | R0差分固定、画像whole-source下書きとnative requestの分離設計 |
| #144/#145 | native v3 lineage、修正snapshot、共有claim→pending+consume→start→lease→writer→全ledger/native/shared完了 |
| #146 | 画像一般/医療の明示本人確定と結果照会を上記へ隔離合成接続。実Google金融provider未接続 |

依存鎖は `main → #123 → #124 → #127 → … → #146 → 本Draft`。#125/#126は仕様文書の別枝である。
今回、既存PRのhead/base/Draft/stateを更新する操作もmain mergeも行わない。

## 2. 一覧・証拠の読み方

[入口一覧JSON](r2-1-ingresses.json)が各入口の詳細表である。
各行に起動元/schedule、実行ref、関数chain/sink、source/Unit、validator/authority/ownership/CAS、迂回条件、重複/retry/旧状態、drain/rollback条件、最小変更と固定SHA付きファイル/行根拠を持つ。
以下の表はその索引。関数名・全経路・各sinkの根拠はJSONの `chain` / `evidence` を参照する。

- **A**: 明示したcompositionの全金融入口でgate必須と確認。ここではS01の隔離合成のみ。
- **B**: 隔離環境でmandatory gate/adapterが必要。実稼働・本番配備を認定しない。
- **C**: 旧入口を受付停止→drain→閉鎖/廃止する候補。今停止したという意味ではない。
- **D**: source/authority/履歴の証拠不足。fail-closed維持。
- **E**: 当該入口自身は金融writeなし。ただしauthority/原本/下書きの作用は別記する。

| ID/判定 | 入口 | 起動/実行ref | 到達先 | 主な迂回/不足 | 固定コード根拠 |
|---|---|---|---|---|---|
| R01 / B | 通常レシート自動取込 | kakeibo-production receipts: 31 */3 * * * UTC / CLI receipt, drive-receipts<br>main/event/validated SHA一致（実値未確認） | レシート/支出明細/取込データ/原本processed移動 | 共有store未読。R1/PDF claim中も裸pipelineがwriterへ到達。ID読取→appendはCASでない | [app/production_flow.py:77](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/production_flow.py#L77)<br>[app/production_source.py:48](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/production_source.py#L48)<br>[app/cli.py:107](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/cli.py#L107)<br>[app/drive_receipts.py:30](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/drive_receipts.py#L30)<br>[app/receipt_pipeline.py:77](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_pipeline.py#L77)<br>[app/receipt_pipeline.py:120](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_pipeline.py#L120) |
| R02 / B | 買取レシート収入 | R01と同じ<br>R01と同じ | 収入一覧 + レシート/取込データ | 別income sinkにも共有gateなし。支出と収入の二重処理 | [app/receipt_pipeline.py:63](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_pipeline.py#L63)<br>[app/receipt_pipeline.py:146](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_pipeline.py#L146)<br>[app/bank_income.py:22](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_income.py#L22) |
| R03 / C | 旧一般レシート確認 | production receiptsのscan先行 / receipt_confirmation手動scope<br>main + validated SHA（実値未確認） | レシート/支出明細/取込データ + v1 store/archive | production constructorがgate未注入。直接applyと裸writerへ到達 | [app/receipt_confirmation_production.py:128](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation_production.py#L128)<br>[app/receipt_confirmation.py:402](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation.py#L402)<br>[app/receipt_confirmation.py:498](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation.py#L498)<br>[app/receipt_reimport_production.py:80](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_reimport_production.py#L80) |
| R04 / C | 医療の旧確認・完全手入力 | R03 + 医療確認Sheet<br>main/実稼働未確認 | R03の3表/native v1 | 旧production constructorと直接writerは共有gate任意 | [app/receipt_confirmation.py:319](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation.py#L319)<br>[app/receipt_confirmation.py:402](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation.py#L402)<br>[app/receipt_confirmation.py:498](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation.py#L498) |
| R05 / B | R1本人確定の本番候補 | isolated hostへone_tab明示注入。production金融providerなし<br>固定#146、main未統合 | memory 3表/native v3/共有ConditionalRequests | 本番provider/live観測/ACLがないため本番Aではない。旧runnerは別にwrite可能 | [staging_shadow/iphone_confirmation_ui.py:154](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/iphone_confirmation_ui.py#L154)<br>[staging_shadow/synthetic_draft_writer.py:83](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/synthetic_draft_writer.py#L83)<br>[staging_shadow/receipt_draft_contract.py:296](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/receipt_draft_contract.py#L296)<br>[staging_shadow/synthetic_legacy_writer.py:150](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/synthetic_legacy_writer.py#L150) |
| S01 / A | R1隔離合成compositionのみ | #146 synthetic fixtures/isolated one_tab host<br>固定#146 memory-only | 完全合成memoryのみ | Aはこの固定memory composition内だけ。productionや任意Pythonの迂回不能性ではない | [staging_shadow/synthetic_legacy_writer.py:60](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/synthetic_legacy_writer.py#L60)<br>[staging_shadow/synthetic_legacy_writer.py:133](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/synthetic_legacy_writer.py#L133)<br>[staging_shadow/synthetic_draft_writer.py:66](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/synthetic_draft_writer.py#L66)<br>[staging_shadow/receipt_draft_contract.py:331](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/receipt_draft_contract.py#L331) |
| R06 / B | PDF自動Unit | production receipts multipage plans / PDF_INTAKE_AUTOMATION_ENABLED またはmanual dispatch<br>main validated（実値未確認） | 3表/registry intent/原本archive | 直接runnerにshared ownershipなし。wholeと別IDで二重計上し得る | [app/pdf_intake_production.py:177](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/pdf_intake_production.py#L177)<br>[app/pdf_intake_runner.py:219](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/pdf_intake_runner.py#L219)<br>[app/pdf_intake_runner.py:241](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/pdf_intake_runner.py#L241)<br>[app/pdf_intake_runner.py:334](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/pdf_intake_runner.py#L334) |
| R07 / B | PDF一般manual Unit | 既存hostがreceipt_posting proof保存→同runner。PDFManualUnitPortも存在<br>main runner + auth-session固定bundle | R06の3表/registry/journal | 共有bridge経由と直接runnerは別。shared/native/authority完全binding・独立start不足 | [auth-session/generic_review.py:87](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/auth-session/generic_review.py#L87)<br>[app/pdf_intake_runner.py:308](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/pdf_intake_runner.py#L308)<br>[app/unified_receipt_native.py:165](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/unified_receipt_native.py#L165)<br>[app/pdf_intake_authority.py:133](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/pdf_intake_authority.py#L133) |
| R08 / D | 複数ページgrouped Unit | bridge adapter注入契約 / test_tree historical load<br>app #146、processing c29ecc44固定。実host構成不明 | 3表 + historical processing store | 金融issuer/live対応証拠未確認。裸portに共通gate強制なし | [app/unified_receipt_native.py:236](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/unified_receipt_native.py#L236)<br>[app/pdf_unit_processing.py:296](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/app/pdf_unit_processing.py#L296)<br>[app/pdf_unit_processing.py:333](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/app/pdf_unit_processing.py#L333) |
| R09 / C | p14固定canary | p14-posting-canary workflow_dispatch apply/replay<br>main/event/validated SHA（実値未確認） | 3表 + PostingStore | 独立native claim。共有whole/Unit ownership・writer_startedなし | [.github/workflows/p14-posting-canary.yml:61](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/p14-posting-canary.yml#L61)<br>[app/receipt_plan/posting.py:168](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_plan/posting.py#L168)<br>[app/receipt_plan/posting.py:152](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_plan/posting.py#L152) |
| R10 / C | p4/p10明示Unit canary | receipt-unit-posting-canary workflow_dispatch<br>main/event/validated SHA（実値未確認） | 3表 + Unit PostingStore | 直接port/CLIは共通ownership強制なし | [.github/workflows/receipt-unit-posting-canary.yml:66](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/receipt-unit-posting-canary.yml#L66)<br>[app/receipt_plan/unit_posting.py:179](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_plan/unit_posting.py#L179)<br>[app/unified_receipt_native.py:315](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/unified_receipt_native.py#L315) |
| R11 / C | 歴史的PDF Unit write canary workflow | pdf-unit-write-canary workflow_dispatch WRITE_CANARY<br>inputs.approved_sha = PR #91 head c29ecc44（API観測）。main event検査後別SHA checkout | 3表 + CanaryStore | 現在appにmoduleがなくても歴史的writerに到達。current gate修正だけでは遮断不能 | [.github/workflows/pdf-unit-write-canary.yml:67](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/pdf-unit-write-canary.yml#L67)<br>[.github/workflows/pdf-unit-write-canary.yml:81](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/pdf-unit-write-canary.yml#L81)<br>[app/pdf_unit_write_canary.py:113](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/app/pdf_unit_write_canary.py#L113)<br>[app/pdf_receipt_write_canary.py:169](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/app/pdf_receipt_write_canary.py#L169) |
| R12 / C | historical/test_tree一般pipeline | auth-session.compose(test_tree=True) / 旧checkout・CLI転用<br>c29ecc44 historical tree。test_treeはCI用、実配備証明なし | 3表/収入一覧。fixtureならmemory | test_treeにはwriterあり。minimal bundle whitelistとは異なる。live資格情報と組合せると迂回 | [auth-session/compose.py:39](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/auth-session/compose.py#L39)<br>[auth-session/baseline.json:2](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/auth-session/baseline.json#L2)<br>[app/receipt_pipeline.py:106](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/app/receipt_pipeline.py#L106)<br>[app/receipt_pipeline.py:151](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/app/receipt_pipeline.py#L151) |
| R13 / E | 固定10件normal再取込・過去再解析 | production scope receipt_reimport、manualのみ<br>main validated | 結果native文書だけ。Sheetsはread_only_sheets_serviceで会計writeなし | 金融sinkなし。v1結果saveの安全性を金融pendingへ流用しない | [app/receipt_reimport_production.py:121](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_reimport_production.py#L121)<br>[app/receipt_reimport_production.py:217](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_reimport_production.py#L217)<br>[app/receipt_pipeline.py:39](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_pipeline.py#L39) |
| R14 / C | 旧CLI・local cron・旧host・直接DB/API | app.cli各種/ローカルscript/OS scheduler。外部配置の存在は未確認<br>任意checkout/SHA。production_flowを通らないCLIあり | SPREADSHEET_IDの金融表/原本/SQLite capability journal | production wrapper外のconstructor/CLI/SDK/資格情報holderはコードgate外 | [app/cli.py:93](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/cli.py#L93)<br>[app/cli.py:139](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/cli.py#L139)<br>[app/sheets.py:136](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/sheets.py#L136)<br>[app/sheets.py:301](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/sheets.py#L301) |
| D01 / D | 対応不明旧医療・旧native履歴 | H301/旧p1/legacy pending/applied/UNKNOWN<br>保存物と実稼働構成は本番未確認 | writeなし。既存ledger/native/fence保持 | 新request/新Unit/空v3で履歴を落とす迂回を拒否 | [app/iphone_accounting_ownership.py:209](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/iphone_accounting_ownership.py#L209)<br>[staging_shadow/receipt_draft_contract.py:92](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/receipt_draft_contract.py#L92)<br>[app/unified_receipt_eligibility.py:11](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/unified_receipt_eligibility.py#L11) |
| E01 / E | 旧認証host・構造/明細確認・registered proof | hash固定auth-session minimal bundle / historical human_general routes<br>c29ecc44 + hash patch/added/repository files。実配備digest未確認 | 金融ledger直接writeなし。authority/journal/session/audit保存 | 確認成功は記帳完了でない。後続R07/R09が金融sink。Firestoreを金融台帳と誤認しない | [auth-session/generic_review.py:78](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/auth-session/generic_review.py#L78)<br>[auth-session/generic_archive.py:29](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/auth-session/generic_archive.py#L29)<br>[services/human_general/receipt_review_runtime.py:36](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/services/human_general/receipt_review_runtime.py#L36)<br>[services/human_general/reconciliation_runtime.py:45](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/c29ecc44e1fb9fcc854cde1da5cce940e633e8fa/services/human_general/reconciliation_runtime.py#L45) |
| E02 / E | plan-only・構造/privacy下書き・probe | p14-plan-only/receipt-unit-plan-only/pdf-unit-readonly/UI GET/SyntheticAccountingGate<br>event SHAまたはhistorical approved_sha、probeはisolated | 金融writeなし。plan/下書き/構造saveはあり得る | import/probe成功は金融writeの証明でない。旧displayのsync/saveを純粋GETに使わない | [app/receipt_plan/planning.py:30](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_plan/planning.py#L30)<br>[app/iphone_accounting_gate.py:86](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/iphone_accounting_gate.py#L86)<br>[.github/workflows/pdf-unit-readonly.yml:75](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/pdf-unit-readonly.yml#L75)<br>[app/receipt_plan/validated.py:22](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_plan/validated.py#L22) |
| A01 / B | 通常review反映・除外・統合 | production review_apply/process-receipts/CLI review-apply<br>main/local | 支出明細/取込データ/要確認 | 既存receipt値/linksに更新しR1 ledger beforeと競合 | [app/review_pipeline.py:390](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/review_pipeline.py#L390)<br>[app/review_pipeline.py:574](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/review_pipeline.py#L574) |
| A02 / B | 自動照合・既存支出除外 | production reconcile / CLI reconcile<br>main/local | 取込データ/支出明細 status/links | 別sourceでも既存ledger target重複。R1 before読後に変更可能 | [app/reconciliation.py:346](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/reconciliation.py#L346)<br>[app/reconciliation.py:383](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/reconciliation.py#L383) |
| A03 / B | auto-expense・銀行最終反映 | production auto_expense / CLI auto-expense, bank-finalize-*<br>main/approved head/local | 支出明細/取込データ | 共有receipt gateなし。独立銀行authorityをreceiptへ読み替えない | [app/auto_expense.py:185](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/auto_expense.py#L185)<br>[app/bank_finalization.py:287](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_finalization.py#L287) |
| A04 / B | Amazon旧取込・商品展開・分割払い | CLI amazon-* / legacy workflow / 後続materialize<br>main/local | Amazon注文/商品マスタ/取込データ/支出明細 | 同会計表とreceiptに紐付くpaymentを更新可能 | [app/amazon_pipeline.py:232](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/amazon_pipeline.py#L232)<br>[app/amazon_installment.py:276](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/amazon_installment.py#L276) |
| A05 / B | Amazon recurring・money writer・canary | production core/amazon_canary / CLI amazon-*（amazon-daily-importはretired echoだけ）<br>main validated/legacy/local | 取込データ/支出明細/Amazon tables/native state | canonical transportはSDK直接append。receipt shared ownershipなし | [app/amazon_production.py:442](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/amazon_production.py#L442)<br>[app/amazon_money_writer.py:14](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/amazon_money_writer.py#L14)<br>[app/amazon_money_runtime.py:15](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/amazon_money_runtime.py#L15) |
| A06 / B | au PAYカードone-shot/batch/recurring | aupay-card-recurring-production / production core / CLI card-*<br>main validated/expected head/local | 取込データ、後続A02/A03/A05で支出 | SheetsDB.appendだけのguardではSDK直接appendを捕捉しない | [app/aupay_card_production.py:1009](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_card_production.py#L1009)<br>[app/aupay_card_production.py:1030](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_card_production.py#L1030)<br>[app/aupay_card_batch.py:592](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_card_batch.py#L592) |
| A07 / B | 銀行canonical・income recurring/backfill | bank-pdf-recurring / production drive_bank / bank-income-backfill / CLI<br>main/expected head/local | 取込データ/支出明細/収入一覧/native state | receipt shared gateなし。SDK直接writeと同ledger競合 | [app/canonical_one_row_production.py:954](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/canonical_one_row_production.py#L954)<br>[app/bank_income.py:297](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_income.py#L297)<br>[app/bank_income_recurring.py:92](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_income_recurring.py#L92)<br>[app/bank_income_backfill.py:154](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_income_backfill.py#L154) |
| A08 / B | PayPay・au PAY速報/CSV・旧card CSV | process-receipts / production drive_paypay 11 23 * * * UTC / CLI<br>main/legacy/local | 取込データ。後続reconcile/auto-expenseで支出 | import status/targetがR1 beforeと競合。個別dedupeは排他でない | [app/paypay_pipeline.py:166](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/paypay_pipeline.py#L166)<br>[app/aupay_csv_pipeline.py:113](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_csv_pipeline.py#L113)<br>[app/aupay_mail_pipeline.py:647](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_mail_pipeline.py#L647)<br>[app/aupay_card_pipeline.py:206](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_card_pipeline.py#L206) |
| A09 / B | 現金手入力・取消 | Apps Script manual-entry → workflow_dispatch manual-entry → app.manual_entry<br>main/event/validated SHA（実値未確認） | 手入力受付/支出明細 | shared gateなし。取消のM列直接updateもsink | [apps-script/manual-entry/Code.gs:81](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/apps-script/manual-entry/Code.gs#L81)<br>[app/manual_entry.py:69](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/manual_entry.py#L69)<br>[app/manual_entry.py:96](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/manual_entry.py#L96) |
| A10 / B | 日常UI固定ID修正・money review | production daily_requests / 日常form submit<br>main validated + fixed-id-v1 mode | 支出明細 + correction/projection journal/money ledger | 日常UI全体をprojection専用Eとできない。金融stageはR1 beforeと競合 | [app/daily_runtime.py:49](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/daily_runtime.py#L49)<br>[app/daily_corrections.py:109](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/daily_corrections.py#L109)<br>[app/daily_money_review.py:98](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/daily_money_review.py#L98) |
| A11 / B | 分類過去反映・restore・管理手動実行 | category-backfill-sheet 7,27,47 * * * * / category-sheet-request / Apps Script category-submit / production categories / CLI<br>main/event/local。legacy flag条件あり | 支出明細F:G + rules/request/target histories | 分類もfinancial writer。restoreも同支出を変更 | [app/category_backfill.py:356](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/category_backfill.py#L356)<br>[app/category_backfill.py:425](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/category_backfill.py#L425)<br>[app/category_sheet_requests.py:218](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/category_sheet_requests.py#L218)<br>[apps-script/category-submit/Code.gs:174](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/apps-script/category-submit/Code.gs#L174) |
| A12 / B | 台帳整列・直接Sheets/Drive/DB保守 | production ledger_order / CLI/運用script/SDK/owner編集<br>mainまたは任意script/version。実配布/ACL未確認 | 同金融台帳全行/原本/native。SQLiteはcapability/journalで別役割 | owner編集/旧SA直接SDKはPython gateだけで迂回不能にできない | [app/ledger_order.py:65](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/ledger_order.py#L65)<br>[app/sheets.py:427](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/sheets.py#L427)<br>[app/sheets.py:985](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/sheets.py#L985)<br>[app/drive_run_state.py:243](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/drive_run_state.py#L243) |
| E03 / E | 原本archive・backup・retention | receipt/confirmation/PDF terminal、monthly-maintenance 37 18 1 * * UTC<br>main/event/local | Drive parents/processed/backup/delete。金融writeなし | 金融gate外でもsource証拠を変える。再投入/削除は照合破壊。drain窓で保管 | [app/receipt_confirmation_archive.py:8](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation_archive.py#L8)<br>[app/pdf_intake_production.py:256](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/pdf_intake_production.py#L256)<br>[app/maintenance.py:114](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/maintenance.py#L114)<br>[app/drive_processed.py:7](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/drive_processed.py#L7) |
| E04 / E | 給与preview・純粋projection・schema/UI保守 | CLI drive-payroll-preview / expense refresh/daily display/ledger migration/category-navigation<br>main/event/local | 給与preview writeなし。支出一覧/日常projection/schema/UI metadata | daily_requests A10/category apply A11をEに含めない。別repo稼働を推測しない | [app/drive_payroll.py:100](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/drive_payroll.py#L100)<br>[app/expense_view.py:56](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/expense_view.py#L56)<br>[app/ledger_daily_migration.py:215](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/ledger_daily_migration.py#L215) |
| E05 / E | 旧医療automatic helper | 旧関数直接呼出し（production接続を今回復活させない）<br>固定#146 | 金融writeなし | 旧版hostに同名のactive bodyがないことは別途確認必要 | [app/medical_auto_posting.py:150](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/medical_auto_posting.py#L150) |
| E06 / E | 銀行用途確認・分類ルール受付 | category-sheet-request bank branch / 日常bank review<br>main/event/local | bank review/rules metadataだけ。processorはledger write/PDF move 0 | このprocessorだけを金融writeと数えない。後続A07のauthorityとして使われるため受付drainは必要 | [app/bank_review_requests.py:34](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_review_requests.py#L34) |
| E07 / E | retired Amazon legacy workflow | amazon-daily-import workflow_dispatch<br>event main | なし。CLI/credential注入なし | 現在workflowから金融writeなし。歴史的再実行や別checkoutはR14/R12で別確認 | [.github/workflows/amazon-daily-import.yml:1](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/amazon-daily-import.yml#L1) |
| S02 / B | 通常/買取の合成既存writer接続（本番再利用候補） | isolated #135/#136 fixtures<br>固定#146 memory-only | 合成memory 4表/native evidence/shared CASだけ | 本番constructorなし。裸R01/R02がこのguardを通る証拠ではない | [staging_shadow/synthetic_auto_writer.py:182](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/synthetic_auto_writer.py#L182)<br>[staging_shadow/auto_native_evidence.py:290](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/auto_native_evidence.py#L290) |
| S03 / B | 旧snapshot native v2合成guarded writer（本番再利用候補） | isolated #139/#140 fixtures<br>固定#146 memory-only | 合成memory 3表/v2/shared CASだけ | 旧本番v1 constructor R03/R04は別。新draft v3と旧v2 validatorを混同しない | [staging_shadow/synthetic_legacy_writer.py:166](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/staging_shadow/synthetic_legacy_writer.py#L166)<br>[app/receipt_confirmation_contract.py:380](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/receipt_confirmation_contract.py#L380) |
| A13 / B | 銀行status単セル修復・カード行修復 | operator Python API/運用script。現CLI/workflowへの直接登録は見つからず実配布は未確認<br>任意operator checkout/expected head、実行SHAは本番未確認 | 取込データstatus/関連列。後続照合・金融反映と同ledger | SDK直接update/batchUpdateで共有receipt gateなし。修復名でも台帳before/fenceと競合する | [app/bank_status_repair.py:355](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_status_repair.py#L355)<br>[app/bank_status_repair.py:394](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/bank_status_repair.py#L394)<br>[app/aupay_card_repair.py:356](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_card_repair.py#L356)<br>[app/aupay_card_repair.py:391](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/aupay_card_repair.py#L391) |
| R15 / C | 管理state移行・pending解除・初期化 | state-migration workflow / operator recovery Python API / money migration initialize<br>main/expected destination/任意operator artifact。実使用未確認 | native/source checkpoint/production ledger/money book。直接の会計行writeはないが将来の再実行制御を変える | 管理APIが存在する。関数コメントやevidence hash形状だけでは人間の実照合を保証しない。旧pendingをreadyに戻すと後のrunnerを再許可し得る | [app/drive_run_state.py:384](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/drive_run_state.py#L384)<br>[app/production_ledger.py:112](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/production_ledger.py#L112)<br>[app/state_migration.py:128](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/state_migration.py#L128)<br>[app/amazon_money_migration.py:246](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/app/amazon_money_migration.py#L246) |

### 実行参照の横断調査

固定headの45 workflowとauth-sessionの2参照ファイルを走査した。
全workflowのcron/ref/実行module/concurrencyは[静的索引のexecution_refs](r2-1-static-census.json)に保存した。
CLIの全 `add_parser` と行番号も同索引の `cli_commands` に保存し、金融経路はR01〜A13に分類した。
`.env.example`/`app/settings.py`/workflowの変数名は構成契約であり、現在の値/secret/ACLは読まない。

| workflow | コード上のschedule/ref/module（実有効状態はすべて本番未確認） |
|---|---|
| [amazon-daily-import.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/amazon-daily-import.yml) | workflow_dispatch・retired案内。body参照 |
| [aupay-card-recurring-production.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/aupay-card-recurring-production.yml) | 12: - cron: '23 20 * * *'<br>77: python -m app.cli card-gmail-recurring "${args[@]}" |
| [bank-income-backfill.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/bank-income-backfill.yml) | 39: ref: main<br>62: python -m app.bank_income_backfill --expected-head "$EXPECTED_HEAD" --apply<br>64: python -m app.bank_income_backfill --expected-head "$EXPECTED_HEAD" |
| [bank-pdf-diagnose.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/bank-pdf-diagnose.yml) | 48: run: python -m app.bank_pdf_diagnostics |
| [bank-pdf-recurring.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/bank-pdf-recurring.yml) | 5: - cron: '47 21 * * *'<br>89: python -m app.cli bank-pdf-recurring \<br>95: python -m app.cli bank-pdf-recurring \ |
| [category-backfill-sheet.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/category-backfill-sheet.yml) | 14: - cron: '7,27,47 * * * *'<br>48: run: python -m app.cli category-rule-ui-refresh<br>51: run: python -m app.cli category-rule-ui-apply<br>54: run: python -m app.cli category-backfill-ui-refresh<br>57: run: python -m app.cli category-backfill-preview-checked<br>60: run: python -m app.cli category-backfill-confirmations-refresh<br>63: run: python -m app.cli category-backfill-apply-confirmed<br>66: run: python -m app.cli category-backfill-confirmations-refresh |
| [category-navigation.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/category-navigation.yml) | 58: python -m app.category_navigation_maintenance --mode "$NAVIGATION_MODE" "${args[@]}" |
| [category-sheet-request.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/category-sheet-request.yml) | 57: ref: main<br>78: run: python -m app.category_sheet_requests |
| [gemini-availability.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/gemini-availability.yml) | 33: run: python -m app.gemini_probe |
| [kakeibo-production.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/kakeibo-production.yml) | 6: - cron: '17 9,21 * * *'<br>8: - cron: '31 */3 * * *'  # Receipt: every three hours, 00:31 through 21:31 UTC.<br>9: - cron: '47 21 * * *'   # Bank recurring apply: 06:47 JST.<br>10: - cron: '11 23 * * *'   # PayPay: 08:11 JST.<br>143: ref: main<br>174: python -m app.receipt_local_ocr prepare<br>223: python -m app.production_flow --mode "$mode" "${args[@]}" |
| [ledger-daily-migration.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/ledger-daily-migration.yml) | 72: ref: main<br>103: python -m app.ledger_daily_migration "$MIGRATION_OPERATION" --expected-sha "$EXPECTED_SHA" |
| [manual-entry.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/manual-entry.yml) | 49: run: python -m app.manual_entry |
| [monthly-maintenance.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/monthly-maintenance.yml) | 5: - cron: '37 18 1 * *'<br>32: run: python -m app.cli backup<br>35: run: python -m app.cli receipts-cleanup |
| [p14-plan-only.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/p14-plan-only.yml) | 53: run: python -m app.receipt_plan.runner |
| [p14-posting-canary.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/p14-posting-canary.yml) | 61: run: python -m app.receipt_plan.posting_live |
| [pdf-unit-readonly.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/pdf-unit-readonly.yml) | 75: ref: ${{ inputs.approved_sha }}<br>89: python -m app.receipt_local_ocr prepare<br>109: run: python -m app.pdf_unit_readonly_analysis |
| [pdf-unit-write-canary.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/pdf-unit-write-canary.yml) | 81: ref: ${{ inputs.approved_sha }}<br>94: python -m app.receipt_local_ocr prepare<br>116: run: python -m app.pdf_unit_write_canary |
| [process-receipts.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/process-receipts.yml) | 5: - cron: '17 */3 * * *'<br>53: run: python -m app.cli aupay-gmail<br>58: run: python -m app.cli drive-receipts<br>61: run: python -m app.cli review-apply<br>64: run: python -m app.cli drive-paypay<br>67: run: python -m app.cli reconcile<br>70: run: python -m app.cli auto-expense<br>73: run: python -m app.cli review-refresh<br>76: run: python -m app.cli expenses-refresh |
| [receipt-unit-plan-only.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/receipt-unit-plan-only.yml) | 58: run: python -m app.receipt_plan.unit_runner |
| [receipt-unit-posting-canary.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/receipt-unit-posting-canary.yml) | 66: run: python -m app.receipt_plan.unit_posting_live |
| [state-cache-inspect.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/state-cache-inspect.yml) | 52: ref: ${{ github.sha }}<br>115: python -m app.state_cache_inspect --runner-temp "$RUNNER_TEMP" |
| [state-migration.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/7e13f6034b20ad75719ac553e031ae0813e8a3d4/.github/workflows/state-migration.yml) | 70: ref: ${{ github.sha }}<br>129: python -m app.state_migration "$MIGRATION_OPERATION" --expected-destinations "$EXPECTED_DESTINATIONS" |

重要な違い:

1. `kakeibo-production.yml` はmain/event/validated SHA、production/legacy/schedule flagと共通concurrencyを要求する。しかし各pipelineを通るsource別pendingと、新共有source ownershipは別機構。
2. `process-receipts.yml` / card recurring / bank recurringは旧legacy flag条件を持つ。変数の現在値は未確認。コード上の条件だけで「停止済み」としない。
3. `amazon-daily-import.yml` の現在のbodyはretired案内のechoだけで金融writeなし（E07）。過去の同名runや直接CLIを現在bodyと混同しない。
4. `category-backfill-sheet`、bank income backfill、manual entryなどの個別jobは条件が異なる。同じconcurrency groupは実行順制御であり、source fence・別host・手動編集の排他ではない。
5. `pdf-unit-write-canary` はmain eventを検査してからPR #91の `inputs.approved_sha` をcheckoutする。API観測時の#91 headは `c29ecc44e1fb9fcc854cde1da5cce940e633e8fa`。現appにmoduleがないことは経路閉鎖の証拠にならない。
6. `auth-session/baseline.json` は同SHAをpinし、patch/added/repository filesをhash照合する。minimal bundleのwhitelistと `compose(test_tree=True)` が取り出す全historical treeは別物。test_treeには旧writerがある。CIのmemory成功は実hostにそれが配備されている証拠ではない。

### sink到達と危険度

| 優先度 | 実際に迂回可能な境界 | 事故条件・最小統制 |
|---|---|---|
| 最優先 | R01/R02 `process_bytes → _materialize_result` | 同sourceを旧通常取込と新R1/PDFが同時/逐次に記帳。source whole ownershipを全入口で共有し、直接materializerにもguard |
| 最優先 | R03/R04 `request_gate is not None`、gateなしconstructor、裸 `_write_accounting_plan` | 新UIだけgate化しても旧Sheet確認から到達。旧受付drain後閉鎖、constructorとsinkを必須guard |
| 最優先 | R03の `ReimportStore.save → transport.write` | before readとwriteの間に競合。DriveStateTransport.writeにIf-Matchなし。read-backは競合排他の代用にならない。旧v1をread-onlyへ |
| 最優先 | R11/R12のhistorical checkout、R14の旧host/local/direct SDK | 新branchのgateが実行コードに含まれない。旧artifact/資格情報/dispatch/re-runの統制が先。コードだけで外部holderは閉鎖不能 |
| 高 | R06/R07/R08/R09/R10のnative専用claim | UnitごとのCASはwhole/別namespaceの競合を防がない。native CASを残して外側共有claimへ結合 |
| 高 | A01〜A13の同ledger更新・SDK直接append | source別でもtarget衝突、R1 before後の修正、receiptとpaymentの二重計上。全ledger/targets/nativeを同時観測。SDK transportもguard対象 |
| 保留 | D01とR08の履歴/mapping/issuer不足 | 新request・原本再投入・空v3で回避しない。独立証拠不足はUNKNOWN/保留 |

importされたgate関数名は判定根拠にしていない。constructor→branch→実writer→保存先まで追った。
`_materialize_result` のcall siteにはPlanningDBでの計画生成もあるため、すべてを実金融writeと数えない。
逆に `SheetsDB.append` だけで全sinkを覆うとはしない。sealed card/canonical transportはSDKへ直接appendする。

## 3. source競合・旧状態の共通契約

既存 `app/iphone_accounting_ownership.py:parse_ownership/partition_valid/relation/TrustedOwnershipReader.observe` を再利用する。
新DB/CAS engine/authority schema/ログイン方式は設計しない。receiptのscopeは以下を守る。

| 左/右 | 条件 | 判定 |
|---|---|---|
| 通常/買取/旧確認/新R1 whole同士 | 同じ原本・完全一致した版でも | conflict。入口/action/request IDが異なっても同じfence |
| whole とPDF Unit/grouped/canary | 同じ原本 | 全Unitとconflict。IDの接頭辞が違っても独立にしない |
| 別Unit | 同じ完全な原本版・確定partition・独立証拠、全member/bbox非重複、native operation/ledger targetsも別 | この条件をすべて満たす場合だけindependent。native側が拒否する組合せは停止 |
| 不完全/未確定partition | 一部pageだけ/単なるUnit index/未知bbox/独立証拠なし | UNKNOWN。非競合扱いしない |
| 同source、版/partition違い | 同hashでもDrive revision/numeric/native版/grouping revisionが違う | UNKNOWN。新しいsourceと見なして取り直さない |
| 異source | native operationまたはledger target衝突 | UNKNOWN。銀行・PayPay・Amazonもこのtarget重複条件が重要 |
| 旧pending | 同sourceの別Unitを含む | 停止。TTL切れやwriter未開始らしいという推測で解除しない |
| applied/complete | 記帳済み証拠とscopeが一致 | fence保持・同要求結果照会だけ。新requestで再所有しない |
| UNKNOWN/PARTIAL/未scope履歴 | source/authority/開始/完了/lineageのどれか不足 | 照合保留。全行があるだけでも新R1 COMPLETEとしない |

source（ID/bytes SHA-256/独立Drive revisionとnumeric version/page count）、ledger（全対象ID・全before値/数式・無関係行・generation）、native（owner/purpose/operation/入力/authority revision/pending/lineage）を一体観測する。
前後readの完全一致、強いETag、単調generationを要求し、Drive版とnative版を相互代入しない。
原本の別コピーはIDが異なるだけでは同一経済事象の独立証拠にならない。既存duplicate validatorとledger照合を残す。

旧runnerが通常取込を実行中に新R1が記帳できる構成は許容しない。
旧runnerが共有claimを保存しない現状では、新R1側のstoreだけ見て「claimなし＝安全」と判断できない。
**旧writerの新規受付閉鎖・in-flightの全件照合・旧SHAからの資格情報利用統制が証明されるまでは新金融入口をenableしない。**

## 4. 切替/drain/rollbackの運用設計（実行しない）

P1〜P3は全入口に共通。receiptだけ止めて同ledgerを変更する日常修正/照合/分類を走らせ続ける切替は不可。
短い承認済み変更窓を既存concurrencyの上に設け、対象source/ledgerを固定する。新しい常駐serviceは不要。

### P1: 受付停止と実行完了待ちを分離する

1. 人間が対象workflow/手動CLI/旧host/Sheet・Apps Script受付/固定SHA/資格情報principalを一覧に署名し、停止対象・窓・照会手段を承認する。未確認hostが残れば切替延期。
2. **新規受付だけを停止**する手順を入口ごとに準備する。workflow schedule/dispatch、Apps Script受付、host金融POST、local schedulerを含める。今回は実値変更・無効化しない。
3. 停止前にaccepted済みの要求と、queued/waiting/in_progressのActions jobを別々に列挙する。concurrency queueも対象。同groupの空きを「in-flightなし」と見なさない。
4. 承認済み時点のworkflow定義blob、event/head/checkout/validated SHA、run ID/attempt/job ID、actor、scope、request/native operation、start/完了/結果を照合する。GitHubのworkflow/runs/jobs API（全page）とhostの既存request/native journalをread-onlyで確認する。
5. runnerの新規取得を閉じた後、既にwriterを開始した要求は既存契約の許す範囲で完了を待つ。無断cancel/killしない。期限到来でも強制解除せずUNKNOWNとして次の承認を求める。job completedは会計完了の証拠ではない。

### P2: job・request・保存先を全件照合する

1. Actions待機/実行中、host request、local process/cron/systemd/Windows Task Scheduler、再試行queue、旧canary/re-runの各集合を対応付ける。実host情報は運用者のread-only確認が必要。
2. 既存ProductionLedger/source DurableState、旧confirmation_items、ReimportStore records、PDF registry intent、grouped processing、canary PostingStore、共有ConditionalRequests、native v2/v3 lineage、card/bank/amazon journal/capabilityを読んで一覧化する。保存先をリセット/複製して新規扱いしない。
3. pending/claimed/executing/awaiting_readback/running/dispatching/requested/failed/UNKNOWN/PARTIAL/complete/appliedを状態名だけで同一視せず、各native requestとsource/ledgerの証拠へ対応付ける。旧AUTO医療pendingと対応不明p1/H301は凍結。
4. 原本bytes/hash/版/所在、金融全表のID・値・数式・行順序・無関係行、native raw/strong tag/generation、監査記録を既存の承認済み保存先に退避する。医療/secret/実snapshotはGit/PR/CIへ出さない。今回は新しいDrive資源も作らない。
5. 旧v1 `save`、旧SHA checkout、直接SDK/所有者編集を含む迂回が閉じたことを承認する。mainの新wrapperだけで過去runのre-runや別hostのartifactは防げない。旧資格情報を使える経路の統制はR3で別承認。
6. 全in-flightを「正確な完了証拠あり」「writeなしを独立証明」「保留/UNKNOWN」のどれかへ分類する。UNKNOWNがあっても勝手に修復しない。新規対象と重なる、または対応不明ならそのscopeは新受付不可。

### P3: 切替前後read-back・異常停止・rollback

1. 切替直前にP2 snapshotを再readし、保持対象が変わっていないこと、全runnerが同じ既存shared backend/owner/source/native readerを使うこと、gate未注入/旧schema/旧SHAがfail-closedになることを確認する。
2. 人間の別承認後だけ、画像一般/完全手入力医療の必要最小入口を段階有効化する。通常取込/買取を同時に全改修する前提にしない。未移行PDF/canaryは保留/閉鎖し、同source全体fenceを共有する。
3. 最初の新規正常一般canaryはさらに別承認。writer戻り値ではなくsource/native/ledger/sharedの独立read-back、全旧行の不変、追加記帳0の同request GET結果を確認する。原本archiveは金融完了とは別手順/別証拠で行う。
4. CAS 412/timeout/応答喪失/版・式・owner・ledger不一致/部分反映なら**新規金融受付を停止**し、照会をread-onlyへ限定する。未開始要求と開始済みを区別して保留し、再POSTしない。
5. rollbackはUI/受付を閉じて最後の安全なread-only版へ戻す。**旧v1無条件writer、gateなし旧workflow/資格情報、空native文書を復活させない**。新規writeは停止したまま、原本/台帳/native/監査を保存する。会計値やpending/fenceを自動巻戻ししない。
6. 本番UNKNOWNの解消は独立証拠と人間の個別判断が必要。推測再実行、別request、原本再投入、空native再初期化、旧authorityの移植は禁止。既存release/recovery APIが存在しても今回の承認には含まれない。

## 5. R2-2の最小実装順序（提案のみ）

| 順 | 変更候補ファイル・関数 | 既存再利用・完了条件 |
|---|---|---|
| M1 | `cli.make/make_receipt_pipeline`、`production_source.receipts`、`receipt_confirmation_production.execute`、`ReceiptPipeline.process_bytes/_materialize_result`、`ReceiptConfirmation.apply_confirmations/_write_accounting_plan` | 隔離compositionで必須gate/guarded handleを明示DI。未注入はconstructor/実body前で拒否。共有 `ConditionalRequests.read/replace/transition` と `TrustedOwnershipReader.observe/relation` を再利用。既存production constructorの本変更はR2-2承認後 |
| M2 | `staging_shadow/auto_native_evidence.py:AutoNativeEvidenceAdapter`、`synthetic_auto_writer.py:SyntheticReceiptWriter`、R1 `SyntheticDraftConfirmation/IsolatedDraftContract` のadapter境界 | 通常/買取/本人確定が同じsource wholeを所有。nativeの元validator/authorityを残し、共有claim→native pending+consume同CAS→fresh再検証→start/readback→単回lease→既存body→全表readback→native完了→shared完了。probeにwriter権限を足さない |
| M3 | `receipt_reimport_production.py:ReimportStore.save`、`receipt_confirmation.py:save_item` と旧constructor/直接writer利用者 | 旧v1は金融read-only、既存v2/v3契約を明示隔離で再利用。native schemaや保存文書の自動migrationはしない。v1履歴/pendingを空v3へ置換しない。再解析専用の非金融saveまで一括廃止せず、金融境界で拒否 |
| M4 | `pdf_intake_runner.py:PageIntake.post_manual/_post/recover`、`pdf_intake_production.py:Context.runner/process`、`unified_receipt_native.py:PDFManualUnitPort/GroupedUnitPort/ExplicitUnitPostingPort` | PDFは画像MVP後。既存RegistryStore/AuthorityAdapter/DecisionSealer/ConfirmedItemsを維持しsource全体partition、3 request binding、consume+pending/start/lease/完了証拠を接続。manual一般を先行、autoは後。grouped issuer/医療PDF mapping不明はD維持。p4/p10/p14を汎用PDFへ拡張しない |
| M5 | `pdf-unit-write-canary.yml`、p14/Unit posting workflow、legacy workflow、`auth-session/compose.py`の実行参照と旧host運用手順 | 実装は新Draftの隔離検証から。運用者が旧受付drain/旧artifact/SHAを確認後、閉鎖またはgate対応SHAだけの新経路へ限定。hash pinを外して最新へ無条件追随させない。test_treeをproduction bundle化しない。現PRではworkflow変更0 |
| M6 | `sheets.py:append/append_raw/update_row_raw/update_rows/set_raw_range/update_expense_fields/update_expense_categories`、sealed card/canonical `write_once/write_batch_once`・bank/card repair transportの `write_once`、`MoneyLedger.append`、`manual_entry.execute`、`DailyCorrections.apply`、`CategoryBackfillPipeline.apply/restore`、`order_ledgers` | 各入口のsource/ledger targets/before/native既存権限を小adapterで渡す。raw SDKも対象。独立銀行/PayPay/Amazon parserやauthority schemaを再設計しない。全表snapshotを壊すmaintenanceは同変更窓でdrain。既存SheetsDB全体に大規模な新permission engineを作らない |

**段階移行:** 先に隔離環境でM1/M2/M3と旧入口拒否を検証し、画像一般/医療の最小組合せをレビューする。
未移行入口を閉鎖・保留できることが、本番で一部だけ移行する前提。稼働したままの未gate通常取込と新R1を併存させない。
PDFはM4で後続、canaryは新機能を追加せずM5で閉鎖優先。M6は共有資源を変更する入口のadapter/運用統制に限定する。

R2-2の隔離検証は既存2/16並列、通常↔買取↔R1↔PDF whole/Unit、同/別request、同/別backend誤配線、native/ledger/source race、全CAS境界の412/応答喪失、writer前/各行後/native完了後/shared完了前crashを再利用/拡張する。
合格条件は二重dispatch/追加write0、未設定直接sink拒否、UNKNOWN維持、GET書込み0、全旧ledgerとnative履歴不変。
すべての競合をCOMPLETEにする条件ではない。

## 6. 静的検証・CIと限界

追加検査は `tests/r2_1_static_audit.py` と `tests/test_r2_1_static_audit.py`。
appをimportせず、gitの固定treeを文字列/ASTとして読み、候補call siteと参照・source hash・行番号を保存する。
39分類行（A=1は隔離合成のみ、B=20、C=8、D=2、E=8）。R15は直接金融writeでなく再実行許可を変える統制経路としてCに含める。
現基点299 Pythonファイル/2,407候補call、historical 292ファイル/2,216候補callを収録した。
これはlist.append/dict.updateやmemory/planも含む**構文候補数**で、金融writer数や全到達経路の証明数ではない。
通常のmaterializer/confirmation delegate全12 call site、SDK直接sink、v1 save、optional gate、historical checkout、金融と非金融の境界を対照する。
固定headのproduction/R1/auth bundle/既存workflowのsource hashと差分allowlistも検査する。

専用 `r2-1-static-audit.yml` はUbuntu 24.04/Python 3.12、exact PR head、contents:read、credentials保持なし、Secrets/Variables/environment/schedule/deployなし。
静的16テストと既存の隔離合成安全回帰を分け、実際のpassed/failed/skippedをCIのstep/JUnit summaryで報告する。
既存tests/conftest.pyがlive socket接続を拒否する。Google SDKをインストールしてもlive clientや資格情報は与えない。

CI URL/確定head/実行済み件数/未実行範囲は新Draft PR本文に記録する。失敗をskip/xfailへ変更しない。
静的テストは「危険経路を見落とさず現状を固定する」検査であり、発見した経路を閉鎖した検査ではない。

限界: 任意の `getattr`/動的DI/monkeypatch/外部コード、過去の任意SHA、未追跡script、CI外host、Google ACL/本番データ/実backendの耐久性、正しいlive reader、実request/in-flight、現在のActions有効状態は証明しない。
今回はブラウザー・iPhone・本番Cloudの検証なし。#146のブラウザー成功件数を本CIへ加算しない。

## 7. 本番確認台帳・R3の前提・承認点

| 確認済み | 本番未確認（今回アクセスしない） |
|---|---|
| 固定headのコード/call graph/sink、workflow定義/cron/ref条件 | workflow enabled/disabled、schedule flag/validated SHAの実値、現在/queued jobと本番checkout SHA |
| GitHub APIの#123〜#146 metadata、#91 head、cloneしたmain SHA | 実Cloud revision/image digest/traffic、旧host/local scheduler/manual script、実行principal/ACL |
| minimal bundle whitelist・historical tree・conditional transportのコード | 配備済みbundle digest、実Firestore/Drive CAS/耐久性/監査/保持/鍵/復旧能力 |
| R1は合成native v3/guarded writerだけという制約 | live source/partition/native/ledger/pending読取りの完全性、実pending/UNKNOWN、実医療mapping |
| 本PRの許可差分と静的/合成CI（結果はPR本文） | 実Google OAuth/追加ユーザー認可、iPhone Safari/Sheets本人確定の実機受入 |

R3で実Google接続へ進む前に必要な条件:

1. R2-2の隔離mandatory gate/直接sink拒否/同原本競合/crash/CAS試験が合格し、全旧受付のdrain対象と不明hostの扱いを人間が承認。
2. 同じ既存shared backendの実体/ACL/strong conditional update/耐久性/鍵/保持/監査/backup・復旧をread-onlyで検証できる計画。合成のmemory_only制約を外すだけでは不可。
3. 実source revision bytes、完全partition、native request/authority/pending、全ledger値/数式の独立live readerとbefore/after binding。旧v1→新native migrationは別設計/別承認、空初期化不可。
4. 既存本人認証のsubject/owner/session/Origin/CSRF/TTLをlive hostへ結合。妻等の追加主体・医療閲覧範囲は明示認可。新login/schemaを作らない。
5. 監査とread-only結果照会、UNKNOWN時停止、旧writerを復活させないrollback、対象source/ledgerと原本保管がレビュー済み。
6. 実OAuth/read-only Google接続、Cloud設定/資格情報/ACL変更、main統合/deploy、旧runner停止・schedule/固定SHA切替、新規一般canary/本番金融write/原本移動、医療・PDF本番導入はそれぞれ対象・影響・復旧を示した**人間の別承認**が必要。

本R2-1が承認しているのは文書・一覧・静的検査・専用CIと新Draft保存まで。
R2-2実装、本番gate導入、旧job停止、workflow切替、実Google接続は実行していない。
