"""Anonymous pre-candidate observations for the medical Level 2 shadow.

This module is a read-only observer.  It cannot create candidates, change OCR,
or authorize production behavior.  Raw text, coordinates and numeric values are
used transiently and are never retained in the returned fixed-schema result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
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


SCHEMA_VERSION = "medical-level2-pre-candidate-diagnostics-v2"
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
class UnsupportedLabelShapeDiagnostics:
    observation_count: int = 0
    token_count_one: int = 0
    token_count_two: int = 0
    token_count_many: int = 0
    contiguous_count: int = 0
    fragmented_count: int = 0
    fragment_count_one: int = 0
    fragment_count_two: int = 0
    fragment_count_many: int = 0
    allowlist_length_delta_zero: int = 0
    allowlist_length_delta_one: int = 0
    allowlist_length_delta_two: int = 0
    allowlist_length_delta_large: int = 0
    prefix_only_count: int = 0
    suffix_only_count: int = 0
    interior_fragment_count: int = 0
    no_allowlist_fragment_count: int = 0
    separator_or_whitespace_split_count: int = 0
    mixed_character_class_count: int = 0
    geometry_isolated_count: int = 0
    geometry_pair_count: int = 0
    geometry_multi_fragment_count: int = 0
    geometry_unknown_count: int = 0
    neighboring_token_count_zero: int = 0
    neighboring_token_count_one: int = 0
    neighboring_token_count_many: int = 0


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
    schema_version: Literal["medical-level2-pre-candidate-diagnostics-v2"] = SCHEMA_VERSION
    label: PaymentLabelObservation = field(default_factory=PaymentLabelObservation)
    unsupported_shape: UnsupportedLabelShapeDiagnostics = field(
        default_factory=UnsupportedLabelShapeDiagnostics
    )
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
            "unsupported_shape_observation_count": self.unsupported_shape.observation_count,
            "unsupported_token_count_one": self.unsupported_shape.token_count_one,
            "unsupported_token_count_two": self.unsupported_shape.token_count_two,
            "unsupported_token_count_many": self.unsupported_shape.token_count_many,
            "unsupported_contiguous_count": self.unsupported_shape.contiguous_count,
            "unsupported_fragmented_count": self.unsupported_shape.fragmented_count,
            "unsupported_fragment_count_one": self.unsupported_shape.fragment_count_one,
            "unsupported_fragment_count_two": self.unsupported_shape.fragment_count_two,
            "unsupported_fragment_count_many": self.unsupported_shape.fragment_count_many,
            "unsupported_allowlist_length_delta_zero": (
                self.unsupported_shape.allowlist_length_delta_zero
            ),
            "unsupported_allowlist_length_delta_one": (
                self.unsupported_shape.allowlist_length_delta_one
            ),
            "unsupported_allowlist_length_delta_two": (
                self.unsupported_shape.allowlist_length_delta_two
            ),
            "unsupported_allowlist_length_delta_large": (
                self.unsupported_shape.allowlist_length_delta_large
            ),
            "unsupported_prefix_only_count": self.unsupported_shape.prefix_only_count,
            "unsupported_suffix_only_count": self.unsupported_shape.suffix_only_count,
            "unsupported_interior_fragment_count": (
                self.unsupported_shape.interior_fragment_count
            ),
            "unsupported_no_allowlist_fragment_count": (
                self.unsupported_shape.no_allowlist_fragment_count
            ),
            "unsupported_separator_or_whitespace_split_count": (
                self.unsupported_shape.separator_or_whitespace_split_count
            ),
            "unsupported_mixed_character_class_count": (
                self.unsupported_shape.mixed_character_class_count
            ),
            "unsupported_geometry_isolated_count": (
                self.unsupported_shape.geometry_isolated_count
            ),
            "unsupported_geometry_pair_count": self.unsupported_shape.geometry_pair_count,
            "unsupported_geometry_multi_fragment_count": (
                self.unsupported_shape.geometry_multi_fragment_count
            ),
            "unsupported_geometry_unknown_count": (
                self.unsupported_shape.geometry_unknown_count
            ),
            "unsupported_neighboring_token_count_zero": (
                self.unsupported_shape.neighboring_token_count_zero
            ),
            "unsupported_neighboring_token_count_one": (
                self.unsupported_shape.neighboring_token_count_one
            ),
            "unsupported_neighboring_token_count_many": (
                self.unsupported_shape.neighboring_token_count_many
            ),
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


def _shape_characters(value: str) -> str:
    """Drop only visible separators for anonymous shape measurements."""
    return "".join(
        character for character in unicodedata.normalize("NFKC", value)
        if character.isalnum()
    )


def _allowlist_fragment_position(value: str) -> str:
    if any(label.startswith(value) or value.startswith(label) for label in _STRONG_LABELS):
        return "PREFIX"
    if any(label.endswith(value) or value.endswith(label) for label in _STRONG_LABELS):
        return "SUFFIX"
    if any(value in label or label in value for label in _STRONG_LABELS):
        return "INTERIOR"
    return "NONE"


def _mixed_character_classes(value: str) -> bool:
    classes = set()
    for character in value:
        if character.isdecimal():
            classes.add("DIGIT")
        elif character.isascii() and character.isalpha():
            classes.add("ASCII_ALPHA")
        elif character.isalpha():
            classes.add("NON_ASCII_ALPHA")
    return len(classes) > 1


def _unsupported_shape_diagnostics(
    observation: OcrObservation,
    labels: list[_LabelShape],
) -> UnsupportedLabelShapeDiagnostics:
    unsupported = [item for item in labels if item.unsupported]
    counts = {
        field_name: 0
        for field_name in UnsupportedLabelShapeDiagnostics.__dataclass_fields__
    }
    counts["observation_count"] = len(unsupported)
    for item in unsupported:
        normalized = unicodedata.normalize("NFKC", item.region.text).strip()
        tokens = re.findall(r"\S+", normalized)
        fragments = [value for value in re.split(r"[\W_]+", normalized) if value]
        token_bucket = "one" if len(tokens) <= 1 else "two" if len(tokens) == 2 else "many"
        fragment_bucket = (
            "one" if len(fragments) <= 1 else "two" if len(fragments) == 2 else "many"
        )
        counts[f"token_count_{token_bucket}"] += 1
        counts[f"fragment_count_{fragment_bucket}"] += 1
        split = len(fragments) > 1
        counts["fragmented_count" if split else "contiguous_count"] += 1
        counts["separator_or_whitespace_split_count"] += int(split)

        shape = _shape_characters(normalized)
        delta = min(abs(len(shape) - len(label)) for label in _STRONG_LABELS)
        delta_bucket = "zero" if delta == 0 else "one" if delta == 1 else "two" if delta == 2 else "large"
        counts[f"allowlist_length_delta_{delta_bucket}"] += 1
        position = _allowlist_fragment_position(shape)
        position_field = {
            "PREFIX": "prefix_only_count",
            "SUFFIX": "suffix_only_count",
            "INTERIOR": "interior_fragment_count",
            "NONE": "no_allowlist_fragment_count",
        }[position]
        counts[position_field] += 1
        counts["mixed_character_class_count"] += int(_mixed_character_classes(shape))

        if item.region.bbox is None:
            counts["geometry_unknown_count"] += 1
        else:
            adjacent_labels = sum(
                other.region.ordinal != item.region.ordinal
                and _locally_related(item.region, other.region)
                for other in labels
            )
            geometry_bucket = (
                "isolated_count" if adjacent_labels == 0
                else "pair_count" if adjacent_labels == 1
                else "multi_fragment_count"
            )
            counts[f"geometry_{geometry_bucket}"] += 1

        neighbors = sum(
            region.ordinal != item.region.ordinal
            and _locally_related(item.region, region)
            for region in observation.regions
        )
        neighbor_bucket = "zero" if neighbors == 0 else "one" if neighbors == 1 else "many"
        counts[f"neighboring_token_count_{neighbor_bucket}"] += 1
    return UnsupportedLabelShapeDiagnostics(**counts)


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
            unsupported_shape=_unsupported_shape_diagnostics(observation, labels),
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
