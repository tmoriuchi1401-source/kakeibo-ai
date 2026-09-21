# Receipt failure diagnostics

A failed receipt stage may already have committed earlier receipts. The parent
must remain failed and the production ledger must remain pending until readback
and reconciliation; a failed run is not evidence of zero writes.

The receipt adapter now reports fixed error codes and one of these stages:

- `receipt_preflight`: inbox listing or the approved source-plan checks.
- `receipt_processing`: receipt parsing and accounting materialization.
- `receipt_archive`: moving a recorded source to the existing processed folder.
- `receipt_projection`: refreshing derived views after completed receipt writes.

On failure, `counts` describes only completed source results. A commit followed
by a lost response can therefore be absent from these counts. They are diagnostic
lower bounds, not authoritative accounting totals or evidence for releasing a
pending marker. `failure` remains nonzero and dependent accounting remains skipped.
The ledger's stored counts continue to describe its last successful run.

Only enumerated error/stage strings and nonnegative integer count fields cross
the child boundary. Exception bodies, stdout/stderr, filenames, source IDs,
receipt amounts, and provider response bodies are not added to the report.
Unknown error text is still suppressed. No write retries or pending-state recovery
are introduced by this change.

This improves diagnosis of production run 35544075700, whose original child
exception was discarded. It cannot recover that past exception or establish its
root cause. Existing successful receipt writes must be reconciled before a new
bounded production attempt.
