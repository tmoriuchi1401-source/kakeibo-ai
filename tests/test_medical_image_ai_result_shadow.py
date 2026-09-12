from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from app.medical_image_ai_result_shadow import (
    ImageAiCropBinding,
    ImageAiProvenance,
    ImageAiShadowResult,
    ImageAiShadowValidationError,
    build_image_ai_shadow_result,
    evaluate_image_ai_for_authority_boundary,
)


KEY = b"synthetic-image-ai-shadow-key"
CROP = b"synthetic approved crop bytes"


def binding(**updates) -> ImageAiCropBinding:
    values = {
        "source_sha256": "1" * 64,
        "source_image_sha256": "2" * 64,
        "unit": 4,
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
        "prompt_sha256": "4" * 64,
        "input_mode": "fresh_codex_exec_image",
        "approval_ref": "synthetic-explicit-approval",
    }
    values.update(updates)
    return ImageAiProvenance(**values)


def answer(**updates) -> dict:
    values = {
        "amount_yen": 630,
        "label_quote": "synthetic payment label",
        "status": "readable",
        "reason": "",
    }
    values.update(updates)
    return values


def result(**answer_updates) -> ImageAiShadowResult:
    return build_image_ai_shadow_result(
        raw_answer=answer(**answer_updates),
        binding=binding(),
        provenance=provenance(),
        identity_key=KEY,
    )


def assert_non_authority(outcome) -> None:
    assert outcome.production_authorized is False
    assert outcome.write_authorized is False
    assert outcome.write_plan_created is False
    assert outcome.state_changed is False


def test_readable_result_is_distinct_and_stops_at_authority_boundary():
    observed = result()
    outcome = evaluate_image_ai_for_authority_boundary(
        observed,
        current_binding=binding(),
        current_provenance=provenance(),
        crop_bytes=CROP,
        identity_key=KEY,
    )
    assert observed.evidence_type == "IMAGE_AI_CANDIDATE"
    assert observed.amount_yen == 630
    assert outcome.status == "validated_for_authority_evaluation"
    assert outcome.next_boundary == "requires_existing_production_authority"
    assert outcome.amount_yen == 630
    assert outcome.evidence_type == "IMAGE_AI_CANDIDATE"
    assert outcome.binding == observed.binding
    assert outcome.provenance == observed.provenance
    assert_non_authority(outcome)
    serialized = observed.model_dump()
    assert not any("ocr" in key.lower() or "human" in key.lower() for key in serialized)


@pytest.mark.parametrize("amount", [True, 0, -1, 1.5, "630", "6,300", None])
def test_amount_format_is_strict_and_fail_closed(amount):
    with pytest.raises(ImageAiShadowValidationError, match="image AI shadow validation failed"):
        result(amount_yen=amount)


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("ambiguous", "multiple_payment_amounts", "ambiguous_requires_human_review"),
        ("unreadable", "illegible", "unreadable_requires_human_review"),
    ],
)
def test_ambiguity_abstains_without_forwarding_amount(status, reason, expected):
    observed = result(amount_yen=None, label_quote="", status=status, reason=reason)
    outcome = evaluate_image_ai_for_authority_boundary(
        observed,
        current_binding=binding(),
        current_provenance=provenance(),
        crop_bytes=CROP,
        identity_key=KEY,
    )
    assert observed.evidence_type == "IMAGE_AI_ABSTENTION"
    assert outcome.status == expected
    assert outcome.next_boundary == "requires_human_review"
    assert outcome.amount_yen is None
    assert_non_authority(outcome)


@pytest.mark.parametrize(
    "raw",
    [
        {"amount_yen": 630, "label_quote": "x", "status": "readable"},
        {"amount_yen": 630, "label_quote": "x", "status": "readable", "reason": "", "extra": 1},
        {"amount_yen": None, "label_quote": "x", "status": "ambiguous", "reason": "multiple_payment_amounts"},
        {"amount_yen": 630, "label_quote": "", "status": "readable", "reason": ""},
    ],
)
def test_malformed_or_contradictory_answer_is_rejected(raw):
    with pytest.raises(ImageAiShadowValidationError):
        build_image_ai_shadow_result(
            raw_answer=raw,
            binding=binding(),
            provenance=provenance(),
            identity_key=KEY,
        )


