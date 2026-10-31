# Bank-only scheduled operation

This change is a Draft proposal. It does not authorize merge, variable updates,
workflow dispatch, scheduled activation or financial writes. Activation requires
separate user approval. The existing bank authority and processing are unchanged.

## Gates and scope

Use only `.github/workflows/kakeibo-production.yml`. The existing cron
`47 21 * * *` is 06:47 JST daily; its shell branch still selects only
`scope=drive_bank` and `--bank-apply`. It uses the same production concurrency
group, ledger, authority, duplicate checks, checkpoint, pending protection,
financial read-back validation and PDF holds.

| Event | Required schedule variable |
|---|---|
| Bank cron `47 21 * * *` | `KAKEIBO_BANK_SCHEDULE_ENABLED=true` |
| Unified core / receipt / PayPay crons | `KAKEIBO_SCHEDULE_ENABLED=true` |
| Monthly backup / receipt retention cron | `KAKEIBO_SCHEDULE_ENABLED=true` |
| Manual workflow_dispatch | Neither schedule variable; existing approval/input guards still apply |

An unset or false bank flag disables the bank cron even if the general flag is
true. The general flag does not act as an override for bank scheduling. An unset
or false general flag disables the other unified crons and monthly maintenance.
The monthly job previously had no schedule gate; its added condition applies
only to scheduled events, preserving manual use.

Production-enabled, main/validated SHA and legacy-disabled guards remain
mandatory on the unified job. Keep `KAKEIBO_LEGACY_DISABLED=true`, so the old
`bank-pdf-recurring.yml`, card and receipt legacy jobs cannot execute. Keep
`CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED=false` to stop its independent poller.
These conditions cover the existing scheduled workflows in this repository;
they are not a lock against manual dispatch, other repositories or external
writers. No external exclusion guarantee is added.

## Later activation procedure (not performed by this PR)

After separate approval:

1. Review the final diff and successful PR CI, then merge. Read the actual main
   commit M and confirm its tree/diff and CI. Only then update
   `KAKEIBO_VALIDATED_MAIN_SHA` to M and independently GET it back. Do not use the
   PR head or test-merge SHA as an unverified replacement for M.
2. Independently confirm bank/parent ready, no pending or competing production
   run, catch_up_pending=0, current checkpoint and valid existing authority.
   Check the inbox count against max_files, and retain the held PDFs and IDs.
   Do not replay historical UNKNOWN requests or bulk-post unimported IDs.
3. Preserve these values; do not enable legacy workflows:

   ```text
   KAKEIBO_PRODUCTION_ENABLED=true
   KAKEIBO_SCHEDULE_ENABLED=false
   CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED=false
   KAKEIBO_LEGACY_DISABLED=true
   KAKEIBO_BANK_SCHEDULE_ENABLED=false (or unset until the final step)
   ```

4. With the explicit activation approval, set just the bank flag:

   ```powershell
   gh variable set KAKEIBO_BANK_SCHEDULE_ENABLED --body true --repo tmoriuchi1401-source/kakeibo-ai
   gh variable get KAKEIBO_BANK_SCHEDULE_ENABLED --repo tmoriuchi1401-source/kakeibo-ai
   ```

   Independently verify all other variables/secrets remain unchanged. There is
   no need to enable the old bank workflow or change any authority. Do not
   dispatch Apply to test the schedule as part of this flag-only procedure.
5. Observe the next bank cron run. GitHub schedules can be delayed; 06:47 JST is
   the configured time, not an exact-start guarantee. Confirm event=schedule,
   the bank cron, validated SHA, bank-only scope, final ready/checkpoint state,
   and only the expected authorized bank side effects. On an ambiguous response
   or pending state, stop without resend/rollback or automatic recovery.

To pause later with approval, set only the bank flag false; retain the general
flag false and the other stop conditions. Flag changes affect later runs, not a
run already executing. Disabling scheduling does not roll back a completed write.

## Held PDFs and capacity

Apply lists the entire bank inbox, including old held PDFs, in modifiedTime order.
Held PDFs consume max_files capacity even though they are outside the current
write window. The existing list requests `max_files + 1` and rejects either
pagination or a count greater than max_files; it never silently skips excess
files. Example: at max_files=20, five held PDFs leave room for fifteen more;
twenty-one total files cause `bank_recurring_file_bound_exceeded`.

This limit check occurs inside the existing durable source operation, after
pending intent creation. An overflow can therefore stop before financial writes
while leaving pending that needs independent reconciliation. Monitor inbox
capacity before activation and after arrivals. This PR does not raise the limit,
move/delete held PDFs, change timestamps, mark them processed, bulk-post held
IDs, approve meanings or resend UNKNOWN requests. Resolve holds individually
under the existing authority and a separate authorized operation.

The bank flag enables ordinary authorized bank Apply, including its existing
bank review refresh when successful; it is not a read-only/no-op flag. New valid
PDFs may produce financial writes under the standing authority. The non-atomic
CAS/external-writer race and existing partial/UNKNOWN stop behavior remain.

## Validation

Network-blocked tests evaluate the actual YAML job expressions over bank/general
flag combinations, all existing crons, manual preview/apply, legacy/category/
monthly stops and original main/validated/production guards. Backlog tests cover
five held plus fifteen new files and the twenty-first-file rejection. No bank
processing code, authority, ledger/checkpoint or PDF mutation behavior changes.
