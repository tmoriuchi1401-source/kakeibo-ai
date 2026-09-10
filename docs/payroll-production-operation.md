# Payroll minimum production operation

`python -m app.cli payroll-production-run` is the bounded, read-only production
classification pass. It enumerates supported files in the configured Payroll
Drive folder, parses each file independently, resolves employer and statement
type from existing authority, reads the current Payroll Sheets snapshot, and
returns one result per successfully parsed statement. It has no `--apply`
option and never constructs a writer.

Each result includes the content hash and resolved employer/type/pay-period
needed to bind the existing single-statement canary invocation. Treat a
`WRITE_READY` result as a preview only; rerun the canary preview and confirm its
fresh plan hash immediately before any real append.

The existing authority sequence is:

1. `DrivePayrollPreview` lists and downloads source files read-only.
2. `preview_payroll_file` parses each supported source.
3. `resolve_payroll_business_authority` resolves employer and statement type;
   the parser cannot create employer authority.
4. `phase_a_to_storage_candidate` creates the source-bound candidate required
   by persisted review replay.
5. `reload_payroll_review_decisions` optionally reloads the exact signed review
   journal before planning.
6. `build_write_plan` checks schema, identity, review state, storage items, and
   exact/content/statement-key conflicts against the fresh Sheets snapshot.
7. `apply_payroll_source_reconciliation` may replace only a statement-key
   conflict with a signed, operator-confirmed zero-row duplicate plan.
8. `run_payroll_production_preview` maps the existing plan states to the
   production outcome taxonomy. Only `WRITE_READY` is a writer candidate.
9. An actual write remains a separate, exact-source
   `payroll-production-canary --apply --expected-plan-hash ...` operation. That
   path performs a pre-read, fresh replan, exact target/range binding, append,
   post-read, and row-delta verification and stops on an unknown mismatch.

The outcome mapping introduces no second write authority:

| Production outcome | Existing authority | Planned rows |
| --- | --- | ---: |
| `WRITE_READY` | `status=ready`, `reason=safe_new_statement`, duplicate `new` | one header plus resolved items |
| `EXACT_DUPLICATE` | `status=skipped_duplicate`, exact or identical content hash | 0 |
| `ALTERNATE_SOURCE_DUPLICATE` | signed `operator_reconciled_alternate_source` | 0 |
| `NEEDS_REVIEW` | blocked review, parser, schema, or business-authority reason | 0 |
| `CONFLICT` | source-identity or statement-key conflict, including rejected reconciliation | 0 |

Review replay is optional for statements that already have zero review items.
For a reviewed statement to become ready, its signed journal and HMAC key are
required external operational state. A stale, missing, or invalid journal is
`NEEDS_REVIEW`, never `WRITE_READY`.

Alternate-source decisions are also required external operational state when
the same alternate source will recur. The repository provides authenticated
serialization and the production runner accepts one or more
`--reconciliation-decision-file` values with a repo-external
`--reconciliation-hmac-key-file`; it does not create or choose the operator
decision. Persisting the current Source 4 decision is therefore an operator
task. A managed decision store, key rotation, and UI/caller automation can be
added later without weakening the current fail-closed flow.

Ownership remains shadow-only and is neither read nor required by this runner.
Sources 1, 2, and 5 remain review/manual cases; this flow does not attempt to
improve OCR, relax authority, or make them write-ready.
