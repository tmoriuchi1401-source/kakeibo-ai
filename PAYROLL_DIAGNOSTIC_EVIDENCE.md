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
`payroll_ocr_snapshot_bridge` adds a diagnostic-only OCR contract for PDF and
direct PNG input. It binds a private snapshot to an HMAC of the exact source
bytes, page count, renderer and
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

For direct PNG, the source coordinate authority is the decoded native image
pixel frame (top-left origin), bound to actual image dimensions and the exact
geometry-preserving OCR preprocessing. Grayscale and contrast leave geometry
unchanged; any integer resize is recorded and reversed for the normalized
observer frame. The PNG path performs no PDF conversion, crop, rotation, or
visitor-provenance reuse. A dimension or bbox mismatch remains unknown.

The PDF renderer transform is verified only for unrotated, MediaBox-equal
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

### Ledger-incomplete reason taxonomy

`diagnose_incomplete_ownership` is an aggregate-only, read-only observer over
the unchanged parser. It removes one physical token at a time and classifies
only tokens already reported as `ledger_incomplete`. A changed parser result is
decision-dependency evidence for that snapshot; it does not by itself establish
value use, semantics, or adoption. An unchanged result is never upgraded to
unused because filtering, redundancy, and explicit parser-domain exclusion are
not distinguishable without an observation trace.

The safe report contains only fixed reason codes, counts, percentages, missing-
evidence descriptions, and mapping-blocker counts. It contains no OCR text,
coordinates, token locators, source identifiers, or filenames. Existing
ownership states are not mutated or reclassified.

### Ownership completeness conditions

Ownership completeness is scoped to adoption-relevant parser dependencies and
candidate claims; it is not equivalent to classifying every OCR token as used
or unused. A future completeness assertion requires all of the following:

1. Source replay closure: exact source bytes, complete page scope, deterministic
   OCR, parser-mode equality, and a complete same-snapshot success-set replay.
2. Physical identity and coordinate closure: every relevant raw occurrence has
   unique or explicitly ambiguous identity in the verified native pixel frame.
3. Parser observation closure: each relevant raw token is traced as a logical
   label component, value, decision context, redundant input, or explicit
   parser-domain exclusion; unchanged counterfactual output alone is insufficient.
4. Logical relation closure: every successful logical field maps to all physical
   label components and its candidate value occurrence, including normalization
   and OCR label reconstruction provenance.
5. Counterfactual decision closure: necessary-token results are repeatable and
   complete for every adoption-relevant success or candidate relation. Dependencies
   are typed; label/context dependencies are not mislabeled as value consumption.
6. Relevance and competition closure: the adoption-relevant claim set is complete,
   every competing candidate is accounted for, and review/non-success relations
   stay outside adoption unless authoritative semantic/role review closes them.

Only when every relevant gate is pass/fail with no unknown may ownership be
called complete. Tokens with traced, explicit parser-domain exclusion may remain
outside the ownership denominator. No condition authorizes production adoption;
semantics, column membership, review truth, and policy remain separate gates.

### Diagnostic parser-observation instrumentation

`trace_incomplete_ownership` is an opt-in, snapshot-local observer. Production
does not import or call it, and its result has no adoption or mutation API. It
replays `parse_positioned_items` and records a physical occurrence as one of:
successful `label_component`, successful `value`, successful
`decision_context`, parser-stage `observed`, explicitly grammar-excluded, or
redundant only when both a parser-equivalent physical peer and counterfactual
invariance are present. Parser-output absence alone establishes none of these.

The production OCR label construction and parsing control flow are unchanged.
An observer-only private helper calls that existing construction and separately
derives a deterministic tuple of physical component indexes. Production neither
builds nor reads this tuple. A successful mapping closes only when one logical
fact occurrence maps to all raw label components, one physical value occurrence,
and repeatable necessary-token counterfactuals. An unmapped successful item uses
a snapshot-local fact-occurrence reference; it is not promoted to a standard
field and supplies no semantic ground truth.

