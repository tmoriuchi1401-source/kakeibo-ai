from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from app.medical_image_ai_admission_shadow import (
    AdmissionShadowValidationError,
    ImageAiAdmissionPolicy,
    ImageAiAdmissionSignals,
    build_image_ai_admission_signals,
    evaluate_image_ai_admission,
)
from app.medical_image_ai_result_shadow import (
    ImageAiCropBinding,
    ImageAiProvenance,
    ImageAiShadowResult,
    ImageAiShadowValidationError,
    build_image_ai_shadow_result,
)


KEY = b"synthetic-image-ai-admission-key"
CROP = b"synthetic approved admission crop"
PROMPT = "4" * 64


def binding(**updates) -> ImageAiCropBinding:
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


def provenance(**updates) -> ImageAiProvenance:
    values = {
        "model": "synthetic-model-1",
        "prompt_sha256": PROMPT,
        "input_mode": "fresh_codex_exec_image",
        "approval_ref": "synthetic-explicit-approval",
    }
    values.update(updates)
    return ImageAiProvenance(**values)


def policy(**updates) -> ImageAiAdmissionPolicy:
    values = {
        "policy_version": "synthetic-policy-v1",
        "allowed_models": ("synthetic-model-1",),
        "allowed_prompt_sha256s": (PROMPT,),
        "allowed_approval_refs": ("synthetic-explicit-approval",),
        "allowed_crop_provenances": ("anchor-auto", "manually-narrowed"),
    }
    values.update(updates)
    return ImageAiAdmissionPolicy(**values)


def raw(**updates) -> dict:
    values = {
        "amount_yen": 630,
        "label_quote": "領収金額",
        "status": "readable",
        "reason": "",
    }
    values.update(updates)
    return values


def result(answer=None, *, bound=None, proven=None) -> ImageAiShadowResult:
    return build_image_ai_shadow_result(
        raw_answer=answer or raw(),
        binding=bound or binding(),
        provenance=proven or provenance(),
        identity_key=KEY,
    )


def signals(
    observed,
    answer=None,
    *,
    amounts=(630,),
    conflict="none_known",
) -> ImageAiAdmissionSignals:
    return build_image_ai_admission_signals(
        result=observed,
        raw_answer=answer or raw(),
        observed_candidate_amounts=amounts,
        manual_conflict_state=conflict,
        identity_key=KEY,
    )


def admit(observed, facts, **updates):
    arguments = {
        "current_binding": binding(),
        "current_provenance": provenance(),
        "crop_bytes": CROP,
        "identity_key": KEY,
        "policy": policy(),
    }
    arguments.update(updates)
    return evaluate_image_ai_admission(observed, facts, **arguments)


def assert_review(outcome, reason):
    assert outcome.verdict == "REQUIRES_HUMAN_REVIEW"
    assert reason in outcome.reasons
    assert outcome.authority_boundary_result == "not_presented_to_authority"
    assert outcome.production_authorized is False
    assert outcome.write_authorized is False
    assert outcome.write_plan_created is False
    assert outcome.state_changed is False


def test_all_conditions_auto_admit_only_to_existing_authority_boundary():
    observed = result()
    outcome = admit(observed, signals(observed))
    assert outcome.verdict == "AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION"
    assert outcome.admission_reason == "all_shadow_admission_conditions_satisfied"
    assert outcome.amount_yen == 630
    assert outcome.provenance_status == "valid"
    assert outcome.ambiguity_status == "unambiguous"
    assert outcome.authority_boundary_result == "ready_for_existing_authority_evaluation"
    assert outcome.production_authorized is False
    assert outcome.write_authorized is False
    assert outcome.write_plan_created is False
    assert policy().require_ocr_candidate_match is False
    assert policy().use_ai_confidence_threshold is False


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            {"amount_yen": None, "label_quote": "", "status": "ambiguous", "reason": "multiple_payment_amounts"},
            "ambiguous_amount",
        ),
        (
            {"amount_yen": None, "label_quote": "", "status": "unreadable", "reason": "illegible"},
            "unreadable",
        ),
    ],
)
def test_ambiguous_and_unreadable_route_to_human_review(answer, reason):
    observed = result(answer)
    facts = signals(observed, answer, amounts=())
    assert_review(admit(observed, facts), reason)


def test_multiple_payment_candidates_route_to_review():
    observed = result()
    facts = signals(observed, amounts=(630, 2140))
    assert_review(admit(observed, facts), "multiple_payment_candidates")


def test_missing_payment_context_routes_to_review():
    answer = raw(label_quote="total")
    observed = result(answer)
    assert_review(admit(observed, signals(observed, answer)), "missing_payment_context")


def test_negative_context_veto_routes_to_review():
    answer = raw(label_quote="未収金 payment")
    observed = result(answer)
    assert_review(admit(observed, signals(observed, answer)), "negative_context")


