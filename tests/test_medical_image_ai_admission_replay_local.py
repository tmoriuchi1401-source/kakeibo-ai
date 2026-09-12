from __future__ import annotations

import hashlib
import json

from scripts.replay_medical_image_ai_admission_local import (
    FIXED_APPROVAL_REF,
    FIXED_MODEL,
    FIXED_PROMPT_SHA256,
    replay_admission,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _artifacts(tmp_path, *, negative_unit=None):
    root = tmp_path / "fixed-ten"
    root.mkdir()
    records = []
    for unit in range(1, 11):
        crop = f"synthetic-crop-{unit}".encode("ascii")
        crop_sha = _digest(crop)
        filename = f"unit-{unit:02d}.png"
        (root / filename).write_bytes(crop)
        records.append(
            {
                "unit": unit,
                "page": 1,
                "source_sha256": _digest(f"source-{unit}".encode("ascii")),
                "source_image_sha256": _digest(f"image-{unit}".encode("ascii")),
                "source_crop_provenance": "anchor-auto",
                "crop_coordinates_original": [10, 20, 110, 220],
                "rotation_clockwise_degrees": 0,
                "output_file": filename,
                "output_sha256": crop_sha,
                "metadata_removed": True,
                "human_send_review": "APPROVED_FOR_AI_EVAL",
                "review_checks": {
                    "A_content": True,
                    "B_privacy": True,
                    "C_qr_barcode": True,
                    "D_boundary": True,
                },
            }
        )
        result_dir = root / "ai-results" / f"unit-{unit:02d}"
        _write_json(
            result_dir / "answer.json",
            {
                "amount_yen": 100 + unit,
                "label_quote": (
                    "未収金 payment" if unit == negative_unit else "領収金額"
                ),
                "status": "readable",
                "reason": "",
            },
        )
        _write_json(
            result_dir / "attempt.json",
            {
                "unit": unit,
                "image_sha256": crop_sha,
                "origin": "human_approved_final_image",
                "approval": FIXED_APPROVAL_REF,
                "model": FIXED_MODEL,
                "prompt_sha256": FIXED_PROMPT_SHA256,
            },
        )
        _write_json(
            result_dir / "result.json",
            {
                "unit": unit,
                "exit_code": 0,
                "status": "readable",
                "candidate_present": True,
                "tool_calls": 0,
            },
        )
    _write_json(
        root / "manifest.json",
        {
            "schema_version": "medical-final-images-local-v1",
            "records": records,
            "unit_count": 10,
            "human_review_complete": True,
            "external_http": 0,
            "external_ai_requests": 0,
            "production_writes": 0,
            "drive_writes": 0,
            "sheets_writes": 0,
        },
    )
    return root


def test_fixed_replay_auto_admits_without_ground_truth_ocr_or_confidence(tmp_path):
    report = replay_admission(_artifacts(tmp_path))
    assert report["summary"]["auto_admitted"] == 10
    assert report["summary"]["requires_human_review"] == 0
    assert report["summary"]["ground_truth_used_for_admission"] is False
    assert report["summary"]["ocr_candidate_match_used_for_admission"] is False
    assert report["summary"]["ai_confidence_used_for_admission"] is False
    assert all(
        row["authority_boundary_result"]
        == "ready_for_existing_authority_evaluation"
        for row in report["units"]
    )


def test_fixed_replay_routes_negative_context_without_blocking_other_units(tmp_path):
    report = replay_admission(_artifacts(tmp_path, negative_unit=3))
    assert report["summary"]["auto_admitted"] == 9
    assert report["summary"]["requires_human_review"] == 1
    unit_three = report["units"][2]
    assert unit_three["admission_verdict"] == "REQUIRES_HUMAN_REVIEW"
    assert unit_three["admission_reason"] == "negative_context"
    assert unit_three["authority_boundary_result"] == "not_presented_to_authority"
