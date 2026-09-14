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
or write Sheets/Drive. Apply requires an external authority JSON, audit key,
approval file, exact spreadsheet binding, expected Git HEAD, and the existing
bounded bank batch transport. Processed-file markers are written only after a
successful apply; no schedule or authority secret is enabled by this
checkpoint.

The candidate schedule is 06:47 JST (21:47 UTC), separate from the existing
Amazon/au PAY 05:23 JST jobs and the three-hour receipt workflow. The
workflow currently exposes manual dispatch only; enabling the schedule and
standing authority requires a separate final approval.

The external authority JSON fixes `source=bank_pdf_drive`, the exact Drive
folder and spreadsheet, `max_files` (1–20), `max_rows` (1–100), a 1-hour to
7-day overlap, the 31-day maximum window, validity timestamps, and the `main`
Git branch. It is materialized only under the runner temp directory. No
authority secret or schedule is enabled in this checkpoint.
