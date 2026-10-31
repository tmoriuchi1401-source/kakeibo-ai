# Explicit au PAY card window Preview

The ordinary card runner still requires checkpoint-minus-overlap through real
current time and still rejects a window exceeding its existing authority.
This change adds only an explicit historical window to its existing dry-run:

```
python -m app.cli card-gmail-recurring --dry-run \
  --preview-window-start 2026-10-13T08:55:56+09:00 \
  --preview-window-end 2026-10-27T08:55:56+09:00
```

Both bounds are required. The start must equal the actual successful checkpoint
minus the unchanged overlap. The end must advance coverage, must not be in the
future, and must fit the unchanged maximum width. The authority validity period
is checked against real now, independently of these historical bounds. No clock
override, checkpoint substitution, initial-boundary fallback, arbitrary query or
Apply/canary combination is supported.

The source, spreadsheet, worksheet/header, statuses, audit key, parsers and
duplicate classification remain those of the existing card runner. Explicit
Preview also rejects inconsistent Gmail IDs/raw responses and overfull pages;
it never turns a truncated collection into success. The issuer-configured
message cap is unchanged (currently 500). Existing ordinary candidates and new
money records share the authority's batch cap (currently 100); duplicate money
records receive the existing readback. Unresolved money intents stop before
Gmail. Overflow stops the entire Preview rather than slicing candidates.

The dry-run branch returns before manifest/capability/lease/financial transport
creation. Money handling uses the existing read-only preview, not apply or
projection preparation. Existing dry-run summaries are still recorded in the
local SQLite with `advance_checkpoint=False`. A production Preview must restore
a disposable copy through `run_durable_source(..., apply=False)`; it must never
save that copy back to persistent Drive state. Summary history in that local
copy is distinct from financial writes or checkpoint advancement.

## Manual first-window entry

The confirmed checkpoint is 2026-10-15 08:55:56 JST, overlap 172800 seconds and
maximum width 1209600 seconds. The requested first window is consequently
2026-10-13 08:55:56 through 2026-10-27 08:55:56 JST. These are reference values,
not authority JSON or permission to execute this Draft.

`.github/workflows/aupay-card-window-preview.yml` is a new, **workflow_dispatch
only** entry. There is no schedule, push, workflow_call, Apply input, arbitrary
source or second-window mode. It does not alter either stopped legacy card
workflow or the authority diagnostic. `app.aupay_card_window_preview` calls
`run_recurring_ingestion(dry_run=True, preview_window=...)` directly for the card;
it never invokes the parent source orchestrator or another source CLI.

It requires owner and triggering owner, attempt 1, main = event SHA = workflow
SHA = expected_commit = job-start validated SHA, and the exact source, window,
checkpoint and PREVIEW_ONLY confirmation. Before Google credentials and again
before/after Preview it GETs live main/run identity, competing active runs and
same-workflow dispatch history. Any prior dispatch on that commit stops it,
including an earlier failed/skipped run. Do not rerun or dispatch again to
recover a guard failure. A new source invocation is never retried.

Production enabled/legacy disabled/bank schedule true/global schedule false/
category runner false are checked against the job-start Variables snapshot.
`contents:read` and `actions:read` do **not** supply repository Variables API
permission. The entry does not inject a PAT or claim continuous observation of
Variables; the operator must independently GET main/validated/flags immediately
before and after execution and prohibit concurrent configuration changes.

The existing authority JSON/key are temporary files outside the checkout with
mode 0600. Existing target/header/status validation remains in the source. Its
policy must still match the diagnosed 2-day overlap, 14-day width, 500 messages,
100 candidates and validity interval. Real `datetime.now(Asia/Tokyo)` is used
before Gmail authentication and by the source, independently of historical end.
Expired policy or a moved/native-pending checkpoint fails closed.

Durable state restore uses `apply=False`; the transport rejects any persistent
save. Sheets and Drive clients have read-only OAuth scopes. Explicit-window
money preview also selects a read-only Drive store whose `write` rejects; the
normal factory default remains unchanged. Money preview never calls prepare or
apply. Gmail credentials must have exactly gmail.readonly. Complete collection
and the combined ordinary/money batch cap are enforced by the existing explicit
window source. No splitting or candidate truncation is performed.

