# R2-4前半：既存更新writerの共通境界への隔離接続

基点は #150固定commit `d25d3823310c832fe955a2b3d51eb1df539c7184`。作業Aとは独立分岐。本番情報を合成fixtureへ流用していない。**本番移行NO-GOを維持する**。新module/新テスト/新Ubuntu CIだけを追加し、既存app・writer本体・production constructor・auth bundle・workflowは変更しない。

## 接続範囲とnativeの意味

| 入口 | 隔離接続 | 再利用する既存検証・writer / native |
|---|---|---|
| A01 通常review | ReviewApprovalPipeline.apply | 固定import ID、カテゴリ、既存処理状態・判断、重複/統合対象の検証。production_runのreview_apply begin/complete |
| A02 自動照合 | ReconciliationPipeline.apply | 既存scope・店舗alias・duplicate判定・固定ID link/status・支出除外。production_runのreconcile begin/complete |
| A03 自動支出 | AutoExpensePipeline.apply | 既存分類、承認カテゴリrule、既存本人分類保持、固定expense ID、duplicate。production_runのauto_expense begin/complete |
| A03 銀行最終反映 | BankFinalizationPipeline.apply | 既存validate_bank_finalization_canaryの固定対象/commit検証、既存decision・カテゴリ・duplicate・独立_verify。合成laneではauto_expense native stageを使用 |
| A10 固定ID支出修正 | DailyCorrections.apply / bounded apply_pending adapter | 既存queued request、入力・before/after fingerprint、index固定ID再確認、カテゴリrevision、pending→applied。v3へ変換しない |

**A10の収入修正とmoney reviewは、それぞれ未接続**。DailyCorrectionsは元から支出専用。daily_money_review→MoneyReviews→MoneyWriterも汎用の収入修正nativeではなく、既存金銭例外の計上・対応付け等を扱い、writerは取込/支出へ反映する。今回指定された関数群に固定ID収入修正の既存contractは確認できなかったため、支出用nativeの流用や新たな収入authority追加で補わない。この機能は適切な既存native/authorityと対象表の特定が必要な保留事項である。A10全体が完成したとは報告しない。A04〜A09/A11〜A13も未接続のまま。parser・PDF金融writerは変更なし。

A01〜A03に個別の署名済みreceipt authorityがあるとは扱わない。既存productionのnativeは親ProductionLedgerのstageと既存domain validatorであり、今回もその形式を保持する。A10には既存corrections requestがある。合成execution journalはこれらの検証結果・開始/消費・対象bindingを記録するだけで、本番金融authorityの代替ではない。銀行の原本取込authorityはこの最終反映adapterが新たに発行/消費するものではない。

## 最小構成

`staging_shadow/isolated_update_adapters.py` の `UpdateLedgerBoundary` は既存 `LedgerExecutionBoundary` を継承する。既存 `execute_update` の無条件拒否を変更/解除せず、memory-onlyの `compose_updates()` からだけ新しい接続を作る。receipt側は既存 `compose_receipts()` → `MandatoryReceiptComposition` →既存guarded writerのままである。

既存の全ledger snapshot（支出・収入・receipt・取込の全値/数式）と同一shared storeを使う。更新側は**最初に同じledgerの既存completeフラグをCASでfalseへ置く**。更新途中は他のreaderに完全な独立snapshotを公開しないためで、旧receipt構成もこの既存条件で止まる。別の空journalを与えた再構築でもholdを隠せない。その後、全ledger/source/nativeのbeforeとstrong tag、全対象ID/行/値/式・固定plan・ledger claimを合成execution journalへ既存CASで永続化し、**既存shared request documentのrevisionをCASで予約**する。receipt用request/ownership/金融authority schemaへbankやcorrectionを偽装しない。更新中にreceipt側が先にclaimした場合も、独立再観測とledger CASで止まり、両方holdになることがある。自動retryで稼働性を作らない。

