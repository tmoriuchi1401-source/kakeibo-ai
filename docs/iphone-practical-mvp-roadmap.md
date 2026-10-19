# KakeiboAI：iPhone実用運用までの最短ルート（2D-1調査時点）

基点は#140 head `40d824c7bf1b2b8b28df598d67fe30700f32281d`。
[PDF契約棚卸し・最初の接続案](iphone-phase5b2b2d1-pdf-native-design.md)とセットで読む。
今回は提案のみ。UI変更、コード有効化、実OAuth/Google/医療、本番記帳、Cloud、mergeは実施しない。

**推奨：採用済みSheets「確認」1タブを維持し、一般・医療完全手入力の本人確定と結果照合を最初の運用単位にする。**
PDFは原本・構造下書き・保留理由を扱えれば初期MVPを開始可能とし、金融writerの全種類完成を待たない。
PDF金融処理の第一候補は登録済み一般manual Unit。grouped/固定canary/歴史的明示Unitを先行して一般化しない。
これは継続仕様第2節の未対応purpose保留を利用する計画であり、確定済みUIや安全条件を変更する提案ではない。

## 1. 実装済みと本番mainの差

| 領域 | 現在あるもの | 本番運用の完成と扱えない理由 |
|---|---|---|
| UI | 第10節の1タブ・検索・原本往復・入力保持・縮小倍率はユーザー実機採用済み | 認証付き金融操作、live入力新鮮性、並替え/通信断/再認証は未受入。既存production PDF縦カードを1タブの完成形と混同しない |
| Google本人確認 | mainにも既存host向けGoogle共有login、logout/owner/session/CSRF/TTL契約、hash固定baseline/deltaがある | 実環境の現在の権限・運用状態は今回未接続。#124以降のExistingHostSessionGateはsynthetic_only guard付き。金融POSTのproduction adapterではない |
| 下書き/本人確認受付 | #127～#131のID/原本/入力snapshot、raw reader、永続draft CAS、再確認・明細編集・受付 | synthetic/CIのみ。Sheet編集や#131 acknowledgementから金融authorityを発行しない |
| 共有金融安全境界 | #133 ownership/mandatory gateの隔離composition、#134 read-only recovery model | 本番全runnerの強制境界は未接続。別process/別入口が裸のwriterを実行できる状態を解消する必要がある |
| 通常/買取 | #135/#136の合成native evidence＋既存writer memory接続 | 実host/native発行物ではない。既存本番writerの権限をこの合成fixtureへ置換していない |
| 一般/医療手入力 | #138設計、#139 isolated v2/CAS、#140既存writerの合成接続 | 既定v1 constructor/save/runnerはそのまま。実native文書v2 migration、実金融grant/consume、Sheets writeではない |
| PDF | mainにPageIntakeのauto/manual・registry strong CAS・privacy/plan/writer。専用p14/p4/p10もmainにある | 共有ownership、全source partition、native開始/完了への結合は不足。grouping歴史的moduleがtest_treeにあることはhost配備証明ではない |

依存：`main → #123 → #124 → #127 → #128 → #129 → #130 → #131 → #132 → #133 → #134 → #135 → #136 → #137 → #138 → #139 → #140`。
#125/#126はmainへ既に入った仕様文書。残る16件はOPEN/Draftのまま。
#140はmainよりbranch固有commitが41個あり、単なる最新2PRのmergeだけでは依存を満たさない。
AGENTS/確定UI仕様はmainと同じだが、統合review/gate/draft/native v2等はmainとの差分に残っている。
今回、既存PRのhead/base/Draft/stateを変更せず、新しい独立Draftに調査結果を保存する。

**統合方法も先に整理する**：この依存鎖の既存機能・本番非登録の合成部・歴史的試験/Cloud準備を区別し、
レビュー可能な累積差分と採用範囲を作る。全stackを順に本番有効化する計画は採らない。
merge/cherry-pick/rebase/既存PR再構成は今は行わず、採用差分・hash pin影響・全安全CI・rollbackを示して別承認を得る。
過去の成功CIは継承証拠であり、新しい統合headのCIを代替しない。

