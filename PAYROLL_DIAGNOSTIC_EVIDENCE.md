# Payroll diagnostic evidence contract

This opt-in observer has no production caller, persistence or adoption API.
Production modules must not import diagnostic modules; the invariant suite
checks static imports. `shadow_eligible` is an evaluation classification only.

## Snapshot and identity

`observe_tokens` runs the unchanged PDF parser against the exact immutable token
tuple and copies result facts. HMAC binds the snapshot to extraction convention
version, full token order, page occurrence, bbox, original/normalized text and
confidence. Token locators bind each occurrence to that snapshot. Neither Python
object identity, array index alone nor text alone is used. A private evaluation
key is random by default; explicit reuse supports repeat comparison. These are
not permanent IDs across documents, extraction changes or reordered tokens.

## Extraction-path snapshot contract

`payroll_extraction_path_diagnostics` is a local, read-only observer for one
caller-supplied PDF materialization. It reports keyed source identity, byte
length, page count, and anonymous counts for the embedded-text/visitor and
local-OCR paths. It never exports source text, OCR text, coordinates, paths, or
the local key.

An ownership-eligible diagnostic snapshot requires the exact same PDF bytes,
complete page scope, deterministic repeated extraction, physical occurrence
provenance, and an extraction mode that is demonstrably the parser input mode.
For embedded text, matching pypdf visitor occurrences provide this contract.
`payroll_ocr_snapshot_bridge` adds a diagnostic-only OCR contract. It binds a
private snapshot to an HMAC of the exact PDF bytes, page count, renderer and
actual raster dimensions, resize decision, OCR engine/version/language/config,
extraction version, and a repeated-run identity. The byte HMAC and all token
objects remain in memory; its safe report has only opaque IDs and counts.

The bridge reproduces the token-producing portion of the local OCR path and
then compares its complete token tuple with a new call to the unchanged
production extractor. It invokes the unchanged parser with `ocr=True` only
after that equality check. Thus an OCR parser-mode fact is an observer fact for
the same token universe, never a claim that OCR tokens are PDF visitor tokens
or that they are production success/adoption evidence beyond this diagnostic
scope.

OCR token locators are snapshot-scoped HMACs over page occurrence, pixel bbox,
original and NFC-normalized representation, confidence, and the OCR
block/paragraph/line/word provenance. Array position is not identity. Same
text at distinct physical locations stays distinct; an otherwise
indistinguishable duplicate remains explicitly ambiguous.

The current renderer transform is verified only for unrotated, MediaBox-equal
CropBox, UserUnit=1 pages whose actual bitmap dimensions bind the recorded
PDFium scale. It maps the OCR image's top-left `left/top/width/height` envelope
back to canonical PDF top-left coordinates and records one-pixel-equivalent
uncertainty. Rotation, CropBox divergence, UserUnit scaling, incomplete pages,
or a dimension mismatch are not normalized by guesswork and remain
unknown/unsupported. Existing visitor provenance is never reused for OCR.

An OCR snapshot is ownership-ready only when all byte/materialization, page
scope, rerun, physical identity (unique or explicit ambiguity), coordinate,
and parser-mode checks are complete. An ownership aggregate is not produced
when any check is incomplete. This is diagnostic-only; it neither changes nor
authorizes production OCR policy or adoption.
`minimum_pdf_text=0` forces the PDF-text branch even for image-only PDFs; it is
appropriate for synthetic visitor tests, not proof that production's OCR
fallback was attempted.

Same text at distinct locations has distinct locators. Coincident normalized-
identical occurrences remain physically ambiguous even though locators differ.
Keep snapshots, tokens and keys in memory/private storage; never serialize their
dataclasses. Export only CandidateRecord.safe_dict(), which contains keyed IDs,
statuses, reasons and fixed evidence descriptions, not text, values or geometry.
Do not log source-bearing library exceptions with private data or paths.

## Consumption provenance

