# p14 explicit posting canary

This manual-only runner adds no schedules, OAuth clients or credentials. It
does not change plan-only semantics, regular intake, Medical, other pages,
Gemini, archive or cleanup. PR #91 remains Draft/Open and is never checked out.

An explicit owner instruction approves only p14: receipt 1, expense rows 10,
import 1, total 3,801 and discount -13 attributed to the confirmed third product.
The operator binds that instruction to the existing Google-OIDC item proof,
owner actor hash, authenticated request UUID, source/page/unit/item identity
digest, exact input snapshot and exact plan digest in a dedicated private Drive
posting file. Only its reference ID goes to GitHub; same owner/SA-only ACL.
The prior plan-only queue receipt is not used as posting permission.

The grant records `explicit_owner_instruction_plus_existing_oidc_snapshot_v1`,
approval reference/time/expiry and fixed budget. No Sheet input can create it.
Its state is unused -> claimed -> complete; unknown writes are held. Every
transition uses strong If-Match, stale 412 rejection and exact read-back. No
unconditional fallback or blind write retry. Claimed/unknown invocations cannot
fill missing ledger rows: only complete existing read-back can be reconciled.

Before claim, two independent fresh plans must agree. After claim, source,
HGA, input, categories and ledger are reread before any accounting call. A
ScopedMaterializerDB adapts the existing ReceiptPipeline materializer to the
existing SheetsDB.append_raw, admitting only the exact approved rows in order.
Receipt, ten expense rows and import commit marker are written in that order.
The adapter exposes no update/delete interface. Existing category validation
and projection invalidation stay with the existing Sheets writer.

Every old value and formula is protected by the preflight ledger digest. Each
prefix is checked before the next batch. Final verification removes only the
exact new IDs and proves the remaining values/formulas equal the old ledger;
new values/formulas must exactly match the plan (only trailing empty cells are
normalized). Partial/unknown outcomes do not trigger resends.

Only exact accounting read-back allows complete, a compact permanent imported
event and current terminal metadata. History is partitioned by year, one unique
event for the request, below 2 KB; no images, model response, input body, token or
personal names. The p14 card is located by its Unit token and identity and hidden
with row visibility only. Inputs, validation, formats, ledger and proof remain.
Other page cards are untouched. There is no parent PDF terminal/archive action.

Replay requires the same fresh proof, inputs and plan, exact expected ledger
rows and unchanged old ledger. It returns accounting writes 0 and creates no
additional authority/history. A previously complete card is not hidden again.
The replay operation cannot consume an unused grant. A plan-only mode or queue
row cannot be promoted by editing its mode.

Workflow requires reviewed main = validated SHA, production enabled, legacy
disabled, exact workflow ref and POST_P14_3801. It uses the established production
concurrency and existing credential/projection configuration. Logs contain only
counts, digests, fixed failure categories and zero side-effect flags.

Promotion: isolated PR review and full synthetic CI -> main merge under the
owner's limited approval -> main CI -> validated SHA update -> fresh readonly
preflight -> explicit posting grant -> one apply dispatch -> exact read-back ->
replay dispatch. If any Safety Gate fails, stop without widening scope.
