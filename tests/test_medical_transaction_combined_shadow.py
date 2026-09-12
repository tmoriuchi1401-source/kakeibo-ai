from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from app.medical_image_ai_admission_shadow import (
    ImageAiAdmissionPolicy,
    build_image_ai_admission_signals,
)
from app.medical_image_ai_result_shadow import (
    ImageAiCropBinding,
    ImageAiProvenance,
    build_image_ai_shadow_result,
)
from app.medical_issuer_selector_shadow import (
    IssuerBinding,
    IssuerSelectionShadowResult,
    OcrFacilityRegion,
    OcrPageForIssuerSelection,
    select_medical_issuer_shadow,
)
from app.medical_transaction_combined_shadow import (
    MedicalDocumentBinding,
    combine_medical_transaction_shadow,
    facility_ocr_evidence_sha256,
)


AMOUNT_KEY = b"synthetic-combined-amount-key"
COMBINATION_KEY = b"synthetic-combination-key"
CROP = b"synthetic approved crop"
PROMPT = "4" * 64


def crop_binding(**updates):
    values = {
        "source_sha256": "1" * 64,
        "source_image_sha256": "2" * 64,
        "unit": 1,
        "page": 1,
        "crop_sha256": hashlib.sha256(CROP).hexdigest(),
        "crop_coordinates_original": (10, 20, 110, 220),
        "rotation_clockwise_degrees": 0,
        "crop_provenance": "anchor-auto",
        "manifest_sha256": "3" * 64,
    }
    values.update(updates)
    return ImageAiCropBinding(**values)


def provenance(**updates):
    values = {
        "model": "synthetic-model-1",
        "prompt_sha256": PROMPT,
        "input_mode": "fresh_codex_exec_image",
        "approval_ref": "synthetic-explicit-approval",
    }
    values.update(updates)
    return ImageAiProvenance(**values)


def policy():
    return ImageAiAdmissionPolicy(
        policy_version="synthetic-combined-policy-v1",
        allowed_models=("synthetic-model-1",),
        allowed_prompt_sha256s=(PROMPT,),
        allowed_approval_refs=("synthetic-explicit-approval",),
        allowed_crop_provenances=("anchor-auto",),
    )


def answer(**updates):
    values = {
        "amount_yen": 630,
        "label_quote": "領収金額",
        "status": "readable",
        "reason": "",
    }
    values.update(updates)
    return values


def amount_inputs(*, bound=None, raw=None):
    bound = bound or crop_binding()
    raw = raw or answer()
    result = build_image_ai_shadow_result(
        raw_answer=raw,
        binding=bound,
        provenance=provenance(),
        identity_key=AMOUNT_KEY,
    )
    amounts = (result.amount_yen,) if result.amount_yen is not None else ()
    signals = build_image_ai_admission_signals(
        result=result,
        raw_answer=raw,
        observed_candidate_amounts=amounts,
        manual_conflict_state="none_known",
        identity_key=AMOUNT_KEY,
    )
    return result, signals, bound


def document(**updates):
    values = {
        "source_sha256": "1" * 64,
        "source_image_sha256": "2" * 64,
        "unit": 1,
        "page": 1,
    }
    values.update(updates)
    return MedicalDocumentBinding(**values)


def region(ordinal, text, *, y=20.0):
    return OcrFacilityRegion(
        ordinal=ordinal,
        reading_order=ordinal,
        raw_text=text,
        confidence=0.99,
        bbox_xywh=(20.0, y, 300.0, 30.0),
    )


def facility_page(*regions, bound=None):
    return OcrPageForIssuerSelection(
        binding=bound
        or IssuerBinding(
            source_sha256="1" * 64,
            image_sha256="2" * 64,
            unit=1,
            page=1,
        ),
        width=1000,
        height=1400,
        regions=regions,
    )


def combine(
    *,
    amount_bound=None,
    raw=None,
    page=None,
    facility_result="default",
    expected=None,
    expected_facility_digest=None,
    previous_amount=None,
    previous_combination=None,
    amount_result_override=None,
    current_provenance=None,
):
    amount_result, signals, current_binding = amount_inputs(
        bound=amount_bound, raw=raw
    )
    if amount_result_override is not None:
        amount_result = amount_result_override(amount_result)
    page = page or facility_page(region(1, "医療法人青空会あおばクリニック"))
    expected = expected or document()
    expected_issuer_binding = IssuerBinding(
        source_sha256=expected.source_sha256,
        image_sha256=expected.source_image_sha256,
        unit=expected.unit,
        page=expected.page,
    )
    if facility_result == "default":
        facility_result = select_medical_issuer_shadow(
            page, expected_binding=expected_issuer_binding
        )
    digest = expected_facility_digest or facility_ocr_evidence_sha256(page)
    return combine_medical_transaction_shadow(
        amount_result=amount_result,
        amount_signals=signals,
        current_amount_binding=current_binding,
        current_amount_provenance=current_provenance or provenance(),
        amount_crop_bytes=CROP,
        amount_identity_key=AMOUNT_KEY,
        amount_policy=policy(),
        facility_page=page,
        facility_result=facility_result,
        expected_document_binding=expected,
        expected_facility_evidence_sha256=digest,
        combination_identity_key=COMBINATION_KEY,
        previously_accepted_amount_result_id=previous_amount,
        previously_emitted_combination_id=previous_combination,
    )


