# Correlated numeric OCR comparison

This shadow experiment compares numeric observations, never payment roles. It has
no production callers and returns no amount, confidence score, winner or confirmed
state. Inputs and source digests are transient; never serialize the input objects.
Only counter aggregates and anonymous evaluation states may leave the local run.
References are downstream scoring inputs, never observation or grouping inputs.

The fixed evaluation grid is PSM 6/3/11 at relative scales 0.50/0.75/1.00. PNG uses
its loaded frame; PDF uses each independently rendered, pixel-bounded frame.
Downscales use Lanczos. No crop, preprocessing search, engine addition or adaptive
reference-guided condition selection is part of this experiment.

`compare_numeric_passes` retains per-pass numeric shadow evidence. Exact replays
are deduplicated; conflicting re-observations of the same configuration are kept
and flagged. Settings use actual dimensions, so identical effective scales do not
become extra votes. Different PSM/scale settings remain correlated views of one
source and engine. Tokenization diversity is descriptive, not statistical
independence. Counts of views are not counts of independent evidence.

The caller supplies a digest of the immutable base image/source and actual page
ordinals. Groups never cross digests or pages. Coordinate normalization is valid
only for full-frame resizes, not crops, rotations or differently materialized
images. Aspect inconsistency blocks comparisons; matching aspect alone does not
prove registration. PNG/PDF correspondence needs separate image registration.

Comparable whole regions use IoU >= 0.5. Unresolved evidence uses intersection
over the smaller box >= 0.5 as well, so contained small fragments are not lost.
These are correspondence hypotheses, not cell detectors. Every numeric anchor is
compared without ranking values. Different values, malformed and low-confidence
fragments, ambiguous segmentation, configuration inconsistencies and missing
regional observations survive. Constituent fragments can still be competing
numeric interpretations; shared ordinals do not make them independent votes.
All evidence is retained in the per-pass models, including unparsed observations.

Comparison is bounded to 12 input passes and 2,048 numeric rows. Excess row counts
retain the per-pass models but return incomplete comparison coverage. Invalid
inputs fail closed without echoing content. No cross-source registration, majority
vote, semantic payment selection or production integration is provided.
