import ast
from pathlib import Path

import pytest

from app.medical_receipt_privacy import build_receipt_privacy_preview
from app.medical_review_handoff_shadow import (
    ReviewHandoffOutcome,
    ReviewedAssertion,
    build_reviewed_assertion,
    evaluate_reviewed_assertion,
)
from app.medical_review_workflow_shadow import (
    MedicalReviewItem,
    ReviewProvenance,
    SafeReviewValidationError,
    build_medical_review_item,
)
from app.receipt_privacy_gate import ReceiptPrivacyGateResult


KEY = b"synthetic-handoff-identity-key"
SOURCE = "internal-receipt:unit:1"
MATERIALIZATION = "a" * 64
PROVENANCE = ReviewProvenance(
    parser_version="medical-parser-1", policy_version="medical-policy-1"
)


def gate():
    return ReceiptPrivacyGateResult(
        classification="medical",
        extraction_status="extracted",
        extraction_method="pdf_ocr",
        text_present=True,
        status="needs_review",
        reason_code="conflicting_candidates",
        medical_candidate_count=2,
        category="医療費",
        diagnostic_codes=("ambiguous_numeric_observations",),
    )


def review_item(*, source=SOURCE, status="reviewed", provenance=PROVENANCE):
    pending = build_medical_review_item(
        source_unit_identity=source,
        identity_key=KEY,
        gate=gate(),
        materialization_status="stable",
        parser_version=provenance.parser_version,
        policy_version=provenance.policy_version,
    )
    return MedicalReviewItem.safe_validate({**pending.model_dump(), "review_status": status})


def assertion(*, item=None, source=SOURCE, materialization=MATERIALIZATION, decision="confirmed_from_original"):
    selected = item or review_item(source=source)
    return build_reviewed_assertion(
        item=selected,
        source_unit_identity=source,
        identity_key=KEY,
        materialization_binding_digest=materialization,
        decision_type=decision,
        review_revision=1,
    )


def evaluate(value, *, item=None, source=SOURCE, materialization=MATERIALIZATION,
             provenance=PROVENANCE, previous=None):
    return evaluate_reviewed_assertion(
        value,
        item=item or review_item(),
        source_unit_identity=source,
        identity_key=KEY,
        expected_materialization_binding_digest=materialization,
        current_provenance=provenance,
        previously_accepted_assertion_id=previous,
    )


def assert_non_authority(result):
    assert result.write_authorized is False
    assert result.write_plan_created is False
    assert result.state_changed is False
    assert "amount" not in result.model_dump()
    assert "candidate" not in result.model_dump()
    assert "write_plan" not in result.model_dump()


def test_valid_reviewed_assertion_stops_at_existing_authority_boundary():
    result = evaluate(assertion())
    assert result.status == "validated_for_authority_evaluation"
    assert result.next_boundary == "requires_existing_production_authority"
    assert result.decision_type == "confirmed_from_original"
    assert_non_authority(result)


def test_assertion_is_stable_and_keeps_only_safe_metadata():
    first = assertion()
    second = assertion()
    assert first == second
    assert set(first.model_dump()) == {
        "handoff_schema_version", "review_item_ref", "receipt_unit_ref",
        "review_status", "decision_type", "provenance",
        "materialization_binding_digest", "review_revision", "assertion_id",
        "integrity_tag",
    }
    rendered = first.model_dump_json()
    assert SOURCE not in rendered
    assert KEY.decode("ascii") not in rendered
    for forbidden in ("raw_ocr", "amount", "patient", "institution", "filename", "url"):
        assert forbidden not in rendered.lower()


def test_forged_review_item_reference_or_integrity_is_rejected():
    valid = assertion()
    forged_ref = ReviewedAssertion.safe_validate(
        {**valid.model_dump(), "review_item_ref": "b" * 64}
    )
    forged_integrity = ReviewedAssertion.safe_validate(
        {**valid.model_dump(), "integrity_tag": "c" * 64}
    )
    for forged in (forged_ref, forged_integrity):
        result = evaluate(forged)
        assert result.status == "rejected_binding"
        assert_non_authority(result)


def test_wrong_source_or_unit_binding_is_rejected():
    result = evaluate(assertion(), source="internal-receipt:unit:2")
    assert result.status == "rejected_binding"
    assert_non_authority(result)


def test_wrong_materialization_binding_is_rejected():
    result = evaluate(assertion(), materialization="d" * 64)
    assert result.status == "rejected_binding"
    assert_non_authority(result)


def test_stale_parser_or_policy_provenance_is_rejected():
    for stale in (
        ReviewProvenance(parser_version="medical-parser-2", policy_version="medical-policy-1"),
        ReviewProvenance(parser_version="medical-parser-1", policy_version="medical-policy-2"),
    ):
        result = evaluate(assertion(), provenance=stale)
        assert result.status == "stale_provenance"
        assert_non_authority(result)