## 2. MVP分類

| 機能 | 分類 | 最小運用範囲と理由 |
|---|---|---|
| Google本人認証・owner認可・logout/session/CSRF/Origin/TTL | **開始前必須** | 既存Google loginを再利用。本人が誰で何を承認したかを金融snapshotへbinding。妻等の追加ownerは別明示認可・実機確認。共有アカウントやSheet owner権限で代用しない |
| 1タブ・検索・原本リンク・途中入力/メモ・保留/結果表示 | **開始前必須** | UIは第10節を維持。行番号ではなくcase/source/version/inputで検証。金融操作時にはexact snapshotを既存hostで再確認。新規導線の実装前にUI影響の承認 |
| 一般・医療完全手入力の明示確定 | **開始前必須** | 一般の明細/カテゴリ/修正を保護。医療は日付/施設/実支払額/カテゴリを本人入力、外部AI/OCR候補自動採用0。旧医療p1/H301/G103は解除しない |
| 通常取込 | **開始前必須（使用中の経路）** | 既存privacy/duplicate/validatorを維持して同じsource fenceへ。UIの確定経路と競合する裸の自動writerを残さない |
| 買取 | **後追加可能** | 未接続中は明確に保留。既存mainで稼働しているなら勝手に停止/変更せず、同じmandatory gateまたは承認済みのdrainで隔離すること自体は開始前必須。収入を支出に振り替えない |
| PDF原本・構造下書き・医療/未対応保留 | **開始前必須** | PDFの存在/保留理由/次の本人行動が見える。構造確定を会計許可にしない。親PDF移動は全page/Unit終端＋独立照合まで禁止 |
| 構造/種類/重複の認証POST | **後追加可能** | 当面未対応は保留。実装するならpurposeを独立。重複疑いを解決できなければ保留する安全条件自体は開始前必須 |
| PDF manual一般Unit | **後追加可能・PDF第一候補** | 本調査のC拡張と共有/native証跡が先。頻度が高ければ一般/医療の運用基盤と並行するが、基盤を省略しない |
| PDF auto・groupedの金融処理 | **後追加可能** | manualと同じ証跡を使い、auto資格・複数page/Unitを個別検証。mixed medicalを一括一般扱いしない |
| 固定canary/歴史的p4/p10を一般化する開発 | **当面保留** | 対象原本限定の運用者機能。通常のiPhone操作には不要。新しい一般PDFへgrant/contextを流用しない |
| 実金融POST/authority、全旧runner必須gate、native＋共有CAS | **開始前必須** | 合成受付/Sheet状態では実記帳不可。consume/start/complete、全入口のwriter権限境界、正確なreadbackが揃うこと |
| live Drive/Sheets reader・ACL・strong CAS・監査/耐久性/復旧 | **開始前必須** | source独立版/全ledger値/式/既存行を観測。実backendでIf-Match/応答喪失/権限分離を確認。Sheetsの複数appendをtransactionと扱わない |
| iPhone受入・限定一般canary・本番切替/rollback | **開始前必須** | 本人端末で一連の操作/再認証/通信断/再送/保留/結果を検証。canaryは新規一般1件を別承認後。医療/混在PDFの実データ受入は別承認 |
| 新login/新DB/CAS service/新Apps Script UI/Cloud infrastructure一式 | **当面不要** | 既存host/Drive private JSON/CAS/validator/writerを優先。必要性が具体化した部分だけを別承認する |

## 3. 開発順序・依存・規模・承認

S=既存境界の局所adapter/検査、M=複数保存先/実装＋障害試験、L=全入口/実backend/統合運用。
目安はS 1～2、M 3～7、L 8～15開発人日。既存実装の再利用を前提とした設計見積りであり、確定納期ではない。
承認待ち・実機日程・実backendで新しい制約が見つかった時の再設計は含めない。重複する作業を単純加算しない。

