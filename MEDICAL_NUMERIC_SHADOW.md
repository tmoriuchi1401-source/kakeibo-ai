# Numeric observation shadow

This experiment concerns numeric visibility, not which number is a payment total.
Production imports no numeric shadow module. Ground truth is not an input to
`observe_numeric_fragments`, is never used for repairs, and must not be persisted
in tests, names or logs. The output retains no raw strings or numeric amounts.

All numeric-looking atoms survive, including punctuation, placeholders, malformed
values and low/invalid confidence. Malformed syntax and low confidence are separate
flags, so one does not conceal the other. NFKC and concatenation are the only
surface operations: no decimal substitution, character repair or missing digits.

Possible joins use same-page baseline/overlap and height-relative gaps, independent
of OCR line keys. They cannot skip intervening tokens. Joining is bounded to six
members and 1,024 alternative spans; bounds/invalid coverage are explicit incomplete
observations. A bounding box cannot certify a common table cell. Digit-to-digit joins (including currency-wrapped fragments),
branching adjacency and overlapping OCR observations stay ambiguous. Comma/currency
spans remain segmentation alternatives, retaining source atoms and quality flags.
Every span has payment_role_unproven; neither a span nor a high-confidence atom is
a payment candidate. All views are one OCR observation group, never independent
votes. The original source ordinals allow local reference comparison after
observation without storing the reference or its value in the observation model.

## Fixed OCR comparison protocol

Before real-document comparisons, the grid is fixed to PSM 6 (existing baseline),
PSM 3 (automatic page segmentation), and PSM 11 (sparse text), all using the existing
local Tesseract jpn+eng, same loaded image and same bounded PDF render frame. No
digit whitelist, new engine, crop, contrast/threshold tuning or reference-selected
pass is introduced. All passes are reported; none is adopted into production or
selected merely because it matches a known reference. Passes share one source
group; confidence cannot be added as though the passes were independent sensors.

Reference comparisons are a separate local evaluation operation. Report only exact
observed, reconstructable, not observed or ambiguous. Exact observed requires a
matching high-confidence, syntactically valid atom with valid geometry. A matching
span can be reconstructable only if its quality/geometry is adequate and it has no
branch, overlap, plain-digit concatenation or competing segmentation. Low-confidence
matches or unresolved spans are ambiguous. This is numeric coverage, not payment
role verification: other numeric values, excluded amounts and negative context must
remain for later geometry evaluation. No amount may be confirmed from these labels.
