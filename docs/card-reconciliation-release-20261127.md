# カード限定修復：リリース候補と実行承認境界

2026-11-27。基準PR210 `129599496057fe06fead7c9b0064104f3415c129`。
main `70a6181fb7658a8a8d1885fa5d7ab9eb8d74d9b1`からカード差分のみを分離した候補である。
PR208/PR210を丸ごとmergeせず、receipt本番接続のコード・Script・設定・workflow差分は含めない。
A01～A15、既存STOP・UNKNOWN・恒久フェンスを維持する。

## 既存保守操作だけでは成立しない理由

`DurableState.restore`と`ProductionLedger.begin`は現役pendingを拒否する。
既存`aupay_card_repair`は既存行4セルの修正用で、今回の2行appendとは異なる。
単純release→通常Apply、SQLで成功化、手動直接append、旧attemptの再実行は採用しない。

必要な最小接続として、既存writerを呼ぶ固定保守orchestratorと手動hosted entryを追加した。
金融writer・DB schema/file・サーバー・Google資源・新資格は追加しない。
既存の5 SQLite、manifest、ProtectedBatchApprovalProvider、capability、journal、lease、
SealedSheetsBatchTransport、DriveStateTransportを使う。通常restore/beginのpending拒否は緩和しない。

## 限定差分

取込データへ通常4,386円の1行と振替120円の1行、計4,506円を1回appendする案。
支出追加0・既存行変更0・カテゴリ変更0・振替の支出計上0。
旧10取込行・全金融値/数式・要確認/カテゴリ表・money/review等5文書を独立readerで照合する。
Gmail復旧、原本12取引の照合、alias319件回帰、隔離E2E、PR209適用は再実行していない。
実行直前の原本検査は対象2取引に対応する固定2メールのGET/hash検査だけ。listingや原本窓の再照合はしない。

private固定record digest:
`f1350433e560c216b824f84fe6686f960423fbd75a76d899bad0dc7dc5854d6a`。
原本/取引ID/金額等の実payload、ACL、旧state、money/reviewはGitに含めない。
既存SAのRSA/AES/PSS capsule方式でrecordと本人承認・実行期限・run ID・code treeを結合する。
現在はowner_approved=false、署名済capsuleなし、capability発行0、dispatch0。
公開のGitHub run情報やUUIDを承認の代用にしない。

既存audit HMAC鍵は持ち出し/交換せず、hosted runnerの既存Secretを使う。
保護済み旧31 manifestのkey ID/target refとの連続性を検査し、本人が署名した固定候補から
正規batch approvalのHMAC refsを導出する。以後は既存approval/capability経路で検証する。

## 永続境界と障害時

| 境界 | 永続状態 | 障害時の取扱い |
|---|---|---|
| archive/新run予約前 | 旧pendingを保持 | 新workflowのdispatch履歴が消費され、別dispatch/rerunも拒否 |
| native予約 | 旧exact bytesのarchive＋新run予約＋manifest/capabilityをpendingで保存 | 独立GET不一致なら金融前で停止 |
| 親予約 | 親cardだけ新run pending、他source完全保持 | 不明なら両方を読取確認するだけ。再appendしない |
| append直前 | 両予約の独立GET、fresh全表・原本・ACL・SHA・旧attempt検査 | 不一致ならwriterへ到達しない |
| append/読戻し | 既存writerの1回append、acknowledged/exact readback | 応答不明・部分反映・不一致は新runもUNKNOWN_NO_REPLAY |
| 結果保存 | sealed capability/journalをGoogleのpendingへ保存 | ここで止まってもC0を保持し、補送しない |
| C1確定 | 新run complete/alias/旧archive/全digestを検証してからnative ready | write/readback不明なら停止。削除rollbackも再送もしない |
| 親完了/最終GET | 新runの完了だけを親へ反映し、全表と旧証拠を独立再検証 | 不明なら完了と報告せず、次のApplyを起動しない |

旧native/親のexact bytesとdigestをarchive schema2で残す。旧attemptはfailure/UNKNOWNのまま。
現役native gen121 → 予約122/pending → 結果123/pending → C1確定124/ready。
親gen1414 → 予約1415/card pending → 新run完了1416/card ready。
前段案よりnativeが1世代増えるのは、金融結果をcheckpointより前に永続化するためである。
C1は`2026-11-04T21:00:33+09:00`だけ。旧checkpointの偽造や現在時刻への飛越はしない。
Drive read/write/readをCASや分散lockとは扱わず、共通`kakeibo-production`排他が必須。

