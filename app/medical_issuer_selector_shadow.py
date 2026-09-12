"""Deterministic, OCR-only Medical issuer selection for shadow evaluation.

The selector consumes existing offline OCR metadata.  It has no production
caller, does not mutate OCR or authority state, and cannot create a write plan.
Human adjudication is deliberately absent from every selector input.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


SELECTOR_VERSION = "medical-issuer-selector-shadow-v1"
OUTPUT_SCHEMA_VERSION = "medical-issuer-selection-shadow-v1"

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_REFERENCE_LABELS = (
    "保険医療機関名",
    "処方医療機関",
    "処方元",
    "紹介元",
    "医師名",
    "処方医",
)
_FACILITY_MARKERS = (
    "医療センター",
    "調剤薬局",
    "クリニック",
    "診療所",
    "病院",
    "医院",
    "薬局",
    "歯科",
)
_SPECIALTY_MARKERS = (
    "耳鼻咽喉科",
    "整形外科",
    "皮膚科",
    "小児科",
    "眼科",
    "内科",
    "外科",
    "婦人科",
    "産科",
)
_NON_NAME_PHRASES = ("治療費", "診療費", "として", "矯正", "点数")
_STANDALONE_TYPES = frozenset((*_FACILITY_MARKERS, *_SPECIALTY_MARKERS))

Verdict = Literal["SELECTED_ISSUER", "REQUIRES_HUMAN_REVIEW"]
FacilityType = Literal[
    "pharmacy",
    "hospital",
    "clinic",
    "medical_office",
    "dental",
    "medical_center",
    "medical_facility",
]


class IssuerSelectorValidationError(ValueError):
    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("medical issuer selector validation failed")


class _SafeModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, strict=True
    )

    @classmethod
    def safe_validate(cls, value: object) -> Self:
        try:
            return cls.model_validate(value)
        except ValidationError:
            value = None
            raise IssuerSelectorValidationError() from None

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise IssuerSelectorValidationError()
        return super().model_copy(deep=deep)


class IssuerBinding(_SafeModel):
    source_sha256: str
    image_sha256: str
    unit: int = Field(ge=1)
    page: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_hashes(self) -> Self:
        if not _HEX.fullmatch(self.source_sha256) or not _HEX.fullmatch(
            self.image_sha256
        ):
            raise ValueError("invalid binding")
        return self


class OcrFacilityRegion(_SafeModel):
    ordinal: int = Field(ge=0)
    reading_order: int = Field(ge=0)
    raw_text: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0.0, le=1.0)
    bbox_xywh: tuple[float, float, float, float]

    @model_validator(mode="after")
    def validate_bbox(self) -> Self:
        x, y, width, height = self.bbox_xywh
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError("invalid bbox")
        return self


class OcrPageForIssuerSelection(_SafeModel):
    binding: IssuerBinding
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    regions: tuple[OcrFacilityRegion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ordinals(self) -> Self:
        ordinals = [region.ordinal for region in self.regions]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("duplicate region ordinal")
        return self


class FacilityCandidate(_SafeModel):
    ocr_text: str
    region_ordinal: int = Field(ge=0)
    facility_type: FacilityType
    role_signals: tuple[str, ...]
    referenced_provider: bool
    role_conflict: bool
    confidence: float = Field(ge=0.0, le=1.0)


class IssuerSelectionShadowResult(_SafeModel):
    schema_version: Literal["medical-issuer-selection-shadow-v1"] = (
        OUTPUT_SCHEMA_VERSION
    )
    verdict: Verdict
    issuer_facility_name: str | None
    issuer_region_ordinal: int | None
    issuer_facility_type: FacilityType | None
    issuer_role: Literal["issuer"] | None
    selection_reason: str
    competing_facilities: tuple[FacilityCandidate, ...]
    referenced_providers: tuple[FacilityCandidate, ...]
    ambiguity: bool
    binding: IssuerBinding
    selector_version: Literal["medical-issuer-selector-shadow-v1"] = SELECTOR_VERSION
    production_authorized: Literal[False] = False
    write_authorized: Literal[False] = False
    state_changed: Literal[False] = False

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        selected = self.verdict == "SELECTED_ISSUER"
        values_present = all(
            value is not None
            for value in (
                self.issuer_facility_name,
                self.issuer_region_ordinal,
                self.issuer_facility_type,
                self.issuer_role,
            )
        )
        if selected != values_present or selected == self.ambiguity:
            raise ValueError("invalid selection result")
        return self


def _normalized(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _facility_type(text: str) -> FacilityType:
    if "薬局" in text:
        return "pharmacy"
    if "病院" in text:
        return "hospital"
    if "医療センター" in text:
        return "medical_center"
    if "クリニック" in text:
        return "clinic"
    if "医院" in text or "診療所" in text:
        return "medical_office"
    if "歯科" in text:
        return "dental"
    return "medical_facility"


def _looks_like_facility(text: str) -> bool:
    compact = _normalized(text)
    has_reference = any(label in compact for label in _REFERENCE_LABELS)
    has_marker = any(marker in compact for marker in _FACILITY_MARKERS)
    has_specialty = any(marker in compact for marker in _SPECIALTY_MARKERS)
    legal_entity = "医療法人" in compact and (has_marker or has_specialty)
    if not (has_marker or legal_entity or (has_reference and has_specialty)):
        return False
    if compact in _STANDALONE_TYPES:
        return False
    if any(phrase in compact for phrase in _NON_NAME_PHRASES) and not (
        "医療法人" in compact or "クリニック" in compact or "医院" in compact
    ):
        return False
    non_dental_markers = tuple(
        marker for marker in _FACILITY_MARKERS if marker != "歯科"
    )
    if (
        "歯科" in compact
        and not any(marker in compact for marker in non_dental_markers)
        and not legal_entity
        and not has_reference
        and not compact.endswith("歯科")
        and re.search(r"歯科[a-z0-9]", compact) is None
    ):
        return False
    return True


def _nearby(
    first: OcrFacilityRegion,
    second: OcrFacilityRegion,
    page: OcrPageForIssuerSelection,
) -> bool:
    ax, ay, aw, ah = first.bbox_xywh
    bx, by, bw, bh = second.bbox_xywh
    horizontal_gap = max(0.0, max(ax, bx) - min(ax + aw, bx + bw))
    vertical_gap = max(0.0, max(ay, by) - min(ay + ah, by + bh))
    return horizontal_gap <= page.width * 0.08 and vertical_gap <= page.height * 0.04


def generate_facility_candidates(
    page: OcrPageForIssuerSelection,
) -> tuple[FacilityCandidate, ...]:
    """Generate role-aware candidates without consulting adjudication."""
    reference_regions = tuple(
        region
        for region in page.regions
        if any(label in _normalized(region.raw_text) for label in _REFERENCE_LABELS)
    )
    candidates = []
    for region in page.regions:
        if not _looks_like_facility(region.raw_text):
            continue
        compact = _normalized(region.raw_text)
        direct_reference = any(label in compact for label in _REFERENCE_LABELS)
        nearby_reference = any(
            other.ordinal != region.ordinal and _nearby(region, other, page)
            for other in reference_regions
        )
        is_reference = direct_reference or nearby_reference
        signals = []
        if "薬局" in compact:
            signals.append("independent_pharmacy_name")
        if "医療法人" in compact:
            signals.append("legal_medical_entity_name")
        if not direct_reference:
            signals.append("independent_facility_name")
        if direct_reference:
            signals.append("explicit_referenced_provider_label")
        elif nearby_reference:
            signals.append("nearby_referenced_provider_context")
        role_conflict = is_reference and "薬局" in compact
        if role_conflict:
            signals.append("conflicting_issuer_and_reference_signals")
        candidates.append(
            FacilityCandidate(
                ocr_text=region.raw_text,
                region_ordinal=region.ordinal,
                facility_type=_facility_type(compact),
                role_signals=tuple(signals),
                referenced_provider=is_reference,
                role_conflict=role_conflict,
                confidence=region.confidence,
            )
        )
    return tuple(sorted(candidates, key=lambda item: item.region_ordinal))


def _review(
    *,
    binding: IssuerBinding,
    reason: str,
    competing: tuple[FacilityCandidate, ...],
    referenced: tuple[FacilityCandidate, ...],
) -> IssuerSelectionShadowResult:
    return IssuerSelectionShadowResult(
        verdict="REQUIRES_HUMAN_REVIEW",
        issuer_facility_name=None,
        issuer_region_ordinal=None,
        issuer_facility_type=None,
        issuer_role=None,
        selection_reason=reason,
        competing_facilities=competing,
        referenced_providers=referenced,
        ambiguity=True,
        binding=binding,
    )


def select_medical_issuer_shadow(
    page: OcrPageForIssuerSelection,
    *,
    expected_binding: IssuerBinding,
) -> IssuerSelectionShadowResult:
    """Select only a unique role-safe issuer; every unknown state fails closed."""
    if (
        type(page) is not OcrPageForIssuerSelection
        or type(expected_binding) is not IssuerBinding
    ):
        raise IssuerSelectorValidationError()
    try:
        page = OcrPageForIssuerSelection.safe_validate(page.model_dump(mode="python"))
        expected_binding = IssuerBinding.safe_validate(
            expected_binding.model_dump(mode="python")
        )
    except (AttributeError, IssuerSelectorValidationError):
        raise IssuerSelectorValidationError() from None
    if page.binding != expected_binding:
        return _review(
            binding=page.binding,
            reason="binding_mismatch",
            competing=(),
            referenced=(),
        )
    candidates = generate_facility_candidates(page)
    referenced = tuple(item for item in candidates if item.referenced_provider)
    conflicts = tuple(item for item in candidates if item.role_conflict)
    eligible = tuple(
        item for item in candidates if not item.referenced_provider and not item.role_conflict
    )
    if conflicts:
        return _review(
            binding=page.binding,
            reason="conflicting_role_signals",
            competing=candidates,
            referenced=referenced,
        )
    if not eligible:
        return _review(
            binding=page.binding,
            reason=("referenced_provider_only" if referenced else "facility_candidate_absent"),
            competing=candidates,
            referenced=referenced,
        )

    grouped: dict[str, list[FacilityCandidate]] = defaultdict(list)
    for item in eligible:
        grouped[_normalized(item.ocr_text)].append(item)
    representatives = tuple(
        sorted(
            (
                sorted(group, key=lambda item: (-item.confidence, item.region_ordinal))[0]
                for group in grouped.values()
            ),
            key=lambda item: item.region_ordinal,
        )
    )
    if len(representatives) != 1:
        return _review(
            binding=page.binding,
            reason="multiple_distinct_issuer_candidates",
            competing=candidates,
            referenced=referenced,
        )
    selected = representatives[0]
    duplicates = tuple(
        item
        for item in eligible
        if item.region_ordinal != selected.region_ordinal
    )
    pharmacy_discrimination = (
        selected.facility_type == "pharmacy" and bool(referenced)
    )
    reason = (
        "pharmacy_selected_referenced_provider_excluded"
        if pharmacy_discrimination
        else "duplicate_same_facility_collapsed"
        if duplicates
        else "unique_independent_facility_name"
    )
    return IssuerSelectionShadowResult(
        verdict="SELECTED_ISSUER",
        issuer_facility_name=selected.ocr_text,
        issuer_region_ordinal=selected.region_ordinal,
        issuer_facility_type=selected.facility_type,
        issuer_role="issuer",
        selection_reason=reason,
        competing_facilities=duplicates,
        referenced_providers=referenced,
        ambiguity=False,
        binding=page.binding,
    )
