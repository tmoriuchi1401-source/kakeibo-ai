# Value-free local region correspondence

Region fingerprints describe local OCR structure, not payment fields. They retain
source ordinals, normalized geometry, coarse token kinds, nearby-kind counts,
negative-context flags and unresolved quality. They retain no raw OCR strings,
numeric values, names or text hashes. Inputs stay transient and must not be logged.
No transport, image registration, provider rule or production caller is introduced.

Regions are built from all tokens, before reference scoring. Directed adjacency
uses the existing same-baseline/vertical-overlap and height-relative gap rules.
An intervening token cannot be skipped. Connected local groups contain at most six
members; larger groups retain individual tokens and explicit extent uncertainty.
Branches, invalid boxes, low confidence, malformed numbers and ambiguous numeric
spans are retained. A cluster is not a detected table cell or a payment candidate.

The fingerprint uses four token classes and a 16-counter neighboring-kind pattern.
The context window extends three token heights horizontally and two vertically.
Negative hints use existing strict negative labels, never name/provider matching.
Changing a valid numeric value alone does not change a fingerprint or an edge.

Candidate links require IoU >= 0.5 and bottom alignment within half a region height.
All competing edges survive. Partial overlap covering at least half of the smaller
box remains ambiguous correspondence rather than being reported as region absence. Only mutual single correspondences in a verified
same-source full-frame resize can be described as stable_region, bbox_variant or
tokenization_variant. Context-counter distance above four, branches and uncertain
coordinates remain ambiguous. Stable geometry uses IoU >= 0.9. Missing means no
corresponding OCR region under these fixed constraints, not proof of absent ink.
Low-confidence agreement does not repair or clear the underlying numeric evidence.

Different source images need explicit pairing and remain ambiguous_correspondence
until a separate coordinate relationship is verified. Coincident normalized boxes,
similar patterns or the same numeric value do not establish PNG/PDF registration.
Actual page ordinals isolate pages of the same source. Aspect inconsistency also
blocks correspondence. Matching aspect alone does not certify a resize or crop.

The local evaluation reuses the fixed PSM 6/3/11 and scale 0.50/0.75/1.00 grid.
It freezes all within-source links before reference-region queries. Numeric read
differences then annotate links locally as numeric_read_mismatch; they never alter
edge construction. All 36 unordered view pairs per target are counted. Counts are
pairwise region observations, not unique physical cells or independent evidence.
PNG/PDF comparisons use the nine same-configuration pairs as unregistered hypotheses.

Reference amounts alone are not spatial ground truth. Every region containing a
matching observation is reported as a reference-containing hypothesis, without
choosing a payment-field bbox. Spatial correspondence accuracy remains indeterminate
without independent region annotation. All logs and reports use anonymous counters.
Exact pass replays are deduplicated; different OCR views remain correlated.
