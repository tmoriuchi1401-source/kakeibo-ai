"""Prepare the fixed ten final Medical images for human send review.

This consumes a frozen anchor-crop evaluation.  The rectangles below are a
one-off, visually chosen evaluation plan based only on labels, ruling and
relative layout.  This file deliberately contains no OCR/crop heuristics,
known amounts, network client, production writer or review handoff.
"""
from __future__ import annotations

import argparse
import hashlib
import html
from io import BytesIO
import json
import os
from pathlib import Path

from PIL import Image

from scripts.medical_table_crop_local import png, sha

VERSION = "medical-final-images-local-v1"
PENDING = "PENDING_HUMAN_REVIEW"

# Coordinates are relative to the frozen top anchor crop.  None means retain
# the entire anchor crop.  They were selected without reading a saved answer,
# candidate amount, or expected AI answer.
PLAN = {
    1: {"provenance": "anchor-auto-rotated", "crop": None, "rotation": 270},
    2: {"provenance": "manually-narrowed", "crop": [370, 0, 826, 520], "rotation": 0},
    3: {"provenance": "manually-narrowed", "crop": [425, 235, 930, 395], "rotation": 0},
    4: {"provenance": "anchor-auto", "crop": None, "rotation": 0},
    5: {"provenance": "anchor-auto", "crop": None, "rotation": 0},
    6: {"provenance": "manually-narrowed", "crop": [555, 315, 1026, 499], "rotation": 0},
    7: {"provenance": "manually-narrowed", "crop": [710, 95, 1026, 468], "rotation": 0},
    8: {"provenance": "anchor-auto", "crop": None, "rotation": 0},
    9: {"provenance": "manually-narrowed", "crop": [1450, 650, 3011, 2143], "rotation": 0},
    10: {"provenance": "anchor-auto", "crop": None, "rotation": 0},
}


def inside(rect, size):
    if rect is None:
        return True
    left, top, right, bottom = rect
    return (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1])


def original_coordinates(anchor_box, relative, anchor_size):
    relative = relative or [0, 0, anchor_size[0], anchor_size[1]]
    return [anchor_box[0] + relative[0], anchor_box[1] + relative[1],
            anchor_box[0] + relative[2], anchor_box[1] + relative[3]]


def transform(payload, crop, rotation):
    with Image.open(BytesIO(payload)) as opened:
        source = opened.convert("RGB")
    if not inside(crop, source.size):
        raise ValueError("crop_outside_anchor_image")
    image = source.crop(crop) if crop is not None else source
    if rotation == 90:
        image = image.transpose(Image.Transpose.ROTATE_270)
    elif rotation == 270:
        image = image.transpose(Image.Transpose.ROTATE_90)
    elif rotation != 0:
        raise ValueError("unsupported_rotation")
    return png(image), list(source.size), list(image.size)


def prepare_record(record, anchor_root, output, plan):
    unit = record["unit"]
    if not record.get("candidates"):
        raise ValueError("frozen_anchor_candidate_missing")
    candidate = record["candidates"][0]
    source_name = candidate["image_file"]
    source_path = anchor_root / source_name
    source_payload = source_path.read_bytes()
    if sha(source_payload) != candidate["image_sha256"]:
        raise ValueError("anchor_crop_binding_mismatch")
    output_payload, anchor_size, output_size = transform(
        source_payload, plan["crop"], plan["rotation"])
    final_name = f"unit-{unit:02d}.png"
    final_path = output / final_name
    final_path.write_bytes(output_payload)
    return {
        "unit": unit,
        "page": record["page"],
        "source_sha256": record["source_sha256"],
        "source_image_sha256": record["image_sha256"],
        "source_anchor_file": source_name,
        "source_anchor_sha256": candidate["image_sha256"],
        "source_anchor_box_original": candidate["box"],
        "source_anchor_size": anchor_size,
        "source_crop_provenance": plan["provenance"],
        "crop_coordinates_anchor_relative": plan["crop"] or [0, 0, *anchor_size],
        "crop_coordinates_original": original_coordinates(candidate["box"], plan["crop"], anchor_size),
        "rotation_clockwise_degrees": plan["rotation"],
        "output_file": final_name,
        "output_path": str(final_path.resolve()),
        "output_sha256": hashlib.sha256(output_payload).hexdigest(),
        "output_size": output_size,
        "metadata_removed": True,
        "human_send_review": PENDING,
        "review_checks": {"A_content": None, "B_privacy": None,
                          "C_qr_barcode": None, "D_boundary": None},
    }


