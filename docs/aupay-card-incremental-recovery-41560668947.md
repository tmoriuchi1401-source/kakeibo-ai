# カードrun #41560668947の親ledger整合と次窓guard

## 確定した本番状態

実行SHA `6284b63206813e3f731ea77bd6228b235336dd84` のrun #41560668947は、金融write前の失敗ではなかった。既存接続のreadonly scope・GET専用ラッパー・memory SQLite SELECTで原本と履歴を確認し、分類Cと確定した。新規診断dispatch、失敗runのrerun、追加Applyは行っていない。

| 項目 | 結果 |
| --- | --- |
| 完全取得 | Gmail 16通、原本再照合でも16通 |
| 新規取込 | 22件・63,064円、原本から再構成した全12列と完全一致 |
| 既存重複除外 | 3件 |
| 新規review | 4件・14,360円、原本・fingerprint・record一致 |
| 前回実績 | 33件・149,218円、review4件・4,334円を保持 |
| 重複登録 | 今回と前回のID重複0、全canonicalカードID重複0 |
| append | 1回、acknowledged、post_read confirmed、journal final confirmed |
| native | generation 118、ready、pending 0、capability sealed、lease 0 |
| checkpoint | `2026-11-03T10:35:38+09:00` へ更新済み |
| money保護 | 既存17レコード・alias等を完全保持、新規review4件だけ追加 |
| MoneyReviews | 要求0、変更なし |

旧nativeの全既存manifest/capability/journal/run行も保持されていた。親ledgerのカード項目だけがpendingで、countsは前回33件のままだった。カード実行前後で他sourceの変更はなく、その後の親ledger変更は正常終了した銀行run #41570103363だけだった。この銀行runの金融write要求は0件。

失敗範囲は、nativeのready保存が反映された後から親ledger complete保存が反映されるまで。固定エラーのため元runの直接例外は不明であり、通信・quota・金融write前停止と断定しない。

## 個別承認された親ledger変更

ユーザーは、親ledgerの `aupay_card` 項目だけを1回完了し、金融・review・native・checkpoint・他sourceを変更しない操作を個別承認した。直前GETでmain＝validated＝上記SHA、競合run 0、原本とprivate証拠の一致、pendingの同一性を確認した。

既存 `ProductionLedger.complete('aupay_card', native_summary, 0)` を使用。Drive scopeだけの接続から、固定親ledgerへのmedia updateを1回だけ許可した。金融write clientやGmail clientは構築せず、保存再試行とrollbackは行わなかった。

- 親ledger generation：1328 → **1329**。
- カードsource：**ready、attempt空、error空、pending 0**。
- counts：`found=16`, `fetched=16`, `new_eligible=22`, `written=22`, `duplicate=3`, `money_review=4`, `failure=0`, `write_requests=1`。
- `last_success`：**2026-11-03T04:14:00.641320+00:00**（JST 13:14:00.641320）。これは復旧操作時刻であり、元Applyの取得終了時刻ではない。

保存応答はacknowledgedだったが、直後の厳密readbackは `state_changed_during_read` で停止した。保存要求を再送せず、別のreadonly診断で独立GETした結果、保存前に固定したproposed payloadのSHA-256と全byteが一致した。native・全金融表の値/数式・money・MoneyReviews・銀行を含む他sourceも保存前と一致した。最初のreadback停止を隠して成功へ置換したのではなく、後続の独立証拠によって保存結果を確定した。

この変更は失敗runのrerunや金融appendではない。checkpointは既に確定した値から変更していない。Secrets・authority・Variables・scheduleの変更も行っていない。

## guardの最小修正

第1窓guardの認証前失敗例外と混同せず、**金融完了後に独立整合した固定run**として扱う。

1. 標準ライブラリ専用guardでrun #41560668947・job #125670234114・旧実行SHA・attempt 1・workflow path・作成時刻・step 5/6成功とstep 7失敗を独立GETする。それ以外のfailure、rerun、履歴欠落、同checkpointのdispatchは拒否する。
2. 新checkpoint未満の入力は拒否し、旧窓の再実行を許可しない。新checkpoint以降ではこの固定runの履歴存在も必須とする。
3. この例外を使う場合、prepare・Gmail取得・pending保存・writer能力発行より前に、保護bindingから検証済みnativeと親ledgerをGETして照合する。
4. 元のnative complete結果、manifest payload、sealed capability、confirmed journal全5イベントを公開不可の個別IDの代わりに固定SHA-256で照合する。private証拠全体の参照は `1fcbcbfa043abb9bf7c2a14ea30851bd46dd1f40604baf64b199a763de60226a`。
5. 最初の次窓ではnative全体と、今回整合したカードsource全体（countsと復旧last_successを含む）も完全一致させる。銀行を含む他sourceの正常更新はカードsourceの不一致と混同しない。
6. さらに後の窓でも元の確定証拠を保持していることと、現在checkpointのnative結果＝現在の親ledgerカードcountsを毎回検証する。恒久的な無条件failure許可ではない。

既存のmain/validated/owner/attempt/競合run/flags/authority、strict取得、fresh plan、review-only、HMAC、manifest/capability/journal/lease、exact readback、UNKNOWN停止、checkpoint後置は変更しない。writer追加、金融write、schedule有効化は含まない。

## 次の本番実行

このPRはレビュー用。マージ・validated更新・次の本番Applyは別途承認が必要であり、この修正作業中には行わない。

次窓のexpected checkpointは `2026-11-03T10:35:38+09:00`。開始は `2026-11-01T10:35:38+09:00`、終了は次回Apply開始時に固定する実時刻。14日上限の最終終了は `2026-11-15T10:35:38+09:00`。今回の22件を含む既存行はfresh再照合で重複除外する。

手順は、レビューと明示承認によるマージ → 新main CI全3job成功 → 承認されたvalidated整合 → **次窓の本番Apply別承認** → 1回dispatch。scheduleはまだ有効化しない。元runのrerun、旧checkpointへの巻戻し、親ledgerの再整合は不要。

## 合成検証

原本・個別ID・秘密値をfixtureに含めない。固定run/job/stepの改変、別failure、rerun、履歴削除、旧checkpoint、native/親ledger pending、counts/復旧時刻/manifest/capability/journalの不一致を拒否する。銀行更新を保持するケース、次の日の成功結果を使うケース、元の証拠喪失、prepareより前の拒否、依存導入前のstdlib importも検証する。既存CI全3jobを使い、独立監査workflowは追加しない。
