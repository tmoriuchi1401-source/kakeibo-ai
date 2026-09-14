# Bank PDF recurring production

`python -m app.cli bank-pdf-recurring --dry-run` is the read-only bounded
launcher for new PDFs in the configured Drive folder. It lists only PDF files
in the fixed overlap window, lets the existing three-bank parser and
`build_bank_daily_preview` classify them, and uses source-row identities for
deduplication. Unknown formats and review classifications are withheld.

The launcher reuses `SqliteRecurringRunState` for the successful window
checkpoint and the existing bank steady-state batch writer for a future
explicit apply. A run is limited by the protected authority's `max_files` and
`max_rows` values (at most 20 files and 100 rows). Drive concurrency is also
serialized by the workflow's `bank-pdf-recurring-production` group.

Zero new eligible rows is a safe no-op. Dry-runs never advance the checkpoint
or write Sheets/Drive. Apply requires an external recurring authority JSON,
audit key, exact spreadsheet and Drive bindings, expected Git HEAD, and the
existing bounded bank batch transport. It does not consume or reuse the manual
candidate approval JSON. The recurring launcher validates the standing policy,
then issues an in-memory, short-lived capability for the exact expense batch
produced by the existing finalization gates. Processed-file markers are written
only after a successful apply.

The candidate schedule is 06:47 JST (21:47 UTC), separate from the existing
Amazon/au PAY 05:23 JST jobs and the three-hour receipt workflow. The
workflow currently exposes manual dispatch only; enabling the schedule and
standing authority requires a separate final approval.

The external authority JSON fixes `source=bank_pdf_drive`, the exact Drive
folder and spreadsheet, the `取込データ` sheet and binding/schema versions,
the supported bank-source set, `allowed_classifications=["expense"]`,
`max_files` (1–20), `max_rows` (1–100), a 1-hour to 7-day overlap, the 31-day
maximum window, validity timestamps, and the `main` Git branch. It is
materialized only under the runner temp directory. The authority schema is:

```json
{
  "schema_version": 1,
  "policy_id": "bank-pdf-recurring-v1",
  "source": "bank_pdf_drive",
  "expected_spreadsheet_id": "REPLACE_WITH_EXACT_ID",
  "expected_drive_folder_id": "REPLACE_WITH_EXACT_ID",
  "expected_worksheet": "取込データ",
  "target_binding_version": 1,
  "canonical_schema_version": 1,
  "supported_bank_sources": [
    "auじぶん銀行PDF",
    "ドコモSMTBネット銀行PDF",
    "千葉銀行PDF"
  ],
  "allowed_classifications": ["expense"],
  "max_files": 20,
  "max_rows": 100,
  "overlap_seconds": 3600,
  "max_window_seconds": 604800,
  "initial_start": "APPROVED_AWARE_TIMESTAMP",
  "valid_from": "APPROVED_AWARE_TIMESTAMP",
  "expires_at": "APPROVED_AWARE_TIMESTAMP",
  "expected_branch": "main"
}
```

Manual bank canary and bounded-batch commands retain their exact candidate or
batch approval requirements. The standing authority is accepted only through
the bank recurring launcher; supplying it to the manual batch entry point does
not bypass `BANK_APPROVAL_JSON`.
