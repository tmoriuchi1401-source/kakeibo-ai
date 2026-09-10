"""Anonymous root-cause diagnostics for the medical Level 2 shadow.

This module is an observer only. OCR text, numeric values, coordinates and source
identities are used transiently and are never retained by the returned models.
Nothing here creates a payment candidate or participates in a production decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import unicodedata
from typing import Literal

from .medical_ocr_observation_shadow import OcrObservation, TextRegion
from .medical_payment_evidence import _NUMERIC_RUN, _POSSIBLE_PAYMENT_CONTEXT
from .medical_payment_level2_shadow import (
    _MIN_HIGH_CONFIDENCE,
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


SCHEMA_VERSION = "medical-level2-failure-diagnostics-v1"
_STRONG_LABELS = tuple(
    dict.fromkeys(label for label, _, strength, _ in _LABEL_RULES if strength == "strong")
) + ("支払額",)
_LABEL_REGISTRY = tuple((f"strong-label-{index:02d}", label)
                        for index, label in enumerate(_STRONG_LABELS, start=1))
_MAX_RECONSTRUCTION_PARTS = 4
_MAX_FRAGMENT_REGIONS = 128
_MAX_UNSUPPORTED_LABEL_LENGTH = max(len(label) for label in _STRONG_LABELS) + 3

LabelFailureCategory = Literal[
    "EXACT_FRAGMENT_COMPONENT",
    "MULTI_TOKEN_RECONSTRUCTABLE",
    "OCR_SUBSTITUTION_LIKELY",
    "DECORATION_AFFIX",
    "TRUE_UNSUPPORTED_LABEL",
    "UNRESOLVED",
]
GeometryRelation = Literal[
    "SAME_ROW_ADJACENT",
    "SAME_COLUMN_ADJACENT",
    "LOCAL_OTHER",
    "ISOLATED",
    "UNKNOWN",
]
ConfidenceBucket = Literal["HIGH", "MID", "LOW", "UNKNOWN"]
EditDistanceBucket = Literal["ZERO", "ONE", "TWO", "THREE_OR_MORE", "UNKNOWN"]
RelationFailureCategory = Literal[
    "SAME_ROW_STRONG",
    "SAME_COLUMN_STRONG",
    "NEAREST_NUMERIC_BUT_WEAK",
    "MULTIPLE_EQUAL_COMPETITORS",
    "MULTIPLE_UNEQUAL_COMPETITORS",
    "NEGATIVE_CONTEXT_INTERFERENCE",
    "GEOMETRY_SCOPE_MISMATCH",
    "NUMERIC_NOT_OBSERVED",
    "UNRESOLVED",
]
AnchorKind = Literal["EXACT_STRONG", "OBSERVER_RECONSTRUCTED"]
RelationAxis = Literal["ROW_RIGHT", "COLUMN_BELOW", "OTHER", "NONE"]
NegativeContextProximity = Literal["LABEL_LOCAL", "TARGET_LOCAL", "NONE"]


@dataclass(frozen=True)
class LabelFailureDiagnostic:
    category: LabelFailureCategory
    matched_allowlist_label_id: str | None
    token_count: int
    geometry_relation: GeometryRelation
    confidence_bucket: ConfidenceBucket
    edit_distance: int | None
    edit_distance_bucket: EditDistanceBucket
    normalized_edit_distance: float | None
    length_delta: int | None


@dataclass(frozen=True)
class LabelNumericRelationDiagnostic:
    category: RelationFailureCategory
    matched_allowlist_label_id: str
    anchor_kind: AnchorKind
    axis: RelationAxis
    normalized_gap_ratio: float | None
    row_alignment: float | None
    column_alignment: float | None
    candidate_count: int
    same_amount_count: int
    competing_amount_count: int
    unparsed_numeric_count: int
    negative_context_proximity: NegativeContextProximity
    negative_context_count: int
    confidence_bucket: ConfidenceBucket


@dataclass(frozen=True)
class MedicalLevel2FailureDiagnostics:
    schema_version: Literal["medical-level2-failure-diagnostics-v1"] = SCHEMA_VERSION
    exact_strong_label_count: int = 0
    document_numeric_count: int = 0
    label_failures: tuple[LabelFailureDiagnostic, ...] = ()
    relations: tuple[LabelNumericRelationDiagnostic, ...] = ()
    observation_complete: bool = False
    evaluation_failed: int = 0

    def aggregate(self) -> dict[str, int | bool | str]:
        label_categories = (
            "EXACT_FRAGMENT_COMPONENT",
            "MULTI_TOKEN_RECONSTRUCTABLE",
            "OCR_SUBSTITUTION_LIKELY",
            "DECORATION_AFFIX",
            "TRUE_UNSUPPORTED_LABEL",
            "UNRESOLVED",
        )
        relation_categories = (
            "SAME_ROW_STRONG",
            "SAME_COLUMN_STRONG",
            "NEAREST_NUMERIC_BUT_WEAK",
            "MULTIPLE_EQUAL_COMPETITORS",
            "MULTIPLE_UNEQUAL_COMPETITORS",
            "NEGATIVE_CONTEXT_INTERFERENCE",
            "GEOMETRY_SCOPE_MISMATCH",
            "NUMERIC_NOT_OBSERVED",
            "UNRESOLVED",
        )
        return {
            "schema_version": self.schema_version,
            "exact_strong_label_count": self.exact_strong_label_count,
            "document_numeric_count": self.document_numeric_count,
            **{f"label_{value.lower()}": sum(item.category == value for item in self.label_failures)
               for value in label_categories},
            **{f"relation_{value.lower()}": sum(item.category == value for item in self.relations)
               for value in relation_categories},
            "relation_anchor_count": len(self.relations),
            "observation_complete": self.observation_complete,
            "evaluation_failed": self.evaluation_failed,
        }


@dataclass(frozen=True)
class _LabelCandidate:
    region: TextRegion = field(repr=False)
    compact: str = field(repr=False)


@dataclass(frozen=True)
class _Anchor:
    region: TextRegion = field(repr=False)
    label_id: str
    kind: AnchorKind


def _confidence_bucket(value: float | None) -> ConfidenceBucket:
    if value is None:
        return "UNKNOWN"
    if value >= _MIN_HIGH_CONFIDENCE:
        return "HIGH"
    if value >= 0.70:
        return "MID"
    return "LOW"


def _edit_distance(left: str, right: str) -> int:
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_character in enumerate(right, start=1):
        current = [row]
        for column, left_character in enumerate(left, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_character != right_character),
            ))
        previous = current
    return previous[-1]


def _distance_bucket(value: int | None) -> EditDistanceBucket:
    if value is None:
        return "UNKNOWN"
    if value == 0:
        return "ZERO"
    if value == 1:
        return "ONE"
    if value == 2:
        return "TWO"
    return "THREE_OR_MORE"


def _strip_decorations(value: str) -> str:
    characters = list(value)
    while characters and (characters[0].isspace()
                          or unicodedata.category(characters[0])[0] in {"P", "S"}):
        characters.pop(0)
    while characters and (characters[-1].isspace()
                          or unicodedata.category(characters[-1])[0] in {"P", "S"}):
        characters.pop()
    return "".join(characters)


def _safe_box(region: TextRegion) -> tuple[float, float, float, float] | None:
    box = region.bbox
    if box is None or not all(math.isfinite(value) for value in box) or min(box[2:]) <= 0:
        return None
    return box


def _overlap_ratio(left_start: float, left_length: float,
                   right_start: float, right_length: float) -> float:
    overlap = min(left_start + left_length, right_start + right_length) - max(
        left_start, right_start
    )
    return max(0.0, overlap) / min(left_length, right_length)


def _horizontal_fragment_relation(left: TextRegion, right: TextRegion) -> bool:
    left_box, right_box = _safe_box(left), _safe_box(right)
    if left_box is None or right_box is None:
        return False
    lx, ly, lw, lh = left_box
    rx, ry, _, rh = right_box
    gap_ratio = (rx - (lx + lw)) / max(lh, rh)
    return (_overlap_ratio(ly, lh, ry, rh) >= 0.50 and 0 <= gap_ratio <= 0.75)


def _union_region(regions: tuple[TextRegion, ...], label: str) -> TextRegion:
    boxes = tuple(_safe_box(region) for region in regions)
    if any(box is None for box in boxes):
        polygon: tuple[tuple[float, float], ...] = ()
    else:
        safe_boxes = tuple(box for box in boxes if box is not None)
        x1 = min(box[0] for box in safe_boxes)
        y1 = min(box[1] for box in safe_boxes)
        x2 = max(box[0] + box[2] for box in safe_boxes)
        y2 = max(box[1] + box[3] for box in safe_boxes)
        polygon = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    confidence_values = [region.confidence for region in regions if region.confidence is not None]
    detection_values = [region.detection_confidence for region in regions
                        if region.detection_confidence is not None]
    return TextRegion(
        min(region.ordinal for region in regions),
        label,
        polygon,
        min(confidence_values) if confidence_values else None,
        min(detection_values) if detection_values else None,
        tuple(sorted({issue for region in regions for issue in region.issues})),
    )


def _label_id(label: str) -> str:
    return next(identifier for identifier, value in _LABEL_REGISTRY if value == label)


def _nearest_label(value: str) -> tuple[str | None, int | None]:
    distances = [(identifier, _edit_distance(value, label))
                 for identifier, label in _LABEL_REGISTRY]
    minimum = min(distance for _, distance in distances)
    matches = [identifier for identifier, distance in distances if distance == minimum]
    return (matches[0] if len(matches) == 1 else None), minimum


def _is_label_like(value: str) -> bool:
    if len(value) < 2:
        return False
    if any(signal in value for signal in _POSSIBLE_PAYMENT_CONTEXT):
        return True
    if any(value in label for _, label in _LABEL_REGISTRY):
        return True
    return min(_edit_distance(value, label) for _, label in _LABEL_REGISTRY) <= 2


def _looks_like_distinct_amount_label(value: str) -> bool:
    """Exclude generic context and prose from the true-coverage-gap bucket."""
    if len(value) > _MAX_UNSUPPORTED_LABEL_LENGTH:
        return False
    return (
        ("領収" in value and "額" in value)
        or ("支払" in value and "額" in value)
        or ("請求" in value and "額" in value)
        or ("負担" in value and "額" in value)
    )


def _reconstruction_groups(candidates: list[_LabelCandidate]) -> tuple[
    dict[int, tuple[tuple[_LabelCandidate, ...], str]], tuple[_Anchor, ...]
]:
    if len(candidates) > _MAX_FRAGMENT_REGIONS:
        return {}, ()
    ordered = sorted(candidates, key=lambda item: (
        _safe_box(item.region)[1] if _safe_box(item.region) else math.inf,
        _safe_box(item.region)[0] if _safe_box(item.region) else math.inf,
        item.region.ordinal,
    ))
    assigned: dict[int, tuple[tuple[_LabelCandidate, ...], str]] = {}
    anchors: list[_Anchor] = []
    for start, first in enumerate(ordered):
        if first.region.ordinal in assigned or _safe_box(first.region) is None:
            continue
        group = [first]
        combined = first.compact
        for candidate in ordered[start + 1:]:
            if len(group) >= _MAX_RECONSTRUCTION_PARTS:
                break
            if candidate.region.ordinal in assigned:
                continue
            if not _horizontal_fragment_relation(group[-1].region, candidate.region):
                continue
            proposed = combined + candidate.compact
            possible = [(identifier, label) for identifier, label in _LABEL_REGISTRY
                        if label.startswith(proposed)]
            if not possible:
                continue
            group.append(candidate)
            combined = proposed
            exact = [(identifier, label) for identifier, label in possible if label == combined]
            if len(exact) == 1 and len(group) > 1:
                identifier, label = exact[0]
                frozen_group = tuple(group)
                for member in frozen_group:
                    assigned[member.region.ordinal] = (frozen_group, identifier)
                anchors.append(_Anchor(_union_region(
                    tuple(member.region for member in frozen_group), label
                ), identifier, "OBSERVER_RECONSTRUCTED"))
                break
    return assigned, tuple(anchors)


def _geometry_relation(region: TextRegion, candidates: list[_LabelCandidate]) -> GeometryRelation:
    if _safe_box(region) is None:
        return "UNKNOWN"
    for other in candidates:
        if other.region.ordinal == region.ordinal:
            continue
        direct = classify_structural_relation(region, other.region)
        reverse = classify_structural_relation(other.region, region)
        relations = (direct, reverse)
        if any(item.axis == "row_right" and item.state != "UNRELATED" for item in relations):
            return "SAME_ROW_ADJACENT"
        if any(item.axis == "column_below" and item.state != "UNRELATED" for item in relations):
            return "SAME_COLUMN_ADJACENT"
        if _locally_related(region, other.region):
            return "LOCAL_OTHER"
    return "ISOLATED"


def _label_diagnostics(observation: OcrObservation) -> tuple[
    tuple[LabelFailureDiagnostic, ...], tuple[_Anchor, ...], int
]:
    exact_anchors: list[_Anchor] = []
    candidates: list[_LabelCandidate] = []
    for region in observation.regions:
        exact = _exact_strong_structured_label_match(region.text)
        compact = _compact_ocr_token(region.text)
        if exact is not None:
            if region.confidence is not None and region.confidence >= _MIN_HIGH_CONFIDENCE:
                matching = [item for item in _LABEL_REGISTRY if item[1] == compact]
                if matching:
                    exact_anchors.append(_Anchor(region, matching[0][0], "EXACT_STRONG"))
            continue
        decorated = _strip_decorations(unicodedata.normalize("NFKC", region.text).strip())
        compact_decorated = _compact_ocr_token(decorated)
        if _is_label_like(compact) or (
            compact_decorated != compact and any(compact_decorated == label
                                                 for _, label in _LABEL_REGISTRY)
        ):
            candidates.append(_LabelCandidate(region, compact))

    groups, reconstructed = _reconstruction_groups(candidates)
    diagnostics: list[LabelFailureDiagnostic] = []
    single_reconstructed: list[_Anchor] = []
    for candidate in candidates:
        compact = candidate.compact
        group = groups.get(candidate.region.ordinal)
        matched_id: str | None = None
        distance: int | None = None
        length_delta: int | None = None
        token_count = 1
        category: LabelFailureCategory
        geometry = _geometry_relation(candidate.region, candidates)

        if group is not None:
            members, matched_id = group
            category = "MULTI_TOKEN_RECONSTRUCTABLE"
            token_count = len(members)
            distance = 0
            length_delta = 0
            geometry = "SAME_ROW_ADJACENT"
        else:
            decorated = _compact_ocr_token(_strip_decorations(
                unicodedata.normalize("NFKC", candidate.region.text).strip()
            ))
            decoration_matches = [(identifier, label) for identifier, label in _LABEL_REGISTRY
                                  if decorated == label and decorated != compact]
            fragment_matches = [(identifier, label) for identifier, label in _LABEL_REGISTRY
                                if len(compact) >= 2 and compact != label and compact in label]
            if len(decoration_matches) == 1:
                matched_id, label = decoration_matches[0]
                category = "DECORATION_AFFIX"
                distance = 0
                length_delta = abs(len(compact) - len(label))
                single_reconstructed.append(_Anchor(
                    _union_region((candidate.region,), label), matched_id,
                    "OBSERVER_RECONSTRUCTED"
                ))
            elif len(fragment_matches) == 1:
                matched_id, label = fragment_matches[0]
                category = "EXACT_FRAGMENT_COMPONENT"
                distance = len(label) - len(compact)
                length_delta = distance
            elif len(fragment_matches) > 1:
                category = "UNRESOLVED"
            else:
                matched_id, distance = _nearest_label(compact)
                if matched_id is not None:
                    label = next(value for identifier, value in _LABEL_REGISTRY
                                 if identifier == matched_id)
                    length_delta = abs(len(compact) - len(label))
                normalized_distance = (
                    distance / max(len(compact), len(label))
                    if matched_id is not None and distance is not None else None
                )
                if (matched_id is not None and distance is not None and distance <= 2
                        and normalized_distance is not None and normalized_distance <= 0.34):
                    category = "OCR_SUBSTITUTION_LIKELY"
                    single_reconstructed.append(_Anchor(
                        _union_region((candidate.region,), label), matched_id,
                        "OBSERVER_RECONSTRUCTED"
                    ))
                elif _looks_like_distinct_amount_label(compact):
                    category = "TRUE_UNSUPPORTED_LABEL"
                else:
                    category = "UNRESOLVED"

        normalized = None if distance is None else round(
            distance / max(1, len(compact)), 3
        )
        diagnostics.append(LabelFailureDiagnostic(
            category=category,
            matched_allowlist_label_id=matched_id,
            token_count=token_count,
            geometry_relation=geometry,
            confidence_bucket=_confidence_bucket(candidate.region.confidence),
            edit_distance=distance,
            edit_distance_bucket=_distance_bucket(distance),
            normalized_edit_distance=normalized,
            length_delta=length_delta,
        ))
    return tuple(diagnostics), tuple(exact_anchors) + reconstructed + tuple(single_reconstructed), len(exact_anchors)


def _pair_metrics(label: TextRegion, number: TextRegion) -> tuple[
    RelationAxis, float | None, float | None, float | None, float
]:
    left, right = _safe_box(label), _safe_box(number)
    if left is None or right is None:
        return "NONE", None, None, None, math.inf
    lx, ly, lw, lh = left
    nx, ny, nw, nh = right
    row_alignment = _overlap_ratio(ly, lh, ny, nh)
    column_alignment = _overlap_ratio(lx, lw, nx, nw)
    row_gap = (nx - (lx + lw)) / max(lh, nh)
    column_gap = (ny - (ly + lh)) / max(lh, nh)
    relation = classify_structural_relation(label, number)
    if relation.axis == "row_right":
        axis: RelationAxis = "ROW_RIGHT"
        gap = relation.gap_ratio
    elif relation.axis == "column_below":
        axis = "COLUMN_BELOW"
        gap = relation.gap_ratio
    elif row_gap >= 0 and row_alignment > 0:
        axis, gap = "ROW_RIGHT", row_gap
    elif column_gap >= 0 and column_alignment > 0:
        axis, gap = "COLUMN_BELOW", column_gap
    else:
        axis, gap = "OTHER", min(abs(row_gap), abs(column_gap))
    center_distance = math.hypot(
        (nx + nw / 2) - (lx + lw / 2),
        (ny + nh / 2) - (ly + lh / 2),
    ) / max(lh, nh)
    return (
        axis,
        round(gap, 3) if gap is not None and math.isfinite(gap) else None,
        round(row_alignment, 3),
        round(column_alignment, 3),
        center_distance,
    )


def _negative_proximity(observation: OcrObservation, anchor: TextRegion,
                        targets: list[TextRegion]) -> tuple[NegativeContextProximity, int]:
    negatives = [region for region in observation.regions if _is_negative(region)]
    label_local = [region for region in negatives if region.ordinal != anchor.ordinal
                   and _locally_related(anchor, region)]
    if label_local:
        return "LABEL_LOCAL", len(label_local)
    target_local = [region for region in negatives if any(
        region.ordinal != target.ordinal and _locally_related(target, region)
        for target in targets
    )]
    if target_local:
        return "TARGET_LOCAL", len(target_local)
    return "NONE", 0


def _relation_diagnostic(observation: OcrObservation, anchor: _Anchor,
                         numeric_regions: list[TextRegion]) -> LabelNumericRelationDiagnostic:
    whole = [(region, _whole_numeric(region)) for region in numeric_regions]
    whole = [(region, amount) for region, amount in whole if amount is not None]
    if not whole:
        category: RelationFailureCategory = (
            "NUMERIC_NOT_OBSERVED" if not numeric_regions else "UNRESOLVED"
        )
        return LabelNumericRelationDiagnostic(
            category, anchor.label_id, anchor.kind, "NONE", None, None, None,
            0, 0, 0, 0, "NONE", 0, _confidence_bucket(anchor.region.confidence)
        )

    related = [(region, amount, classify_structural_relation(anchor.region, region))
               for region, amount in whole]
    local = [item for item in related if item[2].state != "UNRELATED"]
    ranked = sorted(related, key=lambda item: (
        {"STRONG": 0, "UNCERTAIN": 1, "UNRELATED": 2}[item[2].state],
        _pair_metrics(anchor.region, item[0])[4],
        item[0].ordinal,
    ))
    selected_region, selected_amount, selected_relation = ranked[0]
    axis, gap, row_alignment, column_alignment, _ = _pair_metrics(
        anchor.region, selected_region
    )
    local_amounts = [amount for _, amount, _ in local]
    same_amount_count = sum(amount == selected_amount for amount in local_amounts)
    competing_amount_count = len(set(local_amounts) - {selected_amount})
    local_targets = [region for region, _, _ in local]
    unparsed_local = [
        region for region in numeric_regions
        if _whole_numeric(region) is None
        and (
            _locally_related(anchor.region, region)
            or any(_locally_related(target, region) for target in local_targets)
        )
    ]
    negative_proximity, negative_count = _negative_proximity(
        observation, anchor.region, [region for region, _, _ in local]
    )

    states = {relation.state for _, _, relation in local}
    axes = {relation.axis for _, _, relation in local if relation.state == "STRONG"}
    if "STRONG" in states and negative_proximity != "NONE":
        category = "NEGATIVE_CONTEXT_INTERFERENCE"
    elif unparsed_local:
        category = "UNRESOLVED"
    elif competing_amount_count:
        category = "MULTIPLE_UNEQUAL_COMPETITORS"
    elif len(local) > 1 and same_amount_count > 1:
        category = "MULTIPLE_EQUAL_COMPETITORS"
    elif "STRONG" in states and axes == {"row_right"}:
        category = "SAME_ROW_STRONG"
    elif "STRONG" in states and axes == {"column_below"}:
        category = "SAME_COLUMN_STRONG"
    elif "UNCERTAIN" in states:
        category = "NEAREST_NUMERIC_BUT_WEAK"
    elif selected_relation.state == "UNRELATED":
        category = "GEOMETRY_SCOPE_MISMATCH"
    else:
        category = "UNRESOLVED"

    confidence_values = [value for value in (
        anchor.region.confidence, selected_region.confidence
    ) if value is not None]
    combined_confidence = min(confidence_values) if confidence_values else None
    return LabelNumericRelationDiagnostic(
        category=category,
        matched_allowlist_label_id=anchor.label_id,
        anchor_kind=anchor.kind,
        axis=axis,
        normalized_gap_ratio=gap,
        row_alignment=row_alignment,
        column_alignment=column_alignment,
        candidate_count=len(local),
        same_amount_count=same_amount_count,
        competing_amount_count=competing_amount_count,
        unparsed_numeric_count=len(unparsed_local),
        negative_context_proximity=negative_proximity,
        negative_context_count=negative_count,
        confidence_bucket=_confidence_bucket(combined_confidence),
    )


def observe_level2_failure_diagnostics(
    observation: OcrObservation,
) -> MedicalLevel2FailureDiagnostics:
    """Explain label and relation failures without returning source values."""
    if type(observation) is not OcrObservation:
        return MedicalLevel2FailureDiagnostics(evaluation_failed=1)
    try:
        label_failures, anchors, exact_count = _label_diagnostics(observation)
        numeric_regions = [region for region in observation.regions if _NUMERIC_RUN.search(
            unicodedata.normalize("NFKC", region.text)
        )]
        relations = tuple(
            _relation_diagnostic(observation, anchor, numeric_regions)
            for anchor in anchors
        )
        return MedicalLevel2FailureDiagnostics(
            exact_strong_label_count=exact_count,
            document_numeric_count=sum(_whole_numeric(region) is not None
                                       for region in numeric_regions),
            label_failures=label_failures,
            relations=relations,
            observation_complete=observation.complete,
        )
    except Exception:
        return MedicalLevel2FailureDiagnostics(
            observation_complete=False,
            evaluation_failed=1,
        )