def assert_review(result, reason):
    assert result.verdict == "REQUIRES_HUMAN_REVIEW"
    assert reason in result.review_reasons
    assert result.authority_ready is False
    assert result.production_authority is False
    assert result.write_authority is False
    assert result.write_plan_created is False
    assert result.state_changed is False


def test_combines_distinct_provenance_only_to_authority_boundary():
    page = facility_page(
        region(1, "保険医療機関名あおばクリニック", y=100.0),
        region(2, "中央調剤薬局本店", y=1200.0),
    )
    result = combine(page=page)
    assert result.verdict == "READY_FOR_EXISTING_AUTHORITY_EVALUATION"
    assert result.amount.amount_yen == 630
    assert result.amount.amount_origin == "IMAGE_AI_CANDIDATE"
    assert result.facility.facility_name == "中央調剤薬局本店"
    assert result.facility.facility_origin == "LOCAL_OCR_ISSUER_SELECTOR"
    assert [item.region_ordinal for item in result.facility.referenced_providers] == [1]
    assert result.next_boundary == "existing_authority_evaluation"
    assert result.production_authority is False
    assert result.write_authority is False


def test_correct_amount_with_wrong_unit_facility_fails_closed():
    wrong = IssuerBinding(
        source_sha256="1" * 64,
        image_sha256="2" * 64,
        unit=2,
        page=1,
    )
    assert_review(
        combine(page=facility_page(region(1, "中央医院"), bound=wrong)),
        "document_binding_mismatch",
    )


def test_correct_facility_with_wrong_unit_amount_fails_closed():
    wrong_amount = crop_binding(unit=2)
    assert_review(combine(amount_bound=wrong_amount), "document_binding_mismatch")


def test_missing_facility_fails_closed():
    assert_review(combine(facility_result=None), "facility_missing")


@pytest.mark.parametrize(
    "page",
    [
        facility_page(region(1, "中央薬局"), region(2, "北口薬局", y=1200.0)),
        facility_page(region(1, "処方元あおばクリニック")),
    ],
)
def test_ambiguous_or_human_review_facility_fails_closed(page):
    assert_review(combine(page=page), "facility_not_selected")


def test_amount_admission_human_review_fails_closed():
    raw = answer(label_quote="未収金 payment")
    assert_review(combine(raw=raw), "amount_not_admitted")


def test_referenced_provider_cannot_be_injected_as_issuer():
    page = facility_page(
        region(1, "保険医療機関名あおばクリニック", y=100.0),
        region(2, "中央薬局", y=1200.0),
    )
    good = select_medical_issuer_shadow(page, expected_binding=page.binding)
    injected = IssuerSelectionShadowResult.model_construct(
        **{
            **good.__dict__,
            "issuer_facility_name": "保険医療機関名あおばクリニック",
            "issuer_region_ordinal": 1,
            "issuer_facility_type": "clinic",
        }
    )
    assert_review(
        combine(page=page, facility_result=injected), "facility_result_mismatch"
    )


@pytest.mark.parametrize(
    ("expected", "wrong_page"),
    [
        (document(source_sha256="9" * 64), None),
        (
            document(page=2),
            IssuerBinding(
                source_sha256="1" * 64,
                image_sha256="2" * 64,
                unit=1,
                page=2,
            ),
        ),
    ],
)
def test_source_or_page_mismatch_fails_closed(expected, wrong_page):
    page = (
        facility_page(region(1, "中央医院"), bound=wrong_page)
        if wrong_page
        else facility_page(region(1, "中央医院"))
    )
    assert_review(combine(page=page, expected=expected), "document_binding_mismatch")


def test_amount_integrity_mismatch_fails_closed():
    def tamper(result):
        return type(result).model_construct(
            **{**result.__dict__, "integrity_tag": "f" * 64}
        )

    assert_review(
        combine(amount_result_override=tamper), "amount_not_admitted"
    )


def test_amount_provenance_mismatch_fails_closed():
    assert_review(
        combine(current_provenance=provenance(model="different-model")),
        "amount_not_admitted",
    )


def test_facility_evidence_provenance_mismatch_fails_closed():
    assert_review(
        combine(expected_facility_digest="f" * 64),
        "facility_evidence_mismatch",
    )


def test_duplicate_amount_or_combination_replay_fails_closed():
    first = combine()
    assert first.authority_ready is True
    assert_review(
        combine(previous_amount=first.amount.image_ai_result_id),
        "amount_not_admitted",
    )
    assert_review(
        combine(previous_combination=first.combination_id), "duplicate_replay"
    )
    assert_review(
        combine(previous_combination="e" * 64), "conflicting_replay"
    )


def test_combiner_has_no_production_writer_or_ground_truth_dependency():
    source = (
        Path(__file__).parents[1]
        / "app"
        / "medical_transaction_combined_shadow.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        token in module
        for module in imported
        for token in ("sheets", "writer", "authority", "ground_truth")
    )
    assert "ISSUER_CORRECT" not in source
    assert "ground_truth" not in source
