# au PAY card canonical executor contract

This phase is write-free. `app.aupay_card_executor` contains no production
writer and never calls `append`, `update`, `clear`, `batchUpdate`, or any Drive
or Gmail mutation. `apply=True` records explicit caller intent but returns
`apply_blocked_writer_unavailable` after read-only revalidation.

## Authority boundary

The executor accepts only the exact internally-issued `CanonicalApplyPlan`
type. At every invocation it revalidates:

- plan schema version and private in-process authority;
- `ready` or `ready_with_withheld` status with no global blocker;
- the complete canonical accounting invariant and decision summary counts;
- unique candidate and decision identities;
- an exact eligible-decision/candidate manifest match;
- each candidate's schema, private authority, purchase kind, and positive
  amount; and
- separation of returns, cross-source ambiguity, existing duplicates, review
  items, and globally withheld items from candidates.

Raw transactions, reconciled transactions, and item decisions are not accepted
as executor input. A `ready_with_withheld` plan is executable only because its
withheld decisions are absent from the candidate manifest.

## Revalidation and idempotency

The Sheets adapter first verifies the complete `取込データ` A:L header; an
unexpected or unavailable schema is a revalidation failure, never evidence of
absence. Canonical identity is the idempotency key. The reader contract returns zero,
one, or multiple stored projections per selected identity:

- zero records: `verified_new` / `still_new`;
- one byte-for-byte-equivalent canonical projection: `already_present`;
- a different record or multiple rows: `conflict` and the selected batch stops;
- missing/unreadable observation: `failed`, or `outcome_unknown` when recovering
  an uncertain prior write, and the selected batch stops.

Every retry performs this read again. A prior `outcome_unknown` is classified
only after read-back: exact present is already applied, confirmed absent is
retryable, and unreadable remains stopped for review. Blind append retry is not
part of the contract.

The future post-write verifier grants `write_confirmed` only when read-back
finds exactly one equivalent projection. Absence is `failed`; unreadable state
is `outcome_unknown`; mismatched or duplicate rows are `conflict`.

## Result and recovery states

`CanonicalExecutionResult` is independent from any Sheets response. It reports
execution status, selected/input/new/already-present/conflict/withheld/
would-write/review counts, reason codes, and a plan audit reference. Candidate
states can represent `not_started`, `verified_new`, `write_attempted`,
`write_confirmed`, `already_present`, `conflict`, `outcome_unknown`, and
`failed`.

Audit and item references are HMAC-SHA-256 values produced with a caller-held
key of at least 32 bytes. Merchant, memo, email content, source records, tokens,
and the key are never emitted. The current CLI preview uses an ephemeral key and
labels its reference `ephemeral_read_only_run`; a future production control
plane must hold a stable protected key if cross-run correlation is required.

## Batch and canary boundary

`ExecutionSubset(limit=N)` ranks identities with keyed HMAC, selects exactly N,
then restores canonical deterministic execution ordering. The same plan and
key therefore choose the same canary without depending on input order. Each
batch is independently revalidated; the design does not assume all candidates
fit in one atomic Sheets transaction.

Before a production writer can be added in a separate phase, it must persist
per-candidate transitions, use bounded batches, record write-attempt state
before sending, require post-write read-back, and preserve
`outcome_unknown` as non-retryable until a conclusive read.