Before and independently after source execution (also on ordinary failure), the
entry validates ready card/native state and all parent stages, Amazon catch-up
complete, the exact successful checkpoint, and compares:

- Card state and parent ledger: payload SHA256 plus Drive metadata digest,
  including version; metadata must also agree across each content download.
- Financial sheets: complete worksheet values AND formulas, current dimensions
  and nonempty data-row counts. Required: 取込データ, 支出明細, 収入明細. Existing
  レシート and Amazon注文 are included if present. No 5,000/10,000-row cutoff;
  incomplete/range-mismatched or over-64-MiB observations stop the run.
- Existing money, money-migration, money-notices and journal document digests
  when projection binding exists. These reads execute no Amazon source.

Only aggregate counts/integer-yen totals, safe digests, fixed provenance and locally
authored error codes are public. No raw candidate list, mail, credential,
authority JSON, IDs, API exception, child stdout/stderr or original state is
uploaded, logged or cached. This entry creates no private Drive destination.
Its normal one-summary SQLite history is **local to the disposable restore**
and discarded on exit; it is not a parent-ledger save, financial write or
checkpoint update. The SQLite connections close before temporary cleanup.

Actions are SHA-pinned; Python is 3.12.12. `requirements-card-preview.lock`
reuses the 57 hash-pinned Linux/Python 3.12 wheels reviewed for diagnostic
8438b1a (lock SHA256
93e6152fe9e2287447f75ef69ac84e8fa543d41b65a9c9e95ada2dc0f962e322).
Download/install/check occur before Google credential injection, with no source
build and an offline install. Runtime planned communication is GitHub GET,
Google OAuth token exchange, Gmail list/get, Drive metadata/list/media GET and
Sheets metadata/values GET. This is trusted fixed-code execution, not OS egress
firewall enforcement or a network packet audit.

Timeout is 20 minutes including setup, with a 900-second Python alarm and
30-second socket default. A hard cancellation/kill can prevent after-reads;
that outcome is **unconfirmed**, never a successful equality check. There is no
automatic retry, rollback or recovery. Full production behavior, message counts,
candidate amounts and the future run's API permission/quotas remain unmeasured
until the separately approved production Preview.

## Bank impact and deployment order

The inspected baseline is main = validated
`62a7503878a4947a66f1e0ca12485e36c8f1962d`. No bank workflow, cadence, scope,
flag or authority is changed by this PR. That does **not** mean zero deployment
impact:

| Actual condition | Consequence |
| --- | --- |
| Unified workflow bank cron `47 21 * * *` (06:47 JST), bank schedule=true, legacy disabled=true, production=true, main=validated | Scheduled mode=apply, scope=drive_bank, bank_apply=true; same existing bank authority |
| main updated to H, validated still old | New scheduled H job fails `github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA` and is skipped before Secret-bearing steps. A skipped schedule is not automatically made up when validated changes. |
| An older queued event/checkout crosses a main change | It may skip at the job guard or fail the exact checkout/event/validated checks before source execution. Do not merge while any production run is active/queued. |
| validated updated to H after successful H CI | Later eligible bank schedules resume on H. An already running job uses its original context; updating a variable is not a stop mechanism. |
| Manual card Preview holds `kakeibo-production` | Bank and other production workflows share the lock and may queue. The card's competition guard stops if another run is observed; avoid the bank schedule window. |

At this baseline, main push runs `synthetic-tests.yml` (three credential-free
jobs). `hga-shared-session-checks.yml` is path-filtered to auth-session files,
receipt_plan/drive.py and itself; this PR touches none. The only other push entry
is bank-pdf-diagnose on `fix/bank-pdf-hold-diagnostics`, not main. Production
unified/card/bank/receipt/category/monthly entries have no main push trigger.
The legacy bank/card/receipt jobs require legacy-disabled != true; the stopped
entries are neither enabled nor edited here. Global schedule=false blocks the
non-bank unified schedules and monthly schedule, category runner=false blocks
the category poller. Scheduled bank processing remains enabled.

