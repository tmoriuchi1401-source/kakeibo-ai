# Phase 5B-2B-1 再開：所有関係と合成会計入口の強制gate

対象はStep A・Bのみ。#132 head `cf15d48bfbbb3412f9b95bf71f31fc49f9d7c80f` から独立分岐する。Google Sheets「確認」1タブ、既存認証ホスト、既存Draftのhead/base/Draft、production constructor/runner/workflow、native authority schema、既存validator/writerは変更しない。

**合成のwriter直前probeに対する共有claimの実装**であり、native authority発行・native execute/会計writer接続・実記帳は行わない。#132の停止報告は、未変更の裸の既存実装について引き続き正しい。本変更は既存本番を安全化/切替済みとするものではない。

## Step A：信頼済み観測契約

`app/iphone_accounting_ownership.py` の `TrustedOwnershipReader` に、5つの独立したserver側portを注入する。Sheet/browserからその実装・原本URL・行番号・authorityを受け取る入口はない。

| port | 必須情報・照合 |
|---|---|
| source | source file ID、revision指定bytes、bytes SHA-256、Drive revision ID、numeric version、source kind/page count、観測generation。Drive/native versionの文字列一致を根拠にしない |
| partition | 原本の完全な版binding、確定状態、complete、grouping/partition revisionとgeneration、全Unitの既存ID/index、page番号/既存stable identity、明示された矩形範囲 |
| native | owner、案件ID、purpose/action、独立したnative source version、原本版binding、namespace、native request/context、page/Unit/index、input snapshot、authority revision、既存ledger target ID、native ready状態 |
| ledger | 完全性・generation、既存取引IDと明示したownership。合成compositionでは全入口で同じ原本対応portとnative会計行を共有し、native行のdigestも観測する |
| pending | 完全性・generation、保護された過去のownership。合成compositionでは既存ReceiptConfirmation/PDF/canary/grouped状態も読む同じportを共有し、別入口のpendingを見落とさない |

全frameをread → exact revision bytes取得・hash計算 → 全frameを再readする。同じ観測内で全frameが一致することを要求する。各portのgenerationはABAを防ぐ単調な値であることが実transportの契約。今回のfake成功は実transportの一貫性・耐久性の実証ではない。

既存 `SourceRef/stable_page` と `confirmation_operation_identity/pdf_operation_identity` を再利用する。groupedは既存 `pdf-unit-processing/Unit`、明示Unitは `receipt-unit-posting/context/native request`、固定p14は `posting.finish` と同じ `p14-posting/native request` を使う。外側probe request UUIDとnative permanent request IDは別の名前空間で、相互代入しない。native/Drive versionも相互代入しない。

全原本は `scope=whole, unit=None`。仮のUnit ID/indexを作らない。Unitは確定partitionの要素へ完全一致させる。全Unitの安定page identity、ID/indexの一意性、矩形の妥当性・非重複を検査する。同一pageの別レシートも、nativeが確定した非重複領域がある場合だけ区別する。

`relation` は次の順に判定する。

- 原本が別でもnative operationまたはledger targetが衝突したらUNKNOWN。両方が独立している場合だけindependent。
- 同一原本でhash/Drive revision/numeric version/native version/page count/partitionが違えばUNKNOWN。同hashの版更新でも新しい権限にしない。
- 同一原本のwhole対Unit、whole対whole、同Unit対別入口はconflict。
- 同一原本の別Unitは同じ確定partitionと非重複を証明できた場合だけindependent。
- 既存ledgerが同一/重複scopeなら停止。原本対応が不明、旧版、重複取引ID、不完全なledgerはUNKNOWN。過去pendingは同原本内の別Unitも含め停止する。別の入口で自動補完しない。

保留G103/H301、別owner、非会計purpose、欠けた版・Unit・確定情報は停止する。ledger/旧source identityを再生成・修正・再紐付けするAPIはない。

## Step B：共有CASと強制入口

