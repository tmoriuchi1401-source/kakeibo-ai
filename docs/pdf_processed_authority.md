# Completed PDF source lifecycle

The existing fourteen-page canary is complete: one Medical page reconciled to
an existing ledger, ten ordinary receipts, four receipts on p4/p10, and the p14
receipt. Archiving never posts them again. Before a move, independently verify
every terminal reference against the ledger and its durable authority, verify
the whole-source SHA256/page count, and verify the existing destination.

The hash-pinned HGA and item-confirmation readers accept this same Drive ID in
either its original inbox or the existing receipt_processed folder. This does
not alter source/page/unit/item identities, authority bindings, actor evidence,
history, cookies, or the automatic privacy classification. Only the location
changes. A copy, arbitrary parent, multiple parents, changed bytes, or a moved
authority JSON is still rejected. Source bytes are checked before evidence is
used; metadata must also be unchanged across the read.

The auth container remains the reviewed 43-file whitelist. Its lifecycle fix
contains no accounting writer, AI client, scheduler, or new permission. The
current shared session and standalone logout entry remain unchanged.

## Separate gate for future inbox PDFs

The existing production runner is not a general page-authority transport:
`receipt_plan.drive.validate_config` pins the canary source/hash and p4/p10/p14,
`receipt_plan.unit_runner` accepts only its bound p4/p10 contexts, and the
deployed `PageRouter` resolves only operator-pinned profiles. Their source or
page allowlists must not simply be removed to accept a new PDF. A completed
plan-only receipt does not grant posting authority for a new source.

The existing receipt schedule, cadence, lock, scopes, and validated-main guard
are unchanged by the lifecycle fix. Generalized intake must have its own
trusted source/page context and fresh actor/snapshot-bound conditional state,
while reusing this authentication service and existing writer. Until that
contract and its runtime access are verified, do not enable a new multipage
posting mode or route future sources through the fixed canary capabilities.

Long-term proof is lightweight metadata only; the original remains one copy.
Do not copy PDF/PNG/crops/raw model responses into permanent archive history.
