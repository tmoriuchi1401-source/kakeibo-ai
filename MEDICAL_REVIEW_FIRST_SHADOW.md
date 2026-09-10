# Medical review-first shadow workflow

## Decision

Overall: **A (shadow design)**. Existing Medical production behavior already
fails closed as `needs_review` or privacy-blocked. A small, read-only wrapper can
make manual review a stable expected outcome without changing payment acceptance,
OCR correction, allowlists, selectors, writers, or external-AI policy.

This phase does not implement a review UI, persistence, review completion, a
write plan, or production apply.

## Existing flow

The production privacy gate locally extracts/classifies a receipt, resolves only
existing authorized payment evidence, and returns a decision-only result. Medical
documents never go to Gemini. Confirmed evidence carries an amount; unresolved
Medical evidence carries `needs_review`, no amount, a fixed reason code, a
candidate count, and private fixed diagnostic codes. Extraction failure remains
`sensitive_unknown`/blocked. The receipt production pipeline returns these states
without calling AI or Sheets.

The generic Sheets review pipeline operates on already-imported transactions. It
is not used as Medical review persistence in this phase because unresolved Medical
receipts are blocked before import and because using it would introduce a write.

## Review item and identity

`app.medical_review_workflow_shadow` accepts an existing privacy-gate result and
an existing stable receipt-unit identity. It derives `receipt_unit_ref` and
`review_item_id` with domain-separated HMAC-SHA256 and a caller-held stable key.
The raw source identity and key are transient and are not stored. The item ID does
not include OCR output, materialization, reason, or parser version, so repeated
materialization of the same unit cannot create a new identity.

The item contains only fixed codes and counts: keyed unit/item references, reason,
parser/materialization status, candidate count, ambiguity category, review status,
fixed provenance versions, and a UX requirement. It has no OCR text, patient or
facility data, amount, coordinates, candidate value, filename, URL, or write
authority.

## Reason taxonomy and UX

Existing public reason and private diagnostic codes are reused where possible:

| Existing code | Review category | Minimum review view |
| --- | --- | --- |
| `no_candidate`, `amount_not_observed` | no payment candidate | original receipt |
| `payment_label_not_observed` | unsupported label shape | original receipt |
| `conflicting_candidates`, `ambiguous_numeric_observations`, `conflicting_payment_candidates` | numeric ambiguity | payment-candidate positions |
| `structural_relationship_unresolved` | geometry ambiguity | payment-candidate positions |
| `observation_incomplete` | unstable materialization | existing OCR result |
| `ocr_or_text_extraction_failed`, `empty_text` | parser failure | parser rerun |
| `weak_candidate_only`, `amount_observation_low_confidence`, `insufficient_evidence` | insufficient evidence | automatic processing unavailable |
| otherwise | other fail-closed | automatic processing unavailable |

No free text is accepted as a reason and no new sensitive preview is generated.

## Duplicate, provenance, and authority boundaries

Reconciliation is pure and read-only. Equal identity plus equal provenance and
observation suppresses the duplicate and retains the existing item, including a
previously reviewed status. A provenance change returns `stale_provenance`; an
observation/materialization change returns `materialization_mismatch`. Both keep
the existing item and create or overwrite nothing.

A manual assertion is value-free. Binding and current provenance must match the
pending item. Successful validation produces only
`requires_existing_production_authority` with `write_authorized=false`. A future
flow must therefore be:

`reviewed assertion -> validated binding/provenance -> existing production authority -> write plan`

The review item or assertion cannot supply an amount, candidate, or write plan and
cannot bypass the parser or existing safety gate.

## Fixed prior decisions

Boundary rescue remains C and is not promoted. Coherent numeric rescue remains C,
review-only, with selector exploration ended. Geometry remains B in shadow; this
phase adds no geometry rescue or selector.
