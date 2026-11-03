# au PAYカード 日次Apply（既定では無効）

既存 `.github/workflows/aupay-card-incremental-apply.yml` に毎日 **09:23 JST**（UTC cron `23 0 * * *`）の入口を追加する。sourceは `au_pay_card_gmail` のみ。新workflow・Preview・監査・writerは追加せず、既存Applyの金融処理をそのまま再利用する。

## 実装時の基準

- main/validated：`2c21448d9aee9dcacb23700f3ab0e29a91c5affe`。
- 手動Apply成功run：#41585713898、Gmail4通、新規0件、既存重複10件、append0、支出write0。
- 成功checkpoint：`2026-11-03T14:45:43+09:00`。native・親ledgerはready/pending0。
- `KAKEIBO_CARD_SCHEDULE_ENABLED` は未設定。今回の開発ではVariables・Secrets・authorityを変更せず、schedule/Applyを本番実行しない。

## 起動条件

日次jobは `KAKEIBO_CARD_SCHEDULE_ENABLED == 'true'` の時だけ開始する。未設定・空・falseではjob全体がskippedになり、Google credentialsやApply本体には到達しない。既存の `KAKEIBO_SCHEDULE_ENABLED=false`、銀行専用flag/cron、legacy設定は変更しない。`kakeibo-production` concurrencyを銀行等と共有する。

手動dispatchはこれまでどおりowner/triggering owner、確認文字列、expected commit、expected checkpoint、attempt1を要求する。カードscheduleフラグがfalseでも手動の承認条件は変わらない。今回の変更で手動入力のcheckpointを固定Variableへ移すことはしない。

scheduleは手動確認文字列・owner dispatchの代わりに、schedule event、許可cron、カード専用flag、repository/main、workflow SHA＝event SHA＝validated SHA、attempt1、本番flags、競合runなしを要求する。cronはjobのGitHub contextと `GITHUB_EVENT_PATH` のpayloadの両方で一致を検証する。

## 認証前と認証後の分離

1. 認証前の標準ライブラリguardはGitHubからmain・現在run・競合status・全ページの同workflow履歴をGETする。この段階ではnative checkpointを取得できないため、時刻に対する履歴照合だけを後段へ残す。scheduleでは手動 `EXPECTED_CHECKPOINT` の値を参照しない。
2. credentials導入後、既存RSA保護bindingからnativeと親ledgerをreadonly GETし、binding/schemaを検証する。nativeの成功checkpointをmemory SQLite SELECTで取得し、既存のcheckpoint検査で全capability sealed・journal完了・lease0・ready/attempt空を確認する。
3. 取得したcheckpointで同じguardを再実行する。現在runがcheckpoint後に作成され、過去successがcheckpointより古いことを確認する。同checkpointの二重実行はここで停止し、Gmail取得・pending保存・writerへ到達しない。
4. 親ledger全source ready/pending0、復旧済みrun #41560668947の固定証拠、現在native結果＝親ledgerカードcountsを検証する。元のnative結果・manifest・capability・journalのhash拘束は維持する。
5. この検証が成立して初めて既存money reader/writerの組立てへ進む。状態の再GET一致、authorityの実時刻有効期限・範囲・audit key不変を確認してからGmailを取得する。

checkpointはこの実行中のローカル変数だけに保持する。`GITHUB_ENV`、固定環境値、GitHub Variableに新たに保存しない。状態が途中変更された場合は停止する。

## 取得と金融処理

開始はnative成功checkpoint−172800秒、終了はApplyプロセス開始時の実時刻を秒精度で固定する。最大1209600秒を超えた場合は切詰めず停止する。500メール・100候補、既存authority有効期限と対象・許可statusを維持する。

strict collector、raw/Gmail/source検証、fresh canonical plan、HMAC full-plan/batch再照合、重複排除、review-only保存と独立readback、既存manifest/capability/journal/lease、sealed batch writer、exact readbackを変更しない。新規canonicalだけを許可された取込データへappendし、支出明細writeとmoney post/resumeは引き続き禁止する。

新規0件でも完全取得・重複照合・review保護・金融表不変の検証を省略せず、append0で成功checkpointを進める。全検証成功後のnative commit → 独立GET → 親ledger complete → 最終独立GETも既存処理のまま。

## skippedと失敗の区別

通常successはattempt1・完了・checkpointより古いものだけを許可する。

skippedは一覧のconclusionだけでは許可しない。独立run GETで同じID/SHA、main、同workflow path、schedule/manual event、attempt1、completed/skippedを検証する。さらにjobs GETで完全な1jobのinventoryを取得し、`apply` job自体がcompleted/skippedかつsteps空配列であることを必須にする。フラグ無効やjob条件不一致で本体が一度も起動しなかったケースだけを除外する。stepが開始した形跡、job不一致、履歴欠落、再実行、複数job等は拒否する。

本体を開始したfailure、UNKNOWN、取消、未証明の停止は無条件に許可しない。既存の固定run #41560668947の例外だけは、PR #186で確定・整合した原本証拠へ引き続き拘束する。失敗時は自動retry・rerun・rollback・pending解除をせず、原本確認と別承認を要する。認証前guardでfailureになった場合も、全job skippedと証明できる履歴ではないため自動例外にしない。

## 本番有効化の最小手順（今回実施しない）

1. Draft PRをレビューし、別途承認の下でマージする。
2. 新mainの既存CI全3job成功を確認し、承認された正確なSHAへ `KAKEIBO_VALIDATED_MAIN_SHA` を整合する。
3. 定期金融処理の有効化について別途承認を得る。最新main/validated、競合run、native/親ledger ready、checkpoint/最新counts、authority期限・14日窓の成立を確認し、**`KAKEIBO_CARD_SCHEDULE_ENABLED=true` のみ**を設定する。checkpoint VariableやSecrets/authority変更は不要。authorityが失効していれば緩和せずNO-GOとする。
4. 次の09:23 JST予定runを確認する。GitHubの起動遅延があっても終了時刻は実際のApply開始時刻とする。手動でschedule runを追加起動したり、失敗runをrerunしたりしない。
5. 公開結果と独立GETで件数・金額・重複・review・append数・支出write0・checkpoint・native/親ledger完了を確認する。個別IDや原本は公開しない。

flagをtrueにするまで日次金融処理は開始しない。無効期間の正当なskipped履歴は上記の独立GETで検証され、後日の有効化を妨げない。止める場合は別途承認されたflag falseだけで定期入口を停止でき、手動入口は保持される。

GitHub Variablesはjob開始時snapshotであり、実行中のowner編集を原子的に監視する機構ではない。実行中はbinding・flags・金融表の直接編集を避ける運用前提、直前/事後GETによる変更検知、外部Sheets/Drive間の非原子的な性質は既存Applyと同じ。UNKNOWN時に成功扱い・自動再送へ変更しない。

## 影響と検証

変更対象はカードworkflow、入口bootstrap、guard、native checkpoint読取helper、合成テストと文書のみ。銀行・Amazon・PayPayのworkflow/runner、全体schedule、legacy設定、金融writerは変更しない。

ネットワーク禁止の合成APIと既存batch writer・実SQLiteを使い、日次正常、新規あり→翌日0件の連続成功、checkpoint自動取得、重複、review保持、手動条件維持、false/missing flag、main/validated/cron/競合不一致、pending/最新counts不一致、復旧証拠不一致、authority失効、Gmail欠落、write UNKNOWN、skippedとfailureの区別を確認する。認証前guardはsite-packagesなしでもimportできる。既存の合成CI全3jobを使用する。
