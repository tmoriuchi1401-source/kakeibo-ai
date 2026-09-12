from __future__ import annotations

import hashlib
import json

import pytest

from scripts.replay_medical_image_ai_results_local import ReplayRejected, replay


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture(tmp_path, *, human_amount=999, ai_amount=630):
    root = tmp_path / "artifacts"
    crop = b"synthetic final crop"
    crop_sha = digest(crop)
    root.mkdir()
    (root / "unit-10.png").write_bytes(crop)
    record = {
        "unit": 10,
        "page": 2,
        "source_sha256": "1" * 64,
        "source_image_sha256": "2" * 64,
        "source_crop_provenance": "anchor-auto",
        "crop_coordinates_original": [10, 20, 110, 220],
        "rotation_clockwise_degrees": 0,
        "output_file": "unit-10.png",
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
    manifest = {
        "schema_version": "medical-final-images-local-v1",
        "records": [record],
        "unit_count": 1,
        "human_review_complete": True,
        "external_http": 0,
        "external_ai_requests": 0,
        "production_writes": 0,
        "drive_writes": 0,
        "sheets_writes": 0,
    }
    write_json(root / "manifest.json", manifest)
    result_dir = root / "ai-results" / "unit-10"
    write_json(
        result_dir / "answer.json",
        {
            "amount_yen": ai_amount,
            "label_quote": "synthetic label",
            "status": "readable",
            "reason": "",
        },
    )
    write_json(
        result_dir / "attempt.json",
        {
            "unit": 10,
            "image_sha256": crop_sha,
            "origin": "human_approved_final_image",
            "approval": "synthetic-explicit-approval",
            "model": "synthetic-model",
            "prompt_sha256": "3" * 64,
        },
    )
    write_json(
        result_dir / "result.json",
        {
            "unit": 10,
            "exit_code": 0,
            "status": "readable",
            "candidate_present": True,
            "tool_calls": 0,
        },
    )
    session = tmp_path / "session.json"
    write_json(
        session,
        {
            "schema_version": "medical-local-review-trial-v1",
            "records": [
                {
                    "mode": "assisted",
                    "unit": 10,
                    "page": 2,
                    "source_sha256": "1" * 64,
                    "image_sha256": "2" * 64,
                    "confirmed_amount": human_amount,
                    "local_saved": True,
                    "interrupted": False,
                    "production_write": False,
                }
            ],
        },
    )
    return root, session


def test_replay_uses_explicit_adjudication_without_rewriting_human_provenance(tmp_path):
    root, session = fixture(tmp_path)
    report = replay(root, session, {10: 630})
    row = report["units"][0]
    assert row == {
        "unit": 10,
        "validation": "validated_for_authority_evaluation",
        "duplicate_check": "duplicate_replay",
        "matches_ground_truth": True,
        "ground_truth_provenance": "explicit_adjudication_override",
        "production_authorized": False,
        "write_authorized": False,
        "write_plan_created": False,
    }
    assert report["summary"]["validated_for_authority_evaluation"] == 1
    assert report["summary"]["ground_truth_matches"] == 1


def test_replay_does_not_silently_replace_saved_human_value(tmp_path):
    root, session = fixture(tmp_path)
    report = replay(root, session, {})
    assert report["units"][0]["matches_ground_truth"] is False
    assert report["units"][0]["ground_truth_provenance"] == "saved_assisted_human_review"


def test_replay_rejects_cross_unit_source_binding(tmp_path):
    root, session = fixture(tmp_path)
    data = json.loads(session.read_text(encoding="utf-8"))
    data["records"][0]["source_sha256"] = "a" * 64
    write_json(session, data)
    with pytest.raises(ReplayRejected, match="human_source_binding_mismatch"):
        replay(root, session, {10: 630})


def test_replay_rejects_crop_tampering(tmp_path):
    root, session = fixture(tmp_path)
    (root / "unit-10.png").write_bytes(b"tampered")
    with pytest.raises(ReplayRejected, match="crop_digest_mismatch"):
        replay(root, session, {10: 630})
