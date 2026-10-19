# iPhone MVP R0：採用差分とR1停止判定

2026-10-19。**R0の棚卸し・採用分類・停止判定を完了。R1は未着手。**
今回の完成条件「一般・医療の本人確定→合成記帳→結果照会」は未達。
下書きとnativeの入力契約に重大な接続上の矛盾を確認したため、ユーザー指定のR0停止条件を適用する。
既存実装の安全条件が壊れているという判定ではなく、既存の拒否条件を維持したまま
要求された修正後snapshotを直接渡す契約が存在しないという判定である。

## 基点・依存・レビュー単位

- main：`d62e1e254692e676f4e4a72dc593524a80fbea08`。
- #141固定head：`bcf8f2ce7b791a1a65b8e06d1b3d0e3c83de77dc`。
- 新ブランチ：`codex/iphone-mvp-r0-r1-synthetic`。新Draftのbaseは#141の
  `codex/phase5b2b2d1-pdf-native-design`。既存PRを組み替えず、そのheadから独立分岐。
- mainのAGENTS.md、継続仕様全文と第10節、#123～#141の説明・ref/Draft/state、
  #141のPDF設計・MVPロードマップ、関連実コードとテストを確認した。
- #125/#126はmerged。残る17件はOPEN/Draft。
  依存は `main → #123 → #124 → #127 → #128 → #129 → #130 → #131 → #132 → #133
  → #134 → #135 → #136 → #137 → #138 → #139 → #140 → #141`。
  #127の履歴に存在する過去のmain文書mergeは継承した履歴であり、今回mergeを実行していない。
- mainと#141の比較はmainのみ0／Draftのみ42 commit、136 file、+23,208/-12。
  この136 file全部を本番採用するという意味ではない。実際の新差分はこのR0文書・一覧・テスト・専用CIに限定。
- AGENTS.md、継続仕様、auth-session/baseline.jsonはmainと#141で同一blob。
  main、既存PRのhead/base/Draft/stateは変更しない。merge/rebase/cherry-pickなし。

[PR開始時baseline（19件）](iphone-mvp-r0-pr-baseline.json)と
[mainとの差分全136 file（main/Draft blob SHA・分類・採用・本番残作業）](iphone-mvp-r0-delta.json)
をレビュー用の固定一覧とする。mainとの差分は `git diff d62e1e2 bcf8f2c`、
今回だけの差分は `git diff bcf8f2c HEAD` で分離できる。

## 機能別採用表

「mainにある」はコードの存在を示し、実hostへの配備・稼働や権限の現状を示さない。
本番資源のread-only監査も今回は行わない。

