# Reservation-only, zero-side-effect reconciliation

This candidate extends the existing hosted operator entry, DurableState,
ProductionLedger and no-write history validation. It adds no workflow, financial
writer, database, infrastructure or recursive archive. Production application,
state release and subsequent posting require a separate approval of the exact
head/tree. A01–A15, STOP, all three historical UNKNOWNs and permanent fences stay.

## Authority and evidence

Use the existing `kakeibo-production.yml` maintenance input, scope
`card_expenses`, with a `reservation-v1:` encrypted capsule. The existing RSA/AES
codec verifies the signature, canonical record digest, exact release tree,
owner approval and at most 900-second approval. The only new signed scope is
`card_unused_reservation_state_only`; it cannot authorize a financial writer.
Preview and Apply have separate operations and fresh signatures. Attempt2 and
reuse of a consumed approval are denied. An interruption requires a new approval
binding a fresh independently observed native/parent snapshot.

The record identifies an actual failed main/attempt1 incremental run and job,
the exact ready-to-pending native and parent revisions, the unchanged checkpoint
and its historical anchor, six table roots and schema hashes across the existing five SQLite
files, all financial values/formulas, five protected documents, their revision
heads, ACL and the original source/window contract. Drive revision bodies must
match SHA256, size and MD5. The reservation pair must be adjacent, and every
other source/field must be exact. Missing, ambiguous or unavailable retained
revisions fail closed; present-value equality does not replace past evidence.

The first legacy C2 release is restricted to the already audited native134
content root, financial/document hashes, fixed second-window contract and the
reviewed PR220 code SHA. No new failed-run ID is added to the exemption list.
The mandatory, still-absent money reviews and unchanged protected revision heads
independently establish the pre-financial barrier for this code version. The
direct exception remains unavailable; `competing_run` is only a reproduced
candidate. Later same-class failures require the new reviewed Apply diagnostics
to match every pre-reservation hash and report a prewrite stage with financial
not reached and money not started. Runner disappearance without that completed
evidence, any possible write or changed revision is rejected.

## Persistence and interruption

1. Independently validate all evidence and the current signed snapshot under
   the unchanged shared exclusion and deadline.
2. Through `DurableState.release_after_reconciliation`, append one small signed
   operator receipt to the existing `recurring_runs` and save native ready in
   the **same** envelope update. The business checkpoint, old rows and all four
   other SQLite files stay exact. The receipt is capped at 32 KiB and links the
   previous receipt; the native envelope retains its 64 MiB limit.
3. Independently read back, restore with a fresh DurableState instance, and
   independently recheck the protected financial/document snapshot.
4. Through `ProductionLedger.release_after_reconciliation('aupay_card')`, clear
   only phase/attempt/error and advance the normal generation. Independently
   read both final objects. Counts, duration, last_success and other sources
   remain exact. No checkpoint or financial/review write occurs.

A receipt saved with native ready is detected on a new, separately approved
invocation; native is not saved again. If parent is already ready, neither
object is saved again. An unknown mutation response stops that invocation and
is never retried. The original failed run stays failed and permanently excluded
from replay. The new operator's `noop` row is not a success conversion of it.
Unsigned extra summary/alias fields and altered receipt chains are denied.

Both real catch-up Preview and Apply use the same history proof. RSA receipts
are verified using the existing hosted service-account key, never a public key
supplied by the receipt. Actual failed/operator Actions identities are checked.
Only proven reservation/release generations bridge the old exact native134
hash; old no-write proofs, UNKNOWN archives and coverage checks are retained.
Unknown failures still stop. Business coverage time is distinct from operator
execution and receipt-recording time.

## Competition evaluation — no behavior change

Card Apply and production declare the same workflow-level
`kakeibo-production` group with cancellation disabled. Their guards query
in_progress, queued, waiting, pending and requested across the repository.
Consequently a same-group run waiting for this job can cause the guard to stop,
even though that waiter cannot yet execute its writer. The saved scheduled run
intervened after parent reservation, was finally skipped with zero steps, and
does not establish the exception actually raised by the failed card run.

A possible separate change would exempt only a waiter independently proved to
use the same immutable workflow-level lock, with no started writer job. Runs
in progress, unknown lock configuration, job-level locks, other groups, reruns
or incomplete evidence would still stop. REST active status alone is not this
proof. This candidate implements **none** of that change: active-status checks,
workflow concurrency and bank schedule are unchanged. A separately approved
implementation/evaluation is required before such an exemption can operate.

The shared state API now accepts optional diagnostics and a card-only receipt.
Bank callers retain their existing default behavior and persisted bytes. The
existing reviewed bank-code digest list is extended for this exact shared-code
version; historical digests and all bank transition proof requirements remain.

## Validation and production boundary

Synthetic tests cover another failed identity, existing API release, native and
parent response loss, independent GET failure, fresh restore, saved-stage no-op,
receipt tampering, changed finance/revisions/target, competition and expiry.
The hosted route and existing capsule codec are exercised with synthetic keys
and network denied. Saved real native135/parent1429 data are tested offline with
synthetic authority, preserving old three UNKNOWNs, 46 alias owners, old fixed
proofs and shared Preview/Apply history acceptance. This is not a live release.

After approval: release/CI/validated checks, fresh readonly operator Preview,
independent no-change GET, then a newly signed state-only Apply. With the saved
baseline this plans native135→136 ready and parent1429→1430 ready. No pending
release, production dispatch, capability operation, financial write or merge is
performed during development. Remaining contracts stay 34 imports/229,255 yen
and 40 expenses/243,451 yen; completed W1 and two expenses are not rerun.
