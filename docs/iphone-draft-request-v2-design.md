# B1/B2解消案：immutable baselineと修正snapshotの分離

2026-10-19。#142 head `66a259b7e77d62a153cc918518e09aff8d48ffb5` から独立した設計Draft。
**今回の完成範囲は契約設計とread-only合成検証。新native validatorの隔離実装もまだ行わない。**
mainのAGENTS・継続仕様全文、#142の採用表と停止証拠、既存native/authority/共有CAS/writerを再確認した。
今回の承認は設計のみであり、保存文書移行・writer接続・R1画面統合・本番有効化の承認ではない。
既存PR #123～#142とmainのref/Draft/stateは維持する。

## 1. 推奨する変更と今回変えないもの

**旧保存値を更新してから旧validatorへ戻す方法ではなく、旧値と提案値を別々に検証する新契約を選ぶ。**
一般は画像whole-sourceの既存明細catalogとwriterが既に使う支出IDを固定し、修正値からReceiptResultを作る。
医療は完全手入力だけから別のReceiptResultを作る。双方とも既存validate_receipt_resultと
ReceiptConfirmation._planへ渡し、記帳planの全行を独立再計算する。
新しいauthorityやwriterを作らず、旧validatorの拒否をcatchして通すfallbackも作らない。

| 対象 | 今回 | 後の隔離実装案 |
|---|---|---|
| app/staging_shadow/auth-session | 変更0 | 明示的に新しいrequest validatorとnative facadeを注入 |
| 旧validate_request/validate_operation/store v2 | 変更0 | そのまま残す。旧requestは旧validatorに限定 |
| 新request validator | 文書とtests限定のread-only例 | 明示versionで選ぶvalidate_draft_request_v2 |
| native保存文書 | 読取り・不変性の検証だけ | 下記v3 codecが必要。移行は別承認、今回は行わない |
| 金融authority/VerifiedPost/共有CAS | schema・条件とも変更0 | 既存fieldsのdigest対象を新inputsへbindingするだけ |
| writer | body/validator/dispatch/呼出し元とも変更0 | plan/pendingは後のwriter接続承認時にのみ渡す |
| UI/1タブ/PDF | 変更0 | 画像adapterを後のR1で採用。PDFは金融接続しない |

今回の実差分は本書、tests限定の合成例と検証、新専用CI、そのCI分類テストだけ。
testsの比較oracleは本番・隔離runtimeからimportされず、金融要求・資格・CAS engine・状態機械を生成しない。

## 2. 現在の停止条件と追加で必要な境界

1. #131一般はPDFManualUnitPort、#140一般はReceiptConfirmationPortの画像whole-source。
   source/hash/page/Unitを付け替えず、**新たな画像whole-source下書きadapter**を設計する。
   既存の表示alias G101を一致根拠にしない。
2. 旧一般_parsedはrecords[source].parsedを返し、編集したinputsでは候補を更新しない。
   新validatorだけが修正明細からtyped parsedを作り、既存_plan(item, parsed, linked='')に明示的に渡す。
3. 旧native v2はold.inputs == confirmed、oldと保存legacyの完全一致、legacy不変を要求する。
   この等式を削除せず、新schemaではoldとproposedの異なる役割を明示する。
4. 既存iphone_source_observation.observeはACCOUNTING_POSTにUnit/indexを要求する。
   この条件は変更しない。画像whole-sourceにはTrustedOwnershipReaderの既存source/whole観測を使う新adapterを設計する。
   その単一画像page identityはsource_kind=imageで計算し、架空Unit/indexを補わない。
5. 旧store v2のvalidate_operation/_commitも旧request/pending形へ固定される。
   新requestだけ追加して旧保存文書に押し込むことはできない。保存側の明示version分離も設計対象に含める。

## 3. versionとフィールド契約

requestの既存outer fieldsは維持する：
`mode, kind, case_id, review_id, operation, request_id, shared_request_id, owner, source,
inputs, plan, before, authority, ownership, fingerprint`。

