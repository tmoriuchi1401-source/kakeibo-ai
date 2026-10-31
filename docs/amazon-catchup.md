# Manual Amazon catch-up

The ordinary Amazon runner requests checkpoint-minus-overlap through real current
time. A backlog beyond its existing 259,200-second authority therefore fails with
`amazon_incremental_window_exceeds_authority` before Gmail collection. A ready
durable state does not imply that the current request fits that authority.

`amazon_catchup` is an opt-in manual scope of `kakeibo-production.yml`. It selects
only Amazon and reuses the validated-main boundary, production concurrency lock,
fixed authority/target, money writer, exact ledger readback, native checkpoint and
parent ledger. Ordinary core/all/canary behavior and every cron are unchanged.
The new scope must not be used until separately approved and deployed; this Draft
does not authorize merge, validated/configuration updates or production Apply.

## One window per dispatch

For checkpoint `C`, overlap `O`, maximum window `W`, and real current time `N`:

```
start = C - O
end   = min(N, start + W)
```

The existing initial boundary is used only when no checkpoint exists. Both
authority bounds and the two-hour overlap remain unchanged. With `W=72h` and
`O=2h`, each full window advances coverage by 70 hours; a roughly 16-day-seven-hour
backlog needs six successful windows if no cap or hold stops execution. This is
a coverage estimate, not an assessment of actual messages or purchases.

Every dispatch handles exactly one window. Preview restores a disposable native
copy, performs no financial/Drive writes and never advances persistent coverage.
Apply records the actual window end only after the existing writer succeeds;
the runner then reads back a strictly advanced, exact checkpoint before durable
commit. A remaining backlog reports `catch_up_pending=1`. After a successful
Apply the isolated parent outcome is `partial`, with no error and a successful
run; this is not permission to retry an uncertain write. The last window reports
zero. Core cannot treat an Amazon partial as a successful prerequisite.

There is no internal multi-window loop, invented past `now`, expanded authority
or checkpoint skip. `--catch-up --now` and canary/fixed-target combinations are
rejected. No bank, au PAY, receipt, review/reconciliation/global refresh or final
ledger sort stage runs. The money writer retains its existing projection
preparation needed for duplicate checks; this is not a call to the common stages.

## Per-window stop conditions

Before financial writes, catch-up requires a complete collection of at most 100
Gmail messages (or the tighter existing policy). Pagination, missing/duplicate
message IDs, mismatched fetched IDs, absent raw messages and identity/amount
conflicts stop the window. Existing posted overlaps receive canonical readback.
Unresolved or unknown money intents block processing, including an intent for a
different transaction. They are never automatically resumed by this scope.

At most three distinct new/nonduplicate money records may be processed (or the
tighter existing policy/apply limit). This bound conservatively includes records
that would be held, linked or supplemented. It is checked before notices,
projection preparation or financial writes. Duplicated notifications and verified
posted overlaps do not consume new-record capacity. Overflow stops the whole
window: it never posts three and advances past a fourth. Amount/payment/legacy
ambiguities keep the existing review decisions rather than widening authority.

After a failure, lost response, pending marker, incomplete readback, or absent
checkpoint advancement: stop; do not resend, rollback, release pending or dispatch
the next window. Reconcile independently under a separate operator decision.

For a future approved operation, use one Preview (`mode=preview`,
`scope=amazon_catchup`, `bank_apply=false`), review its candidates/holds and bounds,
then one approved Apply (`confirm=APPLY`). Independently GET the financial IDs,
amounts, duplicate counts, native/parent ready states and exact checkpoint advance
before choosing the next window. All parent stages must be ready at entry.

## Validation

Network-blocked synthetic tests cover short windows, long-window clipping,
multiple consecutive real-writer windows, overlap deduplication, unchanged
Preview state, complete/invalid/overfull Gmail collections, money overflow and
conflicts before writes, lost append/state responses, checkpoint nonprogress,
manual-only execution, normal-core isolation and unchanged schedules. They do
not establish production Gmail completeness, transaction counts or posting.
