# Medical layout shadow evaluation

## Scope and boundary

`app.medical_layout_shadow.observe_layout` is an opt-in, local Python evaluation
function. Production does not import it. It accepts one transient structured OCR
pass and explicit page dimensions; it does not acquire images, run OCR, authorize
AI, store receipts, or return a payment amount. Existing privacy gates, strong
labels, evidence collection and production confirmation remain unchanged.

Every cell hypothesis has status `unresolved` and `payment_role_unproven`, even
with a perfect geometric match. A label-like region is just nonnumeric OCR text;
it may be a name, account identifier or any other text. Geometric uniqueness is
not payment semantics. No success rate or medical auto-confirmation improvement
is claimed from synthetic validation.

## Observation model

- Caller supplies the exact OCR page coordinate frame, expected page count and
  observation completeness. Never infer page size from the last/rightmost token.
- Regions retain source ordinals, page, normalized bbox, numeric quality and
  fixed negative/unresolved flags. Raw strings and numeric values are discarded.
- Each pair on the same page is checked for row overlap, column overlap or box
  overlap. Relations store alignment and a relative gap. OCR line keys do not
  define rows. No transitive clustering, nearest-neighbor winner or amount ranking
  is performed. Relationships between numeric peers remain observable too.
- Label-like or unreadable regions paired with numeric-like regions create cell
  hypotheses. These are not verified table cells. Multiple incident pairings
  retain all alternatives and mark competing relationships.
- Exact negative-context hints (points, medical cost, insurer/public payment,
  subtotal, tax, deposit/change, partial/self payment) flag hypotheses but never
  erase amounts. Broken/split labels remain semantically unproven. These hints
  do not authorize or safely exclude a payment candidate.
- Low-confidence, malformed, unreadable, overlapping and unlocated regions stay
  unresolved. Missing pages/frames and invalid geometry mark incomplete coverage.
  Empty pages are unresolved, not assumed blank. One OCR pass is one observation
  group, including duplicates. Confidence is never combined across relations.

## Heuristic limits requiring real-document evaluation

The exploratory window uses >= 50% overlap, horizontal gap <= 8 times the smaller
token height and vertical gap <= 3 times the smaller token width. Coordinates are
aspect-corrected before measuring gaps. These dimensionless thresholds are
scale-invariant, not validated medical layout rules. They can connect unrelated
columns or miss distant cells. Unlinked numbers remain explicit unassigned
regions. Deskew, rotation, ruling-line detection, merged cells, split-token
reconstruction and column semantics are not implemented. There are no provider
rules. A page coverage flag cannot prove that OCR saw every printed region.

Invalid API objects and inputs above 512 regions or outside 1..32 expected pages
return a fixed incomplete diagnostic; there is no silent successful truncation.
Pairwise graph construction is bounded, with linear hypothesis degree counting.

## Privacy and validation

Results are transient private data. Do not serialize internal dataclasses or
log input objects/exceptions. `aggregate()` is the explicit output allowlist:
integer region/relation/quality/ambiguity counts only, with no amounts, text,
source identifiers, per-region coordinates or original paths. The observer has
no network, filesystem or logging side effects.

Synthetic tests cover garbled labels, horizontal/vertical arrangements, separated
columns, scale/margin changes, search boundaries, token permutation, changing
amount magnitudes, nontransitive row relations, multiple pages, duplicate OCR,
partial-payment and negative hints, unreadable/malformed/low-confidence numbers,
invalid geometry/coverage and production isolation. All hypotheses stay review.

Real originals have not been evaluated in this phase. Production adoption is
prohibited on synthetic evidence alone. The next useful evaluation must pair
geometry coverage with existing production evidence without allowing either to
overwrite the other's unresolved observations, then measure coverage and false
associations against safely available local originals and human ground truth.

## Offline comparison boundary

`app.medical_layout_evaluation.evaluate_medical_layout` accepts transient text,
structured tokens, explicit frames and completeness for the same OCR observation.
It separately runs the unchanged production evidence resolver and the shadow
observer, returning only a fixed integer counter schema. No resolved amount or
source content leaves this evaluation function. Production region counts are
channel views, not independent signals. It does not infer a join from equal
amounts or ordinals, combine confidence, or feed shadow output to production.

`evaluation_failed` distinguishes incomplete/malformed evaluation from a valid
baseline needs_review. Incomplete geometry retains available shadow aggregate
counts, but production counters remain zero (not evaluated). Exceptions discard
all partial counters and expose no exception text. Consumers must check the
failure flag before interpreting or accumulating production outcomes.

This is a callable offline evaluation boundary, not a new receipt CLI or original
acquisition path. Existing production OCR extraction does not provide explicit
page frames; do not guess them. The separate local handoff below supplies actual
renderer dimensions and page traversal coverage for its own evaluation pass.

## Local bytes evaluation handoff

`app.medical_layout_local.evaluate_local_medical_bytes(content, mime_type)` is an
opt-in evaluation entry point for already safely acquired immutable bytes. It
does not accept URLs or paths, fetch originals, classify for external AI, save
results, or return text/tokens/amounts. It adds fixed local failure flags to the
existing counter schema. No production module calls it.

PNG/JPEG inputs use the exact loaded image dimensions. Multiple-frame images,
non-default EXIF orientation and mismatched MIME/actual formats are rejected;
there is no silent first-frame choice or orientation guess. PDFs are validated
locally, then every page is rendered at up to the existing scale of 3. Actual bitmap
dimensions describe OCR coordinates, including PDF rotation and crop boxes.
Embedded text never suppresses the rendering of later pages in this shadow path.
Encrypted/corrupt PDFs are rejected before rendering. This intentionally differs
from production PDF extraction, so baseline resolver counters describe the OCR
observations supplied here, not a replay of production PDF routing.

Both existing Tesseract APIs run on the same image and remain one observation
group. Empty text/tokens, wrong page provenance, rendering/OCR exceptions or any
failed page discard the entire document evaluation. Missing unreadable regions
that the OCR engine never reports remain a limitation; full page traversal does
not prove full visual coverage. No OCR engine or image preprocessing is added.

The evaluation input is bounded to 20 MiB, 20 million pixels per page and three
PDF pages. PDF render allocation also limits either bitmap edge to 16,384 pixels.
For PDF page coordinate frames that exceed those raster bounds at scale 3, the
shadow renderer chooses a smaller scale with rounded dimensions inside both
bounds. This is an allocation decision, never an amount or confidence heuristic.
Ordinary PDF pages keep scale 3. PNG/JPEG size and orientation rejection rules
are unchanged. Pixel allocation is checked before PDF rendering and again against
the actual image. Invalid input never yields a successful subset.
Images and renderer resources close on successful and failed observations.
Only fixed integer counters leave the entry point; exception messages are not
returned or logged. Existing Tesseract may use its normal local temporary files;
this is not a claim of an entirely RAM-only OCR subprocess. No external service
receives bytes, OCR content, metadata or counters.

Synthetic media tests use real image decoding and PDF rendering with mocked OCR,
including rotated/cropped PDFs, embedded-text bypass prevention, partial-page
failures, bounds, immutable snapshots, malformed numbers and resource cleanup.
These tests prove handoff behavior, not OCR accuracy on real medical originals.

Bounded PDF scaling tests additionally cover fractional dimensions, extreme
aspect ratios, rotation symmetry, unchanged scale for smaller pages, actual
renderer-size revalidation, low-confidence competitors, and failure on a later
page. Downsampling may lose text: complete traversal is not proof of complete
visual observation or correct payment semantics. Every shadow payment role
remains unproven; no production extraction path or resolver consumes this scale.