| 順序 | 作業 | 依存 | 規模 | 人間の確認/承認ゲート |
|---|---|---|---|---|
| R0 | 採用する累積差分/未稼働機能とMVP範囲を固定。実運用入口・既存保存先・host運用状態の調査手順を作る | 本調査/継続仕様 | S | PDF金融処理は初期保留でよいか、許可owner、採用差分の確認。実設定/データのread-only監査自体は別承認 |
| R1 | **合成の全体1本化**：1タブ保存draft → exact snapshot本人再確認 → 一般/医療の既存native validator → source共有fence → native consume/start → 既存writer → 結果照会 | R0、#130/#131/#139/#140 | M | 現行UIの列/入力保持を維持。確定リンク/結果導線の具体案を承認後に実装。合成Google sessionのみ、金融POST実配備なし |
| R2 | 全legacy入口へのmandatory gate計画/実装、v1無条件writerのdrain、既存通常取込も同じsource所有へ。disabled入口を本当に迂回不能にする | R1、#132～#136 | L | 全入口一覧・drain/rollback・権限変更範囲の承認。直列workflow lockやflagだけで完了としない |
| R3 | 実backend接続準備：実host attestation adapter/金融authority provider、private保存契約の明示version切替、live source/ledger/ETag reader、ACL/秘密情報/監査/保持/復旧 | R1/R2。設計は並行可 | L | native schema/migration、実host金融POST、既存file条件更新、実OAuth/Google読取り・権限・最小deploy差分を各承認。新Drive資源が必要なら先に理由と承認。Cloud全般の再調査はしない |
| R4 | 統合headの安全CI、実backend非記帳の競合/応答不明検証、iPhone本人のend-to-end受入 | R3 | M | PC/390px/合成1タブ合格を実認証・本番操作合格と混同しない。端末・アカウント・通信断/再認証/再送/入力保持の確認 |
| R5 | 新規一般原本1件の限定canary → 監査/正確ledger read-back → 段階有効化 | R4、未知結果0または隔離運用が実証済み | M | 具体的原本/plan/権限/件数/停止条件への承認。本番CAS切替・記帳・入口切替・main統合を自動でまとめて承認済みとしない |
| P1（任意並行） | PDF manualのsource partition/native v2契約を隔離検証 | 本PDF設計＋保存契約承認 | M | 既存金融MAC/UIDを変えずに成立すること。対応不明なら停止。MVP本線の完了待ちにしない |
| P2（後続） | PDF manual既存writer memory接続、crash/16-way/readback | P1 | M | writer合成実行の明示承認。一般PDFのみ、AI/medical/productionは除外 |
| P3（後追加） | auto、その後必要性が確認できたgrouped/医療PDF独立mapping | P2、実用頻度/負担の確認 | M～L | 各native authority/Unit schemaの変更承認。旧保留移行や医療AIは提案しない |

R1/R2/R3の設計は並行可能だが、R5は全依存が解決するまで開始しない。
利用開始を早める効果が大きいのは、PDF全種類の追加ではなく**R1で最後の本人確定→結果照会を一本通し、R2/R3の本番安全境界を完成させること**。
運用費は既存host＋既存保存先の再利用を第一候補とする。実料金/現在のリソース状態は調べておらず、費用0やdeploy不要とは断定しない。

## 4. 安全上の必須事項と未解決リスク

- 既存host再利用にも、synthetic guardを外すだけで実金融POSTが成立するわけではない。実actor/owner/purposeからnative発行物へのserver-side adapterが必要。
- Sheets入力はdraft。所有者による有効値の交換・入力中更新・sort/delete/insertionは保護だけでは防げない。本人が確定するexact snapshotと原本/入力新鮮性の再検証を残す。
- 同じledgerへ書ける裸のlegacy/キャッシュwriterが残れば共有CASを迂回できる。対象外機能を単に画面から隠すだけでは開始条件を満たさない。
  特に既存PDF Unit canary workflowの歴史的SHA実行も調査対象。最新appへgateを追加しても、旧SHAを実行できるworkflowのwriterは保護されない。運用者権限/drainまで確認する。
