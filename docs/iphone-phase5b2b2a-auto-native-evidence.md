# Phase 5B-2B-2A：通常取込・買取の合成native実行証跡

#134 head `6ede12c838799a4ed24d16e2f531c6bdc2f06080` から独立分岐。隔離合成compositionの証跡契約・adapter・fake永続backend・read-only照合だけを実装する。既存writer/materializerは新compositionから**計画生成目的でも呼ばない**。既存production constructor/runner/workflow、native schema/authority、認証、Google Sheets「確認」1タブは未変更。

## 調査結果と責任境界

`ReceiptPipeline.process_bytes` の既存import ID照合は受付済みmarkerの確認であり、原本版と全入口の所有claim・writer開始前後・部分反映を単独では証明しない。`_materialize_result` は通常支出でreceipt→expense→import、買取でreceipt→income→importの順。importは最後のcommit markerだが、その存在だけでは既存行保持・全対象行/formula・source versionを証明しない。買取は元の`_buyback_income`が分類`その他確認済収入`、元取込ID、receipt_buyback根拠、raw hash、既存収入行を検証している。

#133の既存ConditionalRequests/ownership/強制gateと、#134のUNKNOWN/read-back契約を維持する。下書きCAS・本人確認受付・金融claim・native証跡・ledger観測は別責任。新証跡は金融authority、#131受付の昇格、実native処理の完了証明ではない。旧Step Cのunsupported_autoも書き換えない。

## schemaと保存契約

`staging_shadow/auto_native_evidence.py` の`NativeEvidenceJournal`は、既存`ConditionalRequests`を継承して**同じreplaceのCAS/容量制限/完全read-backエンジン**を使う。readは合成専用schemaをstrict parseする。新DB/CASエンジン/lock/service/live clientは追加しない。新schemaは既存native schemaの変更ではなく、承認範囲内の合成契約。実nativeへの採用は別判断。

root: `schema=synthetic-auto-native-evidence-v1, revision, operations`。keyは既存`confirmation_operation_identity`の原本全体operation。各recordは以下を固定する。

| フィールド | 保持する証拠 |
|---|---|
| request.ownership | owner、案件、canonical source ID、bytes SHA-256、Drive revision/numeric version、独立native version、確定partition、scope=whole/unit=None、既存native operation、native input snapshot、authority revision、観測digest |
| request.kind/inputs/input_digest | 通常支出または買取、全parsed値・全明細・カテゴリmaster等のserver固定入力。既存native parsed snapshotと、全入力digestを区別 |
| request.plan/plan_digest | 全操作順、全ledger ID/期待行、金額・分類・commit marker。caller/browserからの計画生成はしない |
| request.before/before_digest | 会計3表と収入表の全value/formula、schema/extentを固定した保存前snapshot |
| request.native_request_id/shared_request_id/expected_revision | 別のnamespaceの明示IDとexpected共有revision。IDを自動発行・代入・retryしない |
| fingerprint | 全requestのcanonical digest。同ID異内容、同原本別IDの再所有を拒否 |
| shared_claim | 同じoperationの共有request ID/fingerprint、accepted revision、観測strong ETag |
| validation | 既存preflightのdigest、authority revision、synthetic_preflight_only、native_authority=False。実grantを作らない |
| writer_start | sequence=1、全request/共有claim/plan binding。保存＋read-back前は合成probeにも進めない |
| completion | 全ledger read-back digest、request/plan/marker digest、合成COMPLETE分類 |
| phase/events | 明示遷移と監査用の順序。状態名だけでは結果を認定しない |

全原本を仮のPDF Unitへ置換しない。source ID、receipt/import/expense/income IDを既存recordから再生成・書換えしない。fixtureは新規の完全合成初期データだけ。Goldenの既存ID規約を検査するが、実データ移行機能はない。

## 独立した3保存先と順序

全入口・全インスタンスが、同じ金融ConditionalRequests backend、同じnative journal backend、同じledger観測backendとownership readerを参照する。**3保存先は相互に独立**し、単一トランザクションではない。native証跡とledgerを同じatomic imageへ同梱して成功扱いしない。

native repository自身も、共有claimの完全なrequest/fingerprint/bindingと、全保存済みnative ownershipの関係を検査する。同hashでDrive/native版だけが変わったoperationもUNKNOWNとして拒否し、金融store側の一覧だけを所有証拠と仮定しない。

1. 既存owner/session/POST境界、trusted ownership、元のprivacy/通常validator/取引種別/duplicate/買取検証。
2. 共有CAS accepted＋完全read-back。ここでcrashしnative証跡がなければUNKNOWN。共有fenceは残す。
3. native journal claim＋完全read-back。全入力/plan/before/共有claimのbindingを固定。
4. 既存gateの実行直前preflight後、nativeをwriter_readyへCAS。
5. 原本・binding・全before ledger・共有executingを再照合。writer_startとwriter_startedを**同じnative CAS**で保存し完全read-back。
6. 再照合後に副作用のないcounterだけを1回呼ぶ。既存writer callbackは受け取らない。ledgerはread-only port。テスト側だけが独立fake ledgerへprefix/full観測を投影する。
7. 全ledger read-back・原本/binding再照合後にnative completeをCAS。共有CASのawaiting_readback/completeはその後。成功応答喪失も保存済み結果の読取りだけ。

`writer_started`は、writerが実際に呼ばれたとの断定ではなく、**この開始マーカー以降は呼ばれた可能性がある**という不可逆な境界。マーカー保存直後に応答不明/crashならcounterが0でもUNKNOWN。NOT_EXECUTEDへ戻さない。

phase: recordなし（未claim）→claimed→writer_ready→writer_started→complete。writer_startedからhandledな部分/不明はpartial/unknown。終端complete/partial/unknownから再開・release・再dispatchする遷移はない。crashは直前の永続状態を残す。途中CAS応答不明でも自動retryしない。

