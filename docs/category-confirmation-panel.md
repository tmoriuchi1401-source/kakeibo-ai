# Native category confirmation panel

The existing category workflow remains `カテゴリ操作`. Python, Apps Script,
and the management Home target-month formula refer to that title directly.
The new `カテゴリ確認` is a narrow review surface over the existing v1 captured
request runner; renaming the existing sheet is deliberately unnecessary.

## Read-only investigation

The compact legacy rule section has condition/link in A, all-period count,
amount and status in B, combined taxonomy choice in C, an unused D, future
registration choice in E, and the current all-month approval checkbox in F.
G:L contain candidate identity and evidence. The hidden category helper maps
the unchanged combined taxonomy to category IDs and major/minor labels.
The other sections hold immutable historical previews, fixed confirmation
requests and separate bank group approvals. There are no named ranges or
formulas in the legacy category operation grid. Home B4 reads its E5 month.

Native Apps Script owns an installable edit trigger, an existing periodic
status trigger, one immutable UUID queue slot and v1 digest-bound row capture.
The GitHub worker preserves later edits and uses the existing production
concurrency and authority guards. No workflow, accounting pipeline or
classification taxonomy changes are needed by this panel.

## Layout and actions

- A:B have a total width of 334 px. Other columns are hidden.
- Rows 1:28 form a vertical confirmation panel, with a frozen title/count.
- The merchant is large and wrapped; condition/source/account are explicit.
- The month is read from the existing target-month control. Month counts sum
  only active, bound, automatic expenses with `その他 / 未分類` in that month.
- The all-period unclassified amount is separately marked as a reference.
- Up to three recent matching transactions before the target month are
  marked `判断用の過去取引`. They can include classified examples useful for
  judging the category. They never appear in the month total.
- The detail action opens a separate narrow sheet with date, merchant,
  category, amount and a target-month/other-month label.
- The category dropdown displays `大 ＞ 小`, mapped back to the unchanged
  `大｜小` value. It does not add or rename taxonomy entries.
- Future classification defaults to OFF, meaning decline additional rule
  registration; OFF does not deactivate an existing rule.
- Historical scope defaults to `反映しない`, with `対象月のみ` and `全期間`.
- For a historical scope, `対象件数を確認` first runs the existing preview
  engine. A successful immutable count/amount preview changes the action to
  `この内容で確定`. A setting change invalidates that fixed preview.
- A native dropdown action is used rather than a drawing/button that may
  not run reliably in the Sheets mobile app. Controls are 44 px high.
- Row 3 refreshes the existing request status and panel without submitting.
- Rows 30 onward are navigation: wrapped merchant, target-month count/amount,
  category/status and a separate `開く` dropdown. This selects the panel.
- Row 28 filters the navigation into `対象月の未分類` (default),
  `他月の未処理`, `その他の候補`, or `すべて`. Historical/future-only
  candidates are retained. Counts in row 2 distinguish all three groups;
  list amounts are explicitly labelled by their context. Opening a fresh
  candidate always starts OFF / `反映しない`, even after an all-period review.

The current production candidate set consists of service candidates. Product
or receipt-specific candidates outside that kind continue to use the existing
workflow. No source-specific processing is rerouted.

## Safety boundary

Panel capture occurs inside the existing busy guard and script lock. The
visible category/future/scope must match saved state. The selected candidate
and month are bound to a fresh digest of its current active ledger members.
Only that candidate's choice is captured. Other category and bank approvals
are cleared in the captured copy, while their live unsent edits are retained.

Preview snapshots bind the existing preview-only F header. They never use the
legacy all-month direct-apply interpretation. Confirm snapshots use the fixed
request ID and count/digest already held by the existing backfill engine.
Later matching transactions do not expand that fixed target.

A correlated queue must reach `complete` before a candidate is recorded in
the hidden UI completion log and removed from navigation. `error`/`review`
does not record completion. Repeat edits cannot replace a busy UUID snapshot.
The unchanged worker ignores terminal/running replay. The log records the
post-processing transaction signature so a new matching transaction can be
reviewed again. The log is UI state, not accounting authority.

## Installation and rollback

1. Add `CategoryConfirmation.gs` to the existing category-submit project.
2. Apply the optional panel hooks in `Code.gs`, preserving the live PDF
   grouping hook and separate `PdfGrouping.gs` file.
3. Run `installCategoryConfirmation` once only when the existing queue is
   terminal. It adds the new panel and hidden UI log; it does not rename or
   clear the legacy operation sheet or write the ledger.
4. Use `refreshCategoryConfirmation` after source changes, or the native
   `表示・処理結果を更新` control.

For rollback, wait for an authoritative terminal request result. Restore
the pre-install Apps Script core, preserving unrelated hooks, then remove
the new source file. Hide the panel/detail/log sheets rather than deleting
records or overwriting the legacy accounting sheets. No accounting rollback
is needed for UI-only changes. Any confirmed historical write must use the
existing guarded backfill restore, never a blanket workbook restore.

