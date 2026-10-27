# One-time fixed bank backfill

The normal bank runner selects PDF arrival windows. A preview of that window
cannot authorize the older PDFs that apply also revisits. This optional path
selects only an independently frozen private plan and its fixed batch. It does
not extend the standing bank authority or rewind its checkpoint.

The private plan binds every original PDF SHA-256, all PDF member IDs, the
previously imported protected IDs and row digests, the selected new IDs and
their exact transaction/classification records, the existing legacy-rule digest,
Bank Meaning master fingerprint, core parser/classifier code digest, existing
target/inbox, and deterministic batches. Each batch counts the union of source
IDs once, including held and nonposting members, and is capped by both the plan
and live standing limits (never above 100). One PDF may span batches.

No private IDs, aliases, PDF hashes or classifications belong in Git or public
workflow inputs. Retain the JSON and approval worksheet in the owner's private
area. A later authorized activation may store that exact private JSON in the
existing private Drive area. Wrap its file ID with the existing SA public-key
binding format and the new `BANK_FIXED_BACKFILL_PLAN_FILE_ID` label. Only the
ciphertext is supplied as `bank_backfill_plan`.

## Separate approval and execution

After reviewing the complete private plan, obtain one owner approval for all its
batches. No per-transaction approval and no reapproval of standing income scope.
Do not populate the following optional repository Variable until that approval:

```json
{
  "schema": 1,
  "kind": "one_time_fixed_bank_backfill",
  "plan_sha256": "SHA256_OF_CANONICAL_PRIVATE_PLAN",
  "expected_main_sha": "FULL_REVIEWED_MERGED_AND_VALIDATED_MAIN_SHA",
  "expires_at": "OWNER_APPROVED_ABSOLUTE_TIME_WITH_TIMEZONE",
  "operations": ["append_import", "append_income", "append_expense", "update_new_import"]
}
```

Its name is `BANK_FIXED_BACKFILL_APPROVAL_JSON`. It contains commitments only.
The plan digest uses `bank_fixed_backfill.digest`, independent of JSON whitespace.
All operations are further restricted by each fixed classification; an expense
operation cannot post an item frozen as income/nonposting/hold.

Use only `kakeibo-production.yml` on validated `main`, owner actor/triggering actor,
the existing `kakeibo-production` lock, `scope=drive_bank`, and a zero-based
`bank_backfill_batch`. Apply additionally requires `mode=apply`, `bank_apply=true`,
`confirm=APPLY`. Keep schedule false. The approval pins the actual merged main
SHA; a PR head is insufficient. The optional path cannot select another source
or invoke global accounting/sorting. Empty inputs retain the normal flow.

The launcher reuses `SqliteRecurringRunState`, `IncomeAppendEvidence`,
`BankIncomePipeline`, `BankFinalizationPipeline`, the production ledger and the
native bank `DurableState`. A disposable read-only preflight precedes durable
intent. Protected imports and all existing financial rows are immutable. Only
new imports belonging to the current batch may receive finalization link/status
updates. The write facade checks stable ID and all bound columns immediately at
each dispatch; the original source PDFs and full classifications are rechecked.
The existing finalization decision may reduce automation but cannot broaden it.

Income/nonposting imports use existing RAW append/read-back. Confirmed income
uses the existing income writer and append evidence. Expense finalization uses
the existing explicit-selection writer and its duplicate checks. A final
independent read-back compares all preexisting rows and the complete new rows,
including income and expense columns, before success is persisted.

Nonposting items may receive an import row but never an income/expense row.
Held items receive no financial or import write. No PDF is moved/marked complete,
even at the end of a partial batch; the normal checkpoint never advances.
Completed batch receipts live in the existing bank SQLite summaries. An exact
repeat performs only verified read-back with zero writes. Pending/partial/UNKNOWN
leaves existing durable intent pending and requires operator reconciliation;
there is no automatic retry or rollback. A verified receipt with any changed
column also stops. Do not release it merely to resend.

## Current preparation boundary

Draft PR, synthetic tests and a private frozen plan are preparation only. They
do not authorize merging, changing validated SHA, installing the approval
Variable/private plan, or dispatching financial apply. Keep Cloud and competing
writers stopped, Payroll income prohibited, manual completions untouched, and
other authorities/Secrets unchanged.