合成auto入口はnative attempt adapterがなければ共有claim前に拒否する。現在実行中の**完全に同一な保護済みrequestだけ**は自分のpendingを二度目のpreflightで照合できる。他request・旧pendingは除外しない。再送はjournalのID/fingerprintで読取り照合へ分岐し、新しいgate leaseへ入らない。

## 全ledgerのread-back

元の`pdf_intake_runner.exact_readback/ledger_snapshot`を再利用し、収入表を含む全value/formula/IDと保護した既存行を追加照合する。通常支出はreceipt＋全expense＋import、買取はreceipt＋income＋importを必須にする。income金額・分類・元取込ID・根拠/hashは元の`_buyback_income`のread-only検証に一致させる。commit markerだけ、receiptだけ、incomeだけでは完了にしない。

| 分類 | 必要条件 |
|---|---|
| COMPLETE | 保存binding、共有claim/fingerprint、native complete記録、正確なwriter_start、全期待ID/value/formula、全既存行保持、完了時read-back digestが一致。合成証明だけでreal_native_complete=False |
| NOT_EXECUTED | 保護された共有/native claim、claimed/ready、writer_startなし、全before ledgerと原本版/input/authority/旧pendingが一致。このcompositionは開始マーカーなしのprobe到達を拒否する |
| PARTIAL | 対象の部分反映/欠損/値/formula/ID重複/commit marker不整合/既存行変更等。全反映後の破損も含む |
| UNKNOWN | claim/証跡欠落、開始済みでledger不在、全反映でも完了記録前、版変更・観測不足・weak ETag・binding/状態不一致・照合中の競合等 |

read-backでは3保存先のbytes/strong ETagとtrusted bindingを前後readし、一つでも変化したらUNKNOWN。別保存先の一貫した分散snapshot/耐久性を実証したわけではない。全ledger反映後でもnative completeがなければ自動補完しない。完了/未実行でもretry_allowed=False、native_authority=False、accounting_permission=False。UNKNOWN/PARTIALはfence/pendingを保持して論理隔離し、人間向けの原本・全入力・plan・before/観測ledger・pending/marker報告だけを生成する。報告はowner/session read認証・HTML escape、write/復旧ボタンなし。

## 検証の範囲

13障害点×crash/timeout/lost response/stale×通常/買取、実fake CASの反映後応答喪失・strong ETag競合（金融4遷移/native4遷移/ledger2投影）、native証跡欠落、marker欠落、ledger部分/不完全/数式/収入/commit不整合、同hash版更新、旧pending、read-back中の3保存先/原本変更を試験する。

16個の独立journal/adapter/金融gateから同じ各backendへ競合し、同ID/別IDでprobe最大1回を検査する。旧確認/PDF auto/manual/grouped/canary/明示Unit/将来入口との共通ownership/CAS競合も検証する。この新compositionのlegacy側は副作用のないtyped観測projection/spyだけで、実native authority/validator/writerへの新接続ではない。#133/#134までの実validatorを使う既存合成回帰1,808件を別途全て実行し、native条件を緩和しない。

新規テストでは既存`process_bytes/_materialize_result`、旧確認writer、SheetsDB、live clientをfail spyにし、初期化/計画生成でも呼出しを禁止する。固定Golden計画は既存コードを読んで初期化した完全合成データで、今回実writerと実行比較したものではない。金額/分類/ID/行順の改変を拒否する契約検査と既存金融回帰を行うが、実native plannerとの一致の実証は次工程の条件。

専用Ubuntu 24.04/Python 3.12 CI、正確なPR head、contents:read、write flag false、Secrets/vars/environment/資源作成・デプロイなし。確定件数・run URL・commitは新Draft本文へ記録する。

## 5B-2B-2Bへの条件（未実施）

1. **合成接続範囲の別判断**。既存materializerの直前に本契約を置き、保存済みwriter_start＋read-back後にだけ既存ScopedWriterを渡す。guarded leaseを再取得/再発行せず、projection/authority/Unit目的境界は保持。既存writer本体は変更しない案が第一候補。
2. 信頼済みnative plannerから全入力・固定timestamp・全ledger ID/plan/before snapshotを得るread-only接続。今回のGoldenを本番計画として採用しない。買取income schema/根拠・commit順序・既存行保持を実native read-backと比較する。
3. 実native request ID、永続writer-start fence、共有/nativeの照合・timeout・partial_unknown・完了history・authority消費を既存仕様と整合させる。既存native schema/authority変更が必要なら具体差分を提示して事前承認。本schemaを無断で実nativeへ流用しない。
4. writer開始マーカー後のcrash、全ledger反映後/native完了前、native完了後/共有更新前を、実nativeへ接続した合成writerで再試験。NOT_EXECUTEDを証明するには全会計入口でこの順序を強制できることを確認する。
5. live reader、実backendの耐久性/IAM/鍵/暗号化/保持/監査/費用/backup/復旧、実OAuth/iPhone、追加ユーザー認可は未検証。本番constructor/runner/workflowの切替、公開環境、main merge、デプロイ、実writeは全て別承認。

今回の判定は「合成契約を安全に管理できること」まで。5B-2B-2Bは結果確認後に接続範囲を判断し、自動着手しない。本番完了証明・復旧権限・記帳可能化を意味しない。

依存：main → #123 → #124 → #127 → #128 → #129 → #130 → #131 → #132 → #133 → #134 → 本Draft。#125/#126は従来どおりmerge済み仕様文書。既存PRのhead/base/Draft/state、本番・既存ブック・Drive・Firestore/OAuth・医療・Cloud・AI・実会計・authorityは未変更/副作用0。
