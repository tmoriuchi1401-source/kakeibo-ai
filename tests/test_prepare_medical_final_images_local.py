"""Targeted synthetic tests for final local image preparation."""
import ast
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

from scripts import prepare_medical_final_images_local as final


def payload(size=(100, 60)):
    image = Image.new("RGB", size, "white"); image.info["private"] = "synthetic"
    return final.png(image)


def test_rotation_keeps_binding_fields_and_removes_metadata(tmp_path):
    anchor_root = tmp_path / "anchor"; output = tmp_path / "out"
    anchor_root.mkdir(); output.mkdir()
    source = payload(); (anchor_root / "candidate.png").write_bytes(source)
    record = {"unit": 1, "page": 1, "source_sha256": "a" * 64,
              "image_sha256": "b" * 64,
              "candidates": [{"image_file": "candidate.png", "image_sha256": final.sha(source),
                              "box": [10, 20, 110, 80]}]}
    made = final.prepare_record(record, anchor_root, output,
        {"provenance": "anchor-auto-rotated", "crop": None, "rotation": 90})
    assert made["source_sha256"] == "a" * 64
    assert made["source_image_sha256"] == "b" * 64
    assert made["source_anchor_sha256"] == final.sha(source)
    assert made["output_size"] == [60, 100]
    with Image.open(output / "unit-01.png") as image:
        assert not image.info


def test_counterclockwise_rotation_is_supported():
    transformed, source_size, output_size = final.transform(payload(), None, 270)
    assert source_size == [100, 60] and output_size == [60, 100]
    with Image.open(BytesIO(transformed)) as image:
        assert image.size == (60, 100)


def test_crop_must_be_inside_anchor_image():
    with pytest.raises(ValueError, match="crop_outside_anchor_image"):
        final.transform(payload(), [0, 0, 101, 60], 0)


def test_manifest_record_matches_written_image(tmp_path):
    anchor_root = tmp_path / "anchor"; output = tmp_path / "out"
    anchor_root.mkdir(); output.mkdir()
    source = payload(); (anchor_root / "candidate.png").write_bytes(source)
    record = {"unit": 4, "page": 1, "source_sha256": "a" * 64,
              "image_sha256": "b" * 64,
              "candidates": [{"image_file": "candidate.png", "image_sha256": final.sha(source),
                              "box": [10, 20, 110, 80]}]}
    made = final.prepare_record(record, anchor_root, output,
        {"provenance": "anchor-auto", "crop": None, "rotation": 0})
    assert final.sha((output / made["output_file"]).read_bytes()) == made["output_sha256"]
    assert made["human_send_review"] == final.PENDING


def test_source_has_no_ground_truth_or_external_authority():
    source = Path(final.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported.intersection({"requests", "urllib", "app.receipt_pipeline",
                                      "app.medical_gemini_shadow"})
    forbidden = ("original_candidate", "human_entered_amount", "confirmed_amount", "expected_amount")
    assert not any(name in source for name in forbidden)
    assert final.PLAN[2]["crop"] == [370, 0, 826, 520]


def test_review_html_has_no_remote_resource_or_script():
    value = final.review_html([{"unit": 1, "output_file": "unit-01.png",
        "source_crop_provenance": "anchor-auto", "rotation_clockwise_degrees": 0}])
    assert "http://" not in value and "https://" not in value and "<script" not in value
    assert "connect-src 'none'" in value and "APPROVED_FOR_AI_EVAL" in value


def test_explicit_human_review_binds_all_images(tmp_path):
    records = []
    for unit in range(1, 11):
        name = f"unit-{unit:02d}.png"; data = payload(); (tmp_path / name).write_bytes(data)
        records.append({"unit": unit, "output_file": name, "output_sha256": final.sha(data),
                        "human_send_review": final.PENDING,
                        "review_checks": {"A_content": None, "B_privacy": None,
                                          "C_qr_barcode": None, "D_boundary": None}})
    manifest = {"schema_version": final.VERSION, "unit_count": 10, "records": records,
                "human_review_complete": False, "approved_for_ai_eval": [],
                "needs_crop_fix": [], "withhold": []}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    final.approve_human_review(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["human_review_complete"] is True
    assert saved["approved_for_ai_eval"] == list(range(1, 11))
    assert all(r["human_send_review"] == "APPROVED_FOR_AI_EVAL" for r in saved["records"])
    assert all(all(r["review_checks"].values()) for r in saved["records"])
