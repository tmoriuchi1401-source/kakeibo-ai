# au PAY card recurring production

The recurring runner is `python -m app.cli card-gmail-recurring`. It is separate
from the historical backfill and from the disabled raw Gmail import route.

## Incremental window and no-op

Each run reads the last successful absolute window end from cached SQLite state.
It subtracts the policy overlap and freezes the resulting JST-aware start/end in
the batch manifest. Gmail receives absolute Unix `after:` and `before:` values;
relative queries are rejected. On the first run, `initial_start` in the policy is
the objective boundary after the completed 1,240-row backlog. A missing cache
therefore causes a safe overlap re-read from that boundary, not a one-year scan.

The complete fresh collection is reconciled with `取込データ!A2:L` before any
manifest or capability exists. Existing identities, returns, cross-source
ambiguity, parser review, Amazon review, and Amazon unmatched items are excluded.
When no production-eligible identity remains, the run records a successful no-op
and advances the checkpoint without creating a manifest or capability.

## Recurring authority

`AUPAY_CARD_RECURRING_AUTHORITY_JSON` is the protected standing policy, not a
general write credential. It fixes all of the following:

- source `au_pay_card_gmail` and the KDDI sender/subject query;
- exact spreadsheet and `取込データ` worksheet;
- allowed ordinary statuses only;
- maximum messages, batch size, overlap, and total window;
- initial boundary and policy validity period.

For a non-empty batch, the runner persists an immutable manifest and issues a
120-second, exact batch/target-bound, one-use capability. Immediately before the
single RAW append it re-reads every identity and requires all to be absent. It
then performs an exact full-row read-back. The capability is sealed and the lease
released on all terminal paths. An unknown outcome is never retried blindly: the
checkpoint remains at the prior successful end, and the next invocation performs
a fresh collection and identity reconciliation.

Example policy shape (replace the spreadsheet ID and choose the actual backlog
completion boundary before storing it as a GitHub secret):

```json
{
  "schema_version": 1,
  "policy_id": "daily-aupay-card-v1",
  "source": "au_pay_card_gmail",
  "expected_spreadsheet_id": "REPLACE_WITH_EXACT_ID",
  "expected_worksheet": "取込データ",
  "allowed_statuses": ["auto_expense", "matched_receipt", "transfer_aupay_charge", "matched_amazon"],
  "max_batch_size": 100,
  "max_messages": 500,
  "overlap_seconds": 172800,
  "max_window_seconds": 1209600,
  "initial_start": "2026-09-11T00:00:00+09:00",
  "valid_from": "2026-09-11T00:00:00+09:00",
  "expires_at": "2027-09-11T00:00:00+09:00"
}
```

Required GitHub Secrets are `SPREADSHEET_ID`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_GMAIL_TOKEN_JSON`,
`AUPAY_CARD_AUDIT_KEY_JSON`, and
`AUPAY_CARD_RECURRING_AUTHORITY_JSON`. The spreadsheet ID in both secrets must
match. The audit JSON contains `key_id` and at least 32 bytes of base64-encoded
`key_b64`. No secret or capability token is printed or stored in the repository.

The workflow runs daily at 05:23 JST (20:23 UTC) with concurrency disabled. A
manual dispatch defaults to `--dry-run`; `apply=true` is required for a manual
production run. Every invocation writes a compact JSON summary to the job summary
with window, found/fetched, eligible/excluded/written counts, failure status, and
manifest/capability/journal/lease final states.
