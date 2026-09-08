"""Read-only, privacy-safe observation of payroll PDF extraction paths.

No production module imports this observer.  It retains text and token geometry
only while evaluating one caller-supplied PDF, and returns anonymous counts and
keyed source identity.  It cannot write, adopt, or alter an extraction path.
"""
from dataclasses import dataclass
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import secrets
import subprocess

from .payroll_ocr import PositionedText


@dataclass(frozen=True)
class PathObservation:
    status: str
    page_count: int
    token_count: int
    complete: bool
    reason: str


@dataclass(frozen=True)
class ExtractionPathReport:
    source_id: str
    byte_length: int
    page_count: int
    standard: PathObservation
    visitor: PathObservation
    ocr: PathObservation
    production_input: str
    coordinate_snapshot_source: str
    same_materialization: bool
    canonical_snapshot: str
    ownership_snapshot: str
    rerun_stable: bool


def _opaque_id(data: bytes, key: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _visitor_tokens(reader):
    tokens = []
    for number, page in enumerate(reader.pages, 1):
        height = float(page.mediabox.height)
        def visitor(value, cm, tm, font, font_size):
            value = value.strip()
            if value:
                size = float(font_size or 10)
                tokens.append(PositionedText(value, number, float(tm[4]), height-float(tm[5]),
                                              max(size, len(value)*size*.55), size, 100.0))
        page.extract_text(visitor_text=visitor)
    return tuple(tokens)


def _pdf_text_paths(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        empty = PathObservation("unsupported", len(reader.pages), 0, False, "encrypted_pdf")
        return empty, empty, ()
    high_level = tuple(page.extract_text() or "" for page in reader.pages)
    visitor = _visitor_tokens(reader)
    pages = len(reader.pages)
    standard = PathObservation("ok", pages, len(visitor), bool(visitor),
                               "embedded_text_available" if visitor else "embedded_text_empty")
    visitor_observation = PathObservation("ok", pages, len(visitor), bool(visitor),
                                          "visitor_tokens_available" if visitor else "visitor_tokens_empty")
    # pypdf's production helper uses text length to decide whether OCR is needed.
    return standard, visitor_observation, visitor, sum(len(value.strip()) for value in high_level)


def _ocr_environment(timeout_seconds):
    if importlib.util.find_spec("pypdfium2") is None:
        return PathObservation("unavailable", 0, 0, False, "pdf_renderer_unavailable")
    if importlib.util.find_spec("pytesseract") is None:
        return PathObservation("unavailable", 0, 0, False, "ocr_binding_unavailable")
    import pytesseract
    executable = str(pytesseract.pytesseract.tesseract_cmd)
    try:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                 timeout=timeout_seconds, check=False)
    except FileNotFoundError:
        return PathObservation("unavailable", 0, 0, False, "ocr_engine_not_found")
    except subprocess.TimeoutExpired:
        return PathObservation("timeout", 0, 0, False, "ocr_engine_version_timeout")
    if version.returncode:
        return PathObservation("unavailable", 0, 0, False, "ocr_engine_version_failed")
    try:
        languages = subprocess.run([executable, "--list-langs"], capture_output=True, text=True,
                                   timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return PathObservation("timeout", 0, 0, False, "ocr_language_query_timeout")
    if languages.returncode or "jpn" not in languages.stdout.splitlines() or "eng" not in languages.stdout.splitlines():
        return PathObservation("unavailable", 0, 0, False, "ocr_language_data_unavailable")
    return PathObservation("ready", 0, 0, True, "ocr_engine_and_languages_ready")


def _controlled_ocr(path, page_count, timeout_seconds):
    ready = _ocr_environment(timeout_seconds)
    if not ready.complete:
        return ready, ()
    try:
        import pypdfium2 as pdfium
        import pytesseract
        from PIL import ImageEnhance, ImageOps
        document = pdfium.PdfDocument(str(path))
        tokens = []
        for number, page in enumerate(document, 1):
            image = ImageEnhance.Contrast(ImageOps.grayscale(page.render(scale=3).to_pil())).enhance(1.7)
            if image.width < 1800:
                image = image.resize((image.width*2, image.height*2))
            data = pytesseract.image_to_data(image, lang="jpn+eng", config="--psm 6",
                                              timeout=timeout_seconds,
                                              output_type=pytesseract.Output.DICT)
            for index, value in enumerate(data["text"]):
                value = str(value).strip()
                if value:
                    tokens.append(PositionedText(value, number, float(data["left"][index]),
                                                  float(data["top"][index]), float(data["width"][index]),
                                                  float(data["height"][index]), float(data["conf"][index])))
        return PathObservation("ok", page_count, len(tokens), True, "local_ocr_completed"), tuple(tokens)
    except RuntimeError as error:
        # pytesseract uses RuntimeError for timeout and non-zero subprocess exit.
        reason = "ocr_timeout" if "timeout" in str(error).lower() else "ocr_subprocess_failed"
        return PathObservation("timeout" if reason == "ocr_timeout" else "error", page_count, 0, False, reason), ()
    except Exception:
        return PathObservation("error", page_count, 0, False, "ocr_render_or_result_parse_failed"), ()


def inspect_extraction_paths(path, *, local_key=None, ocr_timeout_seconds=15,
                             run_ocr=True) -> ExtractionPathReport:
    """Inspect one exact local PDF materialization without exposing its content."""
    path = Path(path)
    data = path.read_bytes()
    key = secrets.token_bytes(32) if local_key is None else local_key
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("local_key_too_short")
    source_id = _opaque_id(b"payroll-extraction-path-v1" + data, key)
    try:
        standard, visitor, visitor_tokens, text_length = _pdf_text_paths(path)
    except Exception:
        empty = PathObservation("error", 0, 0, False, "pdf_read_failed")
        return ExtractionPathReport(source_id, len(data), 0, empty, empty,
                                    PathObservation("not_run", 0, 0, False, "pdf_read_failed"),
                                    "unavailable", "unavailable", True, "unavailable", "unavailable", False)
    production_input = "embedded_text" if text_length >= 80 else "ocr_fallback_required"
    if production_input == "embedded_text":
        ocr, ocr_tokens = PathObservation("not_run", standard.page_count, 0, False, "ocr_not_required"), ()
    elif run_ocr:
        ocr, ocr_tokens = _controlled_ocr(path, standard.page_count, ocr_timeout_seconds)
    else:
        ocr, ocr_tokens = PathObservation("not_run", standard.page_count, 0, False, "ocr_disabled_by_observer"), ()
    coordinate_source = "visitor_matching_embedded_tokens" if visitor.complete else "unavailable"
    # A second same-source pass checks deterministic physical occurrences in memory.
    try:
        _, _, second_tokens, _ = _pdf_text_paths(path)
        rerun_stable = visitor_tokens == second_tokens
    except Exception:
        rerun_stable = False
    if ocr.complete:
        second_ocr, second_ocr_tokens = _controlled_ocr(path, standard.page_count, ocr_timeout_seconds)
        rerun_stable = second_ocr.complete and ocr_tokens == second_ocr_tokens
    canonical = ("embedded_text_visitor_verified" if visitor.complete and production_input == "embedded_text"
                 else "ocr_snapshot_reproducible" if ocr.complete and rerun_stable
                 else "unavailable")
    # Ownership Snapshot currently calls parse_positioned_items with ocr=False.
    # An OCR snapshot is reproducible here but cannot be claimed parser-equivalent
    # for ownership until that diagnostic-only provenance contract has an OCR mode.
    ownership_snapshot = ("eligible_embedded_text" if canonical == "embedded_text_visitor_verified"
                          else "ineligible_ocr_parser_mode_mismatch" if canonical == "ocr_snapshot_reproducible"
                          else "unavailable")
    return ExtractionPathReport(source_id, len(data), standard.page_count, standard, visitor, ocr,
                                production_input, coordinate_source, True, canonical,
                                ownership_snapshot, rerun_stable)
