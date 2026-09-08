"""Synthetic-only tests for read-only payroll extraction-path diagnostics."""
from pathlib import Path

import subprocess

from app.payroll_extraction_path_diagnostics import _ocr_environment, inspect_extraction_paths

KEY = b"synthetic-extraction-path-diagnostic-key"


def _embedded_pdf(path: Path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 1 0 0 1 20 100 Tm (Synthetic) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _image_only_pdf(path: Path):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)


def test_embedded_text_is_deterministic_and_coordinate_compatible(tmp_path):
    path = tmp_path / "embedded.pdf"
    _embedded_pdf(path)
    first = inspect_extraction_paths(path, local_key=KEY, run_ocr=False)
    second = inspect_extraction_paths(path, local_key=KEY, run_ocr=False)
    assert first.source_id == second.source_id
    assert first.standard.token_count == first.visitor.token_count > 0
    assert first.production_input == "ocr_fallback_required"  # short synthetic text
    assert first.coordinate_snapshot_source == "visitor_matching_embedded_tokens"
    assert first.rerun_stable


def test_image_only_pdf_is_not_mistaken_for_an_embedded_snapshot(tmp_path):
    path = tmp_path / "image-only.pdf"
    _image_only_pdf(path)
    result = inspect_extraction_paths(path, local_key=KEY, run_ocr=False)
    assert result.standard.token_count == result.visitor.token_count == 0
    assert result.production_input == "ocr_fallback_required"
    assert result.canonical_snapshot == "unavailable"
    assert result.ownership_snapshot == "unavailable"


def test_different_materializations_have_distinct_opaque_identity(tmp_path):
    first, second = tmp_path / "one.pdf", tmp_path / "two.pdf"
    _embedded_pdf(first)
    _image_only_pdf(second)
    assert inspect_extraction_paths(first, local_key=KEY, run_ocr=False).source_id != inspect_extraction_paths(second, local_key=KEY, run_ocr=False).source_id


def test_ocr_disabled_is_explicitly_incomplete(tmp_path):
    path = tmp_path / "image-only.pdf"
    _image_only_pdf(path)
    result = inspect_extraction_paths(path, local_key=KEY, run_ocr=False)
    assert result.ocr.reason == "ocr_disabled_by_observer"
    assert not result.ocr.complete


def test_ocr_engine_unavailable_and_timeout_are_explicit(monkeypatch):
    monkeypatch.setattr("app.payroll_extraction_path_diagnostics.subprocess.run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    assert _ocr_environment(1).reason == "ocr_engine_not_found"
    monkeypatch.setattr("app.payroll_extraction_path_diagnostics.subprocess.run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("ocr", 1)))
    assert _ocr_environment(1).reason == "ocr_engine_version_timeout"
