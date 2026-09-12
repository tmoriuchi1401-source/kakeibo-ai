"""Read-only replay of the fixed ten-unit image-AI admission policy."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.medical_image_ai_admission_shadow import (
    AdmissionShadowValidationError,
    ImageAiAdmissionPolicy,
    build_image_ai_admission_signals,
    evaluate_image_ai_admission,
)
from app.medical_image_ai_result_shadow import (
    ANSWER_SCHEMA_VERSION,
    ImageAiProvenance,
    ImageAiShadowValidationError,
    build_image_ai_shadow_result,
)
from scripts.replay_medical_image_ai_results_local import (
    ReplayRejected,
    _binding,
    _digest,
    _read_json,
    _sha256_bytes,
    _strict_int,
    hmac_compare,
)


FIXED_MODEL = "gpt-5.6-sol"
FIXED_PROMPT_SHA256 = "b88e40f1cbdaf29fe2a1dd8cfbbe908f0e35b8c6a414832e8f100257195e13c6"
FIXED_APPROVAL_REF = "explicit_user_2026-09-12_final_ten_images"


def fixed_policy() -> ImageAiAdmissionPolicy:
    return ImageAiAdmissionPolicy(
        policy_version="fixed-ten-shadow-v1",
        allowed_models=(FIXED_MODEL,),
        allowed_prompt_sha256s=(FIXED_PROMPT_SHA256,),
        allowed_approval_refs=(FIXED_APPROVAL_REF,),
        allowed_crop_provenances=(
            "anchor-auto",
            "anchor-auto-rotated",
            "manually-narrowed",
        ),
    )


def replay_admission(artifact_root: Path) -> dict:
    artifact_root = artifact_root.resolve()
    manifest_path = artifact_root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    manifest = _read_json(manifest_path)
    if (
        manifest.get("schema_version") != "medical-final-images-local-v1"
        or manifest.get("human_review_complete") is not True
        or manifest.get("external_http") != 0
        or manifest.get("external_ai_requests") != 0
        or manifest.get("production_writes") != 0
        or manifest.get("drive_writes") != 0
        or manifest.get("sheets_writes") != 0
    ):
        raise ReplayRejected("invalid_manifest_state")
    records = manifest.get("records")
    if type(records) is not list or len(records) != 10:
        raise ReplayRejected("invalid_fixed_unit_set")
    identity_key = hashlib.sha256(b"local-shadow-replay-v1\0" + manifest_bytes).digest()
    policy = fixed_policy()
    seen: set[int] = set()
    rows: list[dict] = []
    for record in records:
        if type(record) is not dict:
            raise ReplayRejected("invalid_manifest_binding")
        unit = _strict_int(record.get("unit"))
        if unit in seen:
            raise ReplayRejected("duplicate_manifest_unit")
        seen.add(unit)
        if (
            record.get("metadata_removed") is not True
            or record.get("human_send_review") != "APPROVED_FOR_AI_EVAL"
            or record.get("review_checks")
            != {
                "A_content": True,
                "B_privacy": True,
                "C_qr_barcode": True,
                "D_boundary": True,
            }
        ):
            raise ReplayRejected("crop_not_approved")
        output_file = record.get("output_file")
        if type(output_file) is not str or Path(output_file).name != output_file:
            raise ReplayRejected("invalid_manifest_binding")
        crop_bytes = (artifact_root / output_file).read_bytes()
        if not hmac_compare(
            _sha256_bytes(crop_bytes), _digest(record.get("output_sha256"))
        ):
            raise ReplayRejected("crop_digest_mismatch")
        unit_dir = artifact_root / "ai-results" / f"unit-{unit:02d}"
        answer = _read_json(unit_dir / "answer.json")
        attempt = _read_json(unit_dir / "attempt.json")
        execution = _read_json(unit_dir / "result.json")
        if (
            attempt.get("unit") != unit
            or attempt.get("image_sha256") != record.get("output_sha256")
            or attempt.get("origin") != "human_approved_final_image"
            or execution.get("unit") != unit
            or execution.get("exit_code") != 0
            or execution.get("status") != answer.get("status")
            or execution.get("candidate_present") is not (
                answer.get("status") == "readable"
            )
            or execution.get("tool_calls") != 0
        ):
            raise ReplayRejected("result_attempt_binding_mismatch")
        provenance = ImageAiProvenance.safe_validate(
            {
                "answer_schema_version": ANSWER_SCHEMA_VERSION,
                "model": attempt.get("model"),
                "prompt_sha256": attempt.get("prompt_sha256"),
                "input_mode": "fresh_codex_exec_image",
                "approval_ref": attempt.get("approval"),
            }
        )
        bound = _binding(record, manifest_sha256)
        observed = build_image_ai_shadow_result(
            raw_answer=answer,
            binding=bound,
            provenance=provenance,
            identity_key=identity_key,
        )
        candidate_amounts = (
            (observed.amount_yen,) if observed.amount_yen is not None else ()
        )
        signals = build_image_ai_admission_signals(
            result=observed,
            raw_answer=answer,
            observed_candidate_amounts=candidate_amounts,
            manual_conflict_state="none_known",
            identity_key=identity_key,
        )
        outcome = evaluate_image_ai_admission(
            observed,
            signals,
            current_binding=bound,
            current_provenance=provenance,
            crop_bytes=crop_bytes,
            identity_key=identity_key,
            policy=policy,
        )
        rows.append(
            {
                "unit": unit,
                "admission_verdict": outcome.verdict,
                "admission_reason": outcome.admission_reason,
                "amount_yen": outcome.amount_yen,
                "provenance_status": outcome.provenance_status,
                "ambiguity_status": outcome.ambiguity_status,
                "authority_boundary_result": outcome.authority_boundary_result,
                "production_authorized": outcome.production_authorized,
                "write_authorized": outcome.write_authorized,
                "write_plan_created": outcome.write_plan_created,
            }
        )
    if seen != set(range(1, 11)) or manifest.get("unit_count") != 10:
        raise ReplayRejected("invalid_fixed_unit_set")
    rows.sort(key=lambda row: row["unit"])
    admitted = sum(
        row["admission_verdict"]
        == "AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION"
        for row in rows
    )
    return {
        "schema_version": "medical-image-ai-admission-local-replay-v1",
        "units": rows,
        "summary": {
            "unit_count": 10,
            "auto_admitted": admitted,
            "requires_human_review": 10 - admitted,
            "ground_truth_used_for_admission": False,
            "ocr_candidate_match_used_for_admission": False,
            "ai_confidence_used_for_admission": False,
            "external_ai_calls": 0,
            "http_calls": 0,
            "production_writes": 0,
            "drive_sheets_writes": 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_root", type=Path)
    args = parser.parse_args()
    try:
        report = replay_admission(args.artifact_root)
    except (
        OSError,
        AdmissionShadowValidationError,
        ImageAiShadowValidationError,
        ReplayRejected,
    ):
        raise SystemExit("medical_image_ai_admission_replay_rejected") from None
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
