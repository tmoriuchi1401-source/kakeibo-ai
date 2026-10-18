# Phase 5B-2B-2C：旧確認native契約の不足による接続停止

判定：**合成検証未完了・一般／医療とも新writer接続なし**。PR #136 head
`237c53585e5bbb1c000682d4916182a118acab0a` から独立した調査・停止証拠のDraft。
ユーザー指定の「不足するauthorityを新規発行・捏造しない。必要な証拠がない場合は停止」
「既存authorityやnative schemaの変更が不可欠なら具体的な差分と理由を提示して停止」に従う。
実装を進めるための再承認待ちを口実にしているのではなく、既存コードから指定証拠を取得できないため停止する。

今回の差分は再現テスト、専用合成CI、その完全一致分類テスト、この報告だけ。
既存authority、pending schema、validator、writer、production constructor/runner/workflowは変更しない。
新CAS、DB、authority provider、実行adapterは追加しない。

## 調査範囲と既存契約

mainのAGENTS.mdと継続仕様全文（第10節を含む）、PR #123～#136の説明・head/base/Draft、
継承したコードと関連設計を調査した。#125/#126は既存どおりmerge済みの仕様文書で、他はDraft。

| PR | 引継ぎと今回の確認 |
|---|---|
| #123/#124 | 旧医療隔離、purpose、既存native port、operation identity、外側ConditionalRequests、read-back |
| #125/#126 | mainの継続仕様、正式な確認1タブ、G103/H301、本番・Cloud凍結 |
| #127/#128 | stable case/input binding、一般明細／医療validator dry-run、Sheet構造変更の拒否 |
| #129/#130/#131 | 合成認証host、独立source観測、下書きCAS、合成本人確認受付は金融authorityでない |
| #132 | 未接続native入口と任意legacy gateの棚卸し |
| #133 | TrustedOwnershipReader、全原本対Unit fence、同じConditionalRequests、private namespaceのmandatory gate |
| #134 | crash/partial/UNKNOWNモデル。モデルの証明と実native保存契約を区別 |
| #135/#136 | 通常取込／買取専用の合成証跡と実materializer。旧確認のnative契約へ転用不可 |

一般は `prepare_general → render/capture_inputs → _parsed → _plan` を再利用できる。
原本と既存会計before、候補解析結果、明細ID、カテゴリ、duplicate/link、既存明細削除・無効行・照合保護がある。
医療は `observe_medical → render/capture_inputs → _parsed → _plan` を使い、完全手入力、
正の日付・施設・実支払額・既存医療カテゴリ、既存取込拒否、医療自動plan拒否を維持できる。
医療のOCR/AI候補は使わない。

実行順は `ReceiptConfirmation.apply_confirmations` の既存検証 → pending保存 → 原本とlive入力再検証
→ `_write_accounting_plan` → `_complete` → applied保存。
pending再開は保存target照合 → 原本検証 → `_complete` → applied保存で、writerを再dispatchしない。
この順序を並べ替えて安全だと評価していない。

## 不足する証拠（一般・医療に共通）

1. **要求されたauthorityの実体を読み出すproviderがない。**
   `ReceiptConfirmationPort.inspect` は注入readerから `NativeInspection` を要求するが、
   ReceiptConfirmation本体からauthority revision、有効期限、actor/owner/purposeとnative requestを
   読み出して検証する実装は存在しない。旧経路の本人入力、保護されたstore、production実行境界はある。
   それを無視するという意味ではなく、今回要求された独立したnative証拠へ変換できない。
   `tests/test_unified_receipt_write.py:medical_native` は明示的な `FakeNative` を注入する。
   #133の `tests/accounting_gate_fixtures.py` は旧一般／医療のauthority revisionを定数1、
   `native_request_id` を空文字として初期化する。`state_read` のdigest/tagもnative状態の観測用で、
   authorityの発行・revision・消費を証明しない。#131受付と#135 journalで埋めない。

