# Fixed batch 0: read-quota failure 40671398177 (Draft)

The failed main is `f665a82d6ce954f1f80e967889bcb3e99db65135`. All 100
original imports remain present, all six incomes (4,669 yen) remain absent, and
all financial/manual/PDF values are unchanged. Bank is **47 pending**; parent
is **1293 bank pending**. Native SQLite/checkpoint bytes remain unchanged.
This Draft does not merge, release either pending state, post money, change any
production SHA/authority, resend imports, or process the remaining 208 IDs.

## Failure evidence

The fixed diagnostic reports HTTP 429 at `sheets._execute_sheet_read`, called
from `FixedWriteDB.append_raw` line 290 in that exact failed main. That statement
reads a fresh financial snapshot; the only underlying financial append in that
method is the later line 293. The exception propagated through
`IncomeAppendEvidence.append` line 103. AST/source order, the exact run log and
independent full raw financial read-back agree that this invocation never
reached the income append request. The failed runner's temporary SQLite was not
retained. This conclusion is not inferred from absence of remote SQLite events.

Owner-local immutable evidence lives in `.private/429-recovery/` outside Git:

- new record SHA-256:
  `b6b03fda99f6539bd644ecb160e63c5b7d749831dfd8dc8a3082e833f81464fe`;
- original 100-import/six-income manifest SHA-256, unchanged:
  `23f0eb18ce93243597faabbd14d99f09c0d66bba4cbeeeb6d8ce31d9b4b7be67`;
- fixed plan SHA-256, unchanged:
  `8f64ac790aafb03c605c270a9540db5826ad248630b0eb2db36a26cc05d99135`.

The new record binds only this failed run/attempt, exact 47/1293 raw hashes,
expected 48/1294 hashes, unchanged native files, failure/read-back evidence and
the original import-run record. It records no unresolved material write result
and an append-not-reached finding. If that finding cannot be established for
another failure, this record/entry cannot authorize it. The old 45/1290 proof
is rejected. Original import timestamps, 100 IDs, six complete income rows,
protected 185, PDFs, classification core and approval remain fixed.

## Read pacing and bounded recovery

[Google's Sheets limits](https://developers.google.com/workspace/sheets/api/limits)
specify 60 reads per minute per user/project and treat a service account as one
account. The fixed-backfill SheetsDB previously had no pacer and retried quota
reads after only 1, 2 and 4 seconds.

The existing shared `SheetsReadPacer` now covers every Sheets read in the fixed
preview/apply DB, including metadata, full verification and retries, at 1.25s
spacing (maximum 48 attempts in a rolling minute in the synthetic clock test).
Other writers must remain stopped; an in-process pacer does not coordinate an
unrelated process. Read-only 429 retries use exponential delays 20+jitter,
40+jitter, 60, 60 seconds, with fresh jitter of 0..1s, at most five attempts.
Other HTTP errors propagate immediately. Retry exhaustion stops. Financial
writes remain single requests without retry, including on 429/UNKNOWN.
The signed stop attestation is checked again after slow pre-append reads, before
the financial request, so read backoff cannot silently outlive its 15-minute TTL.

Only redundant reads are removed:

- reuse the just-read imports within one full `validate_live` call;
- batch the four raw financial/manual table ranges into one fresh API read;
- after the full live guard, reread the current append target once for duplicate
  IDs instead of reading three tables again.

No snapshot is cached between appends. Full literal import/income comparisons,
protected rows, raw table hashes, classification and PDF validation remain.
The same synthetic partial-execute case uses **45 → 34** logical Sheets reads
(11 fewer); its full revalidation is stubbed identically before/after, so this
is not a measurement of the entire production workflow or its previous quota.

## Future operations: stop at each boundary

1. Separately authorize merge after CI, new main/validated alignment and fresh
   stop/configuration evidence. This Draft changes no production setting.
2. Use the existing manual `bank-partial-recovery.yml`, exact new proof,
   main/validated SHA, owner actors and `kakeibo-production` lock. Inspect first.
3. `release_bank` once: **47 → 48 ready**, exact approved after hash. Parent must
   stay 1293 pending. Independently read all state/data; **stop**.
4. `release_parent` once: only exact bank 48-ready bytes permit **1293 → 1294
   bank ready**. Bank stays unchanged. Independently read all state/data; **stop**.
5. Align only the dedicated permit's expected main SHA under the future
   instruction, preserving its expiry/operations/plan. Run the existing partial
   preview: 100 imports skipped, six incomes/4,669 yen, expense 0, nonposting 94,
   held 0, written 0. Both exact new released hashes are required; old ready
   generations 46/1291 are rejected. **Stop** before any financial apply.
6. A separately authorized partial apply may append only the six original
   income rows; no import/expense write is available. Expected final bank is
   **50 ready**, parent **1296 bank ready**, with verified append-only receipts
   and unchanged checkpoint/history. Any failure/UNKNOWN stops, without rerun,
   rollback, automatic release or another workflow completing the write.

The capsule continues to use the existing service-account key/envelope, a
fresh signed 15-minute stop attestation and encrypted state bindings. There is
no new key, database, server, UI, normal-operation feature or arbitrary command.
The 308-ID approval remains valid; no transaction reapproval is needed.