Every successful parser item is included, even if its scalar value is None.
`payroll_ownership_provenance` performs a diagnostic-only counterfactual replay
of the unchanged parser: it removes one candidate physical token at a time and
recognizes consumption only when exactly one removal makes the same parser fact
disappear. This is observation of the parser's decision, not a replacement
pairing algorithm.

`definitely_used` requires a unique snapshot identity and unique necessary-token
counterfactual. `definitely_unused` requires the same extraction snapshot,
complete page scope and success enumeration, a complete mapping for every
successful pair, unique physical identity, physical exclusion from all consumed
tokens, and no fallback-claim competition. It means only “not consumed by a
production successful pair”; it does not approve a fallback, semantics, column,
geometry, numeric validity, or adoption. Missing/ambiguous correspondence makes
the ledger incomplete; absence from that ledger never means unused. Coincident
duplicates remain `ownership_ambiguous`; stale/materialization-mismatched input
and incomplete mappings remain `ledger_incomplete`; two fallback labels claiming
one otherwise-unused physical token are `candidate_competed`.

This ledger describes the unchanged parse_positioned_items output for this
snapshot, not Sheets, reconciliation, or a different preview's post-processing.
Those scopes need explicit provenance before claiming complete consumption.

## Independent structure and coordinate evidence

PositionedText currently has text and estimated boxes, not ruling paths, table
provenance or cm. Parser row/column indices are coordinate ranks, not cell
boundaries. Its repeated-row heuristic is not arbitrary fallback-column proof.

BoundaryEvidence accepts independently established column/section intersection
regions from synthetic structure, operator annotations or observed rules. They
must use the same raw tm/top-left frame. Expected region is selected using only
the label, never the candidate. Sources are explicit trusted input contracts;
the observer does not automatically discover or authenticate those annotations.
Missing/overlapping regions, boundary crossings and uncertain membership never
become known columns. A finite edge-error bound must support containment; zero
is valid for exact synthetic geometry, not a default for real estimated widths.

inspect_pdf_frame reads local pypdf visitors without OCR/network fallback and
matches all extracted tokens to the snapshot. It records cm, text axes, page
rotation and UserUnit. A cm alone is insufficient. Common identity/translation
with identity text axes and standard page units verifies the raw comparison
frame. Positive scaling stays unknown pending calibration. Rotation/skew,
reflection, differing transforms and unsupported page transforms are unsupported.
This does not verify glyph widths or production's absolute-threshold invariance.
Without a verified frame, column/geometry gates stay unknown.

## Gate record

Scope, identity, ownership, coordinates, column/section, geometry, numeric policy
and semantics each have pass/fail/unknown, reason_code and evidence. Numeric-looking
competitors are retained even when their money grammar fails; no repair occurs.
The geometry window remains an observer hypothesis, not an approved production
threshold. A fail yields reject; otherwise any unknown yields unresolved. All
pass yields shadow_eligible, with no authoritative value or mutation capability.

Unresolved standard mapping remains unresolved even with operator confirmation.
Mapped items still require explicit snapshot-bound confirmation of relation AND
role. Geometry and ownership alone never pass semantics. No ground truth is
inferred by this layer. Caller-supplied confirmations must come from actual review.

## Proposed local ground truth (not implemented)

Prefer an access-restricted manifest outside Git containing only opaque keyed
snapshot/label/candidate IDs, decision, relation/role confirmation, annotation
version and an opaque actor reference. Review the original source locally. Keep
the source locator and key in separate private storage. Never commit source PDFs,
text, amounts, names or labels. Re-extraction invalidates annotations and requires
review. The UI must bind source snapshot, exact relation and role before approval.

Coordinate-only records expose layout and are transform-fragile. Unkeyed hashes
of labels/amounts permit dictionary attacks. Random IDs need a private lookup.
Keyed snapshot IDs support repeat checks but still require key protection,
access control and retention limits. No identifier scheme proves operator truth.

Synthetic tests prove collision handling, conservative abstention and unchanged
parser results. Real table-boundary evidence, font-width uncertainty calibration
and verified semantic relations remain necessary. A real read-only evaluation
may collect unresolved reasons; it must not substitute synthetic evidence bounds.
