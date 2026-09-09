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
low-confidence, and multiple-numeric fixtures fail closed. This was a
design-stage grade **A** concept, conditional on real safe-positive evidence.
The follow-up evidence gate below did not satisfy that condition.

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

## Boundary real-evidence follow-up

The same nine files and ten isolated receipt units were evaluated read-only in
two separate pinned RapidOCR worker processes per unit. Seven units were complete
in both materializations; no input failed. The v2 observer records each gate
stage and fixed veto category without retaining source values.

- raw boundary mismatch observations: 0
- separator observations: 0
- adjacent-pair observations: 0
- exact reconstructions: 0
- high-confidence reconstructions: 0
- unique-numeric gate passes: 0
- STRONG-relation gate passes: 0
- competitor-free gate passes: 0
- pre-stability positives: 0
- materialization-stable safe positives: 0
- all individual veto categories: 0, because no raw boundary observation entered
  the gate

No additional designated `medical-eval` corpus was available locally. Synthetic
success therefore cannot substitute for absent real evidence. Boundary
reconstruction is grade **C** for this evidence phase: do not promote it; retain
the observer only if further real collection is expected, otherwise consider
removing the unused complexity.

## Selection

Boundary reconstruction is not a production-candidate after the real-evidence
gate (grade C). Geometry reconstruction remains shadow-only (grade B).
Unsupported coherent forms remain deferred rather than added to an allowlist
(grade C). Production changes in this checkpoint: zero.
