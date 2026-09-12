from __future__ import annotations

import pytest

from app.medical_issuer_selector_shadow import (
    IssuerBinding,
    IssuerSelectorValidationError,
    OcrFacilityRegion,
    OcrPageForIssuerSelection,
    select_medical_issuer_shadow,
)


def binding(**updates) -> IssuerBinding:
    values = {
        "source_sha256": "1" * 64,
        "image_sha256": "2" * 64,
        "unit": 1,
        "page": 1,
    }
    values.update(updates)
    return IssuerBinding(**values)


def region(
    ordinal: int,
    text: str,
    *,
    x: float = 20.0,
    y: float = 20.0,
    confidence: float = 0.99,
) -> OcrFacilityRegion:
    return OcrFacilityRegion(
        ordinal=ordinal,
        reading_order=ordinal,
        raw_text=text,
        confidence=confidence,
        bbox_xywh=(x, y, 260.0, 30.0),
    )


def page(*regions, bound=None) -> OcrPageForIssuerSelection:
    return OcrPageForIssuerSelection(
        binding=bound or binding(),
        width=1000,
        height=1400,
        regions=regions,
    )


def select(observation, expected=None):
    return select_medical_issuer_shadow(
        observation, expected_binding=expected or binding()
    )


def assert_review(result, reason):
    assert result.verdict == "REQUIRES_HUMAN_REVIEW"
    assert result.selection_reason == reason
    assert result.issuer_facility_name is None
    assert result.issuer_region_ordinal is None
    assert result.ambiguity is True
    assert result.production_authorized is False
    assert result.write_authorized is False
    assert result.state_changed is False


def test_pharmacy_selected_and_prescribing_clinic_is_referenced_provider():
    observation = page(
        region(1, "保険医療機関名あおばクリニック", y=100.0),
        region(2, "中央調剤薬局本店", y=1200.0),
    )
    result = select(observation)
    assert result.verdict == "SELECTED_ISSUER"
    assert result.issuer_facility_name == "中央調剤薬局本店"
    assert result.issuer_region_ordinal == 2
    assert result.issuer_facility_type == "pharmacy"
    assert result.selection_reason == "pharmacy_selected_referenced_provider_excluded"
    assert [item.region_ordinal for item in result.referenced_providers] == [1]


def test_only_prescribing_clinic_fails_closed():
    result = select(page(region(1, "処方元あおばクリニック")))
    assert_review(result, "referenced_provider_only")


def test_multiple_pharmacies_fail_closed():
    result = select(
        page(region(1, "中央薬局"), region(2, "みどり調剤薬局", y=1200.0))
    )
    assert_review(result, "multiple_distinct_issuer_candidates")


def test_hospital_selected_while_referral_facility_is_excluded():
    result = select(
        page(region(1, "中央総合病院"), region(2, "紹介元さくらクリニック", y=300.0))
    )
    assert result.verdict == "SELECTED_ISSUER"
    assert result.issuer_facility_name == "中央総合病院"
    assert result.issuer_facility_type == "hospital"
    assert [item.region_ordinal for item in result.referenced_providers] == [2]


def test_duplicated_same_facility_is_collapsed_deterministically():
    result = select(
        page(
            region(7, "中央医院", confidence=0.91),
            region(9, "中央医院", y=1200.0, confidence=0.99),
        )
    )
    assert result.verdict == "SELECTED_ISSUER"
    assert result.issuer_region_ordinal == 9
    assert result.selection_reason == "duplicate_same_facility_collapsed"
    assert [item.region_ordinal for item in result.competing_facilities] == [7]


def test_legal_prefix_and_display_name_are_retained_as_full_text():
    text = "医療法人社団青空会あおばクリニック"
    result = select(page(region(1, text)))
    assert result.verdict == "SELECTED_ISSUER"
    assert result.issuer_facility_name == text
    assert result.selection_reason == "unique_independent_facility_name"


def test_generic_dental_service_text_is_not_a_facility_candidate():
    result = select(
        page(
            region(1, "歯科矮正"),
            region(2, "青空歯科 SmileCare", y=1200.0),
        )
    )
    assert result.verdict == "SELECTED_ISSUER"
    assert result.issuer_region_ordinal == 2


def test_no_facility_candidate_fails_closed():
    assert_review(
        select(page(region(1, "領収金額 630円"))), "facility_candidate_absent"
    )


@pytest.mark.parametrize(
    "actual",
    [
        binding(unit=2),
        binding(page=2),
        binding(source_sha256="3" * 64),
    ],
)
def test_wrong_source_unit_or_page_binding_fails_closed(actual):
    result = select(page(region(1, "中央医院"), bound=actual))
    assert_review(result, "binding_mismatch")


def test_conflicting_issuer_and_reference_roles_fail_closed():
    result = select(page(region(1, "処方元あおば薬局")))
    assert_review(result, "conflicting_role_signals")


def test_invalid_untyped_input_is_not_accepted():
    with pytest.raises(IssuerSelectorValidationError):
        select_medical_issuer_shadow({}, expected_binding=binding())
