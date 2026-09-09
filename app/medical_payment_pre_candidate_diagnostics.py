"""Anonymous pre-candidate observations for the medical Level 2 shadow.

This module is a read-only observer.  It cannot create candidates, change OCR,
or authorize production behavior.  Raw text, coordinates and numeric values are
used transiently and are never retained in the returned fixed-schema result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import unicodedata
from typing import Literal

from .medical_ocr_observation_shadow import OcrObservation, TextRegion
from .medical_payment_evidence import _NUMERIC_RUN, _POSSIBLE_PAYMENT_CONTEXT
from .medical_payment_level2_shadow import (
    _MIN_HIGH_CONFIDENCE,
    _blocking_relation,
    _is_negative,
    _locally_related,
    _whole_numeric,
    classify_structural_relation,
)
from .medical_receipt_privacy import (
    _LABEL_RULES,
    _compact_ocr_token,
    _exact_strong_structured_label_match,
)


SCHEMA_VERSION = "medical-level2-pre-candidate-diagnostics-v1"
_STRONG_LABELS = tuple(
    dict.fromkeys(label for label, _, strength, _ in _LABEL_RULES if strength == "strong")
)
_STRONG_LABELS = _STRONG_LABELS + ("支払額",)
_KNOWN_INCOMPLETE = {
    "blank_text",
    "recognition_missing",
    "invalid_geometry",
    "invalid_confidence",
}

RelationState = Literal["STRONG", "UNCERTAIN", "UNRELATED"]
AxisBucket = Literal["ROW_RIGHT", "COLUMN_BELOW", "NONE"]
DistanceBucket = Literal["NEAR", "WITHIN_STRONG", "UNCERTAIN_BAND", "UNRELATED"]
AlignmentBucket = Literal["HIGH", "ADEQUATE", "PARTIAL", "NONE"]


@dataclass(frozen=True)
class PaymentLabelObservation:
    exact_match_count: int = 0
    partial_or_fragment_match_count: int = 0
    low_confidence_match_count: int = 0
    normalization_near_match_count: int = 0
    unsupported_label_shape_count: int = 0


@dataclass(frozen=True)
class RelationBucketCount:
    state: RelationState
    axis: AxisBucket
    distance: DistanceBucket
    alignment: AlignmentBucket
    count: int


@dataclass(frozen=True)
class LabelToNumberStructuralObservation:
    numeric_observation_count: int = 0
    strong_relation_count: int = 0
    uncertain_relation_count: int = 0
    unrelated_relation_count: int = 0
    buckets: tuple[RelationBucketCount, ...] = ()


@dataclass(frozen=True)
class NumericCompetitorDiagnostics:
    competitor_count: int = 0
    payment_connected_count: int = 0
    label_connected_count: int = 0
    target_connected_count: int = 0
    structurally_near_count: int = 0
    exclusion_role_count: int = 0
    ambiguous_role_count: int = 0


@dataclass(frozen=True)
class OcrIncompleteBreakdown:
    blank_region_count: int = 0
    recognition_missing_count: int = 0
    invalid_geometry_count: int = 0
    invalid_confidence_count: int = 0
    other_incomplete_count: int = 0


@dataclass(frozen=True)
class PreCandidateDiagnostics:
    schema_version: Literal["medical-level2-pre-candidate-diagnostics-v1"] = SCHEMA_VERSION
    label: PaymentLabelObservation = field(default_factory=PaymentLabelObservation)
    structure: LabelToNumberStructuralObservation = field(
        default_factory=LabelToNumberStructuralObservation
    )
    competitors: NumericCompetitorDiagnostics = field(
        default_factory=NumericCompetitorDiagnostics
    )
    incomplete: OcrIncompleteBreakdown = field(default_factory=OcrIncompleteBreakdown)
    observation_complete: bool = False
    evaluation_failed: int = 0

    def aggregate(self) -> dict[str, int | bool | str]:
        """Return only the fixed anonymous public diagnostic schema."""
        return {
            "schema_version": self.schema_version,
            "exact_match_count": self.label.exact_match_count,
            "partial_or_fragment_match_count": self.label.partial_or_fragment_match_count,
            "low_confidence_match_count": self.label.low_confidence_match_count,
            "normalization_near_match_count": self.label.normalization_near_match_count,
            "unsupported_label_shape_count": self.label.unsupported_label_shape_count,
            "numeric_observation_count": self.structure.numeric_observation_count,
            "strong_relation_count": self.structure.strong_relation_count,
            "uncertain_relation_count": self.structure.uncertain_relation_count,
            "unrelated_relation_count": self.structure.unrelated_relation_count,
            "competitor_count": self.competitors.competitor_count,
            "payment_connected_count": self.competitors.payment_connected_count,
            "label_connected_count": self.competitors.label_connected_count,
            "target_connected_count": self.competitors.target_connected_count,
            "structurally_near_count": self.competitors.structurally_near_count,
            "exclusion_role_count": self.competitors.exclusion_role_count,
            "ambiguous_role_count": self.competitors.ambiguous_role_count,
            "blank_region_count": self.incomplete.blank_region_count,
            "recognition_missing_count": self.incomplete.recognition_missing_count,
            "invalid_geometry_count": self.incomplete.invalid_geometry_count,
            "invalid_confidence_count": self.incomplete.invalid_confidence_count,
            "other_incomplete_count": self.incomplete.other_incomplete_count,
            "observation_complete": self.observation_complete,
            "evaluation_failed": self.evaluation_failed,
        }


@dataclass(frozen=True)
class _LabelShape:
    region: TextRegion = field(repr=False)
    exact: bool = False
    partial: bool = False
    low_confidence: bool = False
    normalization_near: bool = False
    unsupported: bool = False


@dataclass(frozen=True)
class _NumericRelation:
    region: TextRegion = field(repr=False)
    state: RelationState = "UNRELATED"
    axis: AxisBucket = "NONE"
    distance: DistanceBucket = "UNRELATED"
    alignment: AlignmentBucket = "NONE"


def _label_shape(region: TextRegion) -> _LabelShape | None:
    normalized = unicodedata.normalize("NFKC", region.text).strip()
    compact = _compact_ocr_token(region.text)
    if not compact:
        return None
    exact_match = _exact_strong_structured_label_match(region.text) is not None
    high_confidence = (
        region.confidence is not None and region.confidence >= _MIN_HIGH_CONFIDENCE
    )
    partial = not exact_match and any(
        len(compact) >= 2 and compact != label and compact in label
        for label in _STRONG_LABELS
    )
    normalization_near = (exact_match or partial) and normalized != compact
    label_vocabulary = any(
        signal in compact for signal in _POSSIBLE_PAYMENT_CONTEXT
    )
    unsupported = label_vocabulary and not exact_match and not partial
    if not (exact_match or partial or unsupported):
        return None
    return _LabelShape(
        region=region,
        exact=exact_match and high_confidence,
        partial=partial,
        low_confidence=exact_match and not high_confidence,
        normalization_near=normalization_near,
        unsupported=unsupported,
    )


def _distance_bucket(state: RelationState, axis: str | None, gap: float | None) -> DistanceBucket:
    if state == "UNRELATED" or gap is None:
        return "UNRELATED"
    if gap <= 1.0:
        return "NEAR"
    if state == "STRONG":
        return "WITHIN_STRONG"
    return "UNCERTAIN_BAND"


def _alignment_bucket(value: float | None) -> AlignmentBucket:
    if value is None:
        return "NONE"
    if value >= 0.80:
        return "HIGH"
    if value >= 0.60:
        return "ADEQUATE"
    if value >= 0.40:
        return "PARTIAL"
    return "NONE"


def _best_relation(labels: list[_LabelShape], number: TextRegion) -> _NumericRelation:
    order = {"UNRELATED": 0, "UNCERTAIN": 1, "STRONG": 2}
    best = None
    for label in labels:
        relation = classify_structural_relation(label.region, number)
        if best is None or order[relation.state] > order[best.state]:
            best = relation
    if best is None or best.state == "UNRELATED":
        return _NumericRelation(number)
    axis: AxisBucket = "ROW_RIGHT" if best.axis == "row_right" else "COLUMN_BELOW"
    return _NumericRelation(
        number,
        best.state,
        axis,
        _distance_bucket(best.state, best.axis, best.gap_ratio),
        _alignment_bucket(best.alignment),
    )


def _incomplete(observation: OcrObservation) -> OcrIncompleteBreakdown:
    issues = [issue for region in observation.regions for issue in region.issues]
    other = sum(issue not in _KNOWN_INCOMPLETE and issue != "low_confidence" for issue in issues)
    other += len(observation.issues)
    if not observation.regions and not observation.issues:
        other += 1
    return OcrIncompleteBreakdown(
        blank_region_count=sum("blank_text" in region.issues for region in observation.regions),
        recognition_missing_count=sum(
            "recognition_missing" in region.issues for region in observation.regions
        ),
        invalid_geometry_count=sum(
            "invalid_geometry" in region.issues for region in observation.regions
        ),
        invalid_confidence_count=sum(
            "invalid_confidence" in region.issues for region in observation.regions
        ),
        other_incomplete_count=other,
    )


def observe_pre_candidate_diagnostics(observation: OcrObservation) -> PreCandidateDiagnostics:
    """Observe one OCR materialization without returning text, values or geometry."""
    if type(observation) is not OcrObservation:
        return PreCandidateDiagnostics(
            incomplete=OcrIncompleteBreakdown(other_incomplete_count=1),
            evaluation_failed=1,
        )
    try:
        labels = [shape for region in observation.regions if (shape := _label_shape(region))]
        # Exact high-confidence labels explain the existing Level 2 path when
        # present. Weaker shapes are fallback diagnostic anchors only when no
        # exact observation exists; they never acquire candidate authority.
        exact_labels = [item for item in labels if item.exact]
        structural_anchors = exact_labels or labels
        label_summary = PaymentLabelObservation(
            exact_match_count=sum(item.exact for item in labels),
            partial_or_fragment_match_count=sum(item.partial for item in labels),
            low_confidence_match_count=sum(item.low_confidence for item in labels),
            normalization_near_match_count=sum(item.normalization_near for item in labels),
            unsupported_label_shape_count=sum(item.unsupported for item in labels),
        )

        # Observe the same bounded numeric-run surface that can participate in
        # a pre-candidate veto. This includes whole numeric tokens and mixed
        # numeric-shaped regions, but retains neither parsed values nor text.
        numeric_regions = [
            region for region in observation.regions
            if _NUMERIC_RUN.search(unicodedata.normalize("NFKC", region.text))
        ]
        relations = [_best_relation(structural_anchors, region) for region in numeric_regions]
        bucket_counts: dict[tuple[str, str, str, str], int] = {}
        for relation in relations:
            key = (relation.state, relation.axis, relation.distance, relation.alignment)
            bucket_counts[key] = bucket_counts.get(key, 0) + 1
        buckets = tuple(
            RelationBucketCount(state, axis, distance, alignment, count)  # type: ignore[arg-type]
            for (state, axis, distance, alignment), count in sorted(bucket_counts.items())
        )
        structure = LabelToNumberStructuralObservation(
            numeric_observation_count=len(numeric_regions),
            strong_relation_count=sum(item.state == "STRONG" for item in relations),
            uncertain_relation_count=sum(item.state == "UNCERTAIN" for item in relations),
            unrelated_relation_count=sum(item.state == "UNRELATED" for item in relations),
            buckets=buckets,
        )

        provisional_target_indices = [
            index for index, region in enumerate(numeric_regions)
            if region.confidence is not None
            and region.confidence >= _MIN_HIGH_CONFIDENCE
            and _whole_numeric(region) is not None
            and any(
                classify_structural_relation(label.region, region).state == "STRONG"
                for label in exact_labels
            )
        ]
        target_index = (
            provisional_target_indices[0] if len(provisional_target_indices) == 1 else None
        )
        competitor_indices = [
            index for index in range(len(numeric_regions)) if index != target_index
        ]
        label_connected = {
            index for index in competitor_indices
            if relations[index].state in {"STRONG", "UNCERTAIN"}
        }
        target_connected = {
            index for index in competitor_indices
            if any(
                _blocking_relation(numeric_regions[target], numeric_regions[index]) is not None
                for target in provisional_target_indices
                if target != index
            )
        }
        payment_connected = label_connected | target_connected
        excluded = {
            index for index in competitor_indices
            if _is_negative(numeric_regions[index])
            or any(
                _is_negative(context) and _locally_related(context, numeric_regions[index])
                for context in observation.regions
                if context.ordinal != numeric_regions[index].ordinal
            )
        }
        competitors = NumericCompetitorDiagnostics(
            competitor_count=len(competitor_indices),
            payment_connected_count=len(payment_connected),
            label_connected_count=len(label_connected),
            target_connected_count=len(target_connected),
            structurally_near_count=len(payment_connected),
            exclusion_role_count=len(excluded),
            ambiguous_role_count=sum(
                index not in payment_connected and index not in excluded
                for index in competitor_indices
            ),
        )
        return PreCandidateDiagnostics(
            label=label_summary,
            structure=structure,
            competitors=competitors,
            incomplete=_incomplete(observation),
            observation_complete=observation.complete,
        )
    except Exception:
        return PreCandidateDiagnostics(
            incomplete=OcrIncompleteBreakdown(other_incomplete_count=1),
            observation_complete=False,
            evaluation_failed=1,
        )