**request v2の識別子はinputs.schema = receipt-confirmation-draft-inputs-v2**。
schemaが存在しない旧inputsは従来のvalidate_requestへ、正確な新schemaだけを新validatorへ渡す。
未知schema、schemaの欠損を補う推測、旧validator失敗後の新validatorへの再試行は禁止。
schema自身もinputsのdigestへ入り、金融bindingからversionを除外しない。
request/input/catalog/planのdigestは既存unified_receipt_review.digestのcanonical JSON（ensure_ascii=True）を使う。
writer向けconfirmation_hashは既存receipt_reimport_production.digest（ensure_ascii=False）を使い、両domainを相互代用しない。
原本hashはJSONのdigestではなく原本bytesのSHA-256である。
新validatorは既存validate_candidateの共通outer/authority/ownership/plan形検査を再利用できる。
旧validate_requestは新inputsのkey集合を拒否し続ける。

inputsの許可key集合は次の7個だけとする。各内部objectも明示key集合・型・容量を定義し、未知keyを拒否する。

| key | serverが保持・再読取りする内容 | 比較・役割 |
|---|---|---|
| schema | 上記固定識別子 | version選択と金融input fingerprintに含む |
| baseline | immutable legacy全体digest、対象old item全体、baseline candidate/catalog digest、初回native raw/tag観測 | old itemは保存legacyと完全一致。新値で置換しない |
| draft | DRAFT_ONLY domain、owner/case、generation、保存revision/strong tag、input_digest、raw value（fields/items/notes/provenance） | 保存repositoryの実際のsnapshotと完全一致。ack recordとは別 |
| source_binding | source ID/hash、独立Drive revision/numeric/native version、単一image page identity、whole scope | 原本readerでbytes/hashと版を前後二重照合。Unit/indexはnull |
| proposed | 確定8入力、typed parsed、固定line bindings | raw入力とcanonical parsedの両方を再表示。旧old.inputsとは独立 |
| categories | masterの全pairs、独立revision/tagまたは同等の版観測 | 有効な組合せだけでなく、表示時と同じmaster版を要求 |
| derivation | prepared_at、baseline/source/line/before/planのdigest | 新inputsとplan全行を結合。timestamp以外を再計算時に差し替えない |

raw draftの必須fieldはdate/merchant/amount/category/payment/memo。
一般のreceipt-level categoryは空文字、分類は明細別。医療は日付・施設・実支払額・医療カテゴリ必須。
boolを整数へ受け入れず、非有限数・数式入力・未知key・duplicate JSON key・過大値を拒否する。
初期の新契約上限案は全明細100件、field文字列4,000文字、明細note8,000文字、総額1～999,999,999,999円、
明細金額は絶対値999,999,999,999円までの整数（既存discount/tax規則を維持）。
review/submit bodyは既存host同様300,000 bytes以下、native文書は既存MAX_BYTES=2MiB以下で容量停止とする。
draft/domainの既存容量上限がより小さい場合は小さい方を適用する。未実装の上限/完全key validatorの検証は後続計画に含む。
raw値の正規化がある場合（医療の日付形式/施設前後空白など）、rawとcanonical parsedを両方snapshotへ保存する。
本人に表示した後で黙って正規化した別値を使わない。

draft入力state・acknowledgement・provenance文字列の自己申告は金融資格ではない。
medical provenanceはtrusted保存adapterがuserEnteredValue由来と確認したものに限る。
OCR/AI候補や保存済みmedical parsedからのprefillを、そのprovenanceへ書き換えない。
実際に人が原本を目視/手入力したことを暗号的に証明できると主張しない。

### 3.1 一般の既存明細identity

旧whole-source nativeのReceiptResult.itemsにはPDFのitem_id/Unitはない。
**存在しない旧item IDがあるとは扱わない。** 現writerが使う`R-{source_id}-{index:02d}`と
immutable baseline candidateのindex、source/review ID、baseline item digestをtrusted catalogに固定する。
既存支出行があればそのID・active状態・source参照・ラベルを照合し、行の値/金額から対応を推測しない。

