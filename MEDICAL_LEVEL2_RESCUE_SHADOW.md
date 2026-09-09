# Medical Level 2 rescue shadow comparison

This checkpoint compares three failure-specific rescue ideas. It does not
change production payment rules, allowlists, OCR correction, privacy gates, or
acceptance behavior. The implementation is isolated in
`medical_payment_rescue_shadow` and returns fixed anonymous counters only.

## Strategy A: boundary / segment reconstruction

The positive prototype accepts only either:

- one high-confidence OCR region containing an explicit separator whose removal
  produces an exact existing strong label; or
- two high-confidence, immediately adjacent regions whose ordered concatenation
  produces an exact existing strong label.

It performs no substitutions, insertions, deletions, edit-distance matching, or
new-label recognition. A safe-positive observation additionally requires one
reconstruction, one whole high-confidence numeric observation in the complete
unit, one existing STRONG relation, no numeric competitor, and zero existing
Level 2 candidates. The result contains no label or amount.

Synthetic positives pass, while reversed, non-exact, intervening-token,
low-confidence, and multiple-numeric fixtures fail closed. This is grade **A**
as a narrowly bounded next-phase production-candidate concept. Promotion is not
part of this checkpoint, and real safe-positive evidence is still required.

## Strategy B: layout / geometry reconstruction

The geometry prototype observes two high-confidence fragments when their ordered
concatenation is an exact existing strong label and their relationship fits an
existing STRONG or UNCERTAIN anonymous geometry bucket. This includes vertical
fragmentation. It never treats UNCERTAIN as positive evidence and never emits a
candidate. Multiple possible pairings set an over-merge risk counter; an
intervening region prevents reconstruction.

Synthetic row and column positives demonstrate feasibility, and ambiguous pair,
intervening-token, unrelated-fragment, and uncertain-number fixtures demonstrate
the principal failure modes. This is grade **B**: continue shadow observation.
Native OCR block/line identity or a comparably strong constraint is needed before
this can be considered for production.

## Strategy C: unsupported but coherent form

The coherent-form observer records only a high-confidence, contiguous,
non-allowlisted payment-like surface. Cross-materialization stability requires
exact NFKC equality; near matches are conflicts, not fuzzy matches. Numeric
relations are counted but never confer authority. No allowlist entry is added.

The real read-only run found coherent unsupported observations near strong
numeric relations, but every such observation was blocked by multiple numeric
observations. That makes proximity insufficient to establish the payment role.
This is grade **C**: do not promote; the false-positive risk outweighs the
observed rescue value.

## Anonymous real-document shadow result

The local pinned RapidOCR worker evaluated nine files as ten isolated receipt
units. Seven units were complete; no input failed. The existing Level 2 shadow
candidate count remained zero for all ten units.

- boundary exact reconstructions: 0; safe positives: 0
- geometry exact reconstructions: 0; over-merge positives: 0
- coherent unsupported observations: 14
- coherent observations with a STRONG numeric relation: 6
- coherent observations vetoed by numeric ambiguity: 14

The run used local files read-only, emitted only anonymous aggregates, performed
no external AI communication, and made no Drive or Sheets writes. Raw OCR text,
amounts, coordinates, source identifiers, and filenames were neither returned
nor persisted.

## Selection

Boundary reconstruction is the sole next-phase production-candidate concept,
subject to the exact conditions above and new real safe-positive evidence.
Geometry reconstruction remains shadow-only. Unsupported coherent forms are
deferred rather than added to an allowlist. Production changes in this
checkpoint: zero.