The anonymous five-PNG evaluation traced all 690 ledger-incomplete occurrences.
The 592 instrumentation-resolvable occurrences closed as observation/exclusion
or typed successful dependencies, while the 98 review/non-success dependencies
remained isolated as requiring authoritative ground truth. Instrumentation-
incomplete remained zero. All four previously unclosed successful logical-label
relations obtained complete component/value/counterfactual provenance; the one
already-closed success was also reproduced. Ownership states were unchanged:
`definitely_used=1`, `definitely_unused=372`, `ledger_incomplete=690`, and no
new ambiguity or competition was introduced. These counts establish diagnostic
evidence closure only, not ownership adoption readiness.

### Candidate enumeration closure

`capture_candidate_enumeration` installs a context-local private observer around
one call to the unchanged `parse_positioned_items`. The parser emits only the
intermediate collections it already computed; the observer does not generate a
second candidate set and nothing in production reads the trace. With no observer,
the hook is a no-op and the public signature and returned `PayrollItem` values are
unchanged.

The production candidate universe is bounded by its actual generator stages. A
parser-created logical label is either explicitly excluded or paired against the
same-page value pool. The primary path records horizontal candidates before OCR
ownership filtering, then the active horizontal, OCR-below, OCR-above, or PDF
logical-row set before winner selection. PDF summary recovery is a distinct
post-primary generator. PDF label deduplication and OCR result deduplication are
post-generation reductions whose removed-to-retained relations must remain
accounted. Review and non-success candidates stay in this universe; they are not
promoted to adoption relevance.

Closure requires all of the following for one immutable snapshot:

1. The token, logical-label, value-pool, page and parser-mode scope is complete.
2. Every mode-applicable generator path emits a terminal coverage record.
3. Every label and same-page value is either in a pre-selection candidate or has
   an explicit parser exclusion such as grammar, geometry, or ownership rejection.
4. Pre-selection candidates remain recorded even when shadowed, filtered,
   ambiguous, review-only, or subsequently deduplicated.
5. Every candidate resolves to all label-component physical IDs and one value
   physical ID in the same snapshot.
6. Dedup/pruning retains an explicit removed-to-retained candidate relation.
7. Candidate identity is a deterministic digest of snapshot, generator path,
   ordered physical label components and physical value occurrence; raw text,
   list indexes, object addresses and temporary paths are not identity inputs.
8. A second observer replay reproduces paths, candidate identities, exclusions,
   reductions and production-visible result structure exactly.
9. A missing ledger candidate, omitted path, stale snapshot, incomplete physical
   lineage, or unaccounted reduction fails verification closed.

The anonymous five-PNG replay contained 20 parser candidates across 1,063 OCR
tokens and 1,039 explicit input/relation exclusions (not a token partition). All
OCR generator and dedup paths were
covered, both observer replays agreed, and ownership assessments were unchanged.
Each of the five successful mappings had exactly one candidate in its claim-local
universe and exactly one selected relation, with complete physical provenance.
The remaining three PNGs contained no successful mappings and zero generated
candidates, but their zero-candidate universes still closed through complete path
coverage and exclusions. The 98 review/non-success ground-truth dependencies were
unchanged and were not omitted from candidate accounting. This closes candidate
enumeration evidence only; it does not re-evaluate adoption readiness or resolve
field authority, review isolation, or portable logical-field identity.

### Successful-claim authority boundary

`analyze_successful_claim_authority` is a read-only three-gate diagnostic over
an ownership-ready snapshot and a closed candidate-enumeration ledger. It does
not produce readiness or adoption decisions. Field authority comes only from an
existing production contract: a parser result with `standard_item_candidate`
is an `authoritative_standard_field`. A successful parser occurrence without
that field remains `snapshot_success_only`. Production storage independently
confirms this boundary: absent an alias or standard candidate it stores no value,
marks the item uncertain, and uses the `unknown_with_value` review reason. The
diagnostic does not load aliases or infer that missing authority.

A portable production claim ID is issued only after that authority is present.
Its deterministic digest binds the immutable snapshot, parser mode, logical
standard field, ordered physical label-component IDs, physical value occurrence,
candidate generator path, and enumerated candidate ID. It excludes parser-list
occurrence, execution order, object identity, temporary path, filename, and raw
OCR text alone. A snapshot-only success receives only an anonymous diagnostic
ID and never a portable production claim ID.

