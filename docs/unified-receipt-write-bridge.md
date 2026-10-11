# Unified receipt write bridge (draft, default OFF)

Purpose separation supersedes the former financial-shaped metadata dispatch.
See [purpose boundaries](unified-receipt-purpose-boundaries.md). Only explicit
ACCOUNTING_POST enters this financial bridge; metadata uses ExistingReviewPort
and purpose-specific evidence without financial placeholders. Host connection
remains unperformed.

Depends on read-only PR #123 at `1db04aca633531871b3519e9eed8f7b76712f0e5`.
This PR is stacked on `codex/unified-receipt-readonly`; review its delta against
that branch. It does not request a merge to main, deployment or real canary.

## Boundary and native routes

The controller accepts requests against server-read Cases. It cannot accept a
browser-supplied Case, actor, authority, route evidence or permission. Its input
dry validation preserves native/unsent inputs and returns no enabled operation.
The host must read the subsequently authenticated native owner snapshot afresh.

| Case | Native connection |
|---|---|
| General image | `ReceiptConfirmation` validator/plan/writer |
| Medical | The existing `ReceiptConfirmation` fully manual validator/writer |
| Registered PDF detail | `PageIntake.post_manual`: snapshot/owner proof, independent posting authority, privacy, native intent and scoped materializer |
| Explicit Unit posting | `receipt_plan.unit_posting.execute`, keeping its existing p4/p10 scope |
| Confirmed grouped Unit | Existing `DriveUnitProcessingStore` reservation and `ScopedWriter`/`ReceiptPipeline`; trusted grouped plan and source/privacy/owner verification injected by host |
| Existing ledger reconciliation | Existing `ReconciliationConfirmation.confirm` injected from the existing host |
| PDF split/merge | Existing `DurablePdfGrouping.review` |
| Page kind/privacy | Existing per-page authenticated gate; selecting a kind does not send to AI |

No accounting writer, authority issuer, AI sender or source mover is introduced.
Missing native host services are unavailable. Grouped and metadata adapters still
need an end-to-end contract test in the pinned existing authority host before
canary; mocked dispatch and coordinator coverage do not prove that integration.

`ReceiptConfirmation.apply_confirmations(keys=[review_id])` restricts the target
and does not close unrelated reviews. Its optional request gate sends legacy
and unified entry points through the same CAS coordinator. A future host must
install it in **every** active legacy instance before enabling that route.
Without fresh evidence of the shared claim, the bridge denies the operation.
The existing default production constructor and unscoped calls are unchanged.

## Enablement

Code/config default: `UNIFIED_RECEIPT_WRITE_ENABLED=false`. Flag alone never
authorizes a write. Required exact pins:

- `UNIFIED_RECEIPT_CASE_ID`
- `UNIFIED_RECEIPT_SOURCE_HASH`
- `UNIFIED_RECEIPT_UNIT_ID`
- `UNIFIED_RECEIPT_EXPECTED_REVISION`
- `UNIFIED_RECEIPT_REQUEST_STORE_ID`

The injected request store must already exist and support strong-ETag conditional
replace plus exact read-back. The bridge creates no production state file.
Native adapters must freshly validate all `unified_receipt_eligibility` conditions,
one operation target, native authority revision/expiry and known unwritten outcome.
Synthetic-labelled, historical, superseded, partial/unknown and legacy-unmapped
cases cannot generate writes. The old p1 medical and related reconciliation
references are explicitly fenced. No identity, input or authority is migrated.

There is no live client factory, HTTP deployment, schedule invocation or change
to production entry points, Secrets, Variables, Apps Script or old Sheets. The
new CI-only workflow targets the stacked base branch, uses no production secrets,
and explicitly keeps the write flag false.

## Requests, identity and state

Requests fix case/purpose, original ID/hash/version, stable page identity,
Unit/index, current snapshot digest, authority revision, action, request ID,
expected CAS revision and native final operation identity. Native image identity
uses its existing receipt ledger namespace; registered PDFs use their native
page/Unit reservation; explicit Unit posting uses its existing permanent event
ID. Changing a card/purpose/request cannot create another financial operation
for the same Unit. A completed/unknown operation never frees that fence.