def review_html(records):
    parts = ["<!doctype html><html lang='ja'><meta charset='utf-8'>",
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'\">",
        "<title>Medical AI送信前 最終画像確認</title>",
        "<style>body{font-family:system-ui;margin:24px;background:#eee;color:#222}section{background:#fff;margin:18px 0;padding:18px}img{display:block;max-width:100%;max-height:70vh;border:1px solid #999;background:#fff}code{font-size:1.05em}.pending{color:#a33;font-weight:700}</style>",
        "<h1>Medical AI送信前 最終画像10枚</h1>",
        "<p class='pending'>まだ外部送信は承認されていません。OCRのPII未検出は承認理由にしないでください。</p>",
        "<p>全unitについて A: 支払総額の判断に必要な情報、B: 不要な個人・診療情報なし、C: QR/barcodeなし、D: 境界欠けなし、を目視してください。</p>"]
    for record in records:
        unit = record["unit"]; name = html.escape(record["output_file"])
        parts.append(f"<section><h2>Unit {unit}</h2><p>provenance: {html.escape(record['source_crop_provenance'])} / rotation: {record['rotation_clockwise_degrees']}°</p>")
        parts.append(f"<a href='{name}'><img src='{name}' alt='Unit {unit} final crop'></a>")
        parts.append("<p>判定: <code>APPROVED_FOR_AI_EVAL</code> / <code>NEEDS_CROP_FIX</code> / <code>WITHHOLD</code></p></section>")
    parts.append("<h2>一括回答用</h2><pre>" + "\n".join(
        f"Unit {r['unit']}: " for r in records) + "</pre></html>")
    return "\n".join(parts)


def prepare(anchor_results, output):
    output = output.resolve(); local = Path(os.environ["LOCALAPPDATA"]).resolve()
    if not output.is_relative_to(local) or output == local:
        raise ValueError("local_output_required")
    anchor_results = anchor_results.resolve(); anchor_root = anchor_results.parent
    frozen_bytes = anchor_results.read_bytes(); frozen = json.loads(frozen_bytes)
    if frozen.get("schema_version") != "medical-anchor-crop-local-v1":
        raise ValueError("unexpected_anchor_results")
    records_by_unit = {record["unit"]: record for record in frozen["records"]}
    if sorted(records_by_unit) != list(range(1, 11)) or sorted(PLAN) != list(range(1, 11)):
        raise ValueError("fixed_unit_mapping_invalid")
    output.mkdir(parents=True, exist_ok=False)
    records = [prepare_record(records_by_unit[unit], anchor_root, output, PLAN[unit])
               for unit in range(1, 11)]
    manifest = {"schema_version": VERSION,
        "source_anchor_results_path": str(anchor_results),
        "source_anchor_results_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
        "records": records, "unit_count": 10,
        "human_review_complete": False,
        "approved_for_ai_eval": [], "needs_crop_fix": [], "withhold": [],
        "ground_truth_or_candidate_amount_used_for_crop_selection": False,
        "external_http": 0, "external_ai_requests": 0, "production_writes": 0,
        "drive_writes": 0, "sheets_writes": 0}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "index.html").write_text(review_html(records), encoding="utf-8")
    if anchor_results.read_bytes() != frozen_bytes:
        raise ValueError("anchor_results_changed_during_preparation")
    print(json.dumps({"unit_count": 10,
        "provenance_counts": {name: sum(r["source_crop_provenance"] == name for r in records)
            for name in ("anchor-auto", "anchor-auto-rotated", "manually-narrowed", "existing-manual-reused")},
        "human_send_review": PENDING, "external_http": 0, "external_ai_requests": 0,
        "production_writes": 0, "drive_writes": 0, "sheets_writes": 0}))


def approve_human_review(manifest_path):
    """Record one explicit all-unit A-D visual decision; never send or hand off."""
    original = manifest_path.read_bytes(); manifest = json.loads(original)
    if manifest.get("schema_version") != VERSION or manifest.get("unit_count") != 10:
        raise ValueError("unexpected_final_manifest")
    if manifest.get("human_review_complete"):
        raise ValueError("human_review_already_complete")
    root = manifest_path.parent
    for record in manifest["records"]:
        image = root / record["output_file"]
        if hashlib.sha256(image.read_bytes()).hexdigest() != record["output_sha256"]:
            raise ValueError("review_image_binding_mismatch")
        record["human_send_review"] = "APPROVED_FOR_AI_EVAL"
        record["review_checks"] = {"A_content": True, "B_privacy": True,
                                   "C_qr_barcode": True, "D_boundary": True}
    manifest["human_review_complete"] = True
    manifest["approved_for_ai_eval"] = list(range(1, 11))
    manifest["needs_crop_fix"] = []; manifest["withhold"] = []
    manifest["human_review_basis"] = "explicit_user_all_units_A_B_C_D_confirmed"
    temporary = manifest_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    print(json.dumps({"approved_for_ai_eval": list(range(1, 11)),
                      "needs_crop_fix": [], "withhold": [],
                      "external_http": 0, "external_ai_requests": 0}))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("prepare")
    build.add_argument("anchor_results", type=Path); build.add_argument("output", type=Path)
    review = sub.add_parser("approve-human-review")
    review.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.anchor_results, args.output)
    else:
        approve_human_review(args.manifest)


if __name__ == "__main__":
    main()
