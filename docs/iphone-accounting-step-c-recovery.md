# Phase 5B-2B-1 Step C：合成障害注入とread-only復旧照合

#133 head `d4aec4e3b90a0009f83edb5547d3388894dfe9ec` から独立分岐。対象はStep Cのみ。#132の停止理由と#133の隔離compositionを維持し、既存native authority/claim/writerの実接続を行わない。本番constructor/runner/workflow、authority schema、認証、1タブUI、既存PRを変更しない。

## 再利用した契約とモデルの限界

共有金融状態は既存 `ConditionalRequests.read/replace/transition`。Step A/Bのtrusted ownership reader、native read-only preflight、private強制gate、Unit fence、PurposeBoundaryをそのまま通る。下書きCAS・本人確認受付は使わない。Gateに追加するcheckpointは、default OFF・memory-only・隔離指定必須の既存合成gate内に限る。HTTPや本番への登録はない。

`staging_shadow/accounting_recovery_model.py` は**既存native状態の観測投影**であり、新native authority schema、native実装の代替、復旧コマンドではない。証跡は別のmemory-only fake IOへ保存する。これは合成native/pending/ledgerを再現するテストadapterであり、新CASエンジン・DB・サービスではない。既存versioned IOの条件付きreplace/read-backを使い、自動retryしない。

| 入口 | 元のnative契約 | 投影とread-back |
|---|---|---|
| 通常自動取込、買取収入 | `receipt_pipeline.ReceiptPipeline.process_bytes/_materialize_result`。import ID確認とcommit markerがあり、同等の永続native claim/完了journalはない | native状態は常に`unsupported`。writer直前spy・部分ledger観測の障害試験は可能だが、COMPLETE/NOT_EXECUTEDのnative証明を捏造しない。**常にUNKNOWN** |
| 旧一般／医療完全手入力 | `ReceiptConfirmation.apply_confirmations/_write_accounting_plan` のwaiting → pending → applied。失敗時pending、再呼出しは完全read-backのみ | 同じ状態名。`ReceiptConfirmation._complete`と既存`same_row`をdetached read viewで使用。全対象行と無関係行の保護も検査 |
| PDF auto/manual | `PageIntake._post/recover`：intentなし → claimed → complete/imported。partial/unknownはclaimed保持 | 同じintent状態。既存 `pdf_intake_runner.exact_readback` とvalue/formula snapshotを使用。`recover(apply=True)`は呼ばない |
| grouped Unit | hash固定native `pdf_unit_processing.DriveUnitProcessingStore.reserve/verify_pending/complete`、既存 `GroupedUnitPort.readback` | absent → pending → applied。既存exact_readbackを使用。実reserve/completeは呼ばない |
| 固定canary／明示Unit | `receipt_plan.posting/unit_posting.execute/finish`：unused → claimed → complete。handledな不完全writer結果はunknown。crash時は直前の状態が残る | 同じ状態名。既存 `posting.outcome` で全ID/value/formulaと既存ledger digestを照合。実execute/finish/history更新は行わない |
| 将来1タブ入口 | #133のprivate in-process slot。#131受付は資格ではない | 既存各native operationへserver側で固定したaliasを試験。買取actionを将来の一般postへ読み替えることは既存validatorが拒否する |

履歴・sealed proof・authority消費・native request ID制約・projection hideを完全なnative実行として再現したものではない。合成evidenceのschema/結果はnative grantではなく、実行API/append/retry/release/authority factoryを持たない。既存コードのstate/exception/read-back契約を基にした抽象投影であり、native storeの正式なレコードへ変換しない。auto/buybackにないnative状態は未解決として残す。

pending投影はownership・request・fingerprint・全planを固定した合成intent bindingも保持する。state名だけがclaimed/completeでもbindingが欠落・不一致ならUNKNOWN。このdigestはnativeのsealed proof/confirmation_hash/authorityの代用品ではない。groupedのterminal phaseはUIのimportedと混同せず、hash固定native `complete` 本体の`phase='applied'`をコード照合する。

全入口が**同じ共有ConditionalRequests backend、同じownership router、同じ元ledger/pending port**を参照する。その上に同じ単一の合成native証跡・ledger投影を重ねる。投影ledgerは全operationを含む一つのatomic imageで、各recordのread viewが異なれば破損として停止する。入口ごとに独立fake storeを接続して共有済みとしない。pending overlayは同じ投影を読み、旧pending/結果不明/完了所有を無断解除しない。

## 全入口と障害ポイント

通常自動取込、買取収入、旧領収書確認、PDF auto/manual/grouped、固定canary、明示Unit、将来1タブの9種に、次の10 checkpoint × crash/timeout/lost response/stale revisionを注入する。

| checkpoint | 位置 |
|---|---|
| before_shared_claim | 所有観測・最初のnative read-only preflight後、共有CAS前 |
| after_shared_claim | acceptedの強いCAS/read-back直後 |
| after_native_validation | executing後、実行直前の所有関係/native validator/authority再照合後 |
| before_native_claim / after_native_claim | 合成native claim投影の前後 |
| before_writer_start / after_writer_start | 合成writer開始証跡・副作用のないspyの前後 |
| after_partial_ledger | 全対象ledgerの一部だけが観測された投影 |
| after_native_complete | 合成native完了後、共有状態更新前 |
| after_shared_update | 共有complete更新後、成功応答前 |

