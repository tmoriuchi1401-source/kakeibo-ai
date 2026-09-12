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

Production requires `AMAZON_RECURRING_AUTHORITY_JSON` as a GitHub Actions secret.
The file materialized from it must remain outside the repository. Example shape:

```json
{
  "policy_id": "amazon-daily-v1",
  "source": "amazon_gmail",
  "expected_spreadsheet_id": "replace-with-target-id",
  "max_messages": 100,
  "max_purchases": 3,
  "overlap_seconds": 7200,
  "max_window_seconds": 259200,
  "initial_start": "2026-09-11T00:00:00+09:00",
  "valid_from": "2026-09-12T00:00:00+09:00",
  "expires_at": "2027-09-12T00:00:00+09:00"
}
```

Authority validation limits a run to at most 100 messages, 20 purchases, a
370-day absolute window, the named spreadsheet, and the validity interval. A
canary must use `apply=true` and `apply_limit=1`; do this only after explicit
approval of the immediately preceding write plan.

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

After a successful canary and duplicate-safe rerun are verified, add the daily
`schedule` trigger to the workflow. Scheduled runs use the same authority,
checkpoint, overlap, bounded collection, concurrency, and exact-readback gates.
