# Payroll diagnostic evidence contract

This opt-in observer has no production caller, persistence or adoption API.
Production modules must not import either diagnostic module; the invariant suite
checks static imports. `shadow_eligible` is an evaluation classification only.

## Snapshot and identity

`observe_tokens` runs the unchanged PDF parser against the exact immutable token
tuple and copies result facts. HMAC binds the snapshot to extraction convention
version, full token order, page occurrence, bbox, original/normalized text and
confidence. Token locators bind each occurrence to that snapshot. Neither Python
object identity, array index alone nor text alone is used. A private evaluation
key is random by default; explicit reuse supports repeat comparison. These are
not permanent IDs across documents, extraction changes or reordered tokens.

Same text at distinct locations has distinct locators. Coincident normalized-
identical occurrences remain physically ambiguous even though locators differ.
Keep snapshots, tokens and keys in memory/private storage; never serialize their
dataclasses. Export only CandidateRecord.safe_dict(), which contains keyed IDs,
statuses, reasons and fixed evidence descriptions, not text, values or geometry.
Do not log source-bearing library exceptions with private data or paths.

## Consumption ledger

Every successful parser item is included, even if its scalar value is None.
Exactly one same-page raw_value occurrence establishes use. Multiple matches
establish possible use only. Missing/ambiguous correspondence makes the ledger
incomplete; absence from that ledger never means unused. Distinct unused requires
a complete ledger for all successful results. Other review labels competing for
the candidate are checked separately; lack of successful consumption does not
prove semantic ownership. No production algorithm is replayed to guess a token.

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
