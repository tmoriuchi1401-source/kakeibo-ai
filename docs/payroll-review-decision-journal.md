# Payroll review decision journal

`app.payroll_review_persistence` persists only explicit operator review
decisions and their signed parser/schema/source provenance. It is deliberately
independent of Payroll transaction writers and ownership production adoption.

The journal is JSON stored outside the repository. A separate 32-byte HMAC key
must also live outside the repository and is never serialized into the journal.
Before a write, callers must inspect `preview_payroll_review_journal_write` and
explicitly pass `confirmed=True` to `write_payroll_review_journal`.
The write call independently revalidates the signed payload, repository-external
target, and current filesystem state; a forged or stale preview is rejected.

Reload is fail-closed and all-or-nothing. Every record is authenticated and
replayed through the ordinary `payroll_review_authority` preview before a copy
of the fresh storage candidate is changed. Source, content, employer, statement
type, pay period, parser, schema, occurrence, raw label/value, source value,
created/applied timestamps, revision, enum, HMAC, duplicates, and replay are
validated. A rejected journal
leaves the fresh candidate unchanged and in review.

Possessing a valid journal does not authorize a Payroll writer/apply operation
and does not enable ownership attestation in production.

## Default-disabled production preview integration

`app.payroll_review_integration` is called only between storage candidate
creation and `PayrollWritePlan` construction. Disabled callers do not inspect a
request and do not load either the key or journal. Enabled callers must provide
an explicit `PayrollReviewReloadRequest` scoped to one exact content SHA-256.
Non-selected sources cause no journal I/O, while a selected source is replayed
all-or-nothing through the existing review authority.

The `payroll-storage-preview` and `payroll-save-preview` commands require the
explicit `--enable-review-journal` flag together with journal file, HMAC key
file, and source content hash arguments. No environment setting enables this
path. The integration returns only adjusted in-memory candidates and read-only
metadata; it never invokes a Payroll writer or apply service.