@pytest.mark.parametrize(
    "changed_binding",
    [
        {"source_sha256": "a" * 64},
        {"source_image_sha256": "a" * 64},
        {"unit": 5},
        {"page": 2},
        {"crop_sha256": "a" * 64},
        {"crop_coordinates_original": (11, 20, 110, 220)},
        {"rotation_clockwise_degrees": 90},
        {"manifest_sha256": "a" * 64},
    ],
)
def test_source_unit_page_and_crop_misbinding_is_rejected(changed_binding):
    outcome = evaluate_image_ai_for_authority_boundary(
        result(),
        current_binding=binding(**changed_binding),
        current_provenance=provenance(),
        crop_bytes=CROP,
        identity_key=KEY,
    )
    assert outcome.status == "invalid_binding"
    assert outcome.next_boundary == "none"
    assert outcome.amount_yen is None
    assert_non_authority(outcome)


def test_crop_bytes_are_bound_not_just_manifest_claims():
    outcome = evaluate_image_ai_for_authority_boundary(
        result(),
        current_binding=binding(),
        current_provenance=provenance(),
        crop_bytes=b"different bytes",
        identity_key=KEY,
    )
    assert outcome.status == "invalid_binding"
    assert_non_authority(outcome)


@pytest.mark.parametrize(
    "changed_provenance",
    [
        {"model": "different-model"},
        {"prompt_sha256": "a" * 64},
    ],
)
def test_model_prompt_or_schema_provenance_cannot_go_stale(changed_provenance):
    outcome = evaluate_image_ai_for_authority_boundary(
        result(),
        current_binding=binding(),
        current_provenance=provenance(**changed_provenance),
        crop_bytes=CROP,
        identity_key=KEY,
    )
    assert outcome.status == "stale_provenance"
    assert_non_authority(outcome)


def test_tampered_result_fails_integrity_validation():
    original = result()
    values = {
        name: getattr(original, name) for name in ImageAiShadowResult.model_fields
    }
    values["amount_yen"] = 631
    tampered = ImageAiShadowResult.model_construct(**values)
    outcome = evaluate_image_ai_for_authority_boundary(
        tampered,
        current_binding=binding(),
        current_provenance=provenance(),
        crop_bytes=CROP,
        identity_key=KEY,
    )
    assert outcome.status == "invalid_binding"
    assert outcome.amount_yen is None
    assert_non_authority(outcome)


def test_duplicate_and_conflicting_replay_fail_closed():
    first = result()
    duplicate = evaluate_image_ai_for_authority_boundary(
        first,
        current_binding=binding(),
        current_provenance=provenance(),
        crop_bytes=CROP,
        identity_key=KEY,
        previously_accepted_result_id=first.result_id,
    )
    other = result(amount_yen=631)
    conflict = evaluate_image_ai_for_authority_boundary(
        other,
        current_binding=binding(),
        current_provenance=provenance(),
        crop_bytes=CROP,
        identity_key=KEY,
        previously_accepted_result_id=first.result_id,
    )
    assert duplicate.status == "duplicate_replay"
    assert conflict.status == "conflicting_replay"
    assert duplicate.replay_detected is conflict.replay_detected is True
    assert duplicate.amount_yen is conflict.amount_yen is None
    assert_non_authority(duplicate)
    assert_non_authority(conflict)


def test_models_are_frozen_and_cannot_be_updated_into_authority():
    observed = result()
    with pytest.raises(ImageAiShadowValidationError):
        observed.model_copy(update={"production_authorized": True})


def test_module_has_no_io_network_writer_or_production_imports():
    root = Path(__file__).parents[1] / "app"
    path = root / "medical_image_ai_result_shadow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    assert not imported.intersection({"requests", "urllib", "socket", "subprocess"})
    assert not any("writer" in value or "production" in value for value in imported)
    assert not called.intersection({"open", "urlopen", "request", "run", "Popen"})


def test_production_modules_do_not_import_image_ai_shadow():
    root = Path(__file__).parents[1] / "app"
    shadow_name = "medical_image_ai_result_shadow"
    for path in root.glob("*.py"):
        if path.name in {
            f"{shadow_name}.py",
            "medical_image_ai_admission_shadow.py",
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