編集可能：既存明細の商品名・金額・master内カテゴリ、receiptの日付/店舗/総額/支払方法/メモ。
編集不可：source/review/operation ID、baseline index、既存expense ID、明細の集合と順序、quantity等の非表示情報。
quantity/confidence/note等の非編集parsed情報はbaselineから正確に保持する。新商品追加・明細削除・並替え・分割/結合は今回の契約対象外。
「既存明細の追加記帳」はbaseline catalogに既に存在する明細の支出行が未登録ならplanに現れるという意味。
writer IDの再割当てや、新しい商品明細をUIから創設する意味ではない。

修正値からReceiptResultを構成→既存validate_receipt_resultで総額完全一致/カテゴリ/日付/名称/quantity等を検査
→既存_planへ渡す。receipt/category一括適用、差額を埋める架空明細、余剰行削除を行わない。
既存privacy/構造の保留・identity不明・require_reconfirmはこの修正で解除しない。

### 3.2 医療の独立snapshot

old itemのstatus/source/履歴はimmutableのまま保存する。
新しいconfirmedは完全手入力のraw fieldsからだけ作り、そのthrowaway itemを既存medical _parsedへ渡す。
旧parsedや一般候補を補完値に使わない。既存medical_categoriesとvalidate_receipt_result、_planを再利用する。
医療は1つの本人確認支出を計画し、病名・患者名・診療詳細の入力や送信は追加しない。
部分入力、automatic由来、旧medical pending/applied、対応不明p1/H301は拒否する。

### 3.3 planとtimestamp

新inputs.proposed.confirmedは旧8欄と同じ順序で、日付/店舗/金額/カテゴリ/支払方法/action/duplicate_target/メモ。
今回の新契約のactionは一般「候補明細で確定」、医療「医療費を確定」に限定し、linked targetは空。
既存_planが重複疑いを検出した場合は保留する。distinct/link actionの新規導線は追加しない。
元のold itemをdeep copyした**execution projection**に新confirmedだけを反映し、そのprojectionとtyped parsedで_planを呼ぶ。
production storeのsave、旧apply_confirmations、UI行の書換えは使わない。

planは全table/ID/値/順序を含め比較する。既存行/既存ラベル/値/FORMULAを含むbefore全体もbindingする。
_planの新規行timestampは表示準備時のserver prepared_atへ固定し、既存行timestampは保持する。
再計算は#139の_freshと同じく、**beforeにIDが無い新規receiptのcol7/importのcol1だけ**を固定値へ正規化できる。
その他の差を無視しない。prepared_atはderivationに含め、productionのglobal clockをmonkeypatchしない。

## 4. 金融authorityと明示操作のbinding（schema変更なし）

金融authorityは既存FIXTURE識別と既存field集合を維持する：
`mode, actor, owner, purpose, request_id, input_fingerprint, revision, issued_at, expires_at,
revoked, authenticated_action_digest, scope_digest`。

- input_fingerprint = digest(new inputs)。schema、immutable baseline、保存draft全値と版、独立原本版、カテゴリ版、plan/before digestを含む。
- scope_digest = digest([operation, source, plan, before])。既存の式を変更しない。
- authenticated_action_digest = digest(asdict(VerifiedPost))。VerifiedPost.binding_digestもdigest(new inputs)と完全一致。
- actor/owner/purpose=ACCOUNTING_POST、issuer、POST、Origin/CSRF、session、600秒TTL、revocation、request IDを従来どおり検証。
- financial VerifiedPost.expected_revision=0という既存native契約を維持する。draft revisionやshared state revisionで置換しない。
  draft版・native文書版・共有CAS版はそれぞれ別domainで検査する。