| 機能 | mainにあるコード | Draftだけのコード | 合成専用の範囲 | 本番適用に必要な変更 | MVP採用／後回し |
|---|---|---|---|---|---|
| 正式UI・保留方針 | AGENTS、継続仕様第10節（#125/#126） | 1タブ契約／raw reader（#127/#128） | fake raw API、保存bundle、検索・順序拒否テスト | 実入力新鮮性・保存/照会導線を実機受入 | 採用：確認1タブ、1行1案件、ID/見出し固定、横scroll、検索。sort前提なし |
| 原本リンク・入力保持 | 旧ReceiptConfirmation render/capture_inputs、既存PDF表示 | iphone_review_drafts/sheet_reader/draft_journal、SavedDraftRepository | 8案件の発行済みcontract、暗号化fixture、表示HTML | live source/Sheet version reader、ACL、保存/再認証/再読込受入 | 採用：原本/メモ/途中入力をIDへ固定。旧4タブ案・別Web UIへの置換は不採用 |
| 本人login・owner/session/CSRF/TTL | auth-sessionのhash固定既存host delta、shared_login契約 | ExistingHostSessionGate、PurposeBoundary、明示隔離host | memory runtime・架空RSA Google JWT、fake token exchange | R3で実host attestation adapter/金融providerと既存運用状態確認 | 契約再利用を採用。新login/Cloud構築は後回し |
| 下書き・snapshot・本人確認受付 | 金融writerとは別の旧入力保持 | #130 PersistentJournal/EncryptedDraftIO/SourceObservation/item_drafts、#131 ConfirmationService | DRAFT_ONLY domain、save_draft/acknowledge_snapshot、600秒proof | live耐久性、入力→native確定snapshotの明示契約が不足 | 採用。ただしacknowledge_snapshotから金融authorityを作らない |
| ownership・共有CAS・異常照合 | Conditional IO等の基礎部品、旧無条件runnerも存在 | #133 accounting_ownership/gate、#134 recovery、ConditionalRequests | isolated sandbox、trusted source/native/ledger/pending reader | R2で全旧入口のmandatory gate/drain、R3で実ETag/ACL/監査 | 採用。全本番入口への接続は今回の対象外 |
| 通常取込・買取 | ReceiptPipeline等の既存writer・privacy/duplicate | #135 auto_native_evidence、#136 synthetic_auto_writer | memory native fixtureとguarded writer | R2で確認と同じsource fenceへ、独立runner迂回を排除 | 回帰と競合対照として再利用。買取操作を今回MVPへ追加しない |
| 一般の修正・確定 | ReceiptConfirmation._parsed/_plan/_write_accounting_plan、PDF item validator | #131一般item draft、#139 receipt_confirmation_contract、#140 synthetic_legacy_writer | #131一般はPDF Unit、#140は旧whole-source画像。両者の直接mappingなし | **下記B1/B2解消が先**。その後R2/R3の本番gate/host/backend | MVP採用対象だがR1接続を停止。既存writer成功を一連操作の成功へ数えない |
| 医療完全手入力 | ReceiptConfirmation._parsed/medical_categories、手入力validator | #131 medical draft preview、#139 v2、#140 guarded writer | 完全手入力の事前固定native fixture。編集後draftとの受渡しなし | **下記B2解消が先**。実医療データ/権限/受入は別承認 | 採用対象、R1未接続。AI/OCRは復活しない |
| PDF | PageIntake、RegistryStore、purpose/plan/privacy等 | #141 read-only棚卸しと不足の明記 | 構造候補、source partitionの拒否証拠 | 別工程で全source partition/Unit/request/consume/start/completion | 原本・構造下書き・保留のみ採用。金融全種類は後回し |
| 結果照会 | 既存read-back、旧結果表示 | #131受付結果、#134分類、#139/#140 native＋共有CAS＋全ledger照合 | read-only COMPLETE/PARTIAL/UNKNOWN、再構築fixture | 新操作の要求ID→native/shared結果binding、live監査/復旧 | 採用対象。受付済みと金融COMPLETEは別表示、一連操作は未実装 |
| Cloud/コンテナ・公開host | 既存host用ソース/固定bundle | #124 staging_shadow packaging/serve/Cloud準備文書 | 隔離コンテナ・mock runtime | 必要性・最小deploy差分・別承認が前提 | 今回不採用・凍結維持。historical準備を実配備と扱わない |

G103/H301/対応不明の旧医療p1、P201/P202と既存PDF保留は維持する。
既存保留の解除・入力破棄・推測mappingを採用差分へ含めない。

## hash固定bundleと認証境界

`auth-session/compose.py` はbaseline commit `c29ecc44e1fb9fcc854cde1da5cce940e633e8fa` を参照。
baseline 40 file、patch後12 file、追加6 file、repository overlay 2 fileのhashを個別に検査する。
patch SHA-256は `28bed62cc1c2f02ec516c292dfc289974580ae19f79d28c465ffe6496d5538a6`。
overlayはpdf_intake_authority.py / pdf_intake_registry.pyのみ。
全固定hashと両overlay hashは差分一覧JSONにも保存した。staging_shadow/requirements.lockも25依存のhash固定で、今回は変更しない。

