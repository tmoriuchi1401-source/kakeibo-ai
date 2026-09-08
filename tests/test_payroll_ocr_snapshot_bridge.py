"""Synthetic-only contract tests; no private PDF, OCR text, or coordinates."""
from dataclasses import replace

from app.payroll_diagnostic_evidence import observe_tokens
from app.payroll_ocr import PositionedText
from app.payroll_ocr_snapshot_bridge import (
    OcrTokenProvenance, ParserModeCompatibility, RasterPage, _coordinate_frame,
    _physical_ambiguity, _rerun_id, capture_ocr_ownership_snapshot,
)
from app.payroll_parser import parse_positioned_items


KEY = b"synthetic-ocr-snapshot-bridge-key-000"
META = ((1, (0, 0, 200, 100), (0, 0, 200, 100), 0, 1),)
TOKENS = (PositionedText("Label", 1, 30, 15, 60, 15, 95),
          PositionedText("1,234", 1, 120, 15, 45, 15, 91))
PROVENANCE = (OcrTokenProvenance(1, 1, 1, 1, 1), OcrTokenProvenance(1, 1, 1, 1, 2))
PAGES = (RasterPage(1, 600, 300, 600, 300, 3, 1, 0, True, 1),)


def _patch_capture(monkeypatch, *, compatibility="established", pages=PAGES, tokens=TOKENS,
                   provenance=PROVENANCE, metadata=META):
    import app.payroll_ocr_snapshot_bridge as bridge
    monkeypatch.setattr(bridge, "_page_metadata", lambda path: metadata)
    monkeypatch.setattr(bridge, "_engine_details", lambda: ("synthetic-pdfium", "synthetic-tesseract"))
    monkeypatch.setattr(bridge, "_run_local_ocr",
                        lambda path, supplied, timeout_seconds: (tokens, provenance, pages))
    monkeypatch.setattr(bridge, "_compatibility", lambda path, values, source_bytes:
                        ParserModeCompatibility(compatibility,
                                                "same_tokens_same_ocr_parser_mode" if compatibility == "established"
                                                else "production_ocr_token_universe_mismatch"))


def test_coordinate_scale_round_trip_binds_pixel_origin_and_uncertainty():
    frame = _coordinate_frame("synthetic", TOKENS, PAGES, META)
    assert frame.status == "verified"
    assert frame.pixel_origin == "top_left"
    assert frame.normalized_tokens[0].x == 10
    assert frame.normalized_tokens[0].y == 5
    assert frame.normalized_tokens[0].width == 20
    assert frame.uncertainty == 1/3


def test_rotation_crop_and_bbox_uncertainty_are_not_silently_promoted():
    rotated = ((1, (0, 0, 200, 100), (0, 0, 200, 100), 90, 1),)
    rotated_pages = (replace(PAGES[0], page_rotation=90),)
    assert _coordinate_frame("synthetic", TOKENS, rotated_pages, rotated).status == "unsupported"
    cropped = ((1, (0, 0, 200, 100), (10, 0, 200, 100), 0, 1),)
    cropped_pages = (replace(PAGES[0], crop_matches_media=False),)
    assert _coordinate_frame("synthetic", TOKENS, cropped_pages, cropped).status == "unsupported"


def test_same_text_different_physical_token_and_indistinguishable_duplicate():
    distinct = observe_tokens((TOKENS[0], TOKENS[1], replace(TOKENS[1], x=300)),
                              local_key=KEY, parser_mode="ocr")
    assert not _physical_ambiguity(distinct, PROVENANCE + (OcrTokenProvenance(1, 2, 1, 1, 1),))
    duplicate = observe_tokens((TOKENS[0], TOKENS[1], TOKENS[1]), local_key=KEY, parser_mode="ocr")
    ambiguity = _physical_ambiguity(duplicate, PROVENANCE + (PROVENANCE[1],))
    assert ambiguity == frozenset(duplicate.token_ids[1:])


def test_complete_snapshot_is_deterministic_and_source_bound(monkeypatch, tmp_path):
    _patch_capture(monkeypatch)
    first_path, second_path = tmp_path / "one.pdf", tmp_path / "two.pdf"
    first_path.write_bytes(b"synthetic-one")
    second_path.write_bytes(b"synthetic-two")
    first = capture_ocr_ownership_snapshot(first_path, local_key=KEY)
    second = capture_ocr_ownership_snapshot(first_path, local_key=KEY)
    different = capture_ocr_ownership_snapshot(second_path, local_key=KEY)
    assert first.ownership_ready and first.rerun_stable and first.page_scope_complete
    assert first.snapshot.snapshot_id == second.snapshot.snapshot_id
    assert first.source.source_id != different.source.source_id
    assert first.snapshot.snapshot_id != different.snapshot.snapshot_id


def test_config_language_change_invalidates_rerun_identity(monkeypatch, tmp_path):
    _patch_capture(monkeypatch)
    path = tmp_path / "one.pdf"
    path.write_bytes(b"synthetic")
    snapshot = capture_ocr_ownership_snapshot(path, local_key=KEY)
    changed = replace(snapshot.source, language="eng")
    assert _rerun_id(KEY, changed, snapshot.snapshot, snapshot.raster_pages,
                     snapshot.token_provenance) != snapshot.rerun_id


def test_incomplete_page_scope_and_parser_mismatch_are_ineligible(monkeypatch, tmp_path):
    path = tmp_path / "one.pdf"
    path.write_bytes(b"synthetic")
    two_pages = META + ((2, (0, 0, 200, 100), (0, 0, 200, 100), 0, 1),)
    _patch_capture(monkeypatch, metadata=two_pages)
    incomplete = capture_ocr_ownership_snapshot(path, local_key=KEY)
    assert not incomplete.ownership_ready
    _patch_capture(monkeypatch, compatibility="ineligible")
    mismatch = capture_ocr_ownership_snapshot(path, local_key=KEY)
    assert mismatch.parser_mode.status == "ineligible"
    assert not mismatch.ownership_ready


def test_incomplete_ocr_physical_provenance_is_ineligible(monkeypatch, tmp_path):
    _patch_capture(monkeypatch, provenance=PROVENANCE[:1])
    path = tmp_path / "one.pdf"
    path.write_bytes(b"synthetic")
    result = capture_ocr_ownership_snapshot(path, local_key=KEY)
    assert result.physical_identity_status == "incomplete"
    assert not result.ownership_ready


def test_diagnostic_ocr_replay_does_not_change_production_parser(monkeypatch, tmp_path):
    _patch_capture(monkeypatch)
    path = tmp_path / "one.pdf"
    path.write_bytes(b"synthetic")
    before = parse_positioned_items(TOKENS, ocr=True)
    snapshot = capture_ocr_ownership_snapshot(path, local_key=KEY)
    assert snapshot.snapshot.parser_mode == "ocr"
    assert parse_positioned_items(TOKENS, ocr=True) == before


def test_actual_local_ocr_rerun_uses_same_production_token_universe(tmp_path):
    """A generated image-only PDF exercises PDFium + local Tesseract end to end."""
    from PIL import Image, ImageDraw
    image = Image.new("L", (300, 100), 255)
    ImageDraw.Draw(image).text((30, 30), "SYNTHETIC", fill=0)
    path = tmp_path / "synthetic-image-only.pdf"
    image.save(path, "PDF", resolution=72.0)
    result = capture_ocr_ownership_snapshot(path, local_key=KEY)
    assert result.source.complete
    assert result.rerun_stable
    assert result.parser_mode.status == "established"
    assert result.coordinate_frame.status == "verified"
    assert result.ownership_ready
