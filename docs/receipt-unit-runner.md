# p4 / p10 Receipt Unit plan adapter

Scope: the existing immutable 14-page source only, pages 4 and 10 only. The
existing p14 workflow and its 10-item / 3,801-yen guards retain their defaults.
Medical, other pages, image processing, source moves and schedules are excluded.

The manual `receipt-unit-plan-only.yml` accepts only a private Drive context
reference and an existing authenticated item-confirmation UUID. The context
uses the existing `receipt-item-runner-context-v1` schema and exact owner +
existing service-account ACL. Its candidate digest, journal and page HGA fix a
single Receipt Unit. No binding, category or amount is stored in GitHub Variables.

The runner fresh-reads the original source hash and private baseline, page HGA,
sealed candidate / position manifest, current input cells, current category
master and OIDC-verified item confirmation. The existing validators preserve
segmentation, item sum, tax/discount targets, explicit structure confirmation,
duplicate, snapshot and replay checks. One Unit's hold does not prevent another
Unit from running.

`ReceiptPipeline._materialize_result` receives the existing memory-only
`PlanningDB`. Every planned row must exactly match the authenticated items,
their individual categories and receipt total. The number of items and amount
come from these verified values, never workflow inputs or the p14 constants.

Only compact request/Unit/item references and digests can be written to the
existing hidden queue. The queue row is read back exactly; the source, HGA,
confirmation, input snapshot and ledger are read again. Repeat dispatch uses
the same row and produces the exact same plan, with accounting writes zero.
Unknown queue delivery is reconciled without resending.

This adapter cannot grant or execute posting. Posting remains a separate
one-time approval bound to the specific Unit, actual amount, actual row count,
input snapshot, confirmation and exact plan digest. The historical p14 approval
cannot authorize p4 or p10. The user must approve those concrete plans first.

No terminalization or card hiding occurs in plan-only mode. Parent PDF movement
is outside this adapter and requires all Pages / Receipt Units terminal plus a
separate PDF move canary.
