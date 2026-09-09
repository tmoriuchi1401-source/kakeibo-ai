"""Shadow-only comparison of conservative Level 2 rescue strategies.

The observer may inspect transient OCR text and geometry, but its public result
contains only fixed counters.  It never emits an amount, reconstructed label,
coordinate, source identifier, or production candidate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata

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
    _compact_ocr_token,
    _exact_strong_structured_label_match,
)


SCHEMA_VERSION = "medical-level2-rescue-shadow-v1"
_INCOMPLETE_ISSUES = {
    "recognition_missing", "invalid_geometry", "invalid_confidence", "blank_text"
}


@dataclass(frozen=True)
class RescueShadowEvaluation:
    schema_version: str = SCHEMA_VERSION
    boundary_exact_reconstruction_count: int = 0
    boundary_safe_positive_count: int = 0
    boundary_ambiguous_reconstruction_count: int = 0
    boundary_numeric_blocker_count: int = 0
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
class _Reconstruction:
    label: TextRegion = field(repr=False)
    fragment_ordinals: tuple[int, ...] = field(repr=False)
    strict_boundary: bool = False


def _high_confidence(region: TextRegion) -> bool:
    return region.confidence is not None and region.confidence >= _MIN_HIGH_CONFIDENCE


def _label_vocabulary(value: str) -> bool:
    compact = _compact_ocr_token(value)
    return any(signal in compact for signal in _POSSIBLE_PAYMENT_CONTEXT)


def _separator_free(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(character for character in normalized if character.isalnum())


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


def evaluate_payment_rescue_shadow(observation: OcrObservation) -> RescueShadowEvaluation:
    """Compare three rescue surfaces without granting candidate authority."""
    if type(observation) is not OcrObservation:
        return RescueShadowEvaluation(evaluation_failed=1)
    try:
        production_count = len(evaluate_level2_payment_shadow(observation).candidates)
        if not observation.complete or any(
            set(region.issues) & _INCOMPLETE_ISSUES for region in observation.regions
        ):
            return RescueShadowEvaluation(
                observation_complete=False, existing_level2_candidate_count=production_count
            )
        reconstructions = _reconstructions(observation)
        strict = [item for item in reconstructions if item.strict_boundary]
        broad = [item for item in reconstructions if not item.strict_boundary]
        numerics = _numeric_regions(observation)
        strict_blocked = sum(_numeric_blocked(item.label, numerics) for item in strict)
        coherent = _coherent_surfaces(observation)
        coherent_strong = sum(bool(_strong_whole_numeric(region, numerics)) for _, region in coherent)
        coherent_blocked = sum(_numeric_blocked(region, numerics) for _, region in coherent)
        return RescueShadowEvaluation(
            boundary_exact_reconstruction_count=len(strict),
            boundary_safe_positive_count=int(
                production_count == 0 and len(strict) == 1 and strict_blocked == 0
            ),
            boundary_ambiguous_reconstruction_count=int(len(strict) > 1),
            boundary_numeric_blocker_count=strict_blocked,
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
            existing_level2_candidate_count=production_count,
            _coherent_surfaces=tuple(value for value, _ in coherent),
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
