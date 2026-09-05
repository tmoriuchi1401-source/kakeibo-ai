# Medical receipt safety boundary

This phase tightens safety only. It does not add OCR engines, layout detection,
provider rules, fuzzy label matching, digit repair, or medical persistence.

## Receipt AI boundary

All receipt media submitted through GeminiAI.analyze_receipt pass
require_receipt_ai_permission before prompt construction, encoding, or transport.
The exact immutable bytes are checked on every allowed invocation. Callers cannot
supply a prior gate result or an allow flag. OCR failures and policy failures raise
the fixed, data-free ReceiptPrivacyBlocked error.
The final boundary also strictly revalidates the gate result, including internal
diagnostics. Inconsistent normal results, non-boolean truthy flags, unknown
classifications, and validation errors never authorize transport.

ReceiptPipeline retains its earlier gate, so medical documents still return
privacy_blocked without Sheets writes. The adapter gate deliberately rechecks
normal documents; this currently costs an extra local OCR pass in pipeline use.
No successful permission is cached. CLI analyze, receipt, and drive-receipts use
the same adapter boundary; normal receipt response fields are unchanged.

known_source_classification is an optional keyword on the adapter, pipeline,
privacy evaluator, and Drive receipt reader. The CLI equivalent is
--source-classification medical (also payroll or sensitive_unknown).
It only restricts processing. A declared normal value never overrides OCR.
Known sensitive sources remain blocked even when OCR text looks normal.

A pipeline instance retains restrictive classification for each source ID across
retries, including changed bytes. An AI adapter retains denial fingerprints for
exact bytes across retries. These are private, in-memory restrictions, not cached
permissions. There is no disk-backed source registry in this phase. Neither the
registry nor a caller classification is needed to trigger per-call validation:
new adapter instances recheck actual bytes and reject medical, sensitive,
insufficient, empty, and failed local results without any caller hint. No prior
normal result is reused, even for the same bytes. Carrying known source metadata
across instances remains an additional deny safeguard, never an allow authority.

Only immutable bytes are accepted. bytearray and memoryview inputs are rejected
before validation or encoding. A caller needing a mutable buffer can create an
immutable bytes snapshot; that same object is checked and encoded, so later
changes to the original buffer cannot replace the validated media.

The gate still relies on local OCR classification for sources without known
provenance; this change does not claim perfect OCR sensitivity detection. If
medical information is lost in OCR and the remaining text is falsely classified
as a consistent normal receipt, this boundary alone cannot recover the lost
ground truth or the history from a previous process. The tested guarantee is
mandatory, fail-closed validation, not zero false negatives for arbitrary images.
Future external AI receipt adapters must use the same mandatory boundary and
transport-spy regression tests. Product classification for Amazon is a separate
API and must not be repurposed to submit receipt bytes or medical OCR text.

## Internal evidence contract

medical_payment_evidence contains frozen, transient evidence:
- NumericObservation retains valid/low-confidence/malformed state and a local
  ordinal; rejected numbers and raw strings are not retained.
- PaymentRegionEvidence retains channel, local page/line provenance, proposal
  candidates, observation states, and fixed diagnostic codes.
- PaymentEvidence retains channel completeness and the legacy proposal result.

Candidates are proposals, not confirmed totals. Numeric-looking rejected
observations (including dot formats, low scores, and unreadable currency-marked
values) do not become candidates.

Scope is explicit:
- payment_region: existing supported payment-label context;
- possible_payment_region: unresolved payment context, used only as a veto;
- excluded: explicit non-payment context such as points, insurer amounts, tax;
- unassigned: no observed payment context.

The current region boundary is the existing text/OCR line. No row/column/cell
detector is introduced. All relevant payment hypotheses may affect the document
total; unrelated excluded or unassigned rows are not automatically global vetoes.
Invalid adapter output and lost channels, whose scope cannot be recovered, are
observation_incomplete.

Cross-channel correspondence requires unique, exactly equal normalized line
content. Matching amount and label role alone is insufficient. Normalized text
exists only inside collection; evidence retains an ordinal correspondence ID.
A text proposal without a corresponding structured proposal is unresolved when
structured observations exist. Text-only embedded-text inputs remain supported.
No fabricated empty channel is required for genuinely text-only processing.

Both OCR calls share one observation_group. Corresponding proposals are
deduplicated, never counted as independent votes or combined confidence.
A low-confidence/malformed numeric observation in a payment region, incompatible
vertical geometry, or a competing possible-payment region vetoes confirmation
from either channel. The numeric resolver never picks the largest/nearest value
or computes a missing total.

The legacy resolver remains a candidate-only compatibility helper.
Production confirmation goes through collect_payment_evidence and
resolve_payment_evidence. The legacy result is an additional upper bound:
a legacy needs_review cannot become confirmed in this phase. Strong label
allowlists and their existing thresholds are unchanged.

## Diagnostic compatibility

Internal diagnostic_codes contains only:
- payment_label_not_observed
- amount_not_observed
- amount_observation_low_confidence
- ambiguous_numeric_observations
- structural_relationship_unresolved
- conflicting_payment_candidates
- observation_incomplete

Resolution, preview, and privacy gate results expose the attribute internally.
It is excluded from repr and ordinary model serialization to preserve public
result shapes. Existing external payment reasons remain unchanged; the source
provenance restriction adds known_sensitive_source. Confirmed medical models
cannot contain unresolved diagnostic codes.

Do not log raw input, evidence internals, malformed numbers, source IDs, denial
fingerprints, OCR exceptions, or derived images. Shadow evaluation should export
only allowlisted aggregate counts/reasons through a separately reviewed boundary.

## Next phase boundary

Synthetic shadow evaluation can use this evidence model without changing
production confirmation. Real-document evaluation still requires safely
materialized local originals. No model, dataset, or original acquisition was
added here. A future layout mapping must demonstrate correspondence and scope,
preserve unresolved observations and source sensitivity, and remain shadow-only
until its separate adoption decision.