def test_unreviewed_item_cannot_build_or_validate_assertion():
    pending = review_item(status="pending")
    with pytest.raises(SafeReviewValidationError):
        assertion(item=pending)
    reviewed_assertion = assertion()
    result = evaluate(reviewed_assertion, item=pending)
    assert result.status == "invalid_review_state"
    assert_non_authority(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("handoff_schema_version", "medical-review-handoff-v2"),
        ("review_status", "pending"),
        ("review_status", "rejected"),
        ("decision_type", "accept_arbitrary_amount"),
    ],
)
def test_schema_status_and_decision_enum_fail_closed(field, value):
    raw = assertion().model_dump()
    raw[field] = value
    with pytest.raises(SafeReviewValidationError):
        ReviewedAssertion.safe_validate(raw)


def test_public_builder_rejects_malformed_decision_and_binding_types_data_free():
    for decision, materialization in (
        ("accept_arbitrary_amount", MATERIALIZATION),
        ("confirmed_from_original", 1234),
    ):
        with pytest.raises(SafeReviewValidationError):
            build_reviewed_assertion(
                item=review_item(),
                source_unit_identity=SOURCE,
                identity_key=KEY,
                materialization_binding_digest=materialization,
                decision_type=decision,
                review_revision=1,
            )


@pytest.mark.parametrize("field", ["amount", "candidate", "raw_amount", "hidden_candidate", "write_plan"])
def test_amount_candidate_and_write_plan_injection_is_rejected_without_echo(field):
    marker = "SYNTHETIC_PRIVATE_MARKER"
    raw = {**assertion().model_dump(), field: marker}
    with pytest.raises(SafeReviewValidationError) as captured:
        ReviewedAssertion.safe_validate(raw)
    assert marker not in str(captured.value)


def test_assertion_copied_to_another_receipt_is_rejected():
    copied = assertion()
    other_item = review_item(source="internal-receipt:other-unit")
    result = evaluate(copied, item=other_item, source="internal-receipt:other-unit")
    assert result.status == "rejected_binding"
    assert_non_authority(result)


def test_unvalidated_construct_cannot_bypass_assertion_schema():
    valid = assertion()
    fields = {
        name: getattr(valid, name) for name in ReviewedAssertion.model_fields
    }
    fields["review_status"] = "pending"
    forged = ReviewedAssertion.model_construct(**fields)
    result = evaluate(forged)
    assert result.status == "invalid_review_state"
    assert_non_authority(result)


def test_replay_is_deterministic_and_never_escalates_authority():
    reviewed = assertion()
    first = evaluate(reviewed)
    replay_one = evaluate(reviewed, previous=reviewed.assertion_id)
    replay_two = evaluate(reviewed, previous=reviewed.assertion_id)
    assert first.status == "validated_for_authority_evaluation"
    assert replay_one == replay_two
    assert replay_one.status == "duplicate_replay"
    assert replay_one.replay_detected is True
    assert replay_one.next_boundary == "none"
    assert_non_authority(first)
    assert_non_authority(replay_one)


def test_different_assertion_for_already_accepted_item_fails_closed():
    first = assertion(decision="confirmed_from_original")
    second = assertion(decision="unable_to_determine")
    result = evaluate(second, previous=first.assertion_id)
    assert result.status == "rejected_binding"
    assert result.replay_detected is True
    assert_non_authority(result)


def test_outcome_cannot_be_changed_into_write_authority():
    result = evaluate(assertion())
    with pytest.raises(SafeReviewValidationError):
        result.model_copy(update={"write_authorized": True})
    with pytest.raises(SafeReviewValidationError):
        ReviewHandoffOutcome.safe_validate(
            {**result.model_dump(), "write_authorized": True}
        )


def test_shadow_has_no_writer_executor_apply_or_plan_surface():
    root = Path(__file__).resolve().parents[1] / "app"
    tree = ast.parse((root / "medical_review_handoff_shadow.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {
        "receipt_pipeline", "review_pipeline", "sheets", "gemini_ai",
        "app.receipt_pipeline", "app.review_pipeline", "app.sheets", "app.gemini_ai",
    }
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not names & {"write", "apply", "execute", "build_write_plan"}
    assert not hasattr(evaluate(assertion()), "write_plan")


def test_production_modules_do_not_import_handoff_shadow():
    root = Path(__file__).resolve().parents[1] / "app"
    production = (
        "receipt_pipeline.py", "receipt_privacy_gate.py", "receipt_text_extraction.py",
        "medical_receipt_privacy.py", "medical_payment_evidence.py", "review_pipeline.py",
    )
    for name in production:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module in {
                "medical_review_handoff_shadow", "app.medical_review_handoff_shadow"
            }
            for node in ast.walk(tree)
        )


def test_production_preview_is_invariant_after_handoff_evaluation():
    text = "病院 診療\n支払額 1200円\n領収金額 3400円"
    before = build_receipt_privacy_preview(text)
    evaluate(assertion())
    after = build_receipt_privacy_preview(text)
    assert before == after
    assert after.status == "needs_review"


def test_input_models_remain_frozen_during_evaluation():
    selected = review_item()
    reviewed = assertion(item=selected)
    item_before = selected.model_dump()
    assertion_before = reviewed.model_dump()
    evaluate(reviewed, item=selected)
    assert selected.model_dump() == item_before
    assert reviewed.model_dump() == assertion_before
