# au PAYカード checkpoint後続期間Apply

手動main専用の `.github/workflows/aupay-card-incremental-apply.yml` から、既存の `aupay_card_first_window_apply.run(..., incremental=True)` を呼ぶ。新writer、独立Preview、監査runは追加しない。第1窓workflowを再実行しない。PR #182はマージ不要。銀行・Amazon・PayPayの入口と旧カード定期workflowは変更しない。

## 開始時の確認と証拠の限界

2026-11-03の実装開始時、GitHubのmainと `KAKEIBO_VALIDATED_MAIN_SHA` はともに `9c7c428f79334c906263a7e5400d8ffbe094ec46`。既存main CI #41546430300はsuccess。

成功Apply #41546920020のjobとログを独立GETで読み、`written=33`, `amount_yen=149218`, `duplicate=6`, `held_protected=4`, `expense_rows_written=0`, `checkpoint=2026-10-27T08:55:56+09:00`, `parent_ledger=complete` を確認した。この33件は再登録しない。

接続Driveの検索ではnative JSONを取得できなかったため、実装時点のnative state・親ledgerを直接再取得済みとは主張しない。最新状態の根拠は上記成功runの独立readback済み完了結果。後続Apply自身は金融write前に既存RSA bindingからnative state、全SQLite完了証跡、checkpoint、親ledger、review、Sheetsを直接GETし、全ての条件を再検証する。追加の監査workflowやPreview実行は不要。

## 対象期間と固定する内容

- sourceは `au_pay_card_gmail` のみ。
- 開始は成功checkpointから172800秒を引いた時刻（包含）。初回は `2026-10-25T08:55:56+09:00`。
- 終了はApplyプロセス開始時に1回だけ固定した現在時刻（秒精度、排他）。取得後の時計で延長しない。
- 最大1209600秒。現在のcheckpointなら終了が **2026-11-08 08:55:56 JSTを超えた時点でNO-GO**。自動切詰め・分割・checkpoint捏造はしない。
- 既存authorityのsource・target・allowed statuses・有効期限を維持する。overlap=172800、window=1209600、messages=500、batch=100と完全一致が必要。上限を緩めない。
- 100候補は新規canonical取込と全unique money review（既存保存済み分も含む）の合計。既存canonical重複はappend対象外。500メールを超す次ページがあっても停止する。
- 件数・金額・取得メール数は33件等に固定しない。完全取得後にfresh plan、候補・分類、既存HMAC full-plan/batch参照を固定する。重複だけの場合もfull-plan HMACを再照合する。

## 共通安全経路

1. 認証前の標準ライブラリ専用guardでmain、workflow SHA、expected SHA、validated SHA、owner、attempt 1、確認文字列、既存flags、debug禁止を照合。`kakeibo-production` concurrencyを共有し、競合runをlive GETで拒否する。
2. 新workflowの全revision・全ページの履歴を照合。同じcheckpoint以降に他dispatchがあれば、認証前失敗でも消費済みとして拒否。以前のrunは、現在のcheckpointより前に作成されたattempt 1の完了successだけ許可する。再実行、過去failure/UNKNOWN、skipped、履歴不完全で停止する。native pendingとexpected checkpoint一致も独立に検証する。
3. 既存strict collectorと `CapturedGmail(window=...)` でlist/get対応、raw、Gmail ID、internalDate、RFC Message-ID、source identityと原本URLを検証する。欠落、上限超過、解析保留、未知分類で停止。1回の取得だけを使い、別Previewは行わない。
4. 現在のSheetsの既存同一性と内容を厳密に検証。前回33件を含む既存行を除外し、新規canonicalだけを既存projectionに選ぶ。Sheets全体、分類、full-plan/batch HMACをwrite直前まで再照合する。
5. 親ledger/native stateのpendingを保存しreadbackした後、MoneyWriterの `review_only=True` だけで新規reviewを保存する。既存reviewは全record・fingerprint・理由・related ID・原本URLが一致した場合のみ保持する。他record・alias・documentの上書きを `ReviewStore` が拒否する。原本URLとfingerprintを既存bookに保存し、後から再検証できる。
6. 独立readonly store/clientでreview全体を復元して照合。保留要求のpending、post/resume/linked/transfer/supplement等の非review money候補、通常canonicalの未解決保留等は金融write前に停止する。moneyの金融writeも支出生成も行わない。
7. 新規がある場合だけ既存manifest、one-use capability、lease、journal、sealed batch transport、exact readbackを実行する。API境界はそのprojection件数・金額に一致する `取込データ!A:L` のRAW append 1回だけ。許可行は既存sealed transportが確定する。
8. acknowledged応答、confirmed journal、sealed capability、lease解放、全金融表の値と数式の正確なappend差分、reviewと他document不変を検証。UNKNOWN応答後にexact readbackが得られても成功扱いせず停止する。
9. 新規0件なら金融API、manifest、capability、lease、journalを新たに起動しない。完全取得、重複照合、review保存・独立readback、全金融表不変の証明は省略しない。
10. 全条件成功後のみ終了時刻へcheckpointを進めnative stateをcommit。独立GETでnative完了証跡とcheckpointを確認してから親ledgerをcompleteし、さらに独立GETでnative・親ledger・全金融表・reviewと他source保持を確認する。