## Strict display-helper compatibility

The read-only 2026-10-04 inventory has seven daily sheets and 41 management
sheets. Category UI/log/queue/detail sheets belong to management, not daily.
The daily Home helper was traced to the earlier authorized Home chart task's
`home-chart-plan.json`, before/after snapshots and saved native chart settings.
Its authored cells exactly match the latest API readback. The management
`_ホーム月別収入` (261003105, hidden, 100 × 2) aggregates income month totals;
the daily `_ホームグラフ` (261003103, hidden GRID, 100 × 11) imports only those
totals and supplies labels/comparison series. No helper data was rewritten.

`DailySheets.verify` allows this one optional exact title/ID/type/hidden/shape
combination. All six original sheet IDs remain mandatory. Unknown titles,
including arbitrary underscore names and management category helpers, are
rejected. Duplicate titles/IDs, wrong helper IDs, visible/non-GRID helpers and
shape changes are rejected; the 100,000-cell budget and source-binding marker
still apply. The renderer's owned-sheet map, schedules, transaction identity,
classification engine, production main/SHA guards and authority are unchanged.
The latest actual daily workbook passes this verification read-only.

Rollback of this code change is a revert of the isolated compatibility commit.
That restores the strict six-sheet contract and therefore also restores the
known projection failure while the Home helper exists; do not delete the
legitimate Home helpers merely to silence that failure.

## Verification and current release limitations

`CategoryConfirmation.test.cjs` checks month/history separation, exact source
binding, isolation of other approvals and rejection of unsaved choices.
`Code.test.cjs` verifies the existing busy/UUID/digest boundary and panel
correlation. `test_category_confirmation.py` feeds real JavaScript-generated
captures to the unchanged Python runner, covering all six ON/OFF and scope
combinations, fixed-target retention, later edits and replay.

The 2026-10-03 production canary uses one already classified candidate, future
OFF and a zero-target historical preview. The first run was skipped while
the main approval SHA was being updated externally. The retry executed the
zero-target preview, then failed during daily projection verification:
`daily_sheets.py:40`, `daily_sheet_contract_changed`. The daily workbook has
an additional `_ホームグラフ` helper outside the six-sheet contract. This
panel did not alter that verification or helper. The limited compatibility
change described above is now tested, but is not yet on production main.
End-to-end confirmation, live rule saving and live completion removal remain
unverified until the compatible worker can run.

Continuation verification: 3,479 Python tests passed locally, plus 15 Node
tests for the category core/panel. The isolated daily contract/renderer/chart
selection passed 50 tests. PR #98's earlier continuation commit and the isolated
compatibility-only PR #99 both passed GitHub synthetic CI. A regeneration test
confirms that completed signatures remain excluded after a legacy key changes,
while a new matching transaction creates a new reviewable signature. Saved
Home chart metadata produces zero chart mutations with the existing renderer.

The fresh audit of stopped UUID `7c5e7ce3-8dd6-4ca7-9ed6-acd40b0edcbb`
validates its v1 232-row snapshot digest
`4ad33b065b021c96073f00cb1e4cd20bf8e01932b3e8937232f5d1d96a9c03a9`.
The queue is terminal `error`; the completion log has only its header.
Every expense category is unchanged, as are all rules, backfill requests and
fixed targets, taxonomy and income. The canary expense itself is unchanged.
The only intervening canonical differences affect another au PAY transaction's
status/link/note and related Home attention count. There were zero canary
category/rule/historical writes. The legacy display gained a `preview_empty`
row and the immutable queue changed; this UI partial write must not be confused
with a completed confirmation or retried using the old UUID.

The latest legacy service inventory contains 124 candidates: 7 with target-month
fallback expenses, 98 with other-month fallback expenses, and 19 with no current
fallback. The existing final-state filter excludes some of the last group.
This results from all-period representative generation, not a month aggregation
error. Historical and future-rule proposals remain available under the new
filter; no candidate or accounting data is deleted.
One other-month group includes a date after the selected month, so the filter
says `他月の未処理`, rather than incorrectly describing every such row as past.

Production dispatch is restricted to the approved main SHA. PR #98 must remain
draft/unmerged until live verification finishes, so testing the compatibility
fix requires an explicitly authorized separate rollout or a different approved
deployment sequence. The execution guard/SHA are not bypassed. This continuation
also has no connected browser surface; Apps Script filter deployment and native
dropdown/canary interactions await reconnection. The code is prepared and tested,
not falsely reported as deployed.

The native grid fits in a 390 px screenshot crop including its row gutter,
but the browser viewport override did not apply (actual viewport remained
1280×720). This is a width/layout check, not a completed iPhone device test.
Real iPhone dropdown and touch verification is still required.
