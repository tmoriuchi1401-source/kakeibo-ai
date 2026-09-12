"""OCR-anchor-guided local crops for the fixed Medical evaluation corpus.

This is an offline evaluation tool, never a production selector or outbound
authority.  It runs the existing OCR once per original image, finds payment
label shapes without using known amounts, and then asks pixel structure for
the smallest useful enclosure.  Manual crops are opened only after automatic
decisions have been frozen and are used solely for evaluation.
"""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata

from PIL import Image, ImageDraw

from app.medical_ocr_observation_shadow import ReceiptImage
from app.medical_rapidocr_shadow import RapidOcrShadowAdapter
from app.medical_rapidocr_worker import offline_guard
from scripts.crop_medical_vision_local import source_cards
from scripts.evaluate_medical_ai_structured_offline import _runtime
from scripts.medical_table_crop_local import (
    detect_tables, inspect_crop, intersection, original_rectangle, png,
    region_box, sha, source_payload,
)

VERSION = "medical-anchor-crop-local-v1"
STRONG_LABELS = ("領収金額", "請求額", "支払額")
OTHER_LABELS = ("合計金額", "合計", "金額")
NUMERIC = re.compile(r"(?<!\d)(?:\d{1,3}(?:[,，]\d{3})+|\d{3,7})(?!\d)")


def compact(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return "".join(c for c in value if not c.isspace() and c not in ":：()（）[]【】")


def edit_distance(left: str, right: str) -> int:
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        next_row = [i]
        for j, b in enumerate(right, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1,
                                row[j - 1] + (a != b)))
        row = next_row
    return row[-1]


def label_match(value: str):
    """Return only a cue class/strength; never return the observed OCR text."""
    value = compact(value)
    for label in STRONG_LABELS:
        if label in value:
            return "exact_strong", 9
    for label in OTHER_LABELS:
        if label in value:
            return "exact_other", 5 if label == "合計金額" else 2
    for label in STRONG_LABELS:
        width = len(label)
        if len(value) >= width - 1 and any(
                edit_distance(value[i:min(len(value), i + width)], label) <= 1
                for i in range(max(1, len(value) - width + 1))):
            return "near_strong", 7
    return None


def union_box(boxes):
    return [math.floor(min(b[0] for b in boxes)), math.floor(min(b[1] for b in boxes)),
            math.ceil(max(b[2] for b in boxes)), math.ceil(max(b[3] for b in boxes))]


def centers_close(left, right):
    lx, ly = (left[0] + left[2]) / 2, (left[1] + left[3]) / 2
    rx, ry = (right[0] + right[2]) / 2, (right[1] + right[3]) / 2
    lh, rh = left[3] - left[1], right[3] - right[1]
    lw, rw = left[2] - left[0], right[2] - right[0]
    row_overlap = max(0, min(left[3], right[3]) - max(left[1], right[1]))
    col_overlap = max(0, min(left[2], right[2]) - max(left[0], right[0]))
    return ((row_overlap >= .25 * min(lh, rh) and abs(rx - lx) <= 8 * max(lh, rh))
            or (col_overlap >= .20 * min(lw, rw) and abs(ry - ly) <= 5 * max(lh, rh)))


def split_fragment(region) -> bool:
    value = compact(region.text)
    return bool(value) and len(value) <= 4 and not any(c.isdigit() for c in value)


def reading_order(left_box, right_box) -> bool:
    """Allow only left-to-right or top-to-bottom adjacent fragments."""
    row_overlap = max(0, min(left_box[3], right_box[3]) - max(left_box[1], right_box[1]))
    col_overlap = max(0, min(left_box[2], right_box[2]) - max(left_box[0], right_box[0]))
    left_h, right_h = left_box[3] - left_box[1], right_box[3] - right_box[1]
    left_w, right_w = left_box[2] - left_box[0], right_box[2] - right_box[0]
    return ((row_overlap >= .25 * min(left_h, right_h) and left_box[0] <= right_box[0])
            or (col_overlap >= .20 * min(left_w, right_w) and left_box[1] <= right_box[1]))


