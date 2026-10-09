# Explicit p4/p10 Receipt Unit posting

This manual-only adapter reuses the authenticated private Drive context,
item confirmation journal, HGA, existing item validation, ReceiptPipeline
materializer, exact accounting reconciliation and existing card projection.
The p14 fixed posting budget and its workflow remain unchanged. PR #91 is
not merged or checked out by a privileged runner. No new Sheet, OAuth client,
credential, schedule, archive operation or cleanup is introduced.

## Permission and binding

A successful `receipt-unit-plan-only.yml` run is required. Its one hidden
intake row must have the exact request/context, Unit/items, confirmation and
snapshot binding, plan digest, actual row counts and total. It is evidence
of planning, not financial posting permission.

After the owner explicitly approves the exact target, amount and detail-row
count, an operator creates a dedicated `receipt-unit-explicit-posting-v1`
grant. The Sheet, OIDC callback and plan-only runner never create this grant.
It binds the owner actor hash, existing authenticated request UUID, private
context reference, source/page/review/revision/Unit/items identity digest,
input snapshot, confirmation, plan, exact dynamic budget and fresh ledger
values/formulas fingerprint. It expires in at most 24 hours and starts unused.
No past p14 financial approval is accepted for p4/p10.

Prepare each grant immediately before its own posting. Other independently
approved Units may have posted since the older plan-only run; its old ledger
fingerprint is not reused as the new posting precondition. Full current
ledger values and formulas must match the new grant. Receipt amounts, item
categories and discount allocations cannot change from the confirmed plan.

`receipt-unit-posting-canary.yml` accepts only private reference IDs, UUID,
apply/replay and the explicit dispatch marker. It requires validated main,
manual workflow_dispatch, debug off, existing production concurrency and the
existing service account. Only source pages 4 and 10 of the currently pinned
14-page source are eligible; caller-supplied page/amount/item counts are not
accepted. Private state ACL must be exactly the owner and existing service
account, in the existing private folder. No receipt content goes to GitHub
Variables or workflow inputs.

## Claim, write and recovery

Before a durable claim: fresh source/HGA/manifest/input/category checks,
duplicate/identity collision checks, two identical plans, exact hidden intake
receipt, UI identity and explicit unused financial grant. A strong ETag
If-Match claim and exact read-back precede any accounting append. Stale ETag
returns 412; there is no unconditional fallback or retry.

The existing writer receives only the approved Unit. ScopedMaterializerDB
accepts the exact ordered prefix of the approved plan and exposes no update
or delete capability. Every append is preceded by exact prefix read-back,
including proof that all old values/formulas remain unchanged. Item categories,
printed tax and discount signs/allocations retain existing validation rules.

Partial or unknown results are held and never automatically retried or filled
in. A later invocation can only read back; fully written exact rows may close
a lost response. Missing/conflicting rows keep reconciliation required. Only
exact three-table accounting read-back can produce imported terminal state,
one compact permanent event in its year partition and hidden card visibility.
Input values, ledger, identity and history remain intact. A failed UI delivery
can be recovered without additional accounting appends or history events.

Complete replay validates the same binding, accounting rows and old ledger
fingerprint, writes zero additional rows and changes no posting state/version.
Unrelated ledger changes during a posting or before replay cause a safe hold;
they are never silently accepted by relaxing read-back.

## Rollout

1. Synthetic tests and CI, isolated minimal PR; preserve p14/plan-only tests.
2. Main deployment only within the owner's authorized minimal rollout scope;
   update validated SHA only after exact main CI success.
3. Read-only fresh plans and owner approval of each exact target/budget.
4. One Unit at a time: grant, apply, exact read-back and immediate replay.
5. Other pages/Units proceed independently when one is held. Parent PDF is
   never moved by this adapter; archive remains a separate all-terminal gate.

Preparing or deploying this code does not authorize financial posting. Until
the owner separately approves the p4/p10 plans, no real posting grant or write
dispatch is created.
