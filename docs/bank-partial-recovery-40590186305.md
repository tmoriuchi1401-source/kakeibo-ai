# Fixed bank batch 0 partial recovery (Draft; never execute from a PR branch)

This case is run **40590186305**: all 100 imports are present, all six new
incomes (4,669 yen total) are absent, and no expense was added. The approved
308-ID plan, four PDFs, classification core and standing authorities stay fixed.
No transaction-by-transaction reapproval is needed. This change is for review and
synthetic CI only. Do not merge, activate, release pending or post money as part
of preparing this Draft PR.

## Read-back failure

`SheetsDB.append_raw` writes literal numeric amounts with `RAW`.
`IncomeAppendEvidence._rows` previously used `SheetsDB.get`, which requests the
API's formatted display values. The observed comparison is:

| Import columns | Formatted mismatch count | Unformatted mismatch count |
| --- | ---: | ---: |
| A–F | 0 | 0 |
| G (amount) | 100: integer expected, string returned | 0 |
| H–L | 0 | 0 |

Dates, execution timestamps and blanks matched. Both observed row widths were
12. The original HTTP request body/failed-run local SQLite was not retained.
Expected rows were reconstructed with the unchanged materializer and fixed plan;
the common observed execution timestamp falls within that run. All 1,200 raw
cells match. This distinguishes direct API observation from reconstruction.

The fix uses existing `get_raw` (`UNFORMATTED_VALUE`, serial datetime option)
through the fixed writer adapters. The income writer's adapter also reads
literals, avoiding formatted-money failure in its existing validation. All 12
import columns and all 10 income columns remain compared; no value is rewritten,
no date/ID/content mismatch is normalized away, and boolean money is rejected.
Trailing omitted blank cells remain padded as before. The six classification
core files and plan fingerprint are unchanged.

## New proof, boundaries and disabled entries

The owner-local files are `.private/partial-recovery/RECONCILIATION_RECORD.private.json`
and `PARTIAL_MANIFEST.private.json`. Their exact commitments are pinned in
`app/bank_partial_recovery.py`. Do not use the earlier zero-import reconciliation
record or workflow for this case. The proof binds:

- generation 45 and its bank attempt/raw bytes; generation 1290 and its separate
  parent-bank attempt/raw bytes;
- exact 100 stored import rows, original import timestamp, missing six income
  IDs, no added expenses, protected 185 and all other existing financial rows;
- full raw financial/manual tables before recovery and after the sole allowed
  six-income append, PDF bytes/metadata, master fingerprint and authority hash;
- unchanged native SQLite bytes/checkpoint and no other persistent pending
  append. The missing failed-run local SQLite does not prove requests absent.

No new key/account/DB/server is used. The new encrypted capsule uses the existing
service-account key and the existing RSA/AES envelope pattern; its record and
manifest hashes and signature must match. A fresh signed owner-local stop/config
attestation (maximum 15 minutes old) is required at each operation. It contains
`main_sha`, `record_sha256`, `at_epoch`, `configuration_unchanged: true` and the
existing writer/disabled-Payroll task observation. The runtime additionally reads
live main/run status, excludes competing/queued runs and requires Cloud stopped.
No private binding or transaction row is printed.

Both entries require `KAKEIBO_BANK_PARTIAL_RECOVERY_ENABLED=true`. This variable
is **not created or enabled by this PR** and defaults to false. The recovery job
and production partial input are rejected while disabled. This additional
activation boundary preserves the current stop; it is not a request to reapprove
the 308 transactions. The old recovery workflow and its immutable guards remain
unchanged.

## Future authorized execution: stop after every operation

1. Review the Draft/CI, then separately authorize merge and activation. Obtain
   the real merged main SHA and successful CI; align validated SHA while
   retaining schedule=false, Cloud/other writers stopped and Payroll prohibited.
   Compare all settings/authorities/plan/core/PDFs against the new private proof.
   Do not initialize or replace either state or the fixed plan.
2. Under a separately authorized activation, create a fresh encrypted capsule
   from the exact new proof and attestation. Use only main and the existing
   wrapped plan ID. Run `bank-partial-recovery.yml`, operation `inspect`.
   It checks bank 45 pending / parent 1290 bank pending and performs no write.
   **Stop** and review the independent full data/SQLite observations.
3. Run the same workflow with `release_bank` once. Existing
   `DurableState.release_after_reconciliation` preserves the old files/checkpoint.
   Exact expected read-back is bank **46 ready**. Parent must remain **1290
   pending**, and all financial/manual/PDF data must remain unchanged.
   **Stop**; ambiguous response/read-back forbids retry or rollback.
4. Only after that independent confirmation, dispatch `release_parent` once.
   Bank's exact 46-ready bytes are required before the existing parent release.
   Exact expected read-back is parent **1291 bank ready**. Other sources/counts/
   last-success and native SQLite/checkpoint remain unchanged. **Stop**, then
   independently inspect both states and all 100 imports again.
5. Before financial completion, align only the dedicated permit's
   `expected_main_sha` to the real new main; preserve plan SHA, expiry and
   operations. Check scope/expiry/authorities and the enable boundary. Refresh
   the signed stop attestation as needed; changing the attestation must not
   change the pinned proof or manifest.
6. Use the existing `kakeibo-production.yml`: `mode=preview`, `scope=drive_bank`,
   `bank_apply=false`, `bank_backfill_batch=0`, the same `bank_backfill_plan` and
   new `bank_partial_recovery` encrypted capsule. The expected output is 100
   imports skipped, six income candidates/4,669 yen, expense 0, nonposting 94,
   review 0 and written 0. Both exact released states are required. Confirm no
   financial/state/checkpoint change. **Stop** before deciding apply.
7. Only in a separately authorized financial operation, use the same inputs with
   `mode=apply`, `bank_apply=true`, `confirm=APPLY`. Never use the ordinary batch
   resend without the partial capsule. The sole write capability is the existing
   income writer through `IncomeAppendEvidence`, limited to the exact six rows.
   There is no import append/update or expense capability in this path.
8. Independently read all 100 original imports and six incomes, all pre-existing
   financial/manual rows, PDF state and checkpoint. New financial rows must be
   exactly the six fixed income IDs totaling 4,669 yen. Bank should finish
   **48 ready**, parent **1293 bank ready**, without unresolved UNKNOWN.
   Existing append-only SQLite gets a new fixed pending summary referencing the
   reconciled 100 imports (not a fictitious old append), income pending/verified
   and fixed verified receipt. All original history/checkpoint is preserved.
   The complete receipt covers all 100 imports and six incomes.
9. A later verified replay validates that exact receipt and all data, returning
   zero financial **and state** writes. Do not run a replay as part of this Draft
   task. Stop at batch 0; no remaining 208-ID workflow is dispatched.

Every operation holds the existing top-level `kakeibo-production` lock with
`cancel-in-progress: false`, exact main/validated SHA, both owner actors and first
run attempt. `release_bank` and `release_parent` are separate choices, never a
combined auto-release. The write transport accepts just one exact expected
payload. A failure or unknown after either release or income append stops; it
does not resend, roll back, auto-release or proceed to the next boundary.

## GO/NO-GO at Draft completion

Synthetic recovery and real read-only evidence can establish **technical GO for
the proposed sequence**. Actual release/posting remains **NO-GO in this task**:
the PR is Draft/unmerged, validated/permit remain on current main and the new
entry is disabled. Future live GO additionally requires authorized merge,
successful main CI/SHA alignment, explicit enablement, fresh stop evidence and
the exact observed states/data matching the pinned proof at every boundary.