def find_anchors(observation):
    regions = [(r, region_box(r)) for r in observation.regions]
    regions = [(r, b) for r, b in regions if b is not None]
    anchors = []
    for region, box in regions:
        match = label_match(region.text)
        if match:
            anchors.append({"ordinals": [region.ordinal], "box": box,
                            "cue": match[0], "strength": match[1]})
    # Split OCR such as 領収 + 金額 or vertical 金 + 額.  Bounded pairs and
    # triples only; this is not general language reconstruction.
    for i, (first, first_box) in enumerate(regions):
        if not split_fragment(first):
            continue
        nearby = [(second, second_box) for second, second_box in regions[i + 1:]
                  if split_fragment(second) and centers_close(first_box, second_box)
                  and reading_order(first_box, second_box)]
        for second, second_box in nearby:
            value = compact(first.text) + compact(second.text)
            match = label_match(value) if len(value) <= 8 else None
            if match:
                anchors.append({"ordinals": sorted([first.ordinal, second.ordinal]),
                                "box": union_box([first_box, second_box]),
                                "cue": "split_" + match[0], "strength": match[1] - .25})
            for third, third_box in nearby:
                if (third.ordinal <= second.ordinal or not centers_close(second_box, third_box)
                        or not reading_order(second_box, third_box)):
                    continue
                value = compact(first.text) + compact(second.text) + compact(third.text)
                match = label_match(value) if len(value) <= 8 else None
                if match:
                    anchors.append({"ordinals": sorted([first.ordinal, second.ordinal, third.ordinal]),
                                    "box": union_box([first_box, second_box, third_box]),
                                    "cue": "split_" + match[0], "strength": match[1] - .5})
    unique = {}
    for anchor in anchors:
        key = tuple(anchor["ordinals"])
        if key not in unique or anchor["strength"] > unique[key]["strength"]:
            unique[key] = anchor
    return list(unique.values())


def numeric_regions(observation):
    result = []
    for region in observation.regions:
        box = region_box(region)
        text = compact(region.text)
        if box is not None and NUMERIC.search(text):
            result.append({"ordinal": region.ordinal, "box": box})
    return result


def pair_relation(anchor, number):
    if number["ordinal"] in anchor["ordinals"]:
        return "same_region", 5.0
    a, n = anchor["box"], number["box"]
    ah, nh = a[3] - a[1], n[3] - n[1]
    aw, nw = a[2] - a[0], n[2] - n[0]
    vertical = max(0, min(a[3], n[3]) - max(a[1], n[1])) / max(1, min(ah, nh))
    horizontal = max(0, min(a[2], n[2]) - max(a[0], n[0])) / max(1, min(aw, nw))
    row_gap = n[0] - a[2]
    if vertical >= .25 and -max(ah, nh) <= row_gap <= 12 * max(ah, nh):
        return "row_right", 4.0 - max(0, row_gap) / max(1, 12 * max(ah, nh))
    column_gap = n[1] - a[3]
    if horizontal >= .15 and -max(ah, nh) <= column_gap <= 10 * max(ah, nh):
        return "column_below", 3.5 - max(0, column_gap) / max(1, 10 * max(ah, nh))
    # A nearby amount can still seed a local fallback, but remains visibly weak.
    ac = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    nc = ((n[0] + n[2]) / 2, (n[1] + n[3]) / 2)
    distance = math.hypot(ac[0] - nc[0], ac[1] - nc[1])
    if distance <= 12 * max(ah, nh):
        return "nearby", 1.0
    return None


def contains(box, target, slack=3):
    return (box[0] - slack <= target[0] and box[1] - slack <= target[1]
            and box[2] + slack >= target[2] and box[3] + slack >= target[3])