既存test_treeは歴史的ソースを持つが、現在の実host搭載証明ではない。
appの新機能を変更しただけで、このbundleや本番hostへ同機能が載るとは扱わない。
今後overlay範囲・pinを変える際は旧/新hashと理由・影響・安全回帰を明示する。
Windowsの一時dirをGit worktree内へ置くとbundle構築が失敗するため、検証用一時dirは
作業領域内かつこのrepo外へ置いた。hash照合やgit apply条件は緩和していない。

認証は既存Google issuer/sub→owner→有効login/ticket/browser→Origin/CSRF→600秒TTL→purpose/request/revision/input binding。
`ExistingHostSessionGate`はsynthetic_only/memory runtimeを要求する資格検証契約であり、金融issuerではない。
`acknowledge_snapshot`のschemaは `synthetic-owner-acknowledgements-v1`、常に
`native_authority=false / accounting_permission=false`。
#139の `synthetic-contract-fixture-not-issued-authority` と#140のcompletionも実発行物ではない。
金融側は別のVerifiedPostとexact native inputsを照合し、共有CAS→consume/pending→start→writer→独立照合を通す。
Sheet編集・下書き保存・受付状態からこの境界を自動的に越える方法は採らない。

## R0停止証拠

### B1：一般の下書き対象とwriter対象が一致しない

#131 `tests/iphone_confirmation_fixtures.py:contexts` の一般G101/G102/G103は
`pdf_native(index=n,count=3)` とPDFManualUnitPortから作る。
その明細ID・カテゴリ・page/Unit/snapshotはPDF Unit契約に固定される。
#140の一般はReceiptConfirmationPort、画像原本whole-sourceのnative v2である。
同じ「一般」というUIラベルだけでsource/hash/kind/partition/Unitを付け替えることはできない。
今回の「PDFを金融writerへ接続しない」という条件にも反する。

旧一般ReceiptConfirmation._parsedはinputsの日付・店舗・金額・カテゴリ・支払方法を取り込まず、
`store.value['records'][source_id]['parsed']`を返す。
有効なフィールドを編集したdraftを旧port.validate_draftへ渡しても、既存候補と同じplan_digestとなることを5条件で再現した。
これを「修正内容を記帳できた」と表示してはならない。旧portの設計範囲を越える接続不足である。

### B2：保存下書きとnative v2のimmutable baselineを直接接続できない

`receipt_confirmation_contract.validate_request`は `inputs.old.inputs == inputs.confirmed` を要求する。
`accept_fixture`はそのold全体とnative保存済みlegacy confirmation itemの完全一致を要求する。
`IsolatedReimportStore._commit`はlegacy全値の変更を `v2_legacy_immutable` で拒否し、saveにも可変draft用入口がない。

医療の有効な支払日・施設名・実支払額・メモの修正は旧native portのdraft validatorを通る。
しかしその保存修正値をconfirmedへ移すと、外側hash/fixture bindingを整合させてもv2_request_invalidとなる。
逆にoldも修正して一致させれば、保存済みlegacyとは一致せず、legacyへの反映も既存guardが拒否する。
一般・医療とも、v2文書の前後bytes/tag、writer dispatch、ledger writeが変わらないことをassertした。

これは単なる不足UIや認証adapter追加では解消しない。
validator/schemaを変更しないという今回の条件と、編集後の別snapshotを既存immutable v2へ受け渡す要求が衝突する。
fixtureを編集後に初期化し直したり、別のnative backendへ置換してガードを通過する方法は、
既存native状態からの受渡し・応答不明後のfence維持を検証したことにならないため採用しない。

### R1再開に必要な最小の契約検討（今回は実装しない）

1. **画像whole-sourceの一般下書きadapter**を明示する。既存ReceiptResult/item validatorを使い、
   source/page identityを変えず、全明細ID・カテゴリ・変更値をnative parsed/planへ結合する。
   PDF Unitを画像whole-sourceへ変換しない。医療は同じ転送契約の完全手入力分岐のみとする。