- ownership.input_snapshotにもdigest(new inputs)、native_preflight_digestにはrequest全体fingerprintを使用。
  source whole operation identity、relation、共有request schema/CAS engineは変更しない。

将来の流れ：owner/session→serverがbaseline/source/saved draft/master/beforeを再読取り→全入力とplanを表示
→本人「この内容で記帳」→独立した金融VerifiedPost/native資格の照合→新validatorで再計算
→native qualification→共有CAS→native consume/pending→start、以降は別承認のwriter接続。
表示GETは保存/qualification/authority発行をしない。draft saveや#131 acknowledgementから金融資格を発行しない。
今回のread-only例にはauthorityそのものが無く、qualified_request=false/accounting_permission=falseである。

## 5. 新validatorの設計手順と旧validatorとの境界

```text
validate_draft_request_v2(request, trusted_readers, existing_financial_context):
  require explicit inputs.schema and strict keys/types/capacity
  run unchanged validate_candidate(request) for common native/authority/ownership shape
  read native state twice; require preserved legacy digest + exact saved baseline item
  reject legacy pending/applied, held/unmapped, prior source operation/fence, uncertain IO
  reread source revision-specific bytes + independent native/Drive versions + whole-image identity
  reread encrypted DRAFT_ONLY snapshot and category master, with versions before/after
  require exact displayed raw draft + revision/tag/generation, owner/case/source binding
  resolve fixed line catalog; rederive confirmed and parsed from raw inputs
  reuse existing manual parser / receipt validator / _plan and all duplicate/privacy gates
  compare all parsed/plan/before/category/source/line digests and values
  verify unchanged PurposeBoundary + verify_attestation against new inputs digest
  reread all trusted evidence; reject drift; return read-only qualification facts
```

この手順は**設計であり今回はruntime関数として実装しない**。
新validatorから旧validate_requestを呼んでその拒否だけを無効化しない。
旧validatorのold==confirmed、旧medical parser一致、旧保存item一致は旧契約のまま残す。
新契約では baseline.item == 保存legacy item と、proposed == 保存draftからの独立再計算、を別々に要求する。
医療でも一般でも、新値をbaselineへ代入して旧等式を満たす方法は禁止。

新requestのproposed digestはpending/start/completionまで不変。
draft/原本/master/versionの変更は再表示を要求し、qualification以降に発生した不明結果はpending/fenceを維持して停止する。
再ログインは既存結果をread-only照会するために使え、旧不明要求の新ID再dispatch許可にはならない。

## 6. 保存互換性：request v2と保存root versionは別

既存receipt-reimport-v2 rootは旧requestしか保存できないため、将来の新codecは
**receipt-reimport-v3（設計候補）**を明示的に区別する。request v2の入力拡張とroot versionを混同しない。
rootはschema/revision/legacy/operationsの既存4領域を維持し、legacyの全decoded値と既存canonical encoded(legacy)を不変とする。
root versionの変更を含む保存文書全体のraw bytesは将来の別承認migrationで変化し得るが、今回はそのraw bytesにもwriteしない。
旧operationsは既存validate_operationで検査し、新inputs.schemaのoperationだけを新validatorで検査する。
別DB/別CAS engine/別金融authorityは導入しない。

| 保存状態 | 読取り/validation案 | mutation/移行 |
|---|---|---|
| plain v1 | 既存validatorでread-only | 自動wrap・昇格なし |
| root v2 + 旧request | 既存v2の契約を維持 | 新requestを混入しない |
| root v2 + 新inputs | 旧validatorが拒否 | 新validatorへfallbackしない |
| root v3 + 旧operations | 旧operationの全証拠を旧validatorへ | 旧記録を再生成/修復しない |
| root v3 + 新operations | 正確な新schemaに限り新validator | 別承認のisolated facadeだけ |
| 未知version/欠損/不明IO | UNKNOWN/read-only隔離 | reset/旧backupで再開しない |