2. **pendingは要求されたclaim/start証拠を保持しない。**
   既存pendingは `status='pending', plan, confirmation_hash=digest(inputs)`、
   医療では `decision_origin='human'` を保存する。native request identity、authority revision/期限、
   actor/owner/purpose、共有claim binding、writer開始・dispatch済みの永続証拠は保存しない。
   一般beforeはあるが、医療ではemptyであることを `_plan` が照合するだけで、全会計beforeをpendingへ固定しない。
   `_complete` はplan対象のID/正規化値を照合する。全既存行・formula・native完了由来の独立証明ではない。

3. **旧storeはstrong ETagのnative CASではない。**
   `ReimportStore.save` は旧payloadをread比較し、`transport.write`、完全read-backを行う。
   compareとwriteの間に原子的preconditionはない。`DriveStateTransport` の既存契約もproduction
   workflowのtop-level直列lockを前提とし、分散lockではないと明示する。
   今回のinterleaving再現では、別storeのsaveが両方成功／各read-back一致しても一方の更新が失われる。
   外側共有CASの正しい排他と、要求されたnative CASの存在は別である。既存本番事故の観測ではない。

4. **原本版・完了の判定範囲が異なる。**
   旧sourceには単一 `version` があり、production観測ではDrive version由来。
   独立したnative versionを旧stateから取得するreaderはない。#133で別名を付けた合成版はその実証でない。
   `ReceiptConfirmationPort._current` はmetadata versionの厳密一致を要求するが、
   native pending再開の `verify_receipt_source` は同bytesならmetadata version更新を許容する。
   旧仕様の変更意図を今回否定するものではないが、独立版一致を要求する新接続の完了根拠へ流用できない。

## 新規再現テストの意味

`tests/test_legacy_confirmation_connection_stop.py` の13ケースは全て完全合成。
既存 `ReceiptConfirmation`、実 `verify_receipt_source`、`ReceiptConfirmationPort`、
既存 `ReimportStore` と既存MemoryTransport/DBを再利用する。socket接続は禁止する既存conftestを使う。

- 一般／医療の実validatorとdry-runが通っても、native reader不在のinspectは拒否。状態・会計writeは0。
- native pendingを実 `apply_confirmations` に保存させ、writer直前のfailing spyで停止。
  before／prefix／fullの合成会計観測を直接fixtureへ置き、既存pending再開だけを実行する。
  before/prefixはreconciliation requiredでpending保持、再dispatch 0。
  fullはwriter未実行でもnative側がappliedとなることを再現する。
  **これを新接続のCOMPLETEとは扱わない**。writer開始とnative完了由来の証拠不足を示すnegative characterizationである。
- 同hashでmetadata versionが変わるとportのexact version検査は拒否するが、
  native pendingのfull観測はappliedへ進む。新接続側でこの差を埋める必要がある。
- 実ReimportStoreのread/write間へ別saveを差し込み、両方成功しても更新が失われることを再現。
- `confirmation_operation_identity` は入力変更では変わらない正しい金融operation identityであり、
  native request/input/authority bindingの代用にはできないことを確認。

full/prefix観測の行はfixtureへの直接代入であり、新writerによる記帳ではない。
同processの再構築を実process再起動・実backend耐久性の証拠としない。

## 接続前に必要な具体的変更案（未実装・要レビュー）

現行native契約を変えずに成立するtrusted証拠providerが別に存在するなら、その実体・保存先・validatorを先に特定する。
現ツリーの合成fixtureや受付をその実体だと扱わない。存在しない場合、少なくとも次が必要となる。

