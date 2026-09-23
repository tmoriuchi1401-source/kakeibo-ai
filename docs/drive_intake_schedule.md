# Drive intake schedule

The active production entry is `kakeibo-production.yml`. With
`KAKEIBO_LEGACY_DISABLED=true`, the older `bank-pdf-recurring.yml` and
`process-receipts.yml` jobs do not run. The active entry keeps the existing
validated-main SHA guard, `kakeibo-production` concurrency group, production
ledger, and each source's native authority and identity checks.

| UTC cron | JST | Selected stages |
| --- | --- | --- |
| `17 9,21 * * *` | 18:17 and 06:17 | Existing Gmail sources and common accounting |
| `31 */3 * * *` | 09:31, 12:31, 15:31, 18:31, 21:31, 00:31, 03:31, 06:31 | Receipt only |
| `47 21 * * *` | 06:47 | Bank preview only |
| `11 23 * * *` | 08:11 | PayPay intake only |

This uses the existing production launcher with independent source scopes.
Separate legacy workflows would need another authority/ledger path or a switch
back to the disabled legacy jobs. A new Drive launcher would duplicate the
production boundary for no benefit. Source selection changes orchestration
only; the source adapters and their parsers, dedupe, writes, processed-folder
handling, and Receipt/Medical privacy gate are unchanged. The old workflows
remain disabled compatibility entries and retain their manual dispatches.
The existing 06:17 / 18:17 common accounting stages continue to handle rows
imported by PayPay. They check that omitted Drive sources have ready ledger
phases before running a dependent stage. A failed Drive source therefore
blocks its dependents without preventing independent sources from running.

Scheduled Bank is still a dry-run preview; manual `bank_apply` remains a
separate protected choice. A zero-item Bank preview reports `dry_run_noop`.
PayPay with an empty inbox imports zero files and has no file to mark or move.
These isolated no-op runs do not invoke the common ledger sort.
Replayed PayPay CSVs use the stable `paypay:<transaction_id>` identity against
the existing import sheet. Receipt uses the existing normal/Medical gate and
the `receipt:<Drive file ID>` identity. All scheduled runs share the same
serialized workflow lock; a failed selected source leaves its ledger state
pending and does not invoke another Drive source in that run.
