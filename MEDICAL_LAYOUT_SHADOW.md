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
