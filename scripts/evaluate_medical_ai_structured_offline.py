"""Offline-only aggregate evaluation for designated real medical fixtures.

The script never prints filenames, OCR text, token IDs, amounts, geometry, model
paths, or source identifiers. RapidOCR runs in its existing guarded worker.
"""
from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
from pathlib import Path

from app.medical_ai_structured_shadow import (
    evaluate_real_medical_offline,
    prepare_structured_shadow_from_level2,
)
from app.medical_ocr_observation_shadow import ReceiptImage
from app.medical_rapidocr_shadow import (
    ModelAsset,
    RapidOcrManifest,
    RapidOcrShadowAdapter,
    SUPPORTED_ASSETS,
)
from app.medical_payment_level2_shadow import evaluate_level2_payment_shadow


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime(private_root: Path) -> tuple[RapidOcrManifest, str]:
    assets: dict[str, Path] = {}
    expected = {value: role for role, value in SUPPORTED_ASSETS.items()}
    for path in private_root.rglob("*.onnx"):
        digest = _sha256(path)
        role = expected.get(digest)
        if role is not None and role not in assets:
            assets[role] = path.resolve()
    if set(assets) != set(SUPPORTED_ASSETS):
        raise RuntimeError("offline_runtime_unavailable")
    executables = [
        path.resolve()
        for path in private_root.rglob("python.exe")
        if (path.parent / "Lib" / "site-packages" / "rapidocr").exists()
        or (path.parent.parent / "Lib" / "site-packages" / "rapidocr").exists()
    ]
    if len(executables) != 1:
        raise RuntimeError("offline_runtime_unavailable")
    manifest = RapidOcrManifest(
        ModelAsset(str(assets["det"]), SUPPORTED_ASSETS["det"]),
        ModelAsset(str(assets["cls"]), SUPPORTED_ASSETS["cls"]),
        ModelAsset(str(assets["rec"]), SUPPORTED_ASSETS["rec"]),
    )
    return manifest, str(executables[0])


def _selected_files(search_root: Path) -> tuple[Path, ...]:
    """Select the unique nine-file corpus recorded by the prior checkpoint."""
    by_parent: dict[Path, list[Path]] = {}
    for path in search_root.rglob("*"):
        if (
            path.is_file()
            and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".pdf"}
            and not any("rapidocr-runtime" in part.casefold() for part in path.parts)
        ):
            by_parent.setdefault(path.parent, []).append(path)
    candidates = [files for files in by_parent.values() if len(files) == 9]
    if len(candidates) != 1:
        raise RuntimeError("offline_corpus_unavailable")
    return tuple(sorted(candidates[0], key=lambda value: value.name.casefold()))


def _images(path: Path):
    content = path.read_bytes()
    if path.suffix.casefold() != ".pdf":
        yield content
        return
    from pypdf import PdfReader
    import pypdfium2 as pdfium
    from app.medical_layout_local import _bounded_render_scale

    with BytesIO(content) as source:
        reader = PdfReader(source)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 3:
            return
    document = pdfium.PdfDocument(content)
    try:
        for index in range(len(document)):
            page = document[index]
            bitmap = image = None
            try:
                bitmap = page.render(scale=_bounded_render_scale(*page.get_size()))
                image = bitmap.to_pil()
                output = BytesIO()
                image.save(output, format="PNG")
                yield output.getvalue()
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
    finally:
        document.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("private_root", type=Path)
    parser.add_argument("corpus_search_root", type=Path)
    args = parser.parse_args()
    private_root = args.private_root.resolve()
    corpus_search_root = args.corpus_search_root.resolve()
    if not private_root.is_dir() or not corpus_search_root.is_dir():
        raise SystemExit("offline_private_root_unavailable")
    files = _selected_files(corpus_search_root)
    manifest, executable = _runtime(private_root)
    adapter = RapidOcrShadowAdapter(manifest, python_executable=executable)
    totals = {
        "selected_files": len(files),
        "receipt_units": 0,
        "observed_units": 0,
        "incomplete_units": 0,
        "anonymous_payload_generated": 0,
        "real_medical_outbound_rejected": 0,
        "level2_connected": 0,
        "evidence_binding_possible": 0,
        "review_first_required": 0,
        "production_authorized": 0,
        "write_authorized": 0,
        "external_ai_http_attempts": 0,
        "drive_sheets_writes": 0,
    }
    for file_ordinal, path in enumerate(files, 1):
        try:
            images = tuple(_images(path))
        except Exception:
            images = ()
        if not images:
            totals["receipt_units"] += 1
            totals["incomplete_units"] += 1
            continue
        for page_ordinal, image_bytes in enumerate(images, 1):
            totals["receipt_units"] += 1
            observation = adapter.observe(
                ReceiptImage(f"real-medical-offline-{file_ordinal}-{page_ordinal}", 1, image_bytes)
            )
            if not observation.complete:
                totals["incomplete_units"] += 1
                continue
            totals["observed_units"] += 1
            try:
                preparation = prepare_structured_shadow_from_level2(
                    observation,
                    evaluate_level2_payment_shadow(observation),
                    source_kind="real_medical",
                )
                build = preparation.build
                totals["level2_connected"] += 1
                result = evaluate_real_medical_offline(build, observation)
            except Exception:
                totals["incomplete_units"] += 1
                continue
            for key in (
                "anonymous_payload_generated",
                "real_medical_outbound_rejected",
                "evidence_binding_possible",
                "review_first_required",
                "production_authorized",
                "write_authorized",
            ):
                totals[key] += result[key]
    print(json.dumps(totals, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
