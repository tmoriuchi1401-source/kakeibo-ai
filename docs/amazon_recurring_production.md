# Amazon Gmail recurring production

`python -m app.cli amazon-production-preview` is the read-only write-plan command.
It uses the existing Gmail `gmail.readonly` token and a read-only Sheets client.
The query is a fixed JST window represented by absolute Gmail epoch boundaries;
pagination beyond the configured maximum fails closed.

Only an order-confirmation event with one order ID, one date, and one positive
order total is eligible.  Cancellation, return, refund, conflicting totals,
parser failure, an existing item-level Amazon order, and identity conflicts are
withheld or marked for review.  Gmail message ID, RFC Message-ID, source hash,
canonical import ID, and canonical expense ID protect overlap reruns.

## Pre-canary preview

The `Amazon bounded production flow` workflow is intentionally manual-only until
the canary has passed. Run it with `apply=false`. Its output contains only hashed
order references, dates, amounts, and row counts. A result with
`collection_complete=false` is not an authority to write; reduce the lookback or
investigate pagination before continuing.

## Protected authority

For a canary, the workflow materializes an authority file under `RUNNER_TEMP` from
the approved fixed window and the existing `SPREADSHEET_ID` secret. The file stays
outside the repository. Its enforced shape is:

```json
{
  "policy_id": "amazon-daily-v1",
  "source": "amazon_gmail",
  "expected_spreadsheet_id": "replace-with-target-id",
  "max_messages": 100,
  "max_purchases": 3,
  "overlap_seconds": 7200,
  "max_window_seconds": 259200,
  "initial_start": "approved preview start",
  "valid_from": "2026-08-01T00:00:00+09:00",
  "expires_at": "2027-09-13T00:00:00+09:00"
}
```

Authority validation limits a run to 100 messages, at most 3 purchases, a
32-day absolute window, the named spreadsheet, and the validity interval. A
canary must use `apply=true`, `apply_limit=1`, the exact preview start/end,
approved hashed target, and expected event/header counts. Any drift fails before
writing. Do this only after explicit approval of the immediately preceding plan.

## Persistence and recovery

The workflow restores a SQLite checkpoint/run log from Actions cache and advances
the checkpoint only after complete collection and exact post-write readback. A
two-hour overlap is deduplicated by source and canonical identities. Workflow
concurrency permits one run at a time. Zero eligible items is a successful no-op.

Sheets appends are multi-request rather than transactional. On retry, an exact
half-written import/expense pair is repaired; conflicting content fails closed.
When a later Amazon Order History CSV materializes item rows, the earlier Gmail
order-total expense is marked `superseded_amazon_items` before item expenses are
used, preventing double counting.

After the successful canary, the daily schedule starts at the canary window end
and runs at 05:23 JST. A manual `recurring_preview=true` run exercises the same
new-mail window with zero writes. Scheduled runs use the same checkpoint,
two-hour overlap, bounded collection, concurrency, and exact-readback gates.