crashは`BaseException`で通常のException後始末を迂回する。再起動はcoordinator/modelオブジェクトを破棄し、同じfake remote IOから再構成する。OS/実DBの耐久性試験ではない。timeout/lost responseのcheckpoint試験に加え、実際のfake条件付きreplaceが反映後に応答を失う試験、強いETagを別process相当の更新が進めて412を生む試験を、共有遷移と合成証跡の各replaceで別途行う。

共有状態はaccepted → executing → awaiting_readback → complete、途中失敗は既存規則のpartial_unknown。hard crashではaccepted/executing等が残る。native投影とledgerは独立して進むため、shared completeだけを完了証明にしない。どの状態も共有ownership fenceを保持し、read-backで共有/native状態を更新しない。

所有確認後・共有CAS前に停止した場合、保護されたclaimもwriter証跡もないのでUNKNOWN。CAS未取得でprobeは0だが、ledger不在から「再実行してよい」とは判定しない。この窓には既存CASの所有claimは存在しないため、将来の別の明示要求を排他済みとは称さない。今回、自動再要求・新ID発行・補完dispatchを行うAPIはない。

## 読取りだけの判定

`EvidenceModel.report` は既存owner/session read認証後、共有CASと証跡IOのstrong ETag/bytes、source/bytes hash/partition/native version・入力・authority revision、元の完全なledger/pending観測を前後で照合する。read-back中に状態が変わればUNKNOWN。既存native intentを持つ再送を無理に新しいnative preflightへ通さず、保存済み要求のowner確認付き`inspect_result`と明示reportで照合する。通常submitがpendingで拒否した場合も自動実行しない。

| 判定 | 必要条件 | 作用 |
|---|---|---|
| COMPLETE | bindingが全て一致、整合する共有状態、native完了投影、writer開始1回、validator確認、全期待行/value/formulaと保護した既存行が正確に一致 | 合成結果をread-only再提示。共有/native完了補完も実行権限発行もしない |
| NOT_EXECUTED | 保護された共有claim、対応するnative unused/未開始またはclaimed intent、完全なwriter開始証跡0、全対象ledgerが完全なbefore snapshot、原本/Unit/入力/authority/pendingが一致 | 読取り証明だけ。claim/intentを解除せず、retry許可も出さない |
| PARTIAL | 部分行、値/formula不一致、重複等を既存read-backが検出 | 論理隔離、追加append・補完・再dispatch禁止 |
| UNKNOWN | claim/intent/証跡の欠落・破損、強いETag不足、版変更、incomplete観測、対応不明、状態矛盾、writer開始済みでledger不在、autoのnative契約不足等 | 論理隔離、成功/未実行へ変換しない |

隔離は既存ownership fenceを維持し、結果にquarantinedを示すこと。別のquarantine DB、会計修正・新authorityを作らない。COMPLETE/NOT_EXECUTEDでも`retry_allowed=False, native_authority=False, accounting_permission=False`。同IDの異内容は拒否、同内容は保存済み状態の照合のみ。partial_unknown/完了の別入口・別IDにも再dispatchしない。

人間向け報告はserver側canonicalの案件/原本版/Unit/入力snapshot/authority revision、対象全ledger ID・before/期待値/観測値/formula、native pending状態、writer開始回数、checkpointを提示する。資格情報・nonce・実行target・POST/復旧ボタンは含めない。HTMLはescapeする。実医療情報は使用せず、表示内容も完全合成。新しい本番GET/POSTは登録しない。既存GETは従来どおりread-only。

## 競合・回帰・未解決

最大16個の別ConditionalRequests/gateが同じbackendへ競合する。future aliasと全legacy、auto/旧確認、全原本/Unit、PDF相互、canary/明示Unitを区別して試験する。fixtureの原本が別の場合を「同一原本の競合」と偽らない。同原本・同Unitは1回、確定済み別Unitはnative pending前の独立claimを許す。旧pending/不明/完了所有が観測される場合は別Unitも保守的に停止する。元のnative state/会計行/writer回数は測定前後不変。

全シナリオの後始末でも、guarded probeへの到達回数をoperationごとに集計して最大1回を検査する。writer開始前に失敗した到達も含め、writer開始回数だけでは隠れる二重許可を検出する。

専用Ubuntu 24.04/Python 3.12 CIは正確なPR head、contents:read、資格情報/Secrets/vars/environmentなし、write flag false。新規node IDのcollect/PASSEDを照合し、#133までの1,300件の安全回帰と新CI境界を実行する。確定件数・run URL・commitは新Draft PR本文に記録する。

Step Cはこの合成モデルでの障害・照合検証。未解決はauto/buybackの永続native claim/実行開始証跡、実nativeレコードと共有claimの原子的な結合、native完了/history/authority消費の実接続、live source/ledger/pending reader、実backendの耐久性・IAM/鍵/保持/復旧/監査、実OAuth/iPhone。これらを合成成功で解消済みとしない。

5B-2B-2の既存authority/writerとの合成接続は**未着手・結果確認後の別判断**。auto/buybackの未解決契約を先に具体化し、既存native request/authority/Unit fence/read-backを維持した接続と再試験の範囲を承認する必要がある。本番constructor/runner/workflow変更、live接続、公開環境、main統合、デプロイ、実writeは別々の承認事項。今回の合成報告を本番復旧権限として使わない。

依存：main → #123 → #124 → #127 → #128 → #129 → #130 → #131 → #132 → #133 → 本Step C Draft。#125/#126は既にmerge済みの仕様文書。既存PRのhead/base/Draft/state、本番・既存検証ブック・Drive/Firestore/OAuth/医療・Cloud/AI/会計への副作用は維持/0。
