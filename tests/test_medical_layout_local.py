"""Synthetic media with mocked local OCR; never real documents or AI transport."""
from io import BytesIO
import json

from PIL import Image
from pypdf import PdfWriter
import pytest

from app import medical_layout_local as local
from app.medical_receipt_privacy import _StructuredOcrToken as Token


def png(size=(600, 800), *, orientation=1):
    output = BytesIO()
    with Image.new("RGB", size, "white") as image:
        exif = Image.Exif()
        exif[274] = orientation
        image.save(output, format="PNG", exif=exif)
    return output.getvalue()


def pdf(pages=2, *, rotated=False, encrypted=False):
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=100, height=200)
        if rotated and index == 0:
            page.rotate(90)
    if encrypted:
        writer.encrypt("synthetic-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def install_ocr(monkeypatch, *, fail_page=None, empty_page=None, competitor=False):
    observed = []
    monkeypatch.setattr(local.extraction, "_run_image_ocr", lambda image: "支払額 1200円")
    def tokens(image, page):
        observed.append((page, image.size))
        if page == fail_page:
            raise RuntimeError("SYNTHETIC_PRIVATE_OCR_FAILURE")
        if page == empty_page:
            return ()
        result = (Token("支払額", page, 20, 20, 80, 20, 96, (1, 1, 1, 5)),
                  Token("1200円", page, 120, 20, 80, 20, 96, (1, 1, 1, 5)))
        if competitor:
            result += (Token("3.400", page, 220, 20, 40, 20, 69, (1, 1, 1, 5)),)
        return result
    monkeypatch.setattr(local.extraction, "_run_image_ocr_tokens", tokens)
    return observed


def test_image_uses_actual_ocr_frame_and_only_exports_counters(monkeypatch, capfd):
    observed = install_ocr(monkeypatch)
    result = local.evaluate_local_medical_bytes(png(), "image/png")
    assert observed == [(1, (600, 800))]
    assert result["evaluation_failed"] == 0
    assert result["shadow_unresolved_hypotheses"] == 1
    assert result["ocr_observation_groups"] == 1
    assert all(type(v) is int for v in result.values())
    assert "1200" not in json.dumps(result)
    assert capfd.readouterr() == ("", "")


def test_actual_pdf_rotation_uses_rendered_dimensions_and_every_page(monkeypatch):
    observed = install_ocr(monkeypatch)
    result = local.evaluate_local_medical_bytes(pdf(rotated=True), "application/pdf")
    assert observed == [(1, (600, 300)), (2, (300, 600))]
    assert result["evaluation_failed"] == 0
    assert result["shadow_numeric_regions"] == 2


def test_embedded_text_does_not_skip_the_rest_of_pdf(monkeypatch):
    observed = install_ocr(monkeypatch)
    monkeypatch.setattr(local.extraction, "_extract_pdf_embedded_text", lambda content: "SYNTHETIC_EMBEDDED_TEXT")
    result = local.evaluate_local_medical_bytes(pdf(), "application/pdf")
    assert len(observed) == 2
    assert result["evaluation_failed"] == 0
    assert "SYNTHETIC" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["failed", "empty"])
def test_failed_or_empty_second_page_discards_all_partial_results(monkeypatch, mode):
    observed = install_ocr(monkeypatch, **{"fail_page" if mode == "failed" else "empty_page": 2})
    result = local.evaluate_local_medical_bytes(pdf(), "application/pdf")
    assert len(observed) == 2
    assert result["evaluation_failed"] == result["local_observation_failed"] == 1
    assert result["shadow_numeric_regions"] == result["production_confirmed"] == 0


def test_low_quality_observation_survives_handoff(monkeypatch):
    install_ocr(monkeypatch, competitor=True)
    result = local.evaluate_local_medical_bytes(png(), "image/png")
    assert result["evaluation_failed"] == 0
    assert result["production_confirmed"] == 0
    assert result["production_needs_review"] == 1
    assert result["shadow_numeric_regions"] == 2
    assert result["shadow_low_confidence_regions"] == 1
    assert result["production_amount_observation_low_confidence"] == 1


def test_page_limit_checked_before_any_ocr(monkeypatch):
    observed = install_ocr(monkeypatch)
    result = local.evaluate_local_medical_bytes(pdf(4), "application/pdf")
    assert result["local_page_limit_exceeded"] == 1
    assert result["evaluation_failed"] == 1
    assert not observed


@pytest.mark.parametrize("content,mime", [(bytearray(b"synthetic"), "image/png"),
    (memoryview(b"synthetic"), "image/png"), (None, "image/png"),
    (b"", "image/png"), (b"synthetic", "text/plain")])