| 対象 | 現在 → 必要な差分案 | 理由 |
|---|---|---|
| native authority/binding | 旧UI条件だけ → trusted native request、actor/owner/purpose、revision/有効条件、exact source版と旧/修正入力snapshotの永続bindingとvalidator | 新しいauthorityを今回捏造せず、誰が何を許可したかの証拠を定義する |
| 旧pending schema | plan/hash → 上記binding、共有claim fingerprint、全対象ID・全before値/式、開始1回・完了read-back根拠を追加 | native完了後／共有更新前crashも独立照合するため。旧pendingは移行せず保留 |
| ReimportStoreの保存 | read比較＋無条件write → 既存backendのstrong conditional更新と完全read-back | 新DBや独立CASエンジンを増やさずnative claimを原子化するため |
| 実行境界 | pending後の原本/入力再検証 → 同位置でauthority/ledger/共有claim再検証、開始証跡の保存/read-back後に既存writerへ1回だけ到達 | 実際のnative順序を保ったままcrash窓を扱うため |
| completion/recovery | `_complete(plan)` → 全ledger/既存行/式＋native start/完了＋共有claim一致を追加検証 | ledger不在からNOT_EXECUTED、全行一致からCOMPLETEを推論しないため |

これはnative schemaと接続契約の変更案であり、このPRでは一切適用しない。
承認を得る場合も、既存validator/writerの条件を緩めず、専用合成環境から再開する。
新しい本人認証方式や自動医療authorityを提案しているのではない。

## 完了条件に対する報告

| 項目 | 今回の結果 |
|---|---|
| 一般接続 | native validator/plan/dry-runと停止再現まで。新共有writer接続なし |
| 医療接続 | 完全手入力validator/plan/dry-runと停止再現まで。新共有writer接続なし |
| authority/pending/CAS/writer | 既存コードを調査・再現。schema無変更。#133共有gate、#135/#136通常・買取接続は既存回帰として維持 |
| 新接続の合成台帳write/正確read-back | 未実施。fixtureの観測行代入を実writer成功件数へ加算しない |
| 新接続のwriter dispatch／二重dispatch／二重記帳 | 各0。未接続による0であり、接続完成の安全実証ではない |
| crash/partial/UNKNOWN | 新規はpending前後～writer直前停止と既存pending照合の限界。全障害matrixの新接続実証は未実施 |
| timeout/ETag/16-way/process restart/通常・PDF競合 | 既存#133～#136回帰を実行。旧確認の新writer接続に対する実証として扱わない |
| G103/H301 | 既存保留・LEGACY_UNMAPPED_HOLDを維持。旧医療p1の推測mappingなし。既存拒否回帰を実行 |
| 未接続入口 | 旧一般、医療手入力、PDF auto/manual/grouped、canary、明示Unit、新1タブ金融入口 |

PDFへ進むには、旧確認の不足解消に加え、各PDF固有の独立posting authority、page/Unit/index/partition、
native request/CAS/intentの実体を保持し、whole-source競合と別Unit非重複を同じ共有backendで検証する。
構造／種類／privacyの確認をACCOUNTING authorityへ昇格しない。

本番までにはnative証拠と保存契約のレビュー・必要なschema変更の承認、合成writer接続と全障害matrix、
live reader/実backendの耐久性・IAM・鍵・監査・復旧、全実行入口の強制gate、実OAuth・追加ユーザー認可、
iPhoneで明示POSTと結果確認が残る。実データcanary、実会計write、原本移動、authority実更新、
本番入口切替、Cloud/deploy、依存PRのmain統合はそれぞれ別途明示承認が必要。

専用Ubuntu CIはPR head固定、contents:read、write flag false、Secrets/Variables/environmentなし。
新規13ケース＋#135/#136の227ケース＋既存安全回帰（新CI分類1件を含む）を実行する。
確定CI結果・commit・新Draft PRはPR本文と最終報告へ記載する。CI成功は接続完了判定ではない。

本番Spreadsheet・既存検証ブック・Drive・Firestore・OAuth・実医療データ・Cloud・外部AIは未使用。
新Drive資源、実会計write、原本移動、実authority更新、既存PR変更、main merge、deployは全て0。
