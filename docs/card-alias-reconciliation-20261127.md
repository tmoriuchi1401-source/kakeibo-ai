# カード再送alias・新checkpoint：承認前差分

2026-11-27。A01～A15、STOP・UNKNOWN・恒久フェンスを維持する。
Gmail資格復旧、原本照合、隔離E2E、PR209適用は完了済みとして再利用した。
本番修復、checkpoint更新、main merge、validated昇格、Script配備、設定・schedule変更、
実AI、金融write、原本操作は行っていない。本差分は実行承認を含まない。

## aliasの最小修正

従来のcanonical plannerは取得窓内で再送をまとめるため、別窓でnoncanonical sourceだけを取得すると新規候補になっていた。
既存取引ID/canonical選択を変更せず、既存`recurring_runs.summary_json`の`card_aliases`へ対応を追記する。
既存5 SQLiteとGoogle Drive envelopeのsnapshot/restoreを使い、新規DB・writer・サービスを追加しない。
旧summaryのfield欠落は許容し、更新・削除・破壊的migrationはしない。

保存内容はowner ID、全12列owner行のdigest、business fingerprint、各source ID/hash。
メール本文・店舗・金額や資格はregistryに複製せず、CLI/step summaryにもregistryを出さない。

- 既知sourceはsource hash/fingerprintと、ownerの独立した全12列readbackが一致した場合だけ重複とする。
- owner欠落・複数物理行・別ownerへの割当て・hash/status/target等の変更は停止する。
- 未知sourceが既知fingerprint又は旧取込行の同内容に一致する場合は保留し、append/checkpoint前進を止める。
- 既存の同一窓canonical規則は保持する。旧owner原本も同時に取得できる場合は、そのplannerの再送対応を初めて保存できる。
- 日付/金額だけで除外しない。同日同額でも店舗又は明細番号/memoの違う正当取引は新規のまま。
  証拠上区別不能な別購入は保留して残す。
- acknowledgedとexact readback後だけalias証拠を追記する。部分反映/UNKNOWNならcheckpointを進めず、再送を拒否する。

保存済み実反例6メール/24source/12canonicalを再取得せずローカルで検証した。
旧処理で新規になる非owner aliasは12。修正後は全24sourceを単独取得しても既存12ownerへ対応する。
仮想2行修復後の全24sourceは新規0/重複12、未知late aliasはHOLD。
private証拠は`.private/alias-counterexample-proof-20261127.json`。Google修復結果ではない。
CI用には同じ12owner/24source/10既存/2不足構造の合成回帰を固定した。

## 2行修復と集計への影響

前回の12取引対応表・2候補の全12列テンプレート・部分行/別ID/支出/振替/履歴照合を再利用する。
既存10取込は通常1・振替9、全件失敗run開始前。今回のrunが10行を書いたとは認定しない。
既存10件への直接の支出/review/money参照は0であり、全支出記帳完了という意味でもない。

| 対象 | 取込データ追加 | 支出明細追加 | 既存行/カテゴリ変更 |
|---|---:|---:|---:|
| 通常 `auto_expense` | 4,386円・1行 | 0 | 0 |
| 振替 `transfer_aupay_charge` | 120円・1行 | 0 | 0 |
| 合計 | 4,506円・2行 | 0 | 0 |

両候補のID/hashは取込・支出・レシートに未検出、前後3日の同額も0。
120円近傍の同店舗9行は既存9振替原本へ全対応し、120円の分割行ではない。
両候補への通常review/money/money-review参照0、原本のmoney input/noticeも0。
旧attemptのwire未送信を証明したとは主張せず、UNKNOWNを保持する。

支出追加0の理由は、修復を既存の**取込専用writer**の2行に限定するため。
4,386円を既存支出が既にカバーしているからではない。`auto_expense`はこの入口の分類値であり、支出作成証拠ではない。
120円の振替を支出へ加えない。将来の通常記帳を混ぜず、今回を全会計処理完了とも扱わない。

取込金額合計は+4,506円（通常+4,386円/振替+120円）。支出明細基準の支出額・カテゴリ別支出と収入明細基準の収入額は差分0円。
取込表全体を足す集計は+4,506円になり得るため、支出集計と混同しない。
支出/収入/レシート/money/review/既存履歴/カテゴリ/数式へのwriteは0。
取込時刻B列だけは将来の実行時JSTをcapability発行時に固定し、旧run時刻を偽造しない。
新runも応答不明ならUNKNOWNとして停止し、補送・rollback・再appendしない。

## 新checkpointの最小方法

C0=`2026-11-03T14:45:43+09:00`→C1=`2026-11-04T21:00:33+09:00`の限定移行案。
旧run41825241169/attempt1のrerun・success化・単純`release_after_reconciliation`は使わない。

1. 承認後、既存`kakeibo-production`排他下でfresh SHA/authority/digest/ACL/競合を確認。不一致なら計画を失効させる。
2. 既存`recurring_runs`へ`UNKNOWN_NO_REPLAY` archiveを追記する。旧exact native、親card要素、旧checkpoint/attempt、
   GitHub失敗identity、evidence digest、再送禁止を保持。新修復runをnative gen122/pending・親gen1415/card pendingで予約し独立readback。
   元はnative gen121/pending・親gen1414。旧SQLite行/manifest/capability/journal/STOP/恒久フェンスは変更・削除しない。