def test_invalid_or_mutable_input_rejected_before_ocr(monkeypatch, content, mime):
    observed = install_ocr(monkeypatch)
    result = local.evaluate_local_medical_bytes(content, mime)
    assert result["local_input_rejected"] == 1
    assert not observed


def test_snapshot_is_independent_of_later_caller_buffer_mutation(monkeypatch):
    buffer = bytearray(png())
    content = bytes(buffer)
    observed = install_ocr(monkeypatch)
    def mutate(image):
        buffer[:] = b"changed"
        return "支払額 1200円"
    monkeypatch.setattr(local.extraction, "_run_image_ocr", mutate)
    result = local.evaluate_local_medical_bytes(content, "image/png")
    assert result["evaluation_failed"] == 0
    assert observed == [(1, (600, 800))]


@pytest.mark.parametrize("content,mime", [(png(orientation=6), "image/png"), (png(), "image/jpeg")])
def test_orientation_and_declared_format_mismatch_are_not_guessed(monkeypatch, content, mime):
    observed = install_ocr(monkeypatch)
    result = local.evaluate_local_medical_bytes(content, mime)
    assert result["local_input_rejected"] == 1
    assert not observed


def test_pixel_limit_checked_before_ocr(monkeypatch):
    observed = install_ocr(monkeypatch)
    monkeypatch.setattr(local, "_MAX_PIXELS", 100)
    assert local.evaluate_local_medical_bytes(png((20, 20)), "image/png")["local_input_rejected"] == 1
    assert not observed


@pytest.mark.parametrize("content", [b"SYNTHETIC_PRIVATE_CORRUPT_PDF", pdf(encrypted=True)])
def test_invalid_pdf_never_reaches_renderer_or_exposes_content(monkeypatch, content, capfd, caplog):
    def forbidden(*args, **kwargs):
        pytest.fail("rejected PDF reached renderer")
    monkeypatch.setattr("pypdfium2.PdfDocument", forbidden)
    result = local.evaluate_local_medical_bytes(content, "application/pdf")
    assert result["evaluation_failed"] == 1
    assert result["local_observation_failed"] == 1
    assert "PRIVATE" not in json.dumps(result) + str(capfd.readouterr()) + caplog.text


def test_success_and_failure_share_fixed_output_schema(monkeypatch):
    install_ocr(monkeypatch)
    ok = local.evaluate_local_medical_bytes(png(), "image/png")
    failure = local.evaluate_local_medical_bytes(b"broken", "image/png")
    assert set(ok) == set(failure)
    assert failure["evaluation_failed"] == 1
    assert failure["production_confirmed"] == 0


def test_actual_pdf_crop_frame_matches_rendered_image(monkeypatch):
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=200)
    page.cropbox.lower_left = (10, 20)
    page.cropbox.upper_right = (90, 170)
    output = BytesIO()
    writer.write(output)
    observed = install_ocr(monkeypatch)
    result = local.evaluate_local_medical_bytes(output.getvalue(), "application/pdf")
    assert result["evaluation_failed"] == 0
    assert observed == [(1, (240, 450))]


def test_multiframe_png_is_not_silently_truncated(monkeypatch):
    output = BytesIO()
    with Image.new("RGB", (10, 10), "white") as first, Image.new("RGB", (10, 10), "black") as second:
        first.save(output, format="PNG", save_all=True, append_images=[second])
    observed = install_ocr(monkeypatch)
    result = local.evaluate_local_medical_bytes(output.getvalue(), "image/png")
    assert result["local_input_rejected"] == 1
    assert not observed


def test_jpeg_path_uses_the_same_local_boundary(monkeypatch):
    output = BytesIO()
    with Image.new("RGB", (600, 800), "white") as image:
        image.save(output, format="JPEG")
    observed = install_ocr(monkeypatch)
    assert local.evaluate_local_medical_bytes(output.getvalue(), " image/JPEG ")["evaluation_failed"] == 0
    assert observed == [(1, (600, 800))]


def test_pdf_resources_closed_after_partial_ocr_failure(monkeypatch):
    import pypdfium2 as pdfium
    closed = []
    for cls, name in ((pdfium.PdfDocument, "document"), (pdfium.PdfPage, "page"), (pdfium.PdfBitmap, "bitmap")):
        original = cls.close
        def close(self, _original=original, _name=name):
            closed.append(_name)
            return _original(self)
        monkeypatch.setattr(cls, "close", close)
    install_ocr(monkeypatch, fail_page=2)
    result = local.evaluate_local_medical_bytes(pdf(), "application/pdf")
    assert result["evaluation_failed"] == 1
    assert closed.count("document") >= 1
    assert closed.count("page") >= 2
    assert closed.count("bitmap") >= 2
