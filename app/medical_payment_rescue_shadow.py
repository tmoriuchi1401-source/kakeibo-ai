"""Shadow-only comparison of conservative Level 2 rescue strategies.

The observer may inspect transient OCR text and geometry, but its public result
contains only fixed counters.  It never emits an amount, reconstructed label,
coordinate, source identifier, or production candidate.
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
    _safe_box,
    _whole_numeric,
    classify_structural_relation,
    evaluate_level2_payment_shadow,
)
from .medical_receipt_privacy import (
    _LABEL_RULES,
    _compact_ocr_token,
    _exact_strong_structured_label_match,
)


SCHEMA_VERSION = "medical-level2-rescue-shadow-v2"
_INCOMPLETE_ISSUES = {
    "recognition_missing", "invalid_geometry", "invalid_confidence", "blank_text"
}
_STRONG_LABEL_SURFACES = tuple(
    dict.fromkeys(
        label for label, _, strength, _ in _LABEL_RULES if strength == "strong"
    )
) + ("支払額",)


@dataclass(frozen=True)
class RescueShadowEvaluation:
    schema_version: str = SCHEMA_VERSION
    boundary_observation_count: int = 0
    boundary_separator_observation_count: int = 0
    boundary_adjacent_pair_observation_count: int = 0
    boundary_exact_reconstruction_count: int = 0
    boundary_high_confidence_count: int = 0
    boundary_unique_numeric_count: int = 0
    boundary_strong_relation_count: int = 0
    boundary_competitor_free_count: int = 0
    boundary_pre_stability_positive_count: int = 0
    boundary_safe_positive_count: int = 0
    boundary_ambiguous_reconstruction_count: int = 0
    boundary_numeric_blocker_count: int = 0
    boundary_veto_incomplete_count: int = 0
    boundary_veto_non_exact_count: int = 0
    boundary_veto_low_confidence_count: int = 0
    boundary_veto_numeric_not_unique_count: int = 0
    boundary_veto_uncertain_relation_count: int = 0
    boundary_veto_unrelated_relation_count: int = 0
    boundary_veto_competitor_count: int = 0
    boundary_veto_ambiguous_reconstruction_count: int = 0
    boundary_veto_existing_level2_candidate_count: int = 0
    boundary_veto_materialization_unverified_count: int = 0
    geometry_exact_reconstruction_count: int = 0
    geometry_strong_relation_count: int = 0
    geometry_overmerge_risk_count: int = 0
    geometry_uncertain_relation_count: int = 0
    coherent_unsupported_count: int = 0
    coherent_strong_relation_count: int = 0
    coherent_numeric_blocker_count: int = 0
    observation_complete: bool = False
    existing_level2_candidate_count: int = 0
    evaluation_failed: int = 0
    _coherent_surfaces: tuple[str, ...] = field(default=(), repr=False)
    _boundary_fingerprints: tuple[tuple[str, int, str], ...] = field(
        default=(), repr=False
    )

    def aggregate(self) -> dict[str, int | bool | str]:
        return {
            name: value
            for name, value in self.__dict__.items()
            if not name.startswith("_")
        }


@dataclass(frozen=True)
class StableCoherentEvaluation:
    schema_version: str = SCHEMA_VERSION
    evaluated_materialization_count: int = 0
    stable_exact_form_count: int = 0
    stable_strong_relation_count: int = 0
    stable_numeric_blocker_count: int = 0
    materialization_conflict_count: int = 0
    evaluation_failed: int = 0

    def aggregate(self) -> dict[str, int | str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class StableBoundaryEvaluation:
    schema_version: str = SCHEMA_VERSION
    evaluated_materialization_count: int = 0
    pre_stability_positive_count: int = 0
    safe_positive_count: int = 0
    materialization_unverified_count: int = 0
    materialization_conflict_count: int = 0
    evaluation_failed: int = 0

    def aggregate(self) -> dict[str, int | str]:
        return dict(self.__dict__)


RelationCategory = Literal[
    "SAME_LINE", "SAME_REGION", "ADJACENT_LINE", "NEARBY_REGION",
    "SEPARATED_REGION", "UNKNOWN",
]
ContextCategory = Literal[
    "PAYMENT_LIKE", "SUBTOTAL_LIKE", "TAX_LIKE", "BURDEN_INSURANCE_LIKE",
    "COUNT_POINTS_LIKE", "UNKNOWN",
]
UniquenessCategory = Literal["A", "B", "C", "D"]


@dataclass(frozen=True)
class _CoherentAmbiguityRecord:
    surface: str = field(repr=False)
    competitor_bucket: str
    relation_counts: tuple[tuple[str, int], ...]
    context_counts: tuple[tuple[str, int], ...]
    uniqueness: UniquenessCategory

    @property
    def comparison_signature(self) -> tuple[object, ...]:
        return (
            self.competitor_bucket, self.relation_counts,
            self.context_counts, self.uniqueness,
        )


@dataclass(frozen=True)
class CoherentAmbiguityEvaluation:
    schema_version: str = "medical-level2-coherent-ambiguity-v1"
    coherent_observation_count: int = 0
    competitor_count_two: int = 0
    competitor_count_three: int = 0
    competitor_count_four_plus: int = 0
    competitor_count_other: int = 0
    relation_same_line_count: int = 0
    relation_same_region_count: int = 0
    relation_adjacent_line_count: int = 0
    relation_nearby_region_count: int = 0
    relation_separated_region_count: int = 0
    relation_unknown_count: int = 0
    context_payment_like_count: int = 0
    context_subtotal_like_count: int = 0
    context_tax_like_count: int = 0
    context_burden_insurance_like_count: int = 0
    context_count_points_like_count: int = 0
    context_unknown_count: int = 0
    structural_uniqueness_a_count: int = 0
    structural_uniqueness_b_count: int = 0
    structural_uniqueness_c_count: int = 0
    structural_uniqueness_d_count: int = 0
    nearest_without_semantic_support_count: int = 0
    ordinal_without_semantic_support_count: int = 0
    extremum_without_semantic_support_count: int = 0
    same_line_ambiguity_count: int = 0
    same_region_ambiguity_count: int = 0
    same_block_identity_unavailable_count: int = 0
    observation_complete: bool = False
    evaluation_failed: int = 0
    _records: tuple[_CoherentAmbiguityRecord, ...] = field(default=(), repr=False)

    def aggregate(self) -> dict[str, int | bool | str]:
        return {
            name: value for name, value in self.__dict__.items()
            if not name.startswith("_")
        }


@dataclass(frozen=True)
class CoherentMaterializationStability:
    schema_version: str = "medical-level2-coherent-stability-v1"
    evaluated_materialization_count: int = 0
    compared_observation_count: int = 0
    stable_count: int = 0
    partially_stable_count: int = 0
    unstable_count: int = 0
    same_source_unverified_count: int = 0
    evaluation_failed: int = 0

    def aggregate(self) -> dict[str, int | str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class _Reconstruction:
    label: TextRegion = field(repr=False)
    fragment_ordinals: tuple[int, ...] = field(repr=False)
    strict_boundary: bool = False


@dataclass(frozen=True)
class _BoundaryObservation:
    surface: str = field(repr=False)
    label: TextRegion | None = field(default=None, repr=False)
    parts: tuple[TextRegion, ...] = field(default=(), repr=False)
    kind: str = "pair"


def _high_confidence(region: TextRegion) -> bool:
    return region.confidence is not None and region.confidence >= _MIN_HIGH_CONFIDENCE


def _label_vocabulary(value: str) -> bool:
    compact = _compact_ocr_token(value)
    return any(signal in compact for signal in _POSSIBLE_PAYMENT_CONTEXT)


def _separator_free(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(character for character in normalized if character.isalnum())


def _proper_strong_label_fragment(value: str) -> bool:
    compact = _separator_free(value)
    return bool(compact) and any(
        compact != label and compact in label for label in _STRONG_LABEL_SURFACES
    )


def _virtual_label(parts: tuple[TextRegion, ...], text: str) -> TextRegion | None:
    boxes = [_safe_box(part) for part in parts]
    if any(box is None for box in boxes):
        return None
    typed_boxes = [box for box in boxes if box is not None]
    x1 = min(box[0] for box in typed_boxes)
    y1 = min(box[1] for box in typed_boxes)
    x2 = max(box[0] + box[2] for box in typed_boxes)
    y2 = max(box[1] + box[3] for box in typed_boxes)
    confidence = min(part.confidence for part in parts if part.confidence is not None)
    return TextRegion(
        min(part.ordinal for part in parts), text,
        ((x1, y1), (x2, y1), (x2, y2), (x1, y2)),
        confidence, None, (), "reconstructed_shadow_label",
    )


def _pair_geometry(left: TextRegion, right: TextRegion) -> tuple[bool, bool]:
    lb, rb = _safe_box(left), _safe_box(right)
    if lb is None or rb is None:
        return False, False
    lx, ly, lw, lh = lb
    rx, ry, rw, rh = rb
    overlap = min(ly + lh, ry + rh) - max(ly, ry)
    alignment = overlap / min(lh, rh)
    gap = (rx - (lx + lw)) / max(lh, rh)
    # The strict bucket models an OCR segmentation boundary.  The broader
    # bucket is evidence for geometry reconstruction only and never authorizes.
    strict = alignment >= 0.80 and 0 <= gap <= 0.75
    broad = (
        alignment >= 0.40 and 0 <= gap <= 5.0
        or classify_structural_relation(left, right).state != "UNRELATED"
    )
    return strict, broad


def _intervening(parts: tuple[TextRegion, TextRegion], regions: tuple[TextRegion, ...]) -> bool:
    left, right = parts
    lb, rb = _safe_box(left), _safe_box(right)
    if lb is None or rb is None:
        return True
    lx, ly, lw, lh = lb
    rx, ry, rw, rh = rb
    relation = classify_structural_relation(left, right)
    for other in regions:
        if other.ordinal in {left.ordinal, right.ordinal}:
            continue
        ob = _safe_box(other)
        if ob is None:
            continue
        ox, oy, ow, oh = ob
        if relation.axis == "column_below":
            overlap = min(max(lx + lw, rx + rw), ox + ow) - max(min(lx, rx), ox)
            if overlap >= 0.40 * min(max(lw, rw), ow) and ly + lh <= oy and oy + oh <= ry:
                return True
        else:
            overlap = min(max(ly + lh, ry + rh), oy + oh) - max(min(ly, ry), oy)
            if overlap >= 0.40 * min(max(lh, rh), oh) and lx + lw <= ox and ox + ow <= rx:
                return True
    return False


def _boundary_observations(observation: OcrObservation) -> tuple[_BoundaryObservation, ...]:
    """Return strict boundary shapes before confidence or exact-match gating."""
    ordered = sorted(
        observation.regions,
        key=lambda region: ((_safe_box(region) or (0, 0, 0, 0))[1],
                            (_safe_box(region) or (0, 0, 0, 0))[0], region.ordinal),
    )
    found: list[_BoundaryObservation] = []
    for region in ordered:
        normalized = unicodedata.normalize("NFKC", region.text)
        fragments = tuple(value for value in re.split(r"[\W_]+", normalized) if value)
        reconstructed = _separator_free(normalized)
        if (
            len(fragments) >= 2
            and reconstructed != _compact_ocr_token(normalized)
            and all(_proper_strong_label_fragment(value) for value in fragments)
        ):
            label = None
            if _exact_strong_structured_label_match(reconstructed) is not None:
                label = _virtual_label((region,), reconstructed)
            found.append(_BoundaryObservation(reconstructed, label, (region,), "separator"))
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1:]:
            lb, rb = _safe_box(left), _safe_box(right)
            if lb is None or rb is None or rb[0] < lb[0]:
                continue
            strict, _ = _pair_geometry(left, right)
            if (
                not strict
                or _intervening((left, right), observation.regions)
                or not _proper_strong_label_fragment(left.text)
                or not _proper_strong_label_fragment(right.text)
            ):
                continue
            combined = _separator_free(left.text) + _separator_free(right.text)
            label = None
            if _exact_strong_structured_label_match(combined) is not None:
                label = _virtual_label((left, right), combined)
            found.append(_BoundaryObservation(combined, label, (left, right), "pair"))
    return tuple(found)


def _boundary_competitor(
    candidate: _BoundaryObservation,
    target: TextRegion,
    observation: OcrObservation,
    exact_count: int,
    existing_level2_count: int,
) -> bool:
    if exact_count != 1 or existing_level2_count:
        return True
    excluded = {part.ordinal for part in candidate.parts} | {target.ordinal}
    return any(
        region.ordinal not in excluded
        and _is_negative(region)
        and (
            candidate.label is not None
            and (_blocking_relation(candidate.label, region) is not None
                 or _blocking_relation(target, region) is not None)
        )
        for region in observation.regions
    )


def _boundary_stage_counts(
    observation: OcrObservation,
    existing_level2_count: int,
) -> tuple[dict[str, int], tuple[tuple[str, int, str], ...]]:
    raw = _boundary_observations(observation)
    exact = [item for item in raw if item.label is not None]
    high = [item for item in exact if all(_high_confidence(part) for part in item.parts)]
    numerics = _numeric_regions(observation)
    unique_numeric = (
        numerics[0]
        if len(numerics) == 1
        and _high_confidence(numerics[0])
        and _whole_numeric(numerics[0]) is not None
        else None
    )
    numeric_ready = high if unique_numeric is not None else []
    strong: list[tuple[_BoundaryObservation, TextRegion, str]] = []
    uncertain = unrelated = 0
    if unique_numeric is not None:
        for item in high:
            relation = classify_structural_relation(item.label, unique_numeric)  # type: ignore[arg-type]
            if relation.state == "STRONG" and relation.axis is not None:
                strong.append((item, unique_numeric, relation.axis))
            elif relation.state == "UNCERTAIN":
                uncertain += 1
            else:
                unrelated += 1
    competitor_free = [
        item for item in strong
        if not _boundary_competitor(
            item[0], item[1], observation, len(exact), existing_level2_count
        )
    ]
    complete = observation.complete and not any(
        set(region.issues) & _INCOMPLETE_ISSUES for region in observation.regions
    )
    pre_stable = competitor_free if complete else []
    counts = {
        "boundary_observation_count": len(raw),
        "boundary_separator_observation_count": sum(item.kind == "separator" for item in raw),
        "boundary_adjacent_pair_observation_count": sum(item.kind == "pair" for item in raw),
        "boundary_exact_reconstruction_count": len(exact),
        "boundary_high_confidence_count": len(high),
        "boundary_unique_numeric_count": len(numeric_ready),
        "boundary_strong_relation_count": len(strong),
        "boundary_competitor_free_count": len(competitor_free),
        "boundary_pre_stability_positive_count": len(pre_stable),
        "boundary_safe_positive_count": 0,
        "boundary_ambiguous_reconstruction_count": int(len(exact) > 1),
        "boundary_numeric_blocker_count": len(high) - len(numeric_ready),
        "boundary_veto_incomplete_count": len(competitor_free) if not complete else 0,
        "boundary_veto_non_exact_count": len(raw) - len(exact),
        "boundary_veto_low_confidence_count": len(exact) - len(high),
        "boundary_veto_numeric_not_unique_count": len(high) - len(numeric_ready),
        "boundary_veto_uncertain_relation_count": uncertain,
        "boundary_veto_unrelated_relation_count": unrelated,
        "boundary_veto_competitor_count": len(strong) - len(competitor_free),
        "boundary_veto_ambiguous_reconstruction_count": (
            len(strong) if len(exact) > 1 else 0
        ),
        "boundary_veto_existing_level2_candidate_count": (
            len(strong) if existing_level2_count else 0
        ),
        "boundary_veto_materialization_unverified_count": len(pre_stable),
    }
    fingerprints = tuple(
        (item.surface, _whole_numeric(target), axis)  # type: ignore[arg-type]
        for item, target, axis in pre_stable
    )
    return counts, fingerprints


def _reconstructions(observation: OcrObservation) -> tuple[_Reconstruction, ...]:
    ordered = sorted(
        (region for region in observation.regions if _high_confidence(region)),
        key=lambda region: ((_safe_box(region) or (0, 0, 0, 0))[1],
                            (_safe_box(region) or (0, 0, 0, 0))[0], region.ordinal),
    )
    found: list[_Reconstruction] = []
    for region in ordered:
        compact = _compact_ocr_token(region.text)
        reconstructed = _separator_free(region.text)
        if (
            reconstructed
            and reconstructed != compact
            and _exact_strong_structured_label_match(region.text) is None
            and _exact_strong_structured_label_match(reconstructed) is not None
        ):
            label = _virtual_label((region,), reconstructed)
            if label is not None:
                found.append(_Reconstruction(label, (region.ordinal,), True))
    for left in ordered:
        for right in ordered:
            if left.ordinal == right.ordinal:
                continue
            lb, rb = _safe_box(left), _safe_box(right)
            if lb is None or rb is None or rb[0] < lb[0]:
                continue
            strict, broad = _pair_geometry(left, right)
            if not broad or _intervening((left, right), observation.regions):
                continue
            combined = _separator_free(left.text) + _separator_free(right.text)
            if not combined or _exact_strong_structured_label_match(combined) is None:
                continue
            # Directly supported regions are not rescue evidence.
            if (_exact_strong_structured_label_match(left.text) is not None
                    or _exact_strong_structured_label_match(right.text) is not None):
                continue
            label = _virtual_label((left, right), combined)
            if label is not None:
                found.append(_Reconstruction(label, (left.ordinal, right.ordinal), strict))
    return tuple(found)


def _numeric_regions(observation: OcrObservation) -> list[TextRegion]:
    return [
        region for region in observation.regions
        if _NUMERIC_RUN.search(unicodedata.normalize("NFKC", region.text))
    ]


def _strong_whole_numeric(label: TextRegion, numerics: list[TextRegion]) -> list[TextRegion]:
    return [
        region for region in numerics
        if _high_confidence(region)
        and _whole_numeric(region) is not None
        and classify_structural_relation(label, region).state == "STRONG"
    ]


def _numeric_blocked(label: TextRegion, numerics: list[TextRegion]) -> bool:
    strong = _strong_whole_numeric(label, numerics)
    if len(numerics) != 1 or len(strong) != 1:
        return True
    target = strong[0]
    return any(
        region.ordinal != target.ordinal
        and (_blocking_relation(label, region) is not None
             or _blocking_relation(target, region) is not None)
        for region in numerics
    )


def _coherent_surfaces(observation: OcrObservation) -> tuple[tuple[str, TextRegion], ...]:
    values: list[tuple[str, TextRegion]] = []
    for region in observation.regions:
        normalized = unicodedata.normalize("NFKC", region.text).strip()
        compact = _compact_ocr_token(normalized)
        if (
            _high_confidence(region)
            and compact
            and compact == _separator_free(normalized)
            and not re.search(r"\s", normalized)
            and _label_vocabulary(compact)
            and _exact_strong_structured_label_match(compact) is None
            and not _is_negative(region)
        ):
            values.append((compact, region))
    return tuple(values)


def _coherent_relation(
    label: TextRegion,
    numeric: TextRegion,
) -> tuple[RelationCategory, str]:
    if label.ordinal == numeric.ordinal:
        return "SAME_REGION", "STRONG"
    lb, nb = _safe_box(label), _safe_box(numeric)
    if lb is None or nb is None:
        return "UNKNOWN", "UNRELATED"
    order = {"UNRELATED": 0, "UNCERTAIN": 1, "STRONG": 2}
    relations = (
        classify_structural_relation(label, numeric),
        classify_structural_relation(numeric, label),
    )
    relation = max(relations, key=lambda item: order[item.state])
    if relation.state != "UNRELATED":
        if relation.axis == "row_right":
            return "SAME_LINE", relation.state
        if relation.axis == "column_below":
            return "ADJACENT_LINE", relation.state
    lx, ly, lw, lh = lb
    nx, ny, nw, nh = nb
    horizontal_gap = max(lx - (nx + nw), nx - (lx + lw), 0.0)
    vertical_gap = max(ly - (ny + nh), ny - (ly + lh), 0.0)
    if max(horizontal_gap, vertical_gap) / max(lh, nh) <= 5.0:
        return "NEARBY_REGION", "UNRELATED"
    return "SEPARATED_REGION", "UNRELATED"


def _coherent_context(region: TextRegion) -> ContextCategory:
    compact = _compact_ocr_token(region.text)
    if any(value in compact for value in ("保険", "公費", "自己負担", "一部負担")):
        return "BURDEN_INSURANCE_LIKE"
    if "税" in compact:
        return "TAX_LIKE"
    if any(value in compact for value in ("小計", "預り", "預かり", "お釣", "釣銭")):
        return "SUBTOTAL_LIKE"
    if any(value in compact for value in ("点数", "数量", "回数", "件数", "単価")):
        return "COUNT_POINTS_LIKE"
    if _label_vocabulary(compact):
        return "PAYMENT_LIKE"
    return "UNKNOWN"


def _coherent_uniqueness(
    relations: tuple[tuple[RelationCategory, str], ...],
    contexts: tuple[ContextCategory, ...],
    complete: bool,
) -> UniquenessCategory:
    if not complete or len(relations) < 2 or any(category == "UNKNOWN" for category, _ in relations):
        return "D"
    negative_contexts = {
        "SUBTOTAL_LIKE", "TAX_LIKE", "BURDEN_INSURANCE_LIKE", "COUNT_POINTS_LIKE"
    }
    local_categories = {"SAME_REGION", "SAME_LINE", "ADJACENT_LINE", "NEARBY_REGION"}
    plausible = [
        index for index, ((category, _), context) in enumerate(zip(relations, contexts, strict=True))
        if category in local_categories and context not in negative_contexts
    ]
    if len(plausible) > 1:
        return "C"
    if not plausible:
        return "D"
    selected = plausible[0]
    selected_relation = relations[selected]
    remaining_are_bounded = all(
        index == selected
        or context in negative_contexts
        or relation[0] == "SEPARATED_REGION"
        for index, (relation, context) in enumerate(zip(relations, contexts, strict=True))
    )
    if selected_relation[1] == "STRONG" and remaining_are_bounded:
        return "A"
    return "B"


def observe_coherent_numeric_ambiguity(
    observation: OcrObservation,
) -> CoherentAmbiguityEvaluation:
    """Classify every coherent/numeric pairing without selecting a numeric."""
    if type(observation) is not OcrObservation:
        return CoherentAmbiguityEvaluation(evaluation_failed=1)
    try:
        complete = observation.complete and not any(
            set(region.issues) & _INCOMPLETE_ISSUES for region in observation.regions
        )
        # Match the established coherent-ambiguity population: incomplete OCR
        # units do not contribute a misleading partial subset.
        coherent = _coherent_surfaces(observation) if complete else ()
        numerics = tuple(_numeric_regions(observation))
        relation_totals = {category: 0 for category in (
            "SAME_LINE", "SAME_REGION", "ADJACENT_LINE", "NEARBY_REGION",
            "SEPARATED_REGION", "UNKNOWN",
        )}
        context_totals = {category: 0 for category in (
            "PAYMENT_LIKE", "SUBTOTAL_LIKE", "TAX_LIKE",
            "BURDEN_INSURANCE_LIKE", "COUNT_POINTS_LIKE", "UNKNOWN",
        )}
        competitor_buckets = {"TWO": 0, "THREE": 0, "FOUR_PLUS": 0, "OTHER": 0}
        uniqueness_totals = {"A": 0, "B": 0, "C": 0, "D": 0}
        records: list[_CoherentAmbiguityRecord] = []
        same_line_ambiguity = same_region_ambiguity = 0
        for surface, label in coherent:
            count = len(numerics)
            bucket = "TWO" if count == 2 else "THREE" if count == 3 else (
                "FOUR_PLUS" if count >= 4 else "OTHER"
            )
            competitor_buckets[bucket] += 1
            relations = tuple(_coherent_relation(label, numeric) for numeric in numerics)
            contexts = tuple(_coherent_context(numeric) for numeric in numerics)
            for category, _ in relations:
                relation_totals[category] += 1
            for context in contexts:
                context_totals[context] += 1
            uniqueness = _coherent_uniqueness(relations, contexts, complete)
            uniqueness_totals[uniqueness] += 1
            relation_counts = tuple(
                (category, sum(item[0] == category for item in relations))
                for category in relation_totals
            )
            context_counts = tuple(
                (category, sum(item == category for item in contexts))
                for category in context_totals
            )
            records.append(_CoherentAmbiguityRecord(
                surface, bucket, relation_counts, context_counts, uniqueness
            ))
            same_line_ambiguity += int(sum(item[0] == "SAME_LINE" for item in relations) > 1)
            same_region_ambiguity += int(sum(item[0] == "SAME_REGION" for item in relations) > 1)
        ambiguous_count = sum(len(numerics) >= 2 for _ in coherent)
        return CoherentAmbiguityEvaluation(
            coherent_observation_count=len(coherent),
            competitor_count_two=competitor_buckets["TWO"],
            competitor_count_three=competitor_buckets["THREE"],
            competitor_count_four_plus=competitor_buckets["FOUR_PLUS"],
            competitor_count_other=competitor_buckets["OTHER"],
            relation_same_line_count=relation_totals["SAME_LINE"],
            relation_same_region_count=relation_totals["SAME_REGION"],
            relation_adjacent_line_count=relation_totals["ADJACENT_LINE"],
            relation_nearby_region_count=relation_totals["NEARBY_REGION"],
            relation_separated_region_count=relation_totals["SEPARATED_REGION"],
            relation_unknown_count=relation_totals["UNKNOWN"],
            context_payment_like_count=context_totals["PAYMENT_LIKE"],
            context_subtotal_like_count=context_totals["SUBTOTAL_LIKE"],
            context_tax_like_count=context_totals["TAX_LIKE"],
            context_burden_insurance_like_count=context_totals["BURDEN_INSURANCE_LIKE"],
            context_count_points_like_count=context_totals["COUNT_POINTS_LIKE"],
            context_unknown_count=context_totals["UNKNOWN"],
            structural_uniqueness_a_count=uniqueness_totals["A"],
            structural_uniqueness_b_count=uniqueness_totals["B"],
            structural_uniqueness_c_count=uniqueness_totals["C"],
            structural_uniqueness_d_count=uniqueness_totals["D"],
            nearest_without_semantic_support_count=ambiguous_count,
            ordinal_without_semantic_support_count=ambiguous_count,
            extremum_without_semantic_support_count=ambiguous_count,
            same_line_ambiguity_count=same_line_ambiguity,
            same_region_ambiguity_count=same_region_ambiguity,
            same_block_identity_unavailable_count=ambiguous_count,
            observation_complete=complete,
            _records=tuple(records),
        )
    except Exception:
        return CoherentAmbiguityEvaluation(evaluation_failed=1)


def evaluate_coherent_ambiguity_stability(
    observations: tuple[OcrObservation, ...],
    *,
    same_source_confirmed: bool,
) -> CoherentMaterializationStability:
    """Compare anonymous coherent structure across independent materializations."""
    if (
        type(observations) is not tuple
        or len(observations) < 2
        or same_source_confirmed is not True
    ):
        return CoherentMaterializationStability(
            evaluated_materialization_count=(
                len(observations) if isinstance(observations, tuple) else 0
            ),
            same_source_unverified_count=1,
        )
    results = tuple(observe_coherent_numeric_ambiguity(item) for item in observations)
    if any(item.evaluation_failed or not item.observation_complete for item in results):
        return CoherentMaterializationStability(
            evaluated_materialization_count=len(results), evaluation_failed=1
        )
    grouped: list[dict[str, list[tuple[object, ...]]]] = []
    for result in results:
        materialization: dict[str, list[tuple[object, ...]]] = {}
        for record in result._records:
            materialization.setdefault(record.surface, []).append(record.comparison_signature)
        for signatures in materialization.values():
            signatures.sort()
        grouped.append(materialization)
    stable = partial = unstable = compared = 0
    for surface in set().union(*(set(item) for item in grouped)):
        occurrence_count = max(len(item.get(surface, ())) for item in grouped)
        for index in range(occurrence_count):
            compared += 1
            signatures = [
                item.get(surface, [])[index]
                for item in grouped
                if index < len(item.get(surface, ()))
            ]
            if len(signatures) != len(grouped):
                unstable += 1
            elif len(set(signatures)) == 1:
                stable += 1
            elif any(len({signature[dimension] for signature in signatures}) == 1
                     for dimension in range(3)):
                partial += 1
            else:
                unstable += 1
    return CoherentMaterializationStability(
        evaluated_materialization_count=len(results),
        compared_observation_count=compared,
        stable_count=stable,
        partially_stable_count=partial,
        unstable_count=unstable,
    )


def evaluate_payment_rescue_shadow(observation: OcrObservation) -> RescueShadowEvaluation:
    """Compare three rescue surfaces without granting candidate authority."""
    if type(observation) is not OcrObservation:
        return RescueShadowEvaluation(evaluation_failed=1)
    try:
        existing_level2_count = len(evaluate_level2_payment_shadow(observation).candidates)
        boundary_counts, boundary_fingerprints = _boundary_stage_counts(
            observation, existing_level2_count
        )
        if not observation.complete or any(
            set(region.issues) & _INCOMPLETE_ISSUES for region in observation.regions
        ):
            return RescueShadowEvaluation(
                **boundary_counts,
                observation_complete=False,
                existing_level2_candidate_count=existing_level2_count,
                _boundary_fingerprints=boundary_fingerprints,
            )
        reconstructions = _reconstructions(observation)
        broad = [item for item in reconstructions if not item.strict_boundary]
        numerics = _numeric_regions(observation)
        coherent = _coherent_surfaces(observation)
        coherent_strong = sum(bool(_strong_whole_numeric(region, numerics)) for _, region in coherent)
        coherent_blocked = sum(_numeric_blocked(region, numerics) for _, region in coherent)
        return RescueShadowEvaluation(
            **boundary_counts,
            geometry_exact_reconstruction_count=len(broad),
            geometry_strong_relation_count=sum(
                bool(_strong_whole_numeric(item.label, numerics)) for item in broad
            ),
            geometry_overmerge_risk_count=(
                int(len(reconstructions) > 1)
                + sum(_intervening(
                    tuple(region for region in observation.regions
                          if region.ordinal in item.fragment_ordinals),
                    observation.regions,
                ) for item in broad)
            ),
            geometry_uncertain_relation_count=sum(
                any(classify_structural_relation(item.label, number).state == "UNCERTAIN"
                    for number in numerics)
                for item in broad
            ),
            coherent_unsupported_count=len(coherent),
            coherent_strong_relation_count=coherent_strong,
            coherent_numeric_blocker_count=coherent_blocked,
            observation_complete=True,
            existing_level2_candidate_count=existing_level2_count,
            _coherent_surfaces=tuple(value for value, _ in coherent),
            _boundary_fingerprints=boundary_fingerprints,
        )
    except Exception:
        return RescueShadowEvaluation(evaluation_failed=1)


def evaluate_stable_coherent_form(
    observations: tuple[OcrObservation, ...],
) -> StableCoherentEvaluation:
    """Require the same exact unsupported surface in independent materializations."""
    if type(observations) is not tuple or len(observations) < 2:
        return StableCoherentEvaluation(
            evaluated_materialization_count=(len(observations)
                if isinstance(observations, tuple) else 0),
            evaluation_failed=1,
        )
    results = tuple(evaluate_payment_rescue_shadow(item) for item in observations)
    if any(item.evaluation_failed or not item.observation_complete for item in results):
        return StableCoherentEvaluation(
            evaluated_materialization_count=len(results), evaluation_failed=1
        )
    surfaces = [set(item._coherent_surfaces) for item in results]
    stable = set.intersection(*surfaces)
    union = set.union(*surfaces)
    return StableCoherentEvaluation(
        evaluated_materialization_count=len(results),
        stable_exact_form_count=len(stable),
        stable_strong_relation_count=int(
            bool(stable) and all(item.coherent_strong_relation_count for item in results)
        ),
        stable_numeric_blocker_count=sum(item.coherent_numeric_blocker_count for item in results),
        materialization_conflict_count=len(union - stable),
    )


def evaluate_materialization_stable_boundary_shadow(
    observations: tuple[OcrObservation, ...],
    *,
    same_source_confirmed: bool,
) -> StableBoundaryEvaluation:
    """Confirm one boundary rescue only across consistent materializations.

    The same-source fact must be established by the local caller. Raw labels,
    values, geometry, and source identity remain private and are never returned.
    """
    if (
        type(observations) is not tuple
        or len(observations) < 2
        or same_source_confirmed is not True
    ):
        return StableBoundaryEvaluation(
            evaluated_materialization_count=(
                len(observations) if isinstance(observations, tuple) else 0
            ),
            materialization_unverified_count=1,
        )
    results = tuple(evaluate_payment_rescue_shadow(item) for item in observations)
    pre_stable = sum(item.boundary_pre_stability_positive_count for item in results)
    if any(item.evaluation_failed or not item.observation_complete for item in results):
        return StableBoundaryEvaluation(
            evaluated_materialization_count=len(results),
            pre_stability_positive_count=pre_stable,
            materialization_conflict_count=1,
            evaluation_failed=1,
        )
    if any(len(item._boundary_fingerprints) != 1 for item in results):
        return StableBoundaryEvaluation(
            evaluated_materialization_count=len(results),
            pre_stability_positive_count=pre_stable,
            materialization_conflict_count=1,
        )
    fingerprints = {item._boundary_fingerprints[0] for item in results}
    return StableBoundaryEvaluation(
        evaluated_materialization_count=len(results),
        pre_stability_positive_count=pre_stable,
        safe_positive_count=int(len(fingerprints) == 1),
        materialization_conflict_count=int(len(fingerprints) != 1),
    )
