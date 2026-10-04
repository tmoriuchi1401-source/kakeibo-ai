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
* Fresh exact PNG privacy remains mandatory for every externally analyzed Unit.
* Keep Medical/privacy/unconfirmed Units on hold and retain partial PDFs.
* Require full CI, a single production PDF canary and verified validated-main
  before connecting the existing receipt entry point. Add no new schedule.

This preparatory change does not activate scheduled PDF processing or archive.