新DB・CAS engine・login方式は追加しない。`Envelope` は合成保存形のrevision/payloadを読む薄いadapterで、compare/replace/read-backそのものは**元のConditionalRequests.replace**を継承する。native payloadはProductionLedger/correctionsの元の形式を保持し、合成用envelopeの内側へ格納する。本番nativeのmigrationや初期化は一切含まない。新たな金融authority schemaではない。

保存先はshared / 全ledger / native / source・カテゴリ等 / execution evidenceの5つ。1 transactionと扱わない。partial prefixが残れば以後のreceipt・更新をともに止める。既存retained_update_portsの未統制history拒否も維持する。空inventoryは新規合成fixtureの宣言だけで、本番にpendingがないとの推論ではない。

## 金融実行の10条件

1. source（取込・review・カテゴリ/rule・補助index）とnativeを別々のversioned frameで観測。前後2回のframeを一致させる。実原本は読まず、合成sourceのみ。
2. 全ledgerのbefore snapshot、ID・診断用行位置・全値/数式、全変更planと完了後snapshotを固定。行番号だけをidentityにしない。
3. 既存native writerを純粋なmemory plan DBで実行し、元のdomain validator・分類・duplicate・bank canary・DailyCorrections fingerprintを再利用。DailyCorrectionsのqueued requestは元のprepare validatorでも再構築し、changes/cells/before/after/category bindingを一致させる。重複IDのplanはclaim前に拒否。validator errorがあれば拒否。
4. 同じledgerの既存完全観測flagを強CASでholdし、合成execution journalの全ledger ownershipと既存shared revisionをbindingする。別source・別IDも全snapshotを変えるため同時実行を許可しない。
5. 元のProductionLedger.begin、または元のDailyCorrectionsが計画時に生成したpending payloadをCASで保存。既存pending/UNKNOWN/PARTIALは受付けない。journalのrequest IDは一度だけ消費する。
6. native pendingと独立してwriter_startedをCAS永続化し、dispatch budgetを1回だけ消費。
7. dispatch直前にsource/native/journal/shared/全ledgerの全frameを再確認し、既存validatorの再実行planと凍結planを一致させる。
8. private moduleの既存apply bodyを1回dispatch。guarded methodは正確な呼出元と専用memory DBを要求する。裸apply/live DB/任意writer callback/approved=Trueは開放しない。production appをmonkeypatchしない。
9. 各sinkがplanの固定ID/行/順序と全frameを確認し、全ledger/backendを直接CAS。全行後はwriterの返却値だけに依存せず、全対象・無関係行・数式を独立read-back。
10. 元native complete/appliedのread-back→journal native_complete→別CASのshared完了予約→journal completeを照合し、最後にこの同一実行だけがledgerの完全観測flagをCASでtrueとして公開する。途中で落ちても追加writeやcleanupをしない。旧UNKNOWNのholdを解除するAPIはない。

完了した旧receiptの行をA02/A10等が正当に更新した場合、過去のreceipt native証跡は消さない。完了済み更新の厳密なbefore/after差分を**読取り上だけ**逆にたどって元のnative proofと照合する。実ledgerへの巻戻しwriteではない。証明できない変更はholdする。

## 既存処理の保持

`apply_core()` は A01→A02→A03 の元の順序で、各stageのnative/shared完了後に次をprepareする。bank-finalizationは既存通り明示的な固定ID canaryであり、自動coreへ勝手に追加しない。既存分類rule、本人のF/G分類、native money_mode、bank内部振替の設定は合成sourceに凍結する。新しい分類/duplicate実装は追加しない。

A02の既存reconciliation_lookback_monthsと、銀行のconfirmed_internal_transfersも独立configから明示的に渡す。欠落を既定値/空集合で補って挙動を変えない。照合期間1か月/6か月の元のscope差分も合成試験で確認する。