There is no repository workflow that promotes validated SHA. Existing operation
is an operator update of repository Variable KAKEIBO_VALIDATED_MAIN_SHA after
CI on the **actual deployed main commit**, followed by an independent GET.
Repository collaborator access and existing operator authorization are needed;
fine-grained credentials need Variables repository **write**, classic/OAuth
tokens need repo scope. This Preview workflow has no variable-write permission
and receives no credential for it. See the [GitHub Variables API](https://docs.github.com/en/rest/actions/variables#update-a-repository-variable).
The operator's existing repository metadata reports admin/push access and its
classic token reports repo scope; no permission grant or variable write was
performed to test that capability in this task.

The following is a future operator procedure, **not executed or authorized by
this PR update**. Merge, variable promotion and production dispatch need the
next explicit authorization. Do not change bank flags to avoid the SHA guard.

1. Review the final PR commit C and successful PR CI. Independently GET current
   main/validated/flags/workflow states and production run inventory. Reuse the
   successful authority diagnostic; do not dispatch it again. Read card/native
   state and parent ledger; require ready/no pending, Amazon catch-up=0 and the
   expected checkpoint. If baseline/diff/checkpoint changed, stop for review.
2. Choose a quiet window after the latest bank run has completed and at least
   60 minutes before the next 06:47 JST bank tick. Record the next tick and
   current queued/active runs; do not merge during an existing bank/production
   run. This margin reduces interruption risk but cannot guarantee GitHub CI
   latency. Explicit future deployment approval must accept the temporary
   main/validated gap. If that risk cannot be accepted, stop before merge.
3. Merge only the reviewed C. Record actual new main H (merge/squash SHA can
   differ from C), parent(s), tree/diff and main-push CI run URL. Require the
   deployed content to equal the reviewed change, with no unrelated commits.
   This new workflow must exist on default main for workflow_dispatch; see
   [GitHub manual workflow requirements](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
4. Await all three synthetic checks on H. GET main again and require H, inspect
   production run inventory/ready state, and verify no missed bank tick. Until
   this succeeds leave validated unchanged and dispatch nothing. If a bank tick
   was skipped/delayed or CI fails/its result is unknown, record it and stop;
   no automatic bank Apply, replay, rollback or guard bypass is authorized.
5. With the existing operator identity, promote only validated to H:

   ```bash
   gh variable set KAKEIBO_VALIDATED_MAIN_SHA --repo tmoriuchi1401-source/kakeibo-ai --body "$H"
   gh api repos/tmoriuchi1401-source/kakeibo-ai/actions/variables/KAKEIBO_VALIDATED_MAIN_SHA --jq .value
   gh api repos/tmoriuchi1401-source/kakeibo-ai/git/ref/heads/main --jq .object.sha
   ```

   Require both GETs=H and all schedule/legacy/category flags unchanged. Do not
   grant permissions, issue credentials or change Secrets. Subsequent bank
   schedules can now execute on H under their existing conditions.
6. Finish CI and other runs; recheck no competition/pending, unchanged checkpoint,
   exact H and no previous dispatch on H. Record this new workflow's state. It
   is manual-only and normally active when introduced; no legacy workflow needs
   enabling. If temporarily enabling a disabled new entry is separately approved,
   record the old state and restore it afterwards even on failure, with a GET.
7. Dispatch **once** using deployed H, never C if it differs:

   ```bash
   gh workflow run aupay-card-window-preview.yml --repo tmoriuchi1401-source/kakeibo-ai --ref main \
     -f expected_commit="$H" -f source=au_pay_card_gmail \
     -f window_start=2026-10-13T08:55:56+09:00 \
     -f window_end=2026-10-27T08:55:56+09:00 \
     -f expected_checkpoint=2026-10-15T08:55:56+09:00 -f confirm=PREVIEW_ONLY
   ```

   Record run ID/URL/head/attempt=1; workflow SHA equality is enforced in the
   job and Python guard, not invented from a REST field that is absent. On
   failure/skipped/timeout/unknown result do not rerun or dispatch again.
8. Read the safe report: before=after for card, parent, checkpoint, financial
   counts/content/formulas and money digests; complete Gmail collection <=500,
   combined batch <=100; no writer/capability/financial requests/state save.
   GET main/validated/flags again independently. If after-read was prevented,
   mark equality unconfirmed and stop. Restore any temporarily enabled new
   workflow to its recorded state and confirm by independent GET. Report the
   Preview only: no first-window Apply, second window or core execution.

The previous 44 synthetic tests and CI run
[41196344062](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/41196344062)
at C=389171a are reused as evidence for explicit-window source semantics. New
tests cover entry identity/dispatch guards, full financial values/formulas and
sparse tail, failure after-read, native checkpoint, read-only money factory,
real-clock source assembly and disposable history cleanup. These are synthetic
tests with live sockets blocked, not production observations. Added entry
validation: 40 focused tests passed, five Python ASTs and four bash steps parsed,
and the reused 57-wheel lock hash matched. The pre-existing google-genai
deprecation warning is unchanged. Linux CI for the final PR head is reported
separately in the PR checks; no extra production workflow was dispatched.

This Draft update performs no merge, validated change, workflow enable or
production dispatch. NO-GO and core Apply prohibition remain in effect.

## Preview集計出力（本番実行前の補完）

`aggregation`は、既存のcanonical候補分類と同じ金銭Preview判断結果から作る
純粋な集計である。分類器の再実行、追加のGmail/Sheets取得、writer/Apply呼出し、
checkpoint更新は集計に使わない。金銭判断は既存の`preview`呼出し1回の結果を
再利用する。既存の通常run/Apply出力は変更せず、明示窓Previewだけに追加する。

- `candidates`は分類済みcanonical取引＋一意な金銭recordの件数・合計金額。
  同一入力や再送を足し直さず、解析できなかったメールを含む総額とは扱わない。
  `candidate_basis`にこの範囲を出力する。
- `by_classification`は新規`new`、既存重複`duplicate`、保留`held`、
  対象外`excluded`、既存金融への照合`linked`、振替`transfer`、補足`supplement`の
  互いに重ならない分類ごとの件数・金額。`ordinary`と`money`には内訳を出力する。
  金額は全て整数円、購入を正・返金を負とする符号付き合計で、絶対額へ変換しない。
- `planned_financial_writes.transactions`は新規分類の予定取引件数・金額。
  `import_rows`/`expense_rows`は既存判断から投影できる予定対象行数・金額であり、
  Apply時の再読取りで判明する実際の不足行数やHTTP要求数を保証しない。
  通常カード候補は取込行のみで、共通stageの後続支出生成はこのPreviewに含まない。
  金銭取引では同じ金額が取込行と支出行に現れるため、行数合計`all_rows.count`と
  取引金額を分ける。テーブル横断の行金額は足さず、`all_rows.amount_yen=null`と
  `overlapping_table_amounts_not_additive`を示す。
- 予定HTTP write要求数`requests`は既存分類だけから算出せず、`count=null`、
  理由`request_count_requires_write_readback`を出力する。writerを起動して数えない。
- `actual_financial_writes`はPreviewの実要求数・実行数・金額を0として区別する。
  出力前に既存の実writeカウンタ0、writer未起動、state/金融データ前後一致を要求する。
  これは金融書込みをしないPreviewであり、予定件数が正でも反映成功を意味しない。
- `unquantified`には解析保留・拒否・identity衝突・入力重複・非canonical再送・
  金銭通知保留等の既存件数を出力する。対応金額が分類結果に残らない場合は
  `amount_yen=null`、`classification_amount_not_available`とし、0円と推測しない。
  メール数・行数・再送数には重なりがあるので、この欄は候補総数へ加算しない。
  金銭resumeの残り行数はintentの照合なしに出せず、
  `resume_requires_intent_readback`とする（明示窓入口は既存pendingを先に拒否する）。
  支出配分が判断結果にない場合は`expense_allocation_not_in_preview`とする。

公開直前に閉じたschema、整数型、既知の理由code、分類合計・予定値の整合性を
検証し、任意の追加keyや自由文を拒否する。店舗名・本文・取引ID・個別金額の一覧、
元の判断理由文字列は出力しない。集計が欠落/不正なら成功結果として公開しない。
集計テストは新規・重複の混在、保留・負額、金銭分割行、未算出理由、公開CLI全体の
詳細非出力と金融/ストア/checkpoint非更新を合成データで確認する。