def detect_structures(payload):
    """Pixel-only strict tables plus partial line groups, in original pixels."""
    import cv2
    import numpy as np
    gray = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None or gray.size > 20_000_000:
        raise ValueError("invalid_image")
    h, w = gray.shape
    ink = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY_INV, 31, 15)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(16, w // 45), 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(16, h // 45))))
    combined = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    loose = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if cw < max(45, w * .06) or ch < max(25, h * .02) or cw * ch < w * h * .0015:
            continue
        hh = horizontal[y:y + ch, x:x + cw] > 0
        vv = vertical[y:y + ch, x:x + cw] > 0
        if not hh.any() or not vv.any():
            continue
        h_lines = int((hh.mean(axis=1) > .25).sum())
        v_lines = int((vv.mean(axis=0) > .25).sum())
        if h_lines and v_lines:
            loose.append({"box": [x, y, x + cw, y + ch], "kind": "line_group",
                          "line_support": min(12, h_lines + v_lines)})
    strict = [{**item, "kind": "closed_table", "line_support": 20}
              for item in detect_tables(payload)]
    return strict + loose


def detect_in_runtime(payload, executable):
    root = str(Path(__file__).resolve().parents[1])
    bootstrap = ("import sys;sys.path.insert(0," + repr(root) + ");"
                 "from scripts.medical_anchor_crop_local import structure_worker;structure_worker()")
    result = subprocess.run([executable, "-I", "-B", "-c", bootstrap], input=payload,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("structure_detection_failed")
    return json.loads(result.stdout)


def structure_worker():
    with offline_guard():
        structures = detect_structures(sys.stdin.buffer.read(20 * 1024 * 1024 + 1))
    sys.stdout.write(json.dumps(structures))


def local_fallback(pair_box, observation):
    width, height = observation.width, observation.height
    seed_h = max(12, pair_box[3] - pair_box[1])
    seed_w = max(12, pair_box[2] - pair_box[0])
    margin_x = max(width * .025, seed_h * 2.5)
    margin_y = max(height * .018, seed_h * 1.8)
    broad = [max(0, pair_box[0] - margin_x), max(0, pair_box[1] - margin_y),
             min(width, pair_box[2] + max(margin_x, seed_w * .20)),
             min(height, pair_box[3] + margin_y)]
    context = [pair_box]
    for region in observation.regions:
        box = region_box(region)
        if box is not None and intersection(broad, box):
            context.append(box)
    box = union_box(context)
    return [max(0, math.floor(box[0] - margin_x * .35)),
            max(0, math.floor(box[1] - margin_y * .35)),
            min(width, math.ceil(box[2] + margin_x * .35)),
            min(height, math.ceil(box[3] + margin_y * .35))]


def choose_crops(observation, structures):
    anchors, numbers = find_anchors(observation), numeric_regions(observation)
    choices = []
    for anchor in anchors:
        for number in numbers:
            relation = pair_relation(anchor, number)
            if relation is None:
                continue
            relation_name, relation_score = relation
            pair_box = union_box([anchor["box"], number["box"]])
            enclosing = [s for s in structures if contains(s["box"], pair_box)]
            if enclosing:
                structure = min(enclosing, key=lambda s: ((s["box"][2] - s["box"][0])
                    * (s["box"][3] - s["box"][1]), s["kind"] != "closed_table"))
                box, method = structure["box"], structure["kind"]
                structure_score = 3 if method == "closed_table" else 2
            else:
                box, method, structure_score = local_fallback(pair_box, observation), "local_rectangle", 0
            area = (box[2] - box[0]) * (box[3] - box[1]) / max(1, observation.width * observation.height)
            score = anchor["strength"] + relation_score + structure_score - min(3, area * 4)
            choices.append({"box": box, "method": method, "cue": anchor["cue"],
                            "relation": relation_name, "anchor_ordinals": anchor["ordinals"],
                            "numeric_ordinal": number["ordinal"], "score": round(score, 3)})
    choices.sort(key=lambda item: (-item["score"], item["box"][1], item["box"][0]))
    unique = []
    for choice in choices:
        if choice["box"] not in [item["box"] for item in unique]:
            unique.append(choice)
    return unique[:3]


def automatic_record(card, payload, source, observation, structures, candidates, output):
    unit = card["unit"]
    for index, candidate in enumerate(candidates, 1):
        name = f"unit-{unit:02d}-anchor-{index}.png"
        crop_bytes = png(source.crop(candidate["box"]))
        (output / name).write_bytes(crop_bytes)
        candidate.update(image_file=name, image_sha256=sha(crop_bytes),
                         inspection=inspect_crop(candidate["box"], observation))
        overlay = source.copy(); draw = ImageDraw.Draw(overlay)
        draw.rectangle(candidate["box"], outline="blue", width=max(2, source.width // 400))
        overlay_name = f"unit-{unit:02d}-position-{index}.png"
        (output / overlay_name).write_bytes(png(overlay)); candidate["overlay_file"] = overlay_name
    (output / f"source-{unit:02d}.png").write_bytes(png(source))
    return {**card, "source_size": list(source.size), "anchor_count": len(find_anchors(observation)),
            "numeric_region_count": len(numeric_regions(observation)), "structure_count": len(structures),
            "candidates": candidates, "selection": "top_scored_provisional" if candidates else "no_anchor_pair",
            "ocr_complete": observation.complete, "human_review": "unreviewed",
            "transmission_authorized": False, "production_authorized": False}


def add_manual_evaluation(record, manual, manual_root, output):
    if any(record[k] != manual[k] for k in ("unit", "page", "source_sha256", "image_sha256")):
        raise ValueError("manual_source_binding_mismatch")
    expected = f'unit-{record["unit"]:02d}.png'
    payload = (manual_root / expected).read_bytes()
    if manual["image_file"] != expected or sha(payload) != manual["prepared_sha256"]:
        raise ValueError("manual_image_changed")
    with Image.open(BytesIO(payload)) as image:
        (output / f"manual-{expected}").write_bytes(png(image))
    manual_box = original_rectangle(manual["crop"], manual["quarter_turns_ccw"], record["source_size"])
    masks = [original_rectangle(box, manual["quarter_turns_ccw"], record["source_size"])
             for box in manual["masks"]]
    for candidate in record["candidates"]:
        box = candidate["box"]
        manual_area = max(1, (manual_box[2] - manual_box[0]) * (manual_box[3] - manual_box[1]))
        crop_area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
        covered = intersection(box, manual_box) / manual_area
        candidate["manual_evaluation"] = {
            "manual_rect_covered_fraction": round(covered, 3),
            "crop_inside_manual_fraction": round(intersection(box, manual_box) / crop_area, 3),
            "intersecting_manual_masks": sum(intersection(box, mask) > 0 for mask in masks),
            "content_proxy": "retained" if covered >= .90 else "partial" if covered >= .50 else "insufficient",
            "privacy_status": "manual_review_required",
            "no_ground_truth_used_for_selection": True,
        }
    record["manual_image"] = f"manual-{expected}"


def evaluate(session_path, manual_root, runtime_root, output):
    output = output.resolve(); local = Path(os.environ["LOCALAPPDATA"]).resolve()
    if not output.is_relative_to(local) or output == local:
        raise ValueError("local_output_required")
    session_bytes = session_path.read_bytes(); cards = source_cards(json.loads(session_bytes))
    output.mkdir(parents=True, exist_ok=False)
    model_manifest, executable = _runtime(runtime_root.resolve())
    adapter = RapidOcrShadowAdapter(model_manifest, python_executable=executable)
    records = []; started = time.monotonic()
    for card in cards:
        unit_started = time.monotonic(); payload = source_payload(card)
        with Image.open(BytesIO(payload)) as image:
            source = image.convert("RGB")
        observation = adapter.observe(ReceiptImage(f"anchor-eval-{card['unit']}", card["page"], payload))
        structures = detect_in_runtime(payload, executable)
        candidates = choose_crops(observation, structures)
        record = automatic_record(card, payload, source, observation, structures, candidates, output)
        record["processing_seconds"] = round(time.monotonic() - unit_started, 3)
        records.append(record)
        print(json.dumps({"unit": card["unit"], "anchors": record["anchor_count"],
                          "candidates": len(candidates), "method": candidates[0]["method"] if candidates else None}),
              flush=True)
    frozen = json.dumps(records, sort_keys=True, ensure_ascii=False).encode("utf-8")
    (output / "automatic-results.json").write_bytes(frozen)
    manual_bytes = (manual_root / "manifest.json").read_bytes()
    manual_records = json.loads(manual_bytes)["records"]
    if sorted(r["unit"] for r in manual_records) != list(range(1, 11)):
        raise ValueError("manual_unit_mapping_invalid")
    by_unit = {r["unit"]: r for r in manual_records}
    for record in records:
        add_manual_evaluation(record, by_unit[record["unit"]], manual_root, output)
    top = [r["candidates"][0] for r in records if r["candidates"]]
    summary = {"schema_version": VERSION, "records": records,
        "automatic_results_sha256": hashlib.sha256(frozen).hexdigest(),
        "total_seconds": round(time.monotonic() - started, 3),
        "units_with_candidates": len(top), "no_candidate": 10 - len(top),
        "top_method_counts": {name: sum(c["method"] == name for c in top)
                              for name in ("closed_table", "line_group", "local_rectangle")},
        "content_proxy_retained": sum(c["manual_evaluation"]["content_proxy"] == "retained" for c in top),
        "content_proxy_partial": sum(c["manual_evaluation"]["content_proxy"] == "partial" for c in top),
        "content_proxy_insufficient": sum(c["manual_evaluation"]["content_proxy"] == "insufficient" for c in top),
        "privacy_manual_review_required": 10, "human_verified_success": None,
        "external_http": 0, "production_writes": 0, "drive_sheets_writes": 0,
        "ocr_source": "existing_offline_RapidOCR_single_original_pass",
        "known_or_candidate_amounts_used_for_selection": False,
        "manual_opened_after_automatic_freeze": True, "qr_barcode": "not_inspected"}
    if session_path.read_bytes() != session_bytes or (manual_root / "manifest.json").read_bytes() != manual_bytes:
        raise ValueError("reference_changed_during_evaluation")
    (output / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "records"}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session", type=Path); parser.add_argument("manual_root", type=Path)
    parser.add_argument("runtime_root", type=Path); parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with offline_guard():
        evaluate(args.session, args.manual_root, args.runtime_root, args.output)


if __name__ == "__main__":
    main()