plan時と実dispatch時に同じ元writerを使い、clockだけprivate module内で固定する。カテゴリrule traceの時刻も同じplanへbindingする。DailyCorrectionsは指定cellだけを変更し、無関係formulaを保持する。既存のUSER_ENTEREDで数式に解釈され得る入力は隔離adapterが保守的に拒否する。本番Sheetsの値解釈・projection invalidation・validation装飾・実権限の挙動は合成成功で証明しない。

銀行canaryのcurrent HEADは固定argvのローカル `git rev-parse HEAD` から独立取得し、既存CLIと同じapproved_target/expected_headを独立source configから読み、元のvalidatorへ渡す。呼出側が同じ偽SHAを2個渡しても通らない。既存writerの固定ソースhashも別途検証する。category_rule_auto_apply_enabledも独立source configの観測値を使用し、caller booleanで上書きしない。このGit照会はcommitメタデータのread-only取得のみで、本番CLIを実行するものではない。

`apply_pending()` はqueuedだけを固定ID順に上限付きで個別gateへ送る。旧pendingを元のDailyCorrections.applyで自動回復する経路は露出させない。1件UNKNOWNになれば次へ進まない。

当該実行が新規queuedから自分で保存したpendingについては、そのphaseからも元のnative計画を求める。同値修正は元のnative read-backでcell writeなしに正常完了できる。実行前から存在したpendingは、値が一致していてもこの例外に入れず拒否する。

## 異常時と再構築

CAS 412、timeout、応答喪失、writer途中停止、全行後停止、native完了後/共有完了前停止を注入する。例外時は追加の共有状態遷移・native recovery・retry・fence解除を行わない。永続phaseをそのまま保持し、`inspect()` はCOMPLETE / UNKNOWN / PARTIAL、全ledger一致、retry_allowed=falseをread-onlyで返す。writer_startedの永続dispatches=1は開始枠の消費であり、プロセス停止時の実call成功数を断定するカウンタではない。

新しいcompositionを同じ合成backendsから再構築しても未完了journal/native/ledger観測flagがreceipt/更新の双方を拒否する。別の新規journalや元のLedgerExecutionBoundaryを使った入口も同じledger holdを拒否する。journal保存前のcrashは所有者未確定のledger holdとしてread-only照会し、journal完了後でもledger公開前ならUNKNOWNを維持する。journal完了labelだけでは不十分で、native_after・candidate binding・開始証拠・全ledgerを照合する。旧v1へのfallbackや解除APIはない。

## 検証と残件

新テストは各applyの正常反映、同一/異native sourceの全snapshot競合、ID/行/値/式/source/native変更、直接sink拒否、2/16並列、5保存先のCAS障害（実際に更新する保存先）、crash、再構築照会、旧pending/UNKNOWN/PARTIAL、通常/買取/R1一般/R1医療との同一境界を検証する。詳細な実測は [validation](r2-4-update-validation.json)。継承したM1〜M6/R1/静的安全回帰は専用Ubuntu CIで分離実行する。

合成backendはlock-backed Memoryのstrong CASであり、実Google backendのCAS/ABA保証は未証明。本番constructorは存在せず、booleanだけで有効化できない。Python introspectionを敵対的sandboxとして防ぐ仕組みではなく、ライブclientを一切与えない隔離compositionである。

R3には①Aの実flag/validated SHA・旧host/image/credential主体/実効ACLの不足確認、②本番pending/UNKNOWN照合への個別read承認、③全金融旧入口（A04〜A09/A11〜A13とA10 moneyを含む）のadapter接続または限定drain/閉鎖、④実backend強CAS/全ledger観測/authorityと保存先bindingの証明、⑤旧金融能力の権限側閉鎖と表示導線の影響承認、⑥限定切替・独立read-back/受付停止/read-only rollbackの個別承認が必要。A10収入修正は、既存native/authority・入力revision・before fingerprint・固定income ID・対象表を先に特定し、その範囲を改めて承認するまで未実装とする。

既存の定期取込を維持し、一般receipt・医療完全手入力R1を先行、PDF金融接続は後続とする方針を維持する。今回の合成接続を本番の全金融入口完成・R3開始GOとは扱わない。
