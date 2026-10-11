# Unified purpose contracts — host connection not performed

This delta stays on draft PR #124, based on the unchanged PR #123 branch at
`1db04ac`. It separates validation contracts; it does not connect a production
host, publish a route, change authority schemas, deploy or approve canary.

| Purpose | Input / required fresh evidence | Native authority / service | Result and prohibited effects |
|---|---|---|---|
| STRUCTURE_REVIEW | Original ID/hash/version; all stable page identities; proposal digest; grouping revision; explicit operation/partition | Existing DurablePdfGrouping.review(request, intent_digest=...) | Grouping only; accounting_allowed remains false. No fabricated Unit/index/posting permission; no accounting append/AI/move |
| METADATA_REVIEW: kind | Source/version/page identity; observation proposal digest/revision; exact selected Japanese kind | Existing PageKindConfirmation.confirm(expected, selections, request_id, intent_digest) | Page-kind-only authority; medical/payroll restrictions retained. Type selection does not authorize AI, posting or move |
| METADATA_REVIEW: privacy | Fresh page model, observation digest/revision, explicit allow-single-page/deny selection | Existing HumanGeneralConfirmation.confirm / hold; its native freshness/kind checks remain | Single-page authority only, no AI sender invocation. Known medical/payroll cannot receive send permission. No posting or move |
| RECONCILIATION_REVIEW | Exact native candidate/identity, ledger snapshot, selected decision and current state revision | Existing ReconciliationConfirmation.confirm, its verified actor/request/TTL/source/ledger/audit validators | same → reconciled_existing; different → confirmed_distinct; unknown → reconciliation_required. Always accounting_write=0; no automatic accounting preparation or append |
| ACCOUNTING_POST | Existing complete Unit/index/page/source/version, current snapshot, posting authority, duplicate resolution, native read-back/replay and unique target | Existing financial controller/UnifiedWriteBridge/native writer | Full PR #123 financial eligibility unchanged. Only explicit post may enter financial execution |

The previous financial-shaped ExistingAuthorityPort now rejects calls. Financial
UnifiedWriteBridge accepts only `post`. Metadata requests carry a purpose and
purpose-specific evidence digest/revision, not financial placeholders. The
existing financial wire request is classified as ACCOUNTING_POST from its only
allowed action; its original review_purpose and native ledger identity remain.

## Shared mechanisms, distinct evidence

PurposeBoundary defines authenticated read-only list/detail and explicit POST
prepare/submit/recovery. It has no HTTP routes, live factories or auth bypass.
Its switch is default OFF and cannot be enabled by a production environment
variable. UNIFIED_RECEIPT_WRITE_ENABLED remains false by default. GET returns
no operation targets even when isolated POST tests opt in.

The future existing host must inject the existing Google/owner/session/Origin/
CSRF gate. VerifiedPost/VerifiedRead are in-process attestations returned by that
trusted gate, not JSON accepted from browsers and not a new identity provider.
Prepare/submit enforce configured owner, Google issuer, live session, method,
CSRF, fixed purpose/request/native-binding/revision, exactly 600-second request
TTL and current expected CAS revision. Identity checks precede native observation;
the fresh native binding is checked again before execution. Login/logout,
Google signatures and existing URL issuance remain the host's responsibilities.

Every enabled purpose uses the SAME ConditionalRequests instance as accounting.
The store reads existing financial records and purpose-tagged metadata records,
without migrating any native authority. Canonical request ID cannot be reused
for another purpose/operation. Same operation/fingerprint replays its state with
zero native executions. Competing claims use existing strong ETag CAS and exact
read-back; conflicts retain HTTP_412, no retry or unconditional fallback.

Metadata operation identity fixes purpose, action and exact evidence,
excluding actor/request/display-card ID. Case ID remains in the fingerprint,
so another card conflicts instead of minting another operation. Financial native operation identities and Unit
ownership fences remain unchanged. Metadata cannot take a financial Unit lock
or supply a posting grant. Foreign evidence, fields or authority objects are
rejected; purpose tags alone cannot convert the evidence.

States: validation-only prepare → accepted → executing → awaiting read-back →
complete; uncertain execution → partial_unknown. Replays never redispatch.
Explicit authenticated recovery calls only existing native read-back, preserving
scope/digest/revision, and never execute/append. Metadata request completion
does not mean source completion or posting permission.

## GET is a pure projection

project_current_snapshot consumes already observed data and copies it into a
display projection with no enabled targets. PurposeBoundary GET calls only the
authenticated read gate and read-only snapshot repository. It never calls the
state-changing grouping display(), fresh observation, native prepare, authority
service, CAS, ledger, writer, AI or mover. A future repository must expose only
read operations; it must not regenerate stale proposals as part of reading.
Any observation/proposal preparation requiring persistence belongs to explicit
authenticated POST outside this pure API. No such native seeding is implemented.

Tests replace state save/CAS/native display/execute and external write capabilities
with fail-on-call spies. GET list/detail leaves every recorded byte/revision and
write counter unchanged. Legacy hold stays display-only. No HTML/form is created
by this contract; use of the existing host HTML and device checks remains later.

## Actual reconciliation contract

The existing pinned validator defines:

```
同一レシート -> same
別のレシート -> different
判断できない -> unknown
```

confirm expects the Japanese selection, compares it with the protected server
binding, then checks native VerifiedActor, Google owner, request digest, TTL,
source identity/hash, complete ledger read-back and state revision. The page-local
identity may have an empty receipt_unit_id: this is permitted by its actual
schema, not a guessed Unit mapping. Old unmapped/held sources remain forbidden.
The former fake `same_existing` is now only a negative test; the actual validator
rejects it. same links without append; different records only a pair-specific
decision and does not waive accounting duplicate/authority gates.

## Synthetic verification boundary

tests/unified_purpose_fixtures.py uses the unchanged auth-session composer and
hash-pinned existing historical validator tree in a Git-external temporary area.
Native modules load into an isolated package, with memory Drive CAS and the
existing MemoryAuditRepository. No live service/client is created. Grouping,
page kind, privacy and reconciliation tests execute their real implementation,
not an accepting fake Service. Existing accounting tests retain native Unit
generation and fake DB/sealed synthetic authority.

Authority creation/update by those existing methods occurs only in disposable
synthetic memory, never durable production authority. Native metadata memory
writes are distinct from accounting append, AI sending and source moves (all
zero for metadata). Fixture setup may observe/create a native proposal before
the GET measurement; setup is not presented as a read-only GET.

The stacked CI only gains checkout fetch-depth:0 to read the exact historical
objects. Production schedules/Secrets/Variables and the hash-pinned baseline,
authority service files/schemas/whitelist are unchanged. CI scope assertions
permit only that credential-free history difference from existing synthetic jobs.

Validation results are recorded in the PR/report. The contract is suitable for
writing a future adapter, but actual existing-host auth/read repository/proof
mapping and shared legacy composition remain unconnected and unverified. Next
minimal work: shadow composition and authenticated read-only projection against
the real host interface in isolation, without production route registration.

Completion label: 契約分離完了・ホスト接続未実施. This is not canary, deployment,
production switching or merge approval.