`SyntheticAccountingGate` は既存 `ConditionalRequests.read/replace/transition` だけを使用する。CASエンジン、別DB、distributed lock、常駐serviceは追加しない。`ConditionalRequests` の変更は、明示した `synthetic_accounting=True` の場合だけ新しい合成probe requestをstrict parseできる拡張。既定の既存request parser/本番動作は維持する。さらにgateはdefault OFFで、明示隔離指定・memory-onlyのbackend/reader・既存 `PurposeBoundary` を必須とする。client factoryやenvによる本番起動経路はない。

この明示合成domainを旧UnifiedWriteBridgeへ渡した場合、prepare/submit/recoverは専用gateが必要として拒否する。旧Unit fenceが新しいownership recordを読み飛ばす組合せを許可しない。既定OFFの既存domainへの動作変更はなく、native inspectorのread-only検査は再利用する。

同じschemaの `operations` 内へ、ownership・native preflight digestを含む `synthetic-accounting-probe-v1` requestを保存する。既存requestと同じatomic payload/strong ETag/条件付きreplace/正確なread-backを使う。新旧入口・全インスタンスが同じbackendを参照する。`accepted/executing/awaiting_readback/complete/partial_unknown` の既存遷移を再利用し、どの状態もownership fenceを解放しない。

request IDは全purposeで一意。同ID・同内容は保存済み状態の再照合だけ、同ID・異内容は拒否。異IDから同原本/Unitを再所有できない。古い金融requestにownership情報がない場合はUNKNOWNで、勝手に移行しない。metadata/reconciliationを会計権限としない。下書きCAS/本人確認受付repositoryを会計claimへ流用しない。

処理順序は、既存認証POST gate → trusted ownership観測 → native read-only preflight → 共有scope比較/条件付きclaim/read-back → executing → 認証再検査 → ownership/native/ledger再観測・完全一致 → 1回だけ消費できるin-process probe lease → 無副作用observer。leaseは外部へ返さない。実writerもnative executeも呼ばない。probe結果は `synthetic_only=True, native_authority=False, accounting_permission=False, accounting_writes=0`。合成request/結果は既存native request parserに拒否される。

### 必須gateの構成

`staging_shadow/native_accounting_sandbox.py` がnativeソースを専用のprivate Python namespaceへ読み込み、以下の**関数本体へ入る前**に共通gateを強制する。production packageのclass/functionへmonkeypatchしない。入口に渡すservice/writer handleは参照先を持たないinert placeholderへ置換し、入力引数の完全なpinも検査する。preflight用の信頼済みread adapterは金融入口から公開しない。

| 入口 | 隔離namespaceの実コード境界 | 既存native preflight |
|---|---|---|
| 通常自動取込 | `ReceiptPipeline.process_bytes` | 元のprivacy classifier、通常ReceiptResult validator、取引種別、import marker、原本/入力binding。外部AIなし。ローカル抽出readerだけを合成入力へ置換 |
| 買取収入 | 同上の別action | 上記＋元の `_buyback_income` によるincome schema/既存行検査。income appendなし |
| 旧一般確認 | `ReceiptConfirmation.apply_confirmations` | 元の `_current/_parsed/_plan/validate_draft` と、切り離したmemory snapshot上の元の `apply_confirmations(dry_run=True)`。原本版、未送信入力、presentation、重複等を維持 |
| 旧医療手入力 | 同上 | 同じ元の完全手入力/medicalカテゴリ/非automatic条件。AI/自動医療記帳を復活させない |
| PDF自動Unit | `PageIntake.post_normal` | 元の `post_normal(apply=False)`、privacy/untouched/重複/plan、現在manifestの全Unit範囲/segmentationとownershipの一致 |
| PDF手入力Unit | `PageIntake.post_manual` | 元の `inspect/post_manual(apply=False)`、独立posting proof/actor/exact snapshot/item validator/plan、現在manifest一致 |
| grouped Unit | `GroupedUnitPort.execute` | 構造確認とは独立した合成ACCOUNTING NativeInspectionと元のeligibility/inspect、`_plan`、hash固定native `_spec/_route`、実際のgrouping confirmation/revision/proposal/privacy、既存DriveUnitProcessingStore load、ledger/duplicate/plan |
| 固定canary | `receipt_plan.posting.execute` | 元のvalidate_state/fresh_plan/bound/outcome、独立grant/owner/期限、p14予算/既存ledger。実executeなし |
| 明示Unit | `receipt_plan.unit_posting.execute` | 元のvalidate_state/fresh_plan/bound/plan_receipt/outcome、独立grant/context/Unit/budget、既存claim。p4等の元の制約を拡大しない |
| 将来1タブ金融入口 | private `future_accounting.request` のin-process slotだけ | 既存native preflightを別途要求。#131受付を使わず、HTTP/本番登録なし。旧確認と同じnative operationを指す合成aliasでも競合させる |