3. 新runの既存manifest/capability/journal/leaseとbatch writerで2行だけ1回append。
   全金融表の厳密な2行差分、acknowledged、exact readback、12owner/24alias、money/review不変を確認。
4. 新runのcomplete summaryへarchiveとevidence、alias/manifest/capability/journalのdigest証拠を結合し、C1を保存。
   native gen123/ready・親gen1416/card readyは新runの完了だけを示す。旧UNKNOWNを残し、独立最終GETを行う。
   親の他sourceを過去snapshotで上書きしない。

archiveは`native_before/native_sha256/parent_card_before/parent_card_sha256`、`checkpoint_before/after`、
`github_failure/repair_run_id/evidence_sha256/replay_allowed=false`を持つ。
新summaryの`checkpoint_reconciliation`は`archive_id/evidence_sha256/aliases_sha256/manifest_sha256/capability_sha256/journal_sha256`を結合する。
digestは検証関数のcanonical JSON方式を使い、前提監査の異なる直列化方式と混同しない。

読取専用`preserved_unknown_proofs`は旧exact archive/全旧append-only行の保持、新runのcomplete、sealed capability、
acknowledged/final confirmed journal、manifest、alias、checkpoint、2日overlap/14日上限を検証する。
現役native/親pending、未sealed、未確定journalは引き続き拒否する。失敗runのGitHub inventoryと独立GETも保存identityへ対応させ、別失敗/attempt2へ転用しない。
資格前guardの照合留保は副作用許可ではない。runtimeはGoogle保護stateと全source readyを**Gmail/prepare/begin/writer前**に検査し、副作用直前も反復する。
証拠なしの通常guardは失敗履歴を拒否する。今回の現役pendingを通すpath、汎用pending解除writer、修復実行entryは追加していない。
移行は承認後の固定保守操作として既存state/writerで行う計画で、未実行。

## 固定試験とcoverageの限界

差分回帰319件合格。同一窓、窓跨ぎalias単独、checkpoint後再取得、Google payloadだけからのfresh restore、
同日同額の別取引、未知alias保留、owner/hash不一致、部分反映/UNKNOWN再送拒否を含む。
新checkpointの検証は仮想postimageに対する旧UNKNOWN保持とguard受理/拒否の試験であり、production移行の実証ではない。
テスト用SQLite変更は使い捨てメモリfixtureだけ。本番migrationへ流用しない。

| checkpoint | 読取窓（JST、end排他） | 状態 |
|---|---|---|
| C1 11/04 21:00:33 | 11/01 14:45:43～11/04 21:00:33 | 12取引の既存証跡を再利用、修復未実行 |
| C2 11/16 21:00:33 | 11/02 21:00:33～11/16 21:00:33 | 未収集・未処理 |
| C3 11/26 21:43:48 | 11/14 21:00:33～11/26 21:43:48 | 未収集・未処理 |

既存2日overlapでgap0、各窓14日以内。C1から現在への日次Applyは上限超過で拒否する。
後続2窓は別の固定期間計画・fresh照合・限定write承認が必要。未処理の実backlogが0とは証明していない。
今回の証明は固定入力と再取得/alias変種に対する重複・欠落防止。scheduleは有効化しない。

## receiptの独立準備

既存4段階案を保持：1.無効設定接続、2.Script固定version配備とreadonly RPC、3.trigger guard後の限定AI/plan、4.別承認の限定native apply。
全段階未実行。1/2の準備はカード修復と独立、3/4で全source/native guardを迂回しない。

Code退避はDesktop`Code.txt`とworkspace保護コピーで7179 bytes、
SHA256 `d82875294746b46beb0c8eed21dd236dfe2cf0a874d0a141498db14b5610fd51`と再確認した。
保存済みHEADとの改行正規化後一致、未保存マークはCode.gsのみという前回証拠を再利用する。
Google保存・破棄・配備は0。現在のeditor本文を再取得したとの主張はしない。
Properties退避420 bytes/非秘密binding一致/backend=false/registration=true/旧解析10・履歴94・book3case/3requestを保持する。

本番Script IDとコンテナbookリンク一致は今回も読取確認できた。
triggerへのUI入力は`coordinate input geometry is unavailable`で停止し、source IDの独立確認は未了。
コンテナ一致や本人の以前の報告をsource IDの証明へ代用しない。
既存`receiptHostedStart`のenabled更新前trigger1件/handler/ON_EDIT/source ID検査を必須とする。

本人が現UIで確認する場合は、本番Script「トリガー」→既存`receiptReviewEdited`行の詳細を読むだけ。
対象ファイルのリンクが露出する場合に限り、そのURLの`/d/`から次の`/`までのIDを固定確認bookと比較する。
リンクが露出しなければ未確認のまま止める。保存、trigger編集/作り直し、Start、新コード保存・実行は今は依頼しない。
第3段階を承認する場合も既存有効化前guardを必須とし、不一致なら設定変更0で停止する。
現在は修復write・checkpoint更新・receipt接続の実行承認前で停止する。