Review dependencies are sibling edges, not field-authority inputs. Each necessary
physical token is typed `production_success_dependency`; when its removal also
changes review output it additionally receives `review_dependency` and
`shared_physical_evidence`. Shared evidence is permitted, but no review result or
unresolved review truth flows into the successful claim. Stale provenance,
unclosed candidate enumeration, a missing selected relation, incomplete physical
mapping, or semantic ambiguity fails the diagnostic closed.

The anonymous P1/P2 replay reproduced all five successful mappings. The standard
mapping was the positive control: existing parser/storage authority, a portable
ID, closed review-edge isolation, and `AUTHORITY_CLOSED`. The four mappings in
scope were all `snapshot_success_only`, sourced from the production
`unknown_with_value` guard. All four had closed physical/counterfactual/candidate
evidence and closed typed review isolation, including explicit shared physical
edges, but none received a portable production claim ID; all remained `BLOCKED`
pending authoritative field or alias ground truth. This adds no meaning to the
98 review/non-success ground-truth-required dependencies and changes no ownership
state. It shows that strengthening identity cannot repair missing semantic
authority: fallback successful occurrences are observations, not production
ownership adoption claims.

### Limited adoption-candidate contract (design only)

`evaluate_adoption_candidate` accepts exactly one already-closed
`SuccessfulClaimAuthorityTrace` plus independently verified source and storage
gates. It returns either an immutable `PayrollOwnershipAdoptionCandidate` or a
rejection reason. The object contains only opaque evidence references: portable
claim ID, snapshot and parser mode, employer scope, authoritative standard item
ID, authority source, physical label/value IDs, enumerated candidate ID,
generator path, closure status, scope binding, and evidence/readiness contract
versions. It has no writer, apply, storage mutation, or parser-control method.
Storage alignment is represented by a typed read-only evidence record containing
the resolved standard item ID, `uncertain` flag, value-persistability flag, and
review reason; a caller cannot satisfy the gate with an untyped success boolean.

Candidate creation requires all existing authority gates: standard field
authority, storage alignment (`uncertain=False` and not `unknown_with_value`),
source replay closure, complete physical/logical provenance, closed candidate
enumeration and competition, counterfactual closure, review isolation, portable
ID integrity, and an exact employer/snapshot/parser-mode scope. The candidate
ledger is replay-verified before acceptance; any stale or hidden relation is
rejected. The portable ID is recomputed and compared, so duplicate generation is
idempotent while a different physical occurrence, snapshot, field, or employer
cannot reuse the same scope binding. Contract and evidence versions are checked
explicitly, preventing old evidence from being treated as current.

Fallback `snapshot_success_only`, `unknown_with_value`, missing authority or
portable ID, review-authority contamination, incomplete provenance, and
unresolved competition all return no candidate. A snapshot containing the one
authoritative standard mapping alongside an unrelated fallback still produces
one candidate only for the standard mapping. The four fallback mappings and the
98 review/non-success ground-truth-required tokens therefore remain outside the
candidate set. This contract is a readiness-to-integration boundary, not an
adoption registry and not a replacement for `PayrollWritePlan`, writer, review,
duplicate, schema, or apply authority.

### Production integration architecture (connected, default disabled)

Three attachment points were compared. Attesting immediately after storage
conversion is too early because schema, duplicate, statement review, and active
standard-item gates have not yet produced their final authority. Making ownership
a writer-preflight gate is too late and risks turning optional evidence into a
second write authority or stopping an otherwise safe existing flow. The selected
design is therefore an optional read-only sidecar after an authoritative
`PayrollWritePlan` has reached `ready` status.