## 本番実行の残手順

このPR作業中にはmerge、validated変更、Apply dispatch、本番金融writeを行わない。

1. Draftのレビュー後にPRをマージ。
2. 新しいmain SHAの既存CI全3job成功を確認。
3. そのSHAと既存 `KAKEIBO_VALIDATED_MAIN_SHA` を別途承認の下で整合させる。Secrets・authority・権限は変更不要。実際のauthority期限は実行時に検証する。
4. 本番Applyについて別途承認を得る。承認対象はcheckpoint−172800秒から実行開始時刻までを完全取得して確定するfresh個別plan、既存HMAC参照、最大100候補・500メール・14日、review保存、canonical import appendのみ、成功時のcheckpoint/native/親ledger commit。新規件数・金額は事前未確定であり、33件149218円を再承認する操作ではない。
5. 他runがない時、mainの `aupay-card-incremental-apply.yml` を1回だけdispatchする。`expected_commit` は手順2のSHA、`source=au_pay_card_gmail`、`expected_checkpoint=2026-10-27T08:55:56+09:00`、`confirm=APPLY_CHECKPOINT_FRESH_PLAN_ONCE`。開始・終了入力はない。
6. 公開結果の `success=true`, `written`, `amount_yen`, `duplicate`, `held_protected`, `expense_rows_written=0`, `checkpoint`, `parent_ledger=complete` を確認。個別ID、原本、HMAC、API例外を公開ログ・artifact・cacheへ出さない。

## NO-GOと失敗時

期限・上限超過、main/validated/authority/key/flags不一致、checkpoint不一致、pending state/ledger/review要求、不完全取得、source不一致、既存行内容相違、途中変更、unsupported候補、money post/resume、保存・write・readback・最終GETの失敗/UNKNOWNはNO-GO。自動retry・rerun・rollback・pending解除を行わない。公開失敗コードは `incremental_apply_stopped_no_retry` のみ。

金融行追加後のcheckpoint失敗、checkpoint後の親ledger失敗、最後のGETだけの失敗も起こり得る。成功扱いせず、既存保護状態を個別照合して最小修正と別承認を要する。14日超過は承認済みの固定期間経路での別計画が必要となり、この入口の上限を緩めたり窓を切り詰めたりしない。

SheetsとDriveには跨る原子的transactionがない。ownerの直接編集や実行中のVariables変更を禁止する運用前提は第1窓と同じ。直前と事後の照合で検出するが、その間の編集をロックできるとは主張しない。

## 後続の定期運用

毎回同じ処理へ、その時点の成功checkpointを渡せる構造で、固定件数・固定金額・固定終了時刻へのコード変更は不要。新規0件の日も成功時だけcheckpointが進む。今回のworkflowには `on.schedule` を追加せず、旧カードworkflowも再有効化しない。

後続Apply成功後の日次化は別の最小設定変更として、同じ入口への日次trigger、検証済みnativeからのexpected checkpoint供給、および標準ライブラリguardのschedule起動条件を接続する。collector・review・writer・readback・checkpoint処理の追加や複製は不要。手動のowner確認を暗黙にscheduleへ転用せず、日次本番運用の承認後に行う。

## 銀行定期の読取専用確認

2026-11-03 06:47 JST（11-02 21:47 UTC）予定の銀行runについて、`kakeibo-production.yml` のschedule履歴を11-02 20:00 UTC以降で検索した結果は0件で、該当runは未生成だった。workflowはactive、cronは `47 21 * * *`、銀行定期flagはtrue。GitHubがschedule eventを生成しなかった理由はAPI履歴だけでは確定できない。

直近の #41512176876（11-03 04:40 JST）は別時刻のscheduleでproduction job全体がskipped。これを銀行06:47 runの結果として扱わない。銀行のdispatch、再実行、workflow/変数/authority変更は行っていない。

## 合成検証

ネットワーク禁止のfake Gmail/Sheets/Driveと実SQLite manifest/capability/journal/leaseを使用。前回33件を登録した状態から再取得し、既存39件を除外して新規2件3423円だけ登録する。既存4review保持、新規review保存と原本/fingerprint復元、0件、14日境界、100候補境界、500メール全ページと501拒否、raw/ID/取得欠落、既存Sheets変化、同額別ID、money非review拒否、review UNKNOWN、金融UNKNOWN/欠落/改変/禁止支出差分、checkpoint後置、二重dispatch/rerun拒否、全履歴ページ、依存導入前guard、入口全体の独立GETを検証する。既存CI全3jobはそのまま使用する。