今回、v3 class/factory、保存文書の初期化・migration・native CASは一切作らない。
将来の移行は同backend上で全legacy/旧operation/fence/ledgerを照合する別承認工程。
既存v2の履歴を捨てた空v3文書や別backendを用意して新規要求を通す方法は禁止。
旧runnerはv3を未知として停止する必要があり、v1へのrollback書戻しで金融処理を復活させない。

baseline.native_initial_tagはqualification時の初期読取り証拠。
その後の自己CASでnative tagが変わるため、初期tagへの永続一致は要求せず、各遷移のcurrent raw/tag・revision・前後read-backと
immutable legacy digestを照合する。draft tag/master tagは自己native CASとは独立して厳密一致を要求する。

将来のpendingはbaseline.itemのコピーから作るexecution projection（新confirmed）＋plan＋confirmation_hash。
medical decision_origin=humanを要求し、保存legacy itemは変えない。
consumeとpendingは既存と同じ1 CAS、startは次の明示CAS。全証跡は新request fingerprintへ結合する。
state名/許可順序はqualified→pending→writer_started→complete_fixtureを維持するが、これは今回の実装対象ではない。

## 7. 必要最小限の将来差分案

| 箇所 | 具体的な責務 | 実装の承認段階 |
|---|---|---|
| 新app/receipt_confirmation_draft_request.py候補 | 新inputs validator、image line catalog、read-only parsed/plan派生、独立reader比較 | 次の隔離実装レビュー後 |
| 新staging_shadow/receipt_draft_contract.py候補 | 明示v3 memory-only facade、既存ConditionalRequests再利用、新operation validation/qualification/pending/start/inspect | 次の隔離実装。保存文書移行とは別 |
| #140 synthetic_legacy_writer.pyのadapter入口 | exact contract type/schemaの明示選択、new pending/inspect dispatch。旧経路はそのまま | **さらに後のwriter接続承認時**。今回も次のwriter非接続段階でも変更しない |
| 既存writer/native authority/共有CAS/privacy/duplicate | 変更なし | 拡張の対象外 |
| 1タブreader/画像draft/UI | 新画像whole-source bindingを明示注入。PDF/旧Unitの契約を流用しない | 後のR1画面統合承認時 |

新moduleへ単純に旧contractをcopyして別のCAS engineを作る案は採らない。
既存bodyを変更せず再利用できる純粋なcheck/IO/CASを呼ぶ。
#140 adapterはexact typeと旧schemaを要求しており、新contractを今渡しても拒否される。そのguardは今回は維持する。
既存_native_completed/_MemoryLedger/lease/call-frame guardとwriter本体は将来も緩めない。

結果照会は将来、認証済ownerのreadだけでfrozen requestとnative completion/shared CAS/独立ledgerを照合する。
writer戻り値のみでは成功にしない。PARTIAL/UNKNOWN/重複/保留を区別し、照会によるrepair/再dispatchは0。
完了後のdraft変更は記帳済snapshotと別表示にする。現在のledger/観測tagが完了証拠と合わなければ、
「完了記録あり」と「現在read-back成立」を混同せずUNKNOWN/要照合にする。旧結果から新権限を発生させない。

## 8. read-only合成検証と後続テスト計画

今回のtestsはbaselineを1回だけfixture準備し、その後store.save、UI/native mutation、ledger append/update、
authority発行、CAS replace/transition/consume/start/completion、writer/materializer、Google client、socketをfail spyで拒否する。
ReceiptResult/既存medical _parsed/validate_receipt_result/_planだけを呼ぶ。
typed parsedとplanを具体的な全ID・値へ照合し、native保存値/既存ledger rows/原本metadataの前後一致も検査する。
純粋planned_afterは期待値の計算だけであり、台帳へ代入しない。
初期fixtureのmemory UI/入力準備と、その後の検証操作を区別する。
既存R0/PDF回帰rigの初期memory文書準備は継承したfixture構築であり、新契約のqualificationや金融claim成功とは数えない。
新規例はnative v2/v3 storeやauthorityを構築せず、freeze後のnative/ledger writeと金融dispatchが0であることを検査する。

