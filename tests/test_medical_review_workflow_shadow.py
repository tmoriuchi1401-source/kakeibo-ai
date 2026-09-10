import ast
from pathlib import Path

import pytest

from app.medical_receipt_privacy import build_receipt_privacy_preview
from app.medical_review_workflow_shadow import (
    MedicalReviewDecision,
    MedicalReviewItem,
    ReviewProvenance,
    SafeReviewValidationError,
    build_medical_review_item,
    reconcile_medical_review_item,
    validate_review_decision_handoff,
)
from app.receipt_privacy_gate import ReceiptPrivacyGateResult


KEY = b"synthetic-review-identity-key"
PROVENANCE = {"parser_version": "medical-parser-1", "policy_version": "medical-policy-1"}


def gate(*, reason="conflicting_candidates", candidates=2, diagnostics=()):
    return ReceiptPrivacyGateResult(
        classification="medical",
        extraction_status="extracted",
        extraction_method="pdf_ocr",
        text_present=True,
        status="needs_review",
        reason_code=reason,
        medical_candidate_count=candidates,
        category="医療費",
        diagnostic_codes=diagnostics,
    )


def item(identity="receipt-source:unit:1", **kwargs):
    parser_version = kwargs.pop("parser_version", PROVENANCE["parser_version"])
    policy_version = kwargs.pop("policy_version", PROVENANCE["policy_version"])
    return build_medical_review_item(
        source_unit_identity=identity,
        identity_key=KEY,
        gate=kwargs.pop("gate", gate()),
        materialization_status=kwargs.pop("materialization_status", "stable"),
        parser_version=parser_version,
        policy_version=policy_version,
        **kwargs,
    )


def test_review_item_is_stable_across_repeated_materialization_and_data_minimised():
    first = item()
    second = item()
    assert first == second
    assert first.review_item_id == second.review_item_id
    assert first.receipt_unit_ref == second.receipt_unit_ref
    assert set(first.model_dump()) == {
        "review_item_id", "receipt_unit_ref", "reason_code", "parser_status",
        "materialization_status", "candidate_count", "ambiguity_category",
        "review_status", "provenance", "ux_requirement",
    }
    serialized = first.model_dump_json()
    assert "receipt-source" not in serialized
    for forbidden in ("raw_ocr", "amount", "patient", "facility", "coordinates"):
        assert forbidden not in serialized.lower()


def test_different_unit_identity_is_a_different_item():
    assert item("receipt-source:unit:1").review_item_id != item("receipt-source:unit:2").review_item_id


def test_duplicate_generation_is_suppressed():
    result = reconcile_medical_review_item(item(), item())
    assert result.action == "duplicate_suppressed"
    assert not result.duplicate_created
    assert not result.status_overwritten


def test_existing_review_status_is_never_overwritten_by_regeneration():
    original = item()
    reviewed = MedicalReviewItem.safe_validate({**original.model_dump(), "review_status": "reviewed"})
    result = reconcile_medical_review_item(reviewed, item())
    assert result.action == "duplicate_suppressed"
    assert result.retained_item.review_status == "reviewed"
    assert not result.status_overwritten


def test_stale_provenance_retains_existing_item_fail_closed():
    original = item()
    newer = item(parser_version="medical-parser-2", policy_version="medical-policy-1")
    result = reconcile_medical_review_item(original, newer)
    assert result.action == "stale_provenance"
    assert result.retained_item == original


def test_materialization_mismatch_does_not_create_or_overwrite_item():
    original = item()
    changed = item(materialization_status="conflicting")
    result = reconcile_medical_review_item(original, changed)
    assert result.action == "materialization_mismatch"
    assert result.retained_item == original
    assert not result.duplicate_created and not result.status_overwritten


@pytest.mark.parametrize(
    "diagnostics,reason,category,ux",
    [
        (("ambiguous_numeric_observations",), "ambiguous_numeric_observations", "numeric_ambiguity", "payment_candidate_position"),
        (("structural_relationship_unresolved",), "structural_relationship_unresolved", "geometry_ambiguity", "payment_candidate_position"),
        (("payment_label_not_observed",), "payment_label_not_observed", "unsupported_label_shape", "original_receipt"),
        (("amount_not_observed",), "amount_not_observed", "no_payment_candidate", "original_receipt"),
        (("amount_observation_low_confidence",), "amount_observation_low_confidence", "insufficient_evidence", "automatic_processing_unavailable"),
    ],
)
def test_existing_diagnostic_codes_drive_fixed_review_taxonomy(diagnostics, reason, category, ux):
    result = item(gate=gate(reason="no_candidate", candidates=0, diagnostics=diagnostics))
    assert (result.reason_code, result.ambiguity_category, result.ux_requirement) == (reason, category, ux)


