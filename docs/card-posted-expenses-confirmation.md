# Posted card expenses: state confirmation release candidate

The failed execution `46083853434` already appended two expenses totalling
4,531 yen and linked their two import owners. Its two new N cells match the
existing category navigation formula. The journal's generation 338 → 339,
`append: false → true` transition matches `ProjectionJournal.mark(append=True)`.
The failed execution remains failed. Neither expense is to be posted again.

This candidate starts from native 130 pending, parent 1423 auto_expense pending
and C1 `2026-11-04T21:00:33+09:00`. Production has not been changed by this PR.
A01–A15, three historical UNKNOWNs, exact archives, STOP, permanent fences and
the existing bank schedule remain in force.

## Permitted state-only sequence after separate production approval

1. Verify the approved release head/tree, normal three-job CI and promoted
   validated SHA. Reuse saved evidence; fresh-check mutable premises only.
2. Under the existing hosted production workflow's common lock, verify the new
   owner-signed capsule, effective settings, exact native/parent metadata and
   bytes, fixed financial tables in both renderings, protected documents,
   planning profile, categories/rules, ACL and failed-run identity. Only fully
   proven bank no-ops may explain parent changes through the existing mechanism.
3. Reuse the original two-row plan and `owner_updates()`. Independently confirm
   that the current planner reports `already_posted = 2`, with zero new rows,
   and the original business/profile contracts still match.
4. Save only the two owner changes as one small row in existing recurring
   history, keeping native pending (130 → 131). Preserve all prior rows and
   checkpoint. Independently read back the exact saved envelope.
5. Commit that same state through `DurableState` (131 → 132 ready), independently
   read back, and restore into a fresh directory. Verify checkpoint, aliases,
   stable expense IDs and the three historical UNKNOWN proofs.
6. Use `ProductionLedger.release_after_reconciliation('auto_expense', ...)`
   to advance parent 1423 → 1424 ready. Only phase, attempt and error change;
   last_success, counts, duration and every other source stay unchanged.
   Independently GET both final states and verify finance again, then stop.

Before any production Apply of this candidate, run the existing hosted readonly
`preview_posted_card_expenses` operation with a new preview-only signature on the
deployed main/validated SHA. Its success and independent before/after proof of
zero financial and persistent-state changes are mandatory Apply conditions.
Verify the same fixed business/profile contract, two already-posted owners,
native/parent snapshot and preserved UNKNOWN evidence. If Preview fails or any
premise changes, stop; never use Apply to test the preflight contract. A later
Apply needs its own independent approval signature and the existing fresh checks.

The state-confirmation DB now uses the same existing
`LiteralRows(SheetsDB(...))` adapter as normal card expenses. Without that
connection, Sheets' default FORMATTED_VALUE returns expense amounts as strings;
the unchanged planner correctly rejects their exact comparison with integer
expected amounts. Captured actual API replies reproduce
`card_expense_existing_conflict` at `auto_expense.py:235`. This is a replay
finding: the failed hosted runs retain their sanitized `financial_preflight` /
`repair_failed_closed` diagnostic; their direct exception text was not recovered.

The fixed private record SHA-256 is
`8922970a0c2f96079ea0692ff34334d4576cdc486e5a79f465f02a7b4d463754`.
Its source data stays private. The existing encrypted/signed capsule input
uses prefix `posted-v1:`, the existing state-only label/scope, operation
`confirm_posted_card_expenses`, a nonempty independent approval reference,
current release head/tree, `state_snapshot: {native, parent}`, and a fresh
observed_at and expiry of at most 900 seconds. A readonly preview uses operation
`preview_posted_card_expenses` and constructs no state writer. This state-only
authority cannot append a financial row. No test dispatch is required.

Each capsule is consumed by its first dispatch even if it stops before saving.
No rerun/attempt 2 or automatic dispatch is permitted. After interruption,
new explicit approval must bind the newly observed exact saved stage. The entry
recognizes proof absent, proof saved pending, and native ready. It skips each
finished write. A confirmed parent ready is also a no-op. Unknown changes stop;
the path does not repair arbitrary state or accept arbitrary records.

Historical checkpoint proofs are inspected on an in-memory ready view only
after the fixed pending stage and exact posted finance have been verified.
That view is never persisted and cannot pass normal restore or daily guards.
Real readiness requires saved owner evidence and independent readback first.

State confirmation performs expense append 0, J write 0, N write 0, journal
write 0, checkpoint advance 0 and archive creation 0. The saved exact evidence
and failed execution identities remain unchanged. The small owner-proof row is
not a recursive archive. Saved actual-data memory validation produced native
envelopes of 40,425,542 and 40,425,508 bytes, below the existing 64 MiB limit;
future production approval must still check the actual envelopes and deadline.

## Existing writer verification

The financial writer is unchanged. `verify_delta()` expects the existing
`return_formula()` only on the newly appended expense rows' N cells. It checks
FORMULA and UNFORMATTED_VALUE separately, using the installed category UI's
stable-ID reference counts to determine the displayed link label. Existing
rows and all other financial/control tables remain exact.

Protected documents remain exact except for the deterministic result of the
existing `mark(append=True)`, evaluated in memory. Both false → true (one normal
generation increment) and already-true no-op (no increment) are accepted. Unknown
formulas, wrong links, unrelated journal fields/generation, money/review changes,
or any other financial difference remain rejected. Fixed diagnostics propagate
without raw exception text, source content or financial details.

## Validation and remaining approved contracts

Focused regressions cover both renderings, zero/one/multiple navigation matches,
journal update/no-op, unapproved changes, signed hosted boundaries, expired or
reused approvals, concurrency, and response loss before/after each of the three
state saves. Explicit resumes use new entries and do not repeat finished saves.

Saved actual bytes and production settings were tested offline with sockets
denied. State confirmation preserved all three UNKNOWN proofs and C1, restored
aliases on a fresh runner and required no financial call. The two future fixed
expense plans were then exercised through the unchanged `SheetsDB.append_raw()`
in memory, including installed category UI and journal: 18 rows / 64,116 yen and
22 rows / 179,335 yen. Their original business/profile contracts match; repeated
plans produce zero additional expenses. These are offline projections, not
production C2/C3 or completed catch-up evidence.

After separately approved production confirmation, return to the existing
sequence: first window 34 imports / 105,315 yen to C2, fresh second-window plan
from actual C2, 34 imports / 229,255 yen to C3, then the 18/22 expense stages.
Each stage requires production readonly Preview, fixed business/profile
agreement, exact readback and ready before proceeding. Never run the old
two-expense Apply again.

Remaining contracts: 68 imports / 334,570 yen; 40 expenses / 243,451 yen; 40 new
J links. Including the already posted two, totals remain 42 expenses / 247,982
yen and 42 J links. Six money cases / 10,902 yen remain held; transfer expenses
stay zero; the 501-row inventory stays excluded. No latest-window Apply,
receipt deployment, AI sending, original operation or schedule change is part
of this candidate. Stop before production approval; normal unattended operation
requires its separate later decision.