## リリースと本人承認

通常CIの全3jobと新しい境界差分試験の結果は、この候補PRの固定headに結び付けて確認する。
既存319件の専用再実行はせず、必要なリリースSHAの通常CIで全体整合を確認する。
新しいローカル境界/entry試験は40件合格。実環境の修復成功証拠ではない。

1. main/baseが固定SHAのまま、カード限定差分・候補head/tree・全3job・40件の結果を本人に提示する。
2. **本番コード適用はその固定候補への本人承認後だけ。** PR208/210のreceipt機能を一括mergeしない。
3. 承認後にカード限定候補だけをmainへ適用。実main SHA/treeを独立取得し、承認tree完全一致を確認する。
   不一致なら昇格しない。新mainの通常CI全3job成功後だけ、validated SHA一変数を実main SHAへ昇格する。
   他の全Variable/Secret/authority/scheduleを変更せず、昇格前後の全件一致を確認する。
4. 銀行コード・workflow・authorityは変更しない。銀行schedule=trueを保持し、銀行/Amazon native readyもGETで確認する。
   main≠validatedの間は既存SHA guardで実行を止め、未検証mainを稼働させない。
5. 本人承認済みrecord/code treeに対する短期capsuleを準備し、main/validated/ACL/state/台帳がなお完全一致する場合だけ手動dispatchする。
   card/core/receipt/PayPay scheduleは停止状態を保つ。新entry自体にscheduleはなく、2回目のdispatch/rerunは拒否する。
6. 原本/台帳/ACL/peer stateの変更、別writer、scope不足、期限切れ、鍵不一致があれば金融write前で止める。
   旧snapshotへ巻き戻したり、勝手に新しい前提へ追従したりしない。

## 後続2窓のreadonly plan

既存2日overlap・最大14日を保持する。
C2: 11/02 21:00:33～11/16 21:00:33、C3: 11/14 21:00:33～11/26 21:43:48（JST、end排他）。
現役C1はまだ未確定なので、準備時のplanはdetached仮想C1後の参考であり実行authorityではない。
実C1/alias証拠へ結び直し、C2完了後のfresh照合を経てからC3を承認する。期間を飛ばさない。
窓間に共通する未取込候補を二重追加しない。money review等の保留を隠してcheckpointを進めない。
現在の通常incremental Applyはend=現在時刻であり、C1からでは14日超過で停止する。
後続窓のApplyには、既存explicit window検証を固定cutoffへつなぐ差分の別レビューも必要。
この候補の1回限りrepair entryをcatch-upへ流用しない。後続Apply・実AI・原本操作・schedule変更は禁止。

## 4,386円の未支出計上を引き継ぐ

今回の`auto_expense`行は取込だけでJ列targetが空、支出明細は0行である。
既存AutoExpensePipelineはunclassified系だけを対象にし、auto_expenseを再計上しない。
ReviewPipelineも通常のauto_expenseを処理済みとして拒否する。単純な再実行では解消しない。

最終会計整合の後続案は既存AutoExpensePipeline/expense_idを再利用する限定接続。
原本/取込ID/hash・実在/未計上・stable支出ID不存在・既存レシート/振替/支出との非重複・カテゴリを再検証し、
この1取引だけ既存支出生成へ渡す。全履歴のstatus付替えや手動直接appendはしない。
将来の別承認対象として、支出1行4,386円と元取込J列のstable ID紐付け、exact readbackを固定する。
owner全12列digestも変わるため、正当な支出生成の完了証拠に結び付けたalias証拠更新を同時に設計する。
現在のalias registryを消す/初期化する/旧digestを黙って上書きする方法は採らない。

証明条件は、4386円のactive支出がstable IDで正確に1行、元取込との金額/日付/source一致、
振替120円の支出追加0、既存支出/カテゴリ/手入力不変、支出集計+4386円・振替除外、
再取得/再実行で支出増分0、正当なowner行変更後もalias対応が保持されること。
今回の修復を全会計完了と呼ばず、この後続支出処理は実行しない。

## receipt

既存4段階案とCode退避を再利用する。receipt追加開発・本番配備・隔離E2Eはしない。
trigger source IDは本人による既存trigger詳細の読取確認待ち。コンテナbook一致で代用しない。
既存triggerの対象リンクが表示された場合だけ確認book IDへ照合し、保存/編集/作り直し/Startは依頼しない。
カード限定リリースにreceipt配備やenabled変更を含めない。