- Drive/native版・Unit/partition・before/全ledgerの原子的な同時観測は未証明。3保存先を同時transactionと見なさず、応答不明は保留・人間監査。
- 読戻しは全ID/値/式・全旧行・完了証拠を必要とする。ledger不在だけでNOT_EXECUTED、全行一致だけで新COMPLETEにしない。
- PARTIAL/UNKNOWNの自動再dispatch・不足行補完・pending解除・fence解除は作らない。監査者・隔離一覧・停止/再開手順は本番前に定義する。
  rollbackはread-onlyへ戻し、消費済みauthority/start/pending/fenceを保持する。v1 backupを書き戻して旧writerを再開しない。既存の詳細設計は[2C-1互換性・rollback](iphone-phase5b2b2c1-native-contract-design.md)を再利用する。
- 追加owner/妻の利用、iPhone実認証、権限失効、実backend耐久性/ACL、実書込みcanaryは未確認。今回のsynthetic対照を実証明へ置き換えない。
- 医療は完全手入力。PDF医療の一意mappingが無ければ保留する。旧医療p1/H301/G103の保留解除をMVPの前提にしない。

## 5. 次工程のCodex指示案

次のPDF工程は**native契約の隔離実装**に絞る。これを実施する承認は本調査には含まれない。
MVPを優先する場合はR0/R1の合成本人確定導線を先に依頼し、PDF P1は後回しにできる。

> KakeiboAI Phase 5B-2B-2D-2を実施してください。2D-1の新Draft headから独立分岐し、登録済み一般PDF manual Unitだけのnative実行契約v2をmemory-onlyに隔離実装してください。
>
> 既存DecisionSealer/AuthorityAdapter/ConfirmedItems/validator/plan/RegistryStore versioned IO/ConditionalRequests/#133 ownershipを再利用してください。新login/DB/CAS engine/financial issuerは作らないでください。
>
> この指示で承認する追加保存契約は2D-1文書のsynthetic v2 envelope（legacy registry view、source_partitions、operations、revision）に限定します。実native schema/保存先のmigrationや本番authority発行/消費は含めません。旧v1 pending/appliedを昇格させないでください。
>
> 全source page/manifest/holdと既存Unit/indexを固定したpartition reader、独立Drive/native版、既存authority request＋native intent UUID＋shared request binding、pending＋consume同一CAS、start/read-back、合成completion照合を検証してください。Unit IDや既存requestを変更・再生成せず、証拠が無ければ停止してください。
>
> source partition資格は共有gateより前、native claimは既存gateのboundary検証後とし、元nativeの順序を維持してください。初期化したfixtureは実発行物と区別してください。
>
> 既存plan生成はmaterializerをPlanningDBへ渡すため、この**計画生成のみ**を許可します。金融writer dispatchはfail spyで0とし、会計台帳へのwriter接続は行わないでください。planner呼出し数を金融dispatch数へ混同しないでください。
>
> 既存runner/constructor/workflow/金融MAC schema/validator/writerを変更せず、save facadeで正確なnative順序が守れない場合は必要な最小hook差分と理由を提示して停止してください。
>
> v1読取り隔離、欠損/失効/版変更、全source partition、多Unit/whole競合、old pending/ledger、2/16-way、native consume/start CAS応答喪失/412/readback不一致、別process読取り再構築を合成テスト・専用Ubuntu CIで検証し、新Draft PR/commit/結果を報告してください。
>
> 本番Google/OAuth/Drive/Sheets/Firestore/医療、Drive資源作成、AI送信、原本移動、実writer/会計append、Cloud、main merge、既存PR変更は禁止です。P201/P202・G103/H301/旧医療p1を維持してください。writer合成接続・production切替・実金融POSTは次の別承認です。

この指示は次段階の判断材料であり、今回それを実行したものではない。