@pytest.mark.parametrize("amount", ["630", "6,300", True, 0, -1, 1.5])
def test_malformed_zero_or_negative_amount_is_rejected_before_admission(amount):
    with pytest.raises(ImageAiShadowValidationError):
        result(raw(amount_yen=amount))


@pytest.mark.parametrize(
    "changed_binding",
    [
        {"source_sha256": "a" * 64},
        {"unit": 2},
        {"page": 2},
        {"crop_sha256": "a" * 64},
        {"crop_coordinates_original": (11, 20, 110, 220)},
    ],
)
def test_wrong_source_unit_page_or_crop_routes_to_review(changed_binding):
    observed = result()
    outcome = admit(
        observed,
        signals(observed),
        current_binding=binding(**changed_binding),
    )
    assert_review(outcome, "invalid_crop_binding")


@pytest.mark.parametrize(
    "changed",
    [
        {"allowed_models": ("other-model",)},
        {"allowed_prompt_sha256s": ("a" * 64,)},
        {"allowed_approval_refs": ("other-approval",)},
        {"allowed_crop_provenances": ("manually-narrowed",)},
    ],
)
def test_wrong_model_prompt_schema_approval_or_crop_provenance_routes_to_review(changed):
    observed = result()
    assert_review(admit(observed, signals(observed), policy=policy(**changed)), "invalid_provenance")


def test_stale_current_provenance_routes_to_review():
    observed = result()
    outcome = admit(
        observed,
        signals(observed),
        current_provenance=provenance(model="other-model"),
    )
    assert_review(outcome, "invalid_provenance")


def test_wrong_response_schema_provenance_routes_to_review():
    observed = result()
    values = {
        name: getattr(observed.provenance, name)
        for name in ImageAiProvenance.model_fields
    }
    values["answer_schema_version"] = "medical-image-ai-answer-v2"
    wrong_schema = ImageAiProvenance.model_construct(**values)
    outcome = admit(
        observed,
        signals(observed),
        current_provenance=wrong_schema,
    )
    assert_review(outcome, "invalid_provenance")


def test_invalid_result_integrity_routes_to_review():
    original = result()
    values = {name: getattr(original, name) for name in ImageAiShadowResult.model_fields}
    values["integrity_tag"] = "a" * 64
    tampered = ImageAiShadowResult.model_construct(**values)
    assert_review(admit(tampered, signals(original)), "integrity_failure")


def test_invalid_signal_integrity_routes_to_review():
    observed = result()
    original = signals(observed)
    values = {name: getattr(original, name) for name in ImageAiAdmissionSignals.model_fields}
    values["integrity_tag"] = "a" * 64
    tampered = ImageAiAdmissionSignals.model_construct(**values)
    assert_review(admit(observed, tampered), "integrity_failure")


def test_duplicate_and_conflicting_replay_route_to_review():
    observed = result()
    facts = signals(observed)
    duplicate = admit(
        observed,
        facts,
        previously_accepted_result_id=observed.result_id,
    )
    other_answer = raw(amount_yen=631)
    other = result(other_answer)
    conflict = admit(
        other,
        signals(other, other_answer, amounts=(631,)),
        previously_accepted_result_id=observed.result_id,
    )
    assert_review(duplicate, "duplicate_replay")
    assert_review(conflict, "conflicting_replay")


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("known_conflict", "human_adjudication_conflict"),
        ("unknown", "unknown_manual_conflict_state"),
    ],
)
def test_manual_conflict_or_unknown_state_routes_to_review(state, reason):
    observed = result()
    assert_review(admit(observed, signals(observed, conflict=state)), reason)


def test_signals_cannot_be_bound_to_a_different_result():
    observed = result()
    other_answer = raw(amount_yen=631)
    other = result(other_answer)
    with pytest.raises(AdmissionShadowValidationError):
        signals(other, amounts=(630,))
    assert_review(admit(other, signals(observed), current_binding=binding()), "integrity_failure")


def test_ground_truth_and_ocr_confidence_are_not_policy_fields():
    names = set(ImageAiAdmissionPolicy.model_fields)
    assert not names.intersection(
        {"ground_truth", "human_amount", "ocr_candidate", "confidence", "confidence_score"}
    )


def test_module_has_no_network_io_writer_or_authority_implementation_import():
    path = Path(__file__).parents[1] / "app" / "medical_image_ai_admission_shadow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = set()
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Call):
            calls.add(
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
    assert not imports.intersection({"requests", "urllib", "socket", "subprocess"})
    assert not any("writer" in value or "production" in value for value in imports)
    assert not calls.intersection({"open", "urlopen", "request", "run", "Popen"})


def test_production_modules_do_not_import_admission_shadow():
    root = Path(__file__).parents[1] / "app"
    shadow_name = "medical_image_ai_admission_shadow"
    for path in root.glob("*.py"):
        if path.name in {
            f"{shadow_name}.py",
            "medical_transaction_combined_shadow.py",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(value.endswith(shadow_name) for value in imports), path