def test_parser_failure_becomes_review_item_without_source_data():
    failed = ReceiptPrivacyGateResult(
        classification="sensitive_unknown",
        extraction_status="extraction_failed",
        extraction_method="pdf_ocr",
        text_present=False,
        status="blocked",
        reason_code="ocr_or_text_extraction_failed",
    )
    result = item(identity="opaque-failed-unit", gate=failed, materialization_status="incomplete")
    assert result.parser_status == "failed"
    assert result.reason_code == "ocr_or_text_extraction_failed"
    assert result.ambiguity_category == "parser_failure"
    assert result.ux_requirement == "parser_rerun"


@pytest.mark.parametrize("identity,key", [("", KEY), ("unit", b"short")])
def test_missing_or_weak_identity_fails_closed(identity, key):
    with pytest.raises(SafeReviewValidationError):
        build_medical_review_item(
            source_unit_identity=identity, identity_key=key, gate=gate(),
            materialization_status="stable", **PROVENANCE,
        )


def test_invalid_reason_and_hidden_candidate_injection_are_data_free_rejections():
    base = item().model_dump()
    marker = "SYNTHETIC_PRIVATE_MARKER"
    for invalid in (
        {**base, "reason_code": marker},
        {**base, "hidden_candidate": marker},
        {**base, "raw_ocr": marker},
        {**base, "amount": 1234},
    ):
        with pytest.raises(SafeReviewValidationError) as captured:
            MedicalReviewItem.safe_validate(invalid)
        assert marker not in str(captured.value)


def test_malformed_decision_is_rejected_without_echoing_input():
    marker = "SYNTHETIC_PRIVATE_MARKER"
    with pytest.raises(SafeReviewValidationError) as captured:
        MedicalReviewDecision.safe_validate({
            "review_item_id": item().review_item_id,
            "provenance": item().provenance.model_dump(),
            "disposition": "confirmed_from_original",
            "raw_amount": marker,
        })
    assert marker not in str(captured.value)


def test_review_handoff_never_grants_write_or_parser_authority():
    pending = item()
    decision = MedicalReviewDecision(
        review_item_id=pending.review_item_id,
        provenance=pending.provenance,
        disposition="confirmed_from_original",
    )
    handoff = validate_review_decision_handoff(
        pending, decision, current_provenance=pending.provenance
    )
    assert handoff.status == "requires_existing_production_authority"
    assert handoff.write_authorized is False
    assert "amount" not in handoff.model_dump()
    assert "write_plan" not in handoff.model_dump()


def test_stale_or_misbound_decision_cannot_bypass_authority():
    pending = item()
    stale = ReviewProvenance(parser_version="medical-parser-2", policy_version="medical-policy-1")
    decision = MedicalReviewDecision(
        review_item_id=pending.review_item_id,
        provenance=pending.provenance,
        disposition="confirmed_from_original",
    )
    with pytest.raises(SafeReviewValidationError):
        validate_review_decision_handoff(pending, decision, current_provenance=stale)
    other = item("another-unit")
    with pytest.raises(SafeReviewValidationError):
        validate_review_decision_handoff(other, decision, current_provenance=other.provenance)


def test_review_observation_does_not_change_production_preview():
    before = build_receipt_privacy_preview("病院 診療\n支払額 1200円\n領収金額 3400円")
    assert before.status == "needs_review"
    item(gate=gate(candidates=before.candidate_count, diagnostics=before.diagnostic_codes))
    after = build_receipt_privacy_preview("病院 診療\n支払額 1200円\n領収金額 3400円")
    assert after == before


def test_shadow_module_exposes_no_io_or_production_apply_surface():
    review = item()
    assert not hasattr(review, "write")
    assert not hasattr(review, "apply")
    assert not hasattr(review, "amount")


def test_production_modules_do_not_import_review_shadow():
    root = Path(__file__).resolve().parents[1] / "app"
    production = (
        "receipt_pipeline.py",
        "receipt_privacy_gate.py",
        "receipt_text_extraction.py",
        "medical_receipt_privacy.py",
        "medical_payment_evidence.py",
    )
    for name in production:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "app.medical_review_workflow_shadow"
            for node in ast.walk(tree)
        )
