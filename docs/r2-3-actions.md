# R2-3 Actions 実行メタデータ

定義は main `d62e1e254692e676f4e4a72dc593524a80fbea08`、API取得時刻は [GET記録](r2-3-evidence/requests-start.json)。
50登録workflowの状態はAPI観測。mainに定義がないbranch CI/削除済みworkflowも登録一覧に残る。active は job 実行・金融write成功の意味ではない。
実行履歴は直近100件＋運用22workflow各直近3件、各最新runの最新attemptの全job（100件未満）を取得。全履歴の全job監査ではない。ログ・artifact・runの入力本文は未取得。
actor/triggering_actorはrepository-owner等にマスク。Google実行principalはjobメタデータにはなく未確認。

## 登録状態とmainの定義

| workflow | API状態 | cron UTC（コード） | dispatch定義 | environment定義 |
|---|---|---|---|---|
| [monthly-maintenance.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/monthly-maintenance.yml) | active | 37 18 1 * * | あり | 指定なし |
| [synthetic-tests.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/synthetic-tests.yml) | active | なし | なし | 指定なし |
| [kakeibo-production.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/kakeibo-production.yml) | active | 17 9,21 * * *<br>31 */3 * * *<br>47 21 * * *<br>11 23 * * * | あり | 指定なし |
| [state-cache-inspect.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/state-cache-inspect.yml) | active | なし | あり | 指定なし |
| [state-migration.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/state-migration.yml) | active | なし | あり | 指定なし |
| [category-backfill-sheet.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/category-backfill-sheet.yml) | active | 7,27,47 * * * * | あり | 指定なし |
| [ledger-daily-migration.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/ledger-daily-migration.yml) | active | なし | あり | 指定なし |
| [gemini-availability.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/gemini-availability.yml) | active | なし | あり | 指定なし |
| [category-sheet-request.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/category-sheet-request.yml) | active | なし | あり | 指定なし |
| [manual-entry.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/manual-entry.yml) | active | なし | あり | 指定なし |
| [bank-pdf-diagnose.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/bank-pdf-diagnose.yml) | active | なし | あり | 指定なし |
| [pdf-unit-readonly.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/pdf-unit-readonly.yml) | active | なし | あり | 指定なし |
| [pdf-unit-write-canary.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/pdf-unit-write-canary.yml) | active | なし | あり | 指定なし |
| [p14-plan-only.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/p14-plan-only.yml) | active | なし | あり | 指定なし |
| [p14-posting-canary.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/p14-posting-canary.yml) | active | なし | あり | 指定なし |
| [receipt-unit-plan-only.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/receipt-unit-plan-only.yml) | active | なし | あり | 指定なし |
| [receipt-unit-posting-canary.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/receipt-unit-posting-canary.yml) | active | なし | あり | 指定なし |
| [hga-shared-session-checks.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/hga-shared-session-checks.yml) | active | なし | なし | 指定なし |
| [category-navigation.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/category-navigation.yml) | active | なし | あり | 指定なし |
| unified-receipt-write-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-review-contract-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-sheet-adapter-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-readonly-host-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-phase5b1-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-phase5b2a-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-phase5b2b1-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-accounting-gate-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-accounting-recovery-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-auto-native-evidence-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-synthetic-native-writer-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-legacy-confirmation-stop-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-legacy-contract-poc-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-native-receipt-v2-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-synthetic-legacy-writer-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-pdf-native-readonly-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-mvp-r0-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-draft-request-design-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-draft-runtime-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-v3-draft-writer-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| iphone-r1-confirmation-checks.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| r2-1-static-audit.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| r2-2-mandatory-gate.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| r2-2-m5-m6.yml | active | mainに定義なし | mainに定義なし | mainに定義なし |
| [process-receipts.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/process-receipts.yml) | disabled_manually | 17 */3 * * * | あり | 指定なし |
| [amazon-daily-import.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/amazon-daily-import.yml) | disabled_manually | なし | あり | 指定なし |
| [aupay-card-recurring-production.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/aupay-card-recurring-production.yml) | disabled_manually | 23 20 * * * | あり | 指定なし |
| [bank-pdf-recurring.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/bank-pdf-recurring.yml) | disabled_manually | 47 21 * * * | あり | 指定なし |
| [bank-income-backfill.yml](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/d62e1e254692e676f4e4a72dc593524a80fbea08/.github/workflows/bank-income-backfill.yml) | disabled_manually | なし | あり | 指定なし |
| medical-amount-selection-synthetic.yml | disabled_manually | mainに定義なし | mainに定義なし | mainに定義なし |
| medical-independent-amount-synthetic.yml | disabled_manually | mainに定義なし | mainに定義なし | mainに定義なし |

