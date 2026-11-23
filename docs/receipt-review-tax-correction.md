# Registered receipt: tax-only correction (code/CI only)

Keep the original request UUID, all ten ledger cells, snapshot JSON/digest,
result JSON and catalog unchanged. Do not register again, re-intake, issue a new
UUID, resend Gemini, move the source or post finance.

The new hidden `_receipt_tax_corrections` sheet stores an intent and a result
as two append-only rows. The deterministic correction ID is `<UUID>:tax:1`.
This minimal path supports one tax-only correction per original registration;
a second correction, duplicate intent/result or pending UNKNOWN is rejected.
The plan binds correction number 1, original UUID, original snapshot digest,
original ten-cell record digest, catalog digest, before/after tax mode and
amended snapshot digest. The old snapshot plus the new `tax_mode` rebuilds the
input-version hash and passes through the existing Python validator. All other
inputs and operations remain byte-for-byte equivalent. Source owner, version,
hash and duplicate-history checks remain in the backend path.

Only an original held with the sole issue `tax_treatment_unknown` and old mode
`不明` is eligible. A known new mode requires `original_print_confirmed`.
`不明` with `insufficient` evidence is a no-op: no correction history, no send,
no write and the old hold remains. A card sales slip showing only the payment
total is insufficient to infer whether individual amounts include tax. Never
manufacture a tax rate or add inner tax to the total.

On success, one atomic Sheets batch appends the result and updates only CS of
the uniquely bound existing UI row. The G formula uses linked intent/result
events to display the effective outcome. The original request result is still
visible in the original ledger. The visible UI remains one tab and 98 columns;
I (registration), individual confirmations, notes and item inputs are unchanged.
Before registration, G explicitly warns when CS is `不明`; CS also has a note.
The validator still holds unknown tax and detects additive tax under inclusive
mode. Formula/validation warnings do not silently fill tax mode.

The local `tax_attempts` SQLite table commits before the first tax RPC. Each
remote write uses the existing Script lock, single RAW batch, REST readback
and durable STOP latch. A lost reply is UNKNOWN, never a resend. The remote
history rejects replay even with a fresh local DB. Setup failures also leave
STOP. No existing attempt table or STOP evidence is reset.

## Existing case: minimum future operations

1. First obtain/read original printed evidence sufficient for `税込` or `税別`.
   If it is absent, retain `不明` and the existing hold; no correction can safely
   remove it from that source alone.
2. After separate activation, deploy the fixed `Review.gs`, `ReviewSetup.gs`
   and new `TaxCorrection.gs` to the same independent Script. Other Script
   files, existing PC/venv, owner OAuth and GitHub PAT are reused.
3. With reception OFF and existing safety checks satisfied, explicitly run
   `receiptReviewPrepareTaxCorrections` once. It creates the hidden history
   and replaces only the result formulas/header/notes; existing ledger and
   input cells are independently checked unchanged. Do not rerun Prepare.
4. In the same bounded owner execution framework, use the existing runner
   config, saved owner access token, permanent attempt DB and one new operation
   evidence directory. Invoke only:

   `python -m scripts.run_receipt_review_tax --execute --config PRIVATE_CONFIG --attempt-db EXISTING_DB --state-dir PRIVATE_OPERATION --request-id ORIGINAL_UUID --tax-mode 税込 --evidence original_print_confirmed`

   The tax mode is an explicit operator choice based on printed evidence, not
   a default. Use `税別` only when corroborated, including separate external
   tax lines already present in the frozen items. No checkbox operation is
   needed. Reuse the existing limited gate controls; no intake/watch/cycle or
   scheduled task is needed. The command closes both gates and removes only
   the Script PAT copy. Independently read the effective result and both OFF
   states at the end. Keep the 300-second outer process limit.

None of these live operations was performed during this implementation.
