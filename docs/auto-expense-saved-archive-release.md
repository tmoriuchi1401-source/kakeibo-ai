# Saved archive: remaining auto_expense release

PR216 saved its compact reconciliation evidence, but run46076915652 failed
before the parent release completed. Its direct exception remains unconfirmed.
This change connects only the remaining state operation to the existing hosted
maintenance entry. It does not retry either failed run or run accounting.

## Fixed evidence and authority

The original record digest remains
`815c1fa78645fe32ec74474fdc14427dbeb68f236ccdc8211ca0b2a60c8e97b0`.
The saved archive digest is
`8d92edad186defce88564a787083d4b0f4d4f3bbe89001a93bbcf466cabeda2b`.
The native129 bytes and metadata are pinned separately from the original
record's pre-archive native127. Neither record nor archive is rewritten.

The existing encrypted, signed `CARD_EXPENSE_RECONCILIATION` capsule must contain
a new independently approved `operation: release_saved_archive`, with:

* The existing fixed record, approved code tree, actual main SHA, control digest,
  owner approval reference, observed time and expiry (maximum 900 seconds).
* `saved_archive` containing exactly `archive_sha256`, `native`, `operator_run`,
  `parent`, and `parent_after_sha256`.
* The fixed saved native proof and failed operator identity; `parent` contains
  the current exact `sha256`, `metadata_digest`, and `revision`.
* The exact post-release payload hash, computed using the existing ledger API.

The old release capsule cannot authorize this operation. The archived operator
head/run, attempt2, non-hosted runs and old-run replay remain denied. The existing
workflow, owner boundary, production concurrency, main/validated guards,
one-release-one-dispatch fence and deadline remain mandatory. No new workflow,
writer, credential, database, capability, archive or infrastructure is added.

## Ordered operation

1. Independently GET native/parent, the saved archive, both failed-run identities,
   financial values/formulas, protected documents, rules and ACL. Verify the
   old three UNKNOWN proofs and C1. All other parent sources must be ready.
2. For parent changes, use the existing complete bank-noop proof mechanism.
   Real bank posting, unexplained revisions and other-source changes are denied.
   Bind the latest snapshot in the new signature; retain the original archive.
3. Validate the original auto_expense target and compute the proposal through
   `ProductionLedger.release_after_reconciliation()` in memory. Check the signed
   post-release hash. Re-read the archive and current native/parent immediately
   before the real API's own prewrite read, under the common lock and deadline.
4. Use the existing `ReleaseOnlyTransport` to allow only one exact parent write.
   Invoke the same ledger API for `auto_expense` and the saved archive digest.
5. Require the API readback and an independent final GET. Verify exact parent
   bytes, unchanged native bytes/metadata, archive, C1, financial tables/formulas
   and protected documents. Stop before any expense or catch-up operation.

For the reconciled baseline the only changes are parent generation1420→1421
and auto_expense `phase=pending→ready`, `attempt=''`, `error=''`.
`last_success`, `counts`, `duration_seconds` and other sources remain identical.
After a proven bank noop, calculate and sign the proposal from that latest
parent; never reuse the old generation or post-release hash.

The saved branch returns before constructing a native transport or temporary
state. It cannot call `persist_archive`, `DurableState.begin/commit`, or a financial
writer. Native129, C1, all three UNKNOWN archives and existing fences remain.
Errors propagate only allowlisted stage/code, exception category, HTTP status
and conservative effect states through the existing production report. No raw
exception, source content or financial detail is printed. Unknown responses,
readback mismatch or runner loss never cause automatic resend or rollback.

## Validation and release boundary

Focused synthetic tests exercise real capsule signing/decryption, the runtime
branch, the existing API, independent reads, target/proof/financial mismatches,
competition, expiry, old approvals and delivery uncertainty. Previously saved
real native129/parent1420 data are separately tested with network denied and a
memory-only parent transport, including all old UNKNOWN proofs and fresh restore.
These are offline evidence, not production release completion.

The bank code fingerprint change admits only the reviewed production diagnostic
connection; bank scheduling and processing are unchanged. Normal three-job CI
must pass. Production merge, validated promotion and the new one-shot release
require a separate approval naming the final head/tree. None is performed by
this implementation task. After actual successful release, return to the
existing approved 42-expense/catch-up plan and its per-stage Preview contracts.
Those business contracts, classifications, 501-item exclusion and money holds
are unchanged. A01–A15, STOP, permanent fences and the bank schedule persist.
