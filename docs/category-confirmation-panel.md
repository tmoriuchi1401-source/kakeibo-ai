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

## Verification and current release limitation

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
panel does not alter that verification or helper. End-to-end confirmation,
live rule saving and live completion removal therefore remain unverified.

Post-canary readback exactly matches the baseline for expenses, income,
imports, automatic rules, taxonomy, historical request/target records and
the management Home. The operation display and immutable request queue
changed as expected. There were no ledger/category writes in this canary.

The native grid fits in a 390 px screenshot crop including its row gutter,
but the browser viewport override did not apply (actual viewport remained
1280×720). This is a width/layout check, not a completed iPhone device test.
Real iPhone dropdown and touch verification is still required.
