# PDF production rollout preparation

This branch starts at validated main. Draft PR 91 is not merged or copied as a
whole. No workflow, schedule, Secret, existing Drive binding, bank, PayPay,
Amazon, Payroll, Medical AUTO or existing spreadsheet UI is changed here.

The shared ordinary receipt policy requires a valid payment date, positive
original total and existing categories. Existing item/total consistency,
transaction-kind, adjustment and privacy checks remain. Merchant/payment are
optional; a payment unsupported by exact local original text becomes blank.
The existing manual-entry writer likewise preserves an explicitly typed optional
payment without inventing cash. It verifies the complete appended row and stops
on conflicting content. A source-bound caller can inject a pre-append freshness
barrier. This callback does not itself create PDF authority.

PDF-bound manual requests now require a verifier; the ordinary manual worker
rejects their marker and cannot cancel/void their accounting rows. The explicit
PDF service reuses that writer for one source-bound expense append, without a
new ledger implementation. It requires existing category membership, explicit
owner request verification and fresh source/authority both before claiming and
before append. OCR/candidates never supply input. Its writer fence permits only
the exact new expense row and the existing hidden manual queue projection.

`pdf-unit-processing-v1` is a separate private completion journal, using the
established Drive v2 If-Match transport. It does not grant grouping, AI, Medical
or archive permission. ACL checks, strong ETag, strict JSON/schema and exact
read-back are required; no missing-state initialization/local fallback exists.
The journal saves pending intent before a write and applied only after independent
accounting read-back. An ambiguous pending write is reconciliation-only, never
an automatic resend. An overlapping new grouping revision is held. Projection
failure replays the view without applying intent or accounting again.

Medical completion contains only identity and an existing backend reference,
never Medical input fields or a Medical accounting plan. A page-kind completion
reference can cover a Medical page outside the general grouping partition;
it does not change its existing Medical source/review identity. General grouping
Unit IDs use the exact existing v2 digest and stay unchanged.

The completion journal and manual service are not provisioned or wired into a
live workflow by this preparation. They need the existing production mutex,
validated main boundary, projection journal, Drive authority provider and current
owner request verifier before activation. No live state/authority or UI is
created by importing these modules.

The parent-completion component is read-only. It requires complete, disjoint page
coverage, stable source/ordinal page identities, durable completion intent and
independent accounting read-back. A Medical page can only be completed by the
Medical manual path. Sensitive automatic provenance cannot count as an automatic
normal import. One pending/unknown Unit always keeps the parent incomplete.
Even all-terminal returns only an archive candidate, with archive permission
false. Actual source movement requires its separate first-PDF canary.

## Remaining before merge/activation

* Finish owner input and durable read-back/replay for the remaining manual pages.
* Implement/inject the generic Drive-authority normal Unit intake; do not use the
  local grouping cache as authority or send original PDF bytes to Gemini.
* Reconcile already posted per-Unit intents without changing their Unit IDs.
* Address the current single-source v2 migration's whole-v1-file dependency
  before admitting unrelated new proposals. Do not invalidate existing confirmed
  intent merely because an unrelated state record is added.
  Draft 91 now checks the exact target source's complete legacy scope instead;
  its original whole-file digest remains migration provenance. Source, page-kind,
  partition, revision or confirmation changes still revoke the migrated scope.
* Fresh exact PNG privacy remains mandatory for every externally analyzed Unit.
* Keep Medical/privacy/unconfirmed Units on hold and retain partial PDFs.
* Require full CI, a single production PDF canary and verified validated-main
  before connecting the existing receipt entry point. Add no new schedule.

This preparatory change does not activate scheduled PDF processing or archive.