States: not received → validated/awaiting owner → accepted → executing → awaiting
read-back → complete. Validation refusal is held; partial/unknown stops execution.
Duplicate requests return the existing state with zero writes. Timeouts never
append again. Explicit recovery only performs complete native ledger/three-table
read-back, never execution or gap filling. Native recovery/finalization and the
original's all-page/all-Unit terminal/archival conditions remain separate; request
completion alone does not authorize moving the original. There is no move API.

## Error contract and compatibility

| Condition | Result (before another native execution) |
|---|---|
| Same final operation, same fixed fingerprint | Idempotent replay; return current state, accounting writes 0, no claim/state/append change |
| Same final operation, different case/scope/snapshot fingerprint | `unified_operation_conflict` |
| Accepted canonical request ID reused for another final operation | `unified_request_id_conflict` |
| Same Unit under another financial operation/request | `unified_unit_already_owned` |
| Stale expected state revision | `unified_request_stale_revision` |
| Competing conditional claim | Native `HTTP_412` (no fallback/retry) |
| Execution/response/read-back unresolved | `unified_partial_unknown_readback_only`; no redispatch, read-back only |

Precedence preserves the existing operation-conflict guard, then checks stale
revision and canonical request-ID conflict, then native validation and the Unit
ownership fence. Persisted canonical request IDs must be unique. Native writer
error codes/state/authority are not renamed or relaxed. Git history and existing
documents contained no published use of the new uncommitted bridge codes; the
two existing bridge guard names above are nevertheless retained.

## Four failed fixtures and fixes

| Failed test | Root cause | Correction |
|---|---|---|
| Different final identity on one Unit | Test intended another operation but `FakeNative` derived the same operation from unchanged Unit identity; its changed card fingerprint correctly hit operation conflict | Check that conflict first; explicitly attempt a different operation and prove request-ID/Unit refusal, unchanged first ledger/read-back, one native execution, zero second append |
| Normal image | Fixture omitted the receipt/import rows that the existing confirmation comparison route receives after initial analysis, and used an unrelated synthetic page identity | Generate native `needs_review` rows with the existing materializer; derive hash from fixture bytes and page/Unit/index from native `SourceRef`/segmentation; preserve its actual before snapshot |
| Multiple receipts, index 1 | Empty item positions made native segmentation return `item_positions_missing` without a segmentation digest | Supply all item boxes inside each non-overlapping receipt box; assert native successful segmentation, distinct Unit/index/operation and shared source/page |
| Multiple receipts, index 2 | Same incomplete spatial fixture | Same correction; also process both Units in one registry, shared request CAS and fake ledger |

Eligibility and segmentation requirements are unchanged. The only new runtime
guard in this repair is canonical request-ID uniqueness across operations.

The original 107-test synthetic suite keeps its test count. A one-Unit native
PDF executes one materialization with three table appends (1 receipt, 2 expenses,
1 import); duplicate/replay adds zero appends. Two different Units legitimately
execute twice with six appends and no duplicate row IDs. Native manual medical
executes once, with one row per accounting table. No medical AI or source moves.

Validation evidence and production read-only counts are recorded in the PR.
Local ordered validation on 2026-10-11: the four repaired tests 4/4, unified
synthetic 107/107, inherited read-only/eligibility 84/84, related native
receipt/PDF/writer tests 644/644, full suite 3999/3999; Node contracts 13/13.
The full run uses a dedicated temporary area outside the Git repository to
preserve existing protected-state contracts. Production read-only recheck:
99 unchanged references, one legacy hold, operation targets 0=99 / 1=0 /
multiple=0 and active unified requests=0. Review/input cells, accounting three
tables, six private state files and original bytes/parent/revision are unchanged;
production accounting writes, duplicate additions, moves, AI sends and authority
updates are all zero. Actual snapshot evidence remains private, outside Git.
Real canary, full existing-host grouped/mixed/reconciliation contracts and
PC/390px/iPhone checks remain separate, unapproved/unverified work. No finished
production UI or canary readiness is claimed.