## 運用workflowの直近runとjob

下表のSHAは **run/jobのhead_sha**。実checkout SHA・その時点のvalidated変数をAPIから独立取得した値ではない。skipped jobにはcheckout実行自体がない。
各rowの全SHA・直近3run・step状態はJSONに保存。旧runの成功は現在のcredential有効性、台帳反映、pending完了を証明しない。

| workflow / API状態 | 最新run UTC / event | head_sha | run結果 | job / 結果 |
|---|---|---|---|---|
| monthly-maintenance.yml / active | [36937573845](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/36937573845)<br>2026-10-01T22:51:51Z / schedule | `7935ea18ab3ebc076eee291983dad0a52187a86b` | success | [110621330185: maintain](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/36937573845/job/110621330185) / success |
| kakeibo-production.yml / active | [39483987991](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39483987991)<br>2026-10-20T19:30:06Z / schedule | `d62e1e254692e676f4e4a72dc593524a80fbea08` | skipped | [118839618684: production](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39483987991/job/118839618684) / skipped |
| state-cache-inspect.yml / active | [34962727963](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34962727963)<br>2026-09-15T11:20:10Z / workflow_dispatch | `21112169ae62c99ac04e66c0b77fde4542fba60b` | success | [104359913603: inspect](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34962727963/job/104359913603) / success |
| state-migration.yml / active | [34967852055](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34967852055)<br>2026-09-15T12:15:56Z / workflow_dispatch | `daef10eb19b96a6c92be1ce57cbb8ff78597b7c8` | success | [104376525040: migrate](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34967852055/job/104376525040) / success |
| category-backfill-sheet.yml / active | [39483038729](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39483038729)<br>2026-10-20T19:23:25Z / schedule | `d62e1e254692e676f4e4a72dc593524a80fbea08` | skipped | [118836254918: apply-sheet-confirmed-request](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39483038729/job/118836254918) / skipped |
| ledger-daily-migration.yml / active | [35486220090](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/35486220090)<br>2026-09-20T03:18:13Z / workflow_dispatch | `85a533f02a4e17d6a286d7c55623193c42f9975e` | success | [106012981870: migrate](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/35486220090/job/106012981870) / success |
| gemini-availability.yml / active | [35728364382](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/35728364382)<br>2026-09-22T12:38:03Z / workflow_dispatch | `51c1faaf7072081b849b1801b96abb43b3e659ca` | success | [106747484774: probe](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/35728364382/job/106747484774) / success |
| category-sheet-request.yml / active | [38143398684](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38143398684)<br>2026-10-11T13:29:50Z / workflow_dispatch | `fffbe7568db3acd647e9a088280bd4fcbed6c5c9` | success | [114481681937: category-request](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38143398684/job/114481681937) / success |
| manual-entry.yml / active | [39349431297](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39349431297)<br>2026-10-20T00:44:32Z / workflow_dispatch | `d62e1e254692e676f4e4a72dc593524a80fbea08` | skipped | [118389863458: manual-entry](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39349431297/job/118389863458) / skipped |
| bank-pdf-diagnose.yml / active | [36810525236](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/36810525236)<br>2026-10-01T03:26:37Z / push | `056b25a067404b8cb5a9f741d565f944934336b8` | success | [110204360970: diagnose](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/36810525236/job/110204360970) / success |
| pdf-unit-readonly.yml / active | [37936083187](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37936083187)<br>2026-10-09T13:20:00Z / workflow_dispatch | `c29ecc44e1fb9fcc854cde1da5cce940e633e8fa` | success | [113840248945: readonly](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37936083187/job/113840248945) / success |
| pdf-unit-write-canary.yml / active | [37174760247](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37174760247)<br>2026-10-04T03:41:01Z / workflow_dispatch | `3d0c92ea4f9ebbb57e7edbf3f7b3d99c6cdb8f4d` | success | [111354983057: canary](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37174760247/job/111354983057) / success |
| p14-plan-only.yml / active | [37910138229](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37910138229)<br>2026-10-09T09:15:48Z / workflow_dispatch | `0c1788bf4c4f76274b6633427a55ec9952de54b4` | success | [113753045368: plan](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37910138229/job/113753045368) / success |
| p14-posting-canary.yml / active | [37927668606](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37927668606)<br>2026-10-09T12:04:23Z / workflow_dispatch | `30c9ddf1fc184450921aa0427088cb619b4c2394` | success | [113810420027: post](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/37927668606/job/113810420027) / success |
| receipt-unit-plan-only.yml / active | [38002449935](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38002449935)<br>2026-10-09T23:03:04Z / workflow_dispatch | `9f502a96a826ff59231354e7f7ebfa982ec3ad96` | success | [114063480662: plan](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38002449935/job/114063480662) / success |
| receipt-unit-posting-canary.yml / active | [38005016533](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38005016533)<br>2026-10-09T23:33:36Z / workflow_dispatch | `9f502a96a826ff59231354e7f7ebfa982ec3ad96` | success | [114071690798: post](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38005016533/job/114071690798) / success |
| category-navigation.yml / active | [38109383309](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38109383309)<br>2026-10-11T03:45:02Z / workflow_dispatch | `fffbe7568db3acd647e9a088280bd4fcbed6c5c9` | success | [114381582604: navigation](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38109383309/job/114381582604) / success |
| process-receipts.yml / disabled_manually | [34964888952](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34964888952)<br>2026-09-15T11:44:08Z / schedule | `21112169ae62c99ac04e66c0b77fde4542fba60b` | success | [104366904961: run](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34964888952/job/104366904961) / success |
| amazon-daily-import.yml / disabled_manually | [34907742177](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34907742177)<br>2026-09-14T23:12:09Z / schedule | `73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2` | success | [104188204788: import](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34907742177/job/104188204788) / success |
| aupay-card-recurring-production.yml / disabled_manually | [34907806301](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34907806301)<br>2026-09-14T23:12:59Z / schedule | `73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2` | success | [104188401714: ingest](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34907806301/job/104188401714) / success |
| bank-pdf-recurring.yml / disabled_manually | [34911236780](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34911236780)<br>2026-09-14T23:59:02Z / schedule | `73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2` | success | [104198959801: ingest](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34911236780/job/104198959801) / success |
| bank-income-backfill.yml / disabled_manually | [34956603502](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34956603502)<br>2026-09-15T10:11:22Z / workflow_dispatch | `e0075c81fb063e3f12a04dda8b51f900c7e44caa` | success | [104340057868: backfill](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34956603502/job/104340057868) / success |

