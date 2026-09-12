"""Prepare privacy-minimised pixel crops for an explicitly approved vision eval.

No image or OCR text leaves this process.  Output is new metadata-free PNG data
plus a data-minimised manifest in an explicitly supplied directory outside Git.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
import unicodedata

from PIL import Image

from scripts.evaluate_medical_ai_structured_offline import _images, _runtime, _selected_files
from app.medical_ocr_observation_shadow import ReceiptImage, TextRegion
from app.medical_payment_evidence import _NUMERIC_RUN
from app.medical_payment_level2_shadow import classify_structural_relation
from app.medical_rapidocr_shadow import RapidOcrShadowAdapter


_CONTEXT = frozenset({
    "支払額", "支払金額", "領収額", "領収金額", "請求額", "請求金額",
    "請求金額合計", "合計", "合計額", "合計金額", "負担金", "自費",
    "点数", "合計点数", "預り金", "預かり金", "釣銭", "お釣", "お釣り",
})
_PII = ("氏名", "患者", "番号", "保険", "生年月日", "住所", "診療", "病名", "処方", "薬剤", "電話")
_DATE_OR_ID = re.compile(r"(?:\d{2,4}[/.-]){1,2}\d{1,4}\Z")


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _safe_box(region: TextRegion):
    box = region.bbox
    if box is None or min(box[2:]) <= 0:
        return None
    return tuple(int(round(value)) for value in box)


def select_regions(regions: tuple[TextRegion, ...]) -> tuple[TextRegion, ...]:
    """Select context pixels and locally related numeric pixels, never OCR text."""
    eligible = tuple(r for r in regions if _safe_box(r) is not None and not any(p in _compact(r.text) for p in _PII))
    anchors = tuple(r for r in eligible if _compact(r.text).strip("：:¥￥円-") in _CONTEXT)
    selected = set(r.ordinal for r in anchors)
    for numeric in eligible:
        text = _compact(numeric.text)
        if not _NUMERIC_RUN.search(text) or _DATE_OR_ID.fullmatch(text):
            continue
        if numeric.ordinal in selected or any(
            classify_structural_relation(anchor, numeric).state != "UNRELATED"
            for anchor in anchors
        ):
            selected.add(numeric.ordinal)
    return tuple(r for r in regions if r.ordinal in selected)


def render_sanitized(image_bytes: bytes, regions: tuple[TextRegion, ...]) -> bytes:
    if not regions:
        raise ValueError("no_safe_payment_context_regions")
    with Image.open(BytesIO(image_bytes)) as source:
        source = source.convert("RGB")
        canvas = Image.new("RGB", source.size, "white")
        boxes = []
        for region in regions:
            x, y, width, height = _safe_box(region)
            pad = max(2, min(width, height) // 8)
            box = (max(0, x - pad), max(0, y - pad), min(source.width, x + width + pad), min(source.height, y + height + pad))
            canvas.paste(source.crop(box), box[:2]); boxes.append(box)
        left=min(b[0] for b in boxes); top=min(b[1] for b in boxes)
        right=max(b[2] for b in boxes); bottom=max(b[3] for b in boxes)
        margin=max(8, int(max(right-left, bottom-top)*0.03))
        crop=canvas.crop((max(0,left-margin),max(0,top-margin),min(canvas.width,right+margin),min(canvas.height,bottom+margin)))
        output=BytesIO(); crop.save(output,format="PNG",optimize=True)
        return output.getvalue()


def _outside_repo(path: Path, repository_root: Path) -> Path:
    target=path.resolve()
    try: target.relative_to(repository_root.resolve())
    except ValueError: return target
    raise ValueError("output_must_be_outside_repository")


def prepare(runtime_root: Path, corpus_root: Path, output_dir: Path, repository_root: Path):
    output_dir=_outside_repo(output_dir,repository_root); output_dir.mkdir(parents=True,exist_ok=False)
    manifest, executable=_runtime(runtime_root.resolve())
    adapter=RapidOcrShadowAdapter(manifest,python_executable=executable)
    records=[]; unit=0
    for path in _selected_files(corpus_root.resolve()):
        for image_bytes in _images(path):
            unit+=1
            observation=adapter.observe(ReceiptImage(f"vision-eval-unit-{unit}",1,image_bytes))
            selected=select_regions(observation.regions)
            record={"unit":unit,"status":"not_prepared","reason":"no_safe_payment_context_regions",
                    "selected_region_count":len(selected),"manual_crop_used":False,"requires_local_visual_approval":True}
            if selected:
                payload=render_sanitized(image_bytes,selected)
                target=output_dir/f"unit-{unit:02d}.png"; target.write_bytes(payload)
                record.update(status="prepared",reason="payment_context_pixel_crop",image_file=target.name,
                              image_sha256=hashlib.sha256(payload).hexdigest())
            records.append(record)
    if unit != 10: raise ValueError("fixed_corpus_unit_count_mismatch")
    summary={"schema_version":"medical-vision-eval-preparation-v1","records":records,
             "prepared_count":sum(r["status"]=="prepared" for r in records),
             "manual_crop_count":0,"original_images_in_output":0,"external_requests":0,
             "production_writes":0,"drive_sheets_writes":0}
    (output_dir/"manifest.json").write_text(json.dumps(summary,indent=2,sort_keys=True),encoding="utf-8")
    return output_dir


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("runtime_root",type=Path); parser.add_argument("corpus_root",type=Path)
    parser.add_argument("output_dir",type=Path); parser.add_argument("repository_root",type=Path)
    args=parser.parse_args(); print(prepare(args.runtime_root,args.corpus_root,args.output_dir,args.repository_root))


if __name__ == "__main__": main()