| 証拠 | 今回検証する範囲 | 今回証明しないこと |
|---|---|---|
| 一般修正 | 名前/金額/カテゴリ/日付/店舗/支払方法が既存IDのplanへ反映。既存labelと無関係行/数式保持 | writer実行・append/update成功 |
| 医療 | 完全手入力修正が独立parsed/planへ反映、部分/AI/OCR候補拒否 | 実医療/手入力UI/実ユーザー操作 |
| identity | 追加/削除/並替/重複ID/位置変更/カテゴリ一括/PDF変換拒否 | 本番source readerの耐久性・ACL |
| binding | draft revision/tag/generation、原本版、category版、native初期観測、hash再計算済の改ざんも独立再導出で拒否 | 金融VerifiedPost/issuer/qualificationの実装 |
| 状態 | hold、旧p1/G103/H301、旧pending/applied、過去UNKNOWN等を新規試行にしない比較モデル | native状態機械・CAS応答喪失・16並列の新契約実証 |
| 旧境界 | runtime/pin/旧validatorのdiff 0、#142の旧拒否証拠を再実行 | 旧validatorで新契約を認可すること |

例のschemaはreadonly-draft-review-example-v1。新inputsの一部を示すが、authority/資格を持つnative requestではない。
例にはfinancial request ID/VerifiedPost/authorityがなく、accounting_permission=false / qualified_request=false。
compare_with_current_readersは**tests限定の設計比較oracle**であり、新しいruntime validatorの完成とは扱わない。
現検証はネットワーク/IO原子性や「人が実際に見た」ことを証明せず、serverから供給した架空reader値間の整合を検査する。

次の隔離実装で必要なテスト（今回未実施）：

1. 新/旧/未知schemaの厳密routing、全key/type/duplicate JSON/容量/NaN/bool、既存golden requestの旧挙動完全一致。
2. source bytes/hash/独立Drive/native版、draft revision/tag/raw、master版、baseline catalog/全ledger/FORMULA、prepared_atの前後race。
3. 既存金融owner/actor/issuer/session/Origin/CSRF/600秒TTL/purpose/expected_revision=0/request-IDの全negative。
   新inputs digestを既存authority fieldへ結合し、ack/Sheet値/ブラウザーauthorityだけで認可しない。
4. 同request再送はread-only、異内容再利用/別request同source/通常取込競合は拒否。
   2/16 facade、各native/shared CAS lost response/412/read-back不一致、再起動後read-only、未知結果後の再dispatch 0。
5. writer非接続段階は全dispatchをfail spyで0。writer接続はその後の別承認で、途中停止/全行後証跡前停止、PARTIAL/UNKNOWN、全ID/値/式/旧行照合を実証。
6. 最後にR1の本人明示操作・検索/原本往復/入力保持/再認証/二重タップ/390pxを統合。iPhone実機は別受入。

## 9. 完了判定・レビュー事項

**設計とread-only例の検証を完了対象とする。B1/B2のruntime解消・R1完成とは判定しない。**
新Draftのcommit/run/確定件数はPR本文へ記録する。
レビュー対象は、画像の固定明細catalog、inputs v2のbaseline/proposed分離と全binding、
旧validatorの拒否維持、保存root v3を別に必要とする点、上記2 moduleの将来隔離実装範囲。
設計レビュー後に隔離実装の別承認が必要。writer接続・保存文書migration・R1画面・本番はそれぞれ未承認。

一般・医療の合成記帳件数は今回ともに0。productionコード変更、実Drive/Firestore/OAuth/医療情報アクセス、
新Drive資源、Cloud/deploy、実金融authority発行/消費、本番POST/記帳、原本移動、AI送信、main mergeは0。
G103/H301/対応不明の旧医療p1/PDF保留は維持。正式1タブUIの変更も0。