## SHAの確定範囲

- 統合production最新run `39483987991`、job `118839618684`: main head、skipped、stepsなし。checkout/validatedは未観測。最新3runもskipped。
- 直近successは [38730949804](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38730949804)、2026-10-15、head `fffbe7568db3acd647e9a088280bd4fcbed6c5c9`。job `116408287057`でcheckout・main guard・source実行step成功を確認。コード条件との照合から、その実行時にmain/event/validated/checkout一致のguardを通ったと推論できる。独立checkoutログ証拠・現在のvalidated値ではない。金融行数/対象source/全native完了は未確認。
- 直近failureは `38052312106`、2026-10-10、head `ad2501274d2c07e23cef6eee5d0c71d788f0d0e5`。失敗本文は未取得。
- `pdf-unit-write-canary.yml` はmainのevent/validated検証後に `inputs.approved_sha` を別checkoutする。直近run head `3d0c92ea4f9ebbb57e7edbf3f7b3d99c6cdb8f4d` を処理コードSHAと同一視しない。入力・checkout実値は未確認。#91 head照合＋required checksの定義は確認したが、過去run時点の値は未取得。
- p14/receipt-unit canaryはactiveかつdispatch定義あり、過去のwriter/read-back step成功。現在のflag・資格情報・再実行許可は未確認。試験dispatch/re-runは行わない。
- 全main定義の明示GITHUB_TOKEN権限・environment指定・checkout refsはworkflow-definitions.jsonを参照。Repository/Environmentの実効権限、branch protections、re-run権限、現在の安全flag/validatedは管理画面未認証のため未確認。
- 安全flagがfalseなのかvalidatedが古いのか、skippedの理由は推測しない。自動取込の再開・有効化も今回の範囲外。

## 取得範囲・証拠

- [API workflow状態](r2-3-evidence/workflows.json) / [main定義メタデータ・行番号](r2-3-evidence/workflow-definitions.json)
- [直近100run](r2-3-evidence/recent-runs.json) / [運用22workflow×直近3run](r2-3-evidence/operational-history.json)
- [各最新runのjob/step](r2-3-evidence/latest-operational-jobs.json) / [直近production成功・失敗](r2-3-evidence/production-last-executed.json) / [成功job](r2-3-evidence/production-success-job.json)
- [開始時の実行待ち・実行中](r2-3-evidence/active-runs-start.json) / [取得GETと時刻](r2-3-evidence/requests-start.json)
- `status`別検索は各total_count=0の時点観測。照会間に開始終了したrun、host内request、native内pendingの不存在は証明しない。
- GitHub APIのskipped jobに started_at > completed_at がある。API値を保持し、これをwriter完了時刻へ読み替えない。