`attest_payroll_write_plan_ownership` accepts an unchanged ready plan, one
restricted ownership candidate, and an ephemeral typed crosswalk from the
existing parser/storage facts. It verifies the plan's employer and source
identity, exactly one matching standard-item row, the authoritative value,
non-review state, snapshot/parser/candidate scope, and every contract version.
The value is not copied into the attestation; a caller-supplied local key creates
an opaque HMAC commitment. The resulting attestation has a deterministic ID and
no write capability. Failure returns no attestation and never mutates, replaces,
or invalidates the `PayrollWritePlan`.

`apply_payroll_write_application` invokes the narrow
`payroll_ownership_integration` bridge at the selected integration point: after
its existing plan/materialization validation and before the unchanged writer
call. `ownership_attestation_enabled` defaults to `False`;
disabled mode does not consume candidate inputs, inspect the key, or call the
attestor. The matching Settings flag is
`PAYROLL_OWNERSHIP_ATTESTATION_ENABLED=false`, and no CLI or production caller
passes it through in this checkpoint. Enabling that setting alone therefore does
not activate production evaluation or any write behavior.

When a future explicitly approved caller supplies the boolean, typed requests,
and a local key, results are returned only as `ownership_evidence` sidecar
metadata. Rejections and internal observer errors do not change the plan, writer
payload, writer invocation, schema/duplicate/review gates, or apply authority.
The pure evaluation function permits synthetic ON proof without invoking writer
or apply code.

Synthetic proof covers authoritative success, fallback and `unknown_with_value`
rejection, field/value/source/snapshot/employer/parser/candidate mismatches,
stale versions, review contamination, and identical-claim idempotency. Parser,
storage candidate, review result, write plan, writer preview, and materialization
payload remain byte-for-byte or structurally identical. Production enablement,
real-data evaluation, and any adoption decision remain a separate approval
boundary.

### Read-only production shadow caller

`payroll-ownership-shadow` is the only production-data caller. Activation uses
dual explicit control: `PAYROLL_OWNERSHIP_ATTESTATION_ENABLED=true` and the
command's `--enable` flag must both be present. The environment variable alone,
or the caller flag alone, returns a disabled report before credentials, Drive,
Sheets, candidate inputs, or the HMAC key are touched. Enabled evaluation also
requires `--hmac-key-file`; the file must contain exactly one 32-byte hex key and
must resolve outside the repository. The key is held only in process memory and
is absent from reports, exception messages, plans, and payloads.

The caller reads the existing Payroll Drive folder and Sheets snapshot through
read-only repositories, builds the unchanged parser/storage/`PayrollWritePlan`
values, and creates typed attestation requests only for ownership candidates
that survive the existing evidence contract. It never imports or calls a writer
or apply function. Its report contains anonymous counts and fixed reason codes,
with no file IDs, names, values, text, geometry, token IDs, or attestations.

For each ready real-data request, immutable negative controls check employer,
parser mode, snapshot, hidden-candidate, stale-evidence, field, value, and review
contamination rejection. These controls do not alter the original plan. The
report also verifies that parser output, storage candidate, review state,
`PayrollWritePlan`, writer preview, and materialization payload remain identical
before and after shadow evaluation. PDF-text sources remain evidence-unavailable
until an equally strict production-source snapshot bridge exists; they are not
silently treated as OCR evidence.

The 2026-09-10 approved production-data shadow run read five sources (three
production PDF-text and two production OCR) and evaluated all five without a
processing failure. Existing storage produced 170 items: 63 standard mappings,
three `unknown_with_value`, and 118 review items. All five existing write plans
remained blocked. The OCR evidence contained seven ownership claims: three
authoritative standard claims and four fallback claims. No claim became an
adoption candidate because no authoritative employer scope was configured;
seven candidate evaluations returned `employer_scope_missing`. The three
PDF-text sources also remained explicitly unavailable to the OCR snapshot bridge.
Consequently the run produced zero attestations and zero false attestations;
negative controls had no real-data-ready request to exercise. The complete
synthetic negative-control suite still rejected employer, parser-mode, snapshot,
hidden-candidate, stale-evidence, field, value, and review mutations. The real
run reported an unchanged production differential and zero writer/apply calls.
This is result B: safety held, but real-data evidence is insufficient for
production adoption, so read-only shadow evaluation should continue.

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
