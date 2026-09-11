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
serialization and the production runner accepts one
`--reconciliation-journal-file` with a repo-external dedicated
`--reconciliation-hmac-key-file`; it does not create or choose the operator
decision. The pair must be supplied explicitly and environment variables alone
cannot enable reconciliation authority. A managed decision store, key rotation,
and UI automation can be added later without weakening the fail-closed flow.

On Windows, store both files in a user-local, repo-external directory such as
`%LOCALAPPDATA%\KakeiboAI\Payroll`. The journal and its single decision record
are independently HMAC-signed. Journal writes require a matching preview and
use exclusive creation; existing unequal content is a conflict. The key is
created separately, never overwritten, and is not serialized into the journal.

Ownership remains shadow-only and is neither read nor required by this runner.
Sources 1, 2, and 5 remain review/manual cases; this flow does not attempt to
improve OCR, relax authority, or make them write-ready.

## Windows scheduled read-only scan

The Windows launcher runs the same classifier as a scheduled, read-only scan:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-payroll-scheduled.ps1
```

It resolves the repository from the script location, sets that explicit working
directory, prefers `.venv\Scripts\python.exe`, and otherwise uses the `python`
on `PATH`. The process exit codes are `0` for a completed scan, `2` for a safe
failure, and `3` when another scan owns the single-run lock.

The default external configuration is
`%LOCALAPPDATA%\KakeiboAI\Payroll\scheduled-scan-config-v1.json`. It contains
resource identifiers and absolute paths, but no HMAC key or Google credential
contents. The strict v1 shape binds the Spreadsheet, Payroll Drive folder,
service-account file, Source 3 review journal/key/exact content hash, Source 4
reconciliation journal/key, local log directory, and lock file. All referenced
files and outputs must be outside the repository. Ambient Google credential JSON
cannot override the configured credential file.

Logs are one JSON file per attempt under the configured repo-external directory.
They contain timestamps, source identifier/content digests, outcomes, reasons,
review and row counts, and error categories. They omit HMAC values, Google token
or credential contents, source filenames, OCR text, item labels, and monetary
values. A `WRITE_READY` entry is only a preview: scheduled mode has no writer and
additionally rejects any nonzero writer invocation or actual row count.

The lock uses exclusive file creation. A concurrent Task Scheduler invocation
exits `3` without scanning. The lock is removed when the owning process exits
normally or raises; after an unclean process termination an operator must inspect
and remove the stale lock before retrying. This favors missed scans over overlap.

For Task Scheduler, use `powershell.exe` as the program and pass
`-NoProfile -ExecutionPolicy Bypass -File
"<absolute-repository-path>\scripts\run-payroll-scheduled.ps1"` as its
arguments. The registration contains the one unavoidable repository path; the
launcher itself has no fixed checkout path and resolves its working directory
from its own location. No registration is performed by this repository. Run
only as the Windows user that can read the external config, journals, keys, and
credential.

Future GitHub Actions migration would require moving the two signed decision
journals to durable private storage and placing both HMAC keys plus the Google
service-account credential in repository-independent secret storage. The
Spreadsheet/Drive identifiers and scheduled config must then be supplied as
workflow configuration. This local launcher does not implement that migration.