`NativeReadAdapter` は、serverが確認対象として固定した案件ID・原本の全版情報・partition・native状態bytes/strong tagを保持する。初回claim前でもこれらが変われば再表示・再確認が必要であり、POSTのたびに現在値へpinを付け直す使い方は禁止。native状態をversion付きで前後readし、元のvalidator結果とownershipのoperation/input/authority revision/**実planのledger targets**も完全一致させる。元のnative CAS/pending/authorityはread-only検査の対象として維持し、外側claimだけで省略しない。native claimへの実接続は今回行わない。PlanningDBへ元materializerを呼ぶ既存plan検証はmemory上の計画生成だけで、会計writerの起動ではない。

PDFではnative proofのactorと案件ownerも一致させる。groupedの金融authority revisionとgrouping revisionは独立し、構造確認から金融権限を作らない。groupedの既存NativeInspection/eligibilityの合成証拠は別に初期化し、金融許可を除いた状態でも構造確認は成立するがprobeは拒否されることを試験する。NativeInspectionを検査する既存bridgeにはrequest store/submitを接続せず、実行回数0であることを確認する。

内部 `_post`、既存portの直接execute、materializer、確認writer、SheetsDBのconstructor/append/update、既存CLI/production runner/background入口は、このcompositionで明示拒否する。9種のgate付き入口が揃わない場合は起動拒否。`app` 全体のmaterializer/確認writer呼出しをAST走査し、既知9 call siteとの差があれば起動拒否する。直接関数/instance handle/内部writerからprobeを呼ぶ別経路を作らない。未claimのprobe leaseや二度目の消費も拒否する。

この境界は、レビューしたserverコードとDIに対する隔離compositionであり、任意の敵対的Pythonコードを実行するOS security sandboxではない。production packageを別途importして本番runnerを起動してよいという意味ではない。実資格情報・live client・network・本番資源を渡さない条件をテストでも強制する。

### 共有backendとledger/pendingの検証

全金融入口は同じ既存atomic Memory backendを使う。16-way試験では16個の別ConditionalRequests/gateインスタンスが**同じbackendオブジェクト**へ競合する。native fixtureごとの未使用bridge storeを共有済みという証拠にはしない。

PDF auto/manualは同じnative RegistryStore/Unitも共有する。全入口のownership readerは同じledger/pending portを共有し、既存validatorの会計行も同じ3表へ揃える。canaryのvalue/formula read viewも同じ会計行を返す。旧確認の未記帳review scaffoldはnative stateと正確な行snapshotで保持し、記帳済みauthorityと扱わない。追加/変更された未対応ledger行はUNKNOWN。別入口のPDF intent・旧確認pending・grouped record・canary claimも同じpending観測で停止する。

正常なnative fixtureは測定前に完全合成データだけで初期化する。既存回帰fixtureに任意の初期source hash/ownerを渡せる引数を追加し、今回の新fixtureは実際の合成bytesから求めたhashで最初から作る。既存record/ledgerを後から再生成・上書きする移行ではない。従来の引数省略時の回帰データは不変。

## 自動検証と到達点

Step Aは `tests/test_accounting_ownership.py`、Step Bは `tests/test_accounting_enforced_gate.py`。source全体対Unit、旧/新同native operation、PDF auto/manual/grouped、別Unit/別原本、既存ledger/旧pending、native partitionが変わったのにsaved projectionが古い場合、版変更、request ID再利用、2/16-way、通信断/応答喪失/partial_unknown、別入口再dispatch、全直接入口/CLI/内部writer、gate未設定、認証/owner/CSRF/session/TTL、native validator/CAS/authority拒否を検証する。正常系でも既存native state・会計行は不変で、作用は共有合成claim repositoryとprobe counterのみ。

専用 `iphone-accounting-gate-checks.yml` はUbuntu 24.04/Python 3.12、PRの正確なhead、contents:read、資格情報/Secrets/Variables/environmentなし、write flag false、デプロイ/本番起動なし。新しいnode IDのcollectとPASSEDを照合し、#132までの既存回帰にnative pipeline/privacy/confirmation/PDF/canary/Unitの回帰も追加する。確定件数・CI URL・commitは新Draft PR本文に記録する。

判定対象は**この合成compositionのStep A・B**だけ。#132の裸の既存コード経路は本番も含め未変更であり、既存停止テストも回帰として維持する。実Firestoreの耐久性/IAM/鍵/保持/バックアップ/費用、live Drive/native reader、実OAuth、iPhone実機、本番の全実稼働構成は未検証。合成成功を本番適合性・記帳許可としない。

## 今回の範囲外・次段階

- `inspect_result` はowner/sessionを再検査するread-only照会。CAS通信結果が不明でも再claim/新ID発行/execute補完を自動で行わない。unknown/completeはsource fenceを解放しない。実行中のnonce/authorityを返さない。
- **全入口横断の最終復旧検証**（native claimの前後、native authority消費、ledger部分反映、二重の完了応答喪失、既存native read-back/明示復旧の結合）は次段階。本変更はそこまで完了していない。新しいprobe requestを既存native回復入口へ流用しない。
- **5B-2B-2の既存authority/writerとの合成接続**も未実施。現在はwrite関数をinterposeして本体を実行しない。inert slotを実native引数へ置換する際は、trusted ownership/native grantから引数を再構築し、既存 `validate_request`、native permanent request ID、native CAS/intent、実行直前source/ledger/snapshotとread-backを保持して再検証する。probe結果や#131受付を資格にしない。
- 上記追加作業・5B-2B-2着手は今回の結果をユーザーが確認して判断する。自動で着手しない。

## 本番へ接続する場合の変更案（未実装・個別承認が必要）

1. production constructorと全legacy runnerへ、mandatoryな同一共有store/所有観測/入口gateを渡す。未設定を起動エラーにする。ReceiptPipelineへsource versionを単に代入して済ませず、live co-observationとsource全体対Unitの正式なnative ownershipを渡す。通常自動取込と買取収入、医療手入力、PDF auto/manual/grouped、canaryを漏らさない。
2. 新旧native writerを一つずつ共通outer claimの内側へ接続し、既存native CAS/authority/Unit posting fenceは残す。旧pendingと未scope金融履歴は明示監査/復旧までUNKNOWN。実行直前/partial_unknown/read-backを含む全入口横断試験と切替時の旧runner停止条件をレビューする。
3. 同じ金融storeを保証する実設定、実効IAM、鍵/暗号化、保持/削除、backup/PITR、RPO/RTO、監査/費用上限、移行・rollbackを承認する。auth/draft/金融storeは分離したまま責任境界を固定する。
4. live reader/実OAuth/実機のread-only検証範囲、追加ユーザー（妻等）のGoogle subject・案件/医療閲覧範囲、公開環境、main merge、デプロイ、本番入口切替、実writeは各々別承認。今回どれも行わない。

依存は `main → #123 → #124 → #127 → #128 → #129 → #130 → #131 → #132 → 新Draft`。#125/#126は既にmerge済みの仕様文書PRで、今回変更しない。新Draftのbaseは#132のheadブランチ。既存PRのhead/base/Draftを維持し、main mergeを行わない。
