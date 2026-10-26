# Read-only Authority diagnostics

The existing production preview adds an `authority` object for selected bank and
au PAY card sources. It reuses the protected policy providers and their existing
source, classification/status, schema and bounds validation. It also compares the
configured spreadsheet and bank inbox bindings and checks the current validity
interval. It does not issue a capability or change a policy, flag or checkpoint.

Public stdout and the Actions summary contain boolean checks only:
`policy_valid`, `source_matches`, `spreadsheet_matches`, `bounds_valid`,
`period_active`, and, for bank, `folder_matches`. `policy_valid` requires all
checks to pass. A source ingestion failure does not suppress this separate policy
diagnostic. Missing or invalid policy input produces a fixed unavailable result;
exception text and input values are never included.

Validated policy fields, exact bounds and timestamps, source/account scope,
target IDs and the original input SHA-256 are saved only in a local
`kakeibo-authority-<random>.private.json` report under `RUNNER_TEMP`, falling back
to the operating system temp directory. The directory must be absolute and
outside the repository. The existing exclusive-create, mode-0600 file helper is
used. Only the typed policy fields are saved: unknown JSON fields, audit keys,
OAuth tokens and service-account credentials are not copied. Reports are not
added to a state cache, uploaded as artifacts, printed, or written to Drive.

The report is local to the process host. On an ephemeral hosted runner it is
lost at job teardown; this change does not create a private report retrieval
channel. Public-repository artifacts or plaintext logs must not be used to
retrieve it. Any later live diagnosis must account for that limitation before
claiming that its private details were obtained or retained by an operator.

The hook runs only for `mode=preview`; apply behavior is unchanged. Existing
main/ref/repository/validated-SHA checks, the common production lock and source
selection are unchanged. This does not authorize dispatching apply, enabling a
schedule, changing Secrets/Variables or using Payroll as household income.

The implementation is not approval for its own production activation. A new
commit cannot run through the existing validated-main boundary until the separate
SHA verification and alignment process is authorized. A blocked or skipped run
must not be reported as a completed live policy inspection.

Regression coverage is in the existing `tests/test_production_flow.py`: private
data stays out of public output, unknown credential fields are not copied,
inputs are unchanged, target/source/bounds/expiry failures are detected, private
reports cannot be written inside the repository, and the main hook is preview
only. Existing workflow, bank-authority and card-recurring tests remain in use.