2. **immutable baselineとproposed snapshotの分離**をnative request/qualification契約に明記する。
   保存済みoldは不変のまま、保存draft revision/digestと提案inputs/parsed/categoriesを独立に再読取りしてbindingする。
   既存schema/validatorにこの表現がないため、限定されたversion付き契約差分の設計・承認が必要。
   authorityをdraftから作る変更やexisting validatorの拒否条件削除は提案しない。
3. 変更後も同一sourceの共有所有を維持し、資格・consume/pending・start・completion・全ledgerの証拠順序を保つ。
   native文書の再初期化、過去pending/appliedの昇格、UNKNOWN後の新backend/new-ID再dispatchは禁止。
4. 上記の最小差分をレビューしてから、当初R1の本人明示操作・結果binding・PC/390px・16並列を実装する。

これはR2/R3へ先回りする実host/provider/production migrationではない。
既存writer本体、authority schema、purpose/session/CSRF/TTL/owner、privacy/duplicate/replayを維持する方針。
現時点でその契約差分を承認済みとは扱わない。

## 検証と判定の境界

新テスト `tests/test_iphone_mvp_r0_adoption.py` は停止証拠・非金融ack・runtime/pin無変更・CI境界を検査する。
ライブsocketは既存conftestで拒否し、新テストではGoogle client constructorと金融writer本体もfail spyで拒否する。
#131 fixture初期化だけは既存materializerの**正確なPlanningDB**による計画生成を許可する。
fixtureの初期化・合成資格データ・計画生成は金融dispatchや記帳成功ではない。

専用Ubuntu CIは新停止証拠と、#140の安全selector一式、#141 read-only PDF、hash固定bundleを実行する。
既存#140のwriter正常/停止/16並列は個別コンポーネントの回帰としてのみ扱い、
今回の1タブからの統合成功件数へ加算しない。正確なhead/run/countは新Draft本文に記録する。

| 完了報告項目 | 今回の判定 |
|---|---|
| R0 | 棚卸し・採用分類・重大矛盾の再現と停止判定を完了。採用済みUI自体は変更0 |
| R1一連操作 | 未着手・未完了。本人金融確定routeや連携snapshot/providerを追加していない |
| 一般／医療の今回の合成記帳 | 統合操作として双方0件。既存writerの個別成功とは区別 |
| PC/390px | 新操作UIを作っていないためR1の操作性試験なし。過去#131の24画面を今回の成功にしない |
| 正常/異常/16並列 | 既存安全回帰を再実行。新統合の二重dispatch/記帳0・UNKNOWN後再dispatch0は未検証であり合格判定しない |
| 未接続 | 保存draft→編集後native資格→writer→結果のbinding。B1/B2を解消する契約が先 |
| R2最小残作業 | 全旧runner/歴史的SHA workflowの入口一覧と迂回不能mandatory gate/drain・停止/rollback設計 |
| R3最小残作業 | 実host attestation/金融provider、version付きnative保存、live source/ledger/ETag reader、ACL/秘密情報/監査/保持/復旧 |
| 本番まで | B1/B2→R1→R2/R3→統合安全CI/非記帳実backend検証→iPhone本人受入→別承認の新規一般1件canary→監査/段階有効化 |
| 人間の承認・実機 | 限定native契約差分、UI導線が第10節を変える場合の事前承認、owner追加、R2/R3操作、本番各段階。Safari/Sheetsの実機受入は別途必要 |

本番Spreadsheet・承認済みブック・Drive/Firestore/OAuth/医療情報へアクセスしていない。
新Drive資源、Cloud/deploy、実authority発行/消費、本番金融POST、本番会計write、AI送信、原本移動、production runner/workflow切替は0。
今回の差分は本番有効化・実記帳・main統合を含まない。
