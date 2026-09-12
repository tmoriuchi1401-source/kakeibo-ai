"""Read-only replay of locally stored Medical image-AI evaluation results.

This adapter performs no external calls and writes nothing.  It validates the
saved manifest, crop bytes, answer, invocation metadata, and human comparison
binding before exercising the shadow-only authority-boundary contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from app.medical_image_ai_result_shadow import (
    ANSWER_SCHEMA_VERSION,
    ImageAiCropBinding,
    ImageAiProvenance,
    ImageAiShadowValidationError,
    build_image_ai_shadow_result,
    evaluate_image_ai_for_authority_boundary,
)


class ReplayRejected(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReplayRejected("local_artifact_unreadable") from None
    if type(value) is not dict:
        raise ReplayRejected("invalid_local_schema")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        raise ReplayRejected("local_artifact_unreadable") from None


def _strict_int(value: object, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ReplayRejected("invalid_local_schema")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or len(value) != 64:
        raise ReplayRejected("invalid_local_schema")
    try:
        int(value, 16)
    except ValueError:
        raise ReplayRejected("invalid_local_schema") from None
    if value != value.lower():
        raise ReplayRejected("invalid_local_schema")
    return value


def _overrides(values: list[str]) -> dict[int, int]:
    result: dict[int, int] = {}
    for value in values:
        parts = value.split("=", 1)
        if len(parts) != 2 or not all(part.isascii() and part.isdecimal() for part in parts):
            raise ReplayRejected("invalid_ground_truth_override")
        unit, amount = (int(part) for part in parts)
        if unit in result or not 1 <= unit <= 10_000 or not 1 <= amount <= 999_999_999:
            raise ReplayRejected("invalid_ground_truth_override")
        result[unit] = amount
    return result


def _human_ground_truth(session: dict[str, Any]) -> dict[int, dict[str, Any]]:
    if session.get("schema_version") != "medical-local-review-trial-v1":
        raise ReplayRejected("invalid_human_session_schema")
    records = session.get("records")
    if type(records) is not list:
        raise ReplayRejected("invalid_human_session_schema")
    results: dict[int, dict[str, Any]] = {}
    for row in records:
        if type(row) is not dict or row.get("mode") != "assisted":
            continue
        unit = _strict_int(row.get("unit"))
        if unit in results:
            raise ReplayRejected("duplicate_human_unit")
        if (
            row.get("local_saved") is not True
            or row.get("interrupted") is not False
            or row.get("production_write") is not False
        ):
            raise ReplayRejected("incomplete_human_review")
        results[unit] = {
            "amount": _strict_int(row.get("confirmed_amount")),
            "page": _strict_int(row.get("page")),
            "source_sha256": _digest(row.get("source_sha256")),
            "source_image_sha256": _digest(row.get("image_sha256")),
        }
    return results


def _binding(record: dict[str, Any], manifest_sha256: str) -> ImageAiCropBinding:
    coordinates = record.get("crop_coordinates_original")
    if type(coordinates) is not list or len(coordinates) != 4:
        raise ReplayRejected("invalid_manifest_binding")
    try:
        return ImageAiCropBinding.safe_validate(
            {
                "source_sha256": record.get("source_sha256"),
                "source_image_sha256": record.get("source_image_sha256"),
                "unit": record.get("unit"),
                "page": record.get("page"),
                "crop_sha256": record.get("output_sha256"),
                "crop_coordinates_original": tuple(coordinates),
                "rotation_clockwise_degrees": record.get("rotation_clockwise_degrees"),
                "crop_provenance": record.get("source_crop_provenance"),
                "manifest_sha256": manifest_sha256,
            }
        )
    except ImageAiShadowValidationError:
        raise ReplayRejected("invalid_manifest_binding") from None


def replay(
    artifact_root: Path,
    human_session_path: Path,
    ground_truth_overrides: dict[int, int],
) -> dict[str, Any]:
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
    if type(records) is not list or not records:
        raise ReplayRejected("invalid_manifest_state")
    human = _human_ground_truth(_read_json(human_session_path.resolve()))
    identity_key = hashlib.sha256(b"local-shadow-replay-v1\0" + manifest_bytes).digest()
    seen: set[int] = set()
    rows: list[dict[str, Any]] = []
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
            != {"A_content": True, "B_privacy": True, "C_qr_barcode": True, "D_boundary": True}
        ):
            raise ReplayRejected("crop_not_approved")
        output_file = record.get("output_file")
        if type(output_file) is not str or Path(output_file).name != output_file:
            raise ReplayRejected("invalid_manifest_binding")
        crop_path = artifact_root / output_file
        crop_bytes = crop_path.read_bytes()
        if not hmac_compare(_sha256_bytes(crop_bytes), _digest(record.get("output_sha256"))):
            raise ReplayRejected("crop_digest_mismatch")
        ground = human.get(unit)
        if ground is None or any(
            (
                record.get("page") != ground["page"],
                record.get("source_sha256") != ground["source_sha256"],
                record.get("source_image_sha256") != ground["source_image_sha256"],
            )
        ):
            raise ReplayRejected("human_source_binding_mismatch")
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
            or execution.get("candidate_present") is not (answer.get("status") == "readable")
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
        outcome = evaluate_image_ai_for_authority_boundary(
            observed,
            current_binding=bound,
            current_provenance=provenance,
            crop_bytes=crop_bytes,
            identity_key=identity_key,
        )
        duplicate = evaluate_image_ai_for_authority_boundary(
            observed,
            current_binding=bound,
            current_provenance=provenance,
            crop_bytes=crop_bytes,
            identity_key=identity_key,
            previously_accepted_result_id=observed.result_id,
        )
        truth = ground_truth_overrides.get(unit, ground["amount"])
        rows.append(
            {
                "unit": unit,
                "validation": outcome.status,
                "duplicate_check": duplicate.status,
                "matches_ground_truth": observed.amount_yen == truth,
                "ground_truth_provenance": (
                    "explicit_adjudication_override"
                    if unit in ground_truth_overrides
                    else "saved_assisted_human_review"
                ),
                "production_authorized": outcome.production_authorized,
                "write_authorized": outcome.write_authorized,
                "write_plan_created": outcome.write_plan_created,
            }
        )
    rows.sort(key=lambda row: row["unit"])
    expected_count = _strict_int(manifest.get("unit_count"))
    if len(rows) != expected_count or set(human) != seen:
        raise ReplayRejected("unit_set_mismatch")
    validated = sum(
        row["validation"] == "validated_for_authority_evaluation" for row in rows
    )
    matched = sum(row["matches_ground_truth"] for row in rows)
    return {
        "schema_version": "medical-image-ai-local-replay-report-v1",
        "units": rows,
        "summary": {
            "unit_count": len(rows),
            "validated_for_authority_evaluation": validated,
            "ground_truth_matches": matched,
            "failures": len(rows) - validated,
            "external_ai_calls": 0,
            "http_calls": 0,
            "production_writes": 0,
            "drive_sheets_writes": 0,
        },
    }


def hmac_compare(left: str, right: str) -> bool:
    # Kept local so this read-only adapter has no dependency on shadow internals.
    import hmac

    return hmac.compare_digest(left, right)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("human_session", type=Path)
    parser.add_argument(
        "--ground-truth-override",
        action="append",
        default=[],
        metavar="UNIT=AMOUNT",
    )
    args = parser.parse_args()
    try:
        report = replay(
            args.artifact_root,
            args.human_session,
            _overrides(args.ground_truth_override),
        )
    except (OSError, ImageAiShadowValidationError, ReplayRejected):
        raise SystemExit("medical_image_ai_replay_rejected") from None
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
