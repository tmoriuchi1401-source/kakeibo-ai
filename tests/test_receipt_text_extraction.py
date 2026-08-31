from __future__ import annotations

from io import BytesIO
import logging
import sys
import threading

from PIL import Image
import pytest

from app import receipt_text_extraction as extraction


def _synthetic_png() -> bytes:
    image = Image.new("RGB", (8, 8), "white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_empty_bytes_are_a_safe_failure():
    result = extraction.extract_receipt_text(b"", "image/png")

    assert result.extraction_status == "empty_content"
    assert result.extraction_method == "none"
    assert result.text_present is False


def test_none_content_is_a_safe_failure():
    result = extraction.extract_receipt_text(None, "image/png")

    assert result.extraction_status == "empty_content"
    assert result.text_present is False


def test_unsupported_mime_type_is_a_safe_failure():
    result = extraction.extract_receipt_text(b"synthetic", "text/plain")

    assert result.extraction_status == "unsupported_mime_type"
    assert result.extraction_method == "none"
    assert result.text_present is False


def test_corrupt_image_is_a_safe_failure():
    result = extraction.extract_receipt_text(b"not-an-image", "image/png")

    assert result.extraction_status == "extraction_failed"
    assert result.extraction_method == "image_ocr"
    assert result.text_present is False


def test_corrupt_jpeg_is_a_safe_failure():
    result = extraction.extract_receipt_text(b"not-an-image", "image/jpeg")

    assert result.extraction_status == "extraction_failed"
    assert result.extraction_method == "image_ocr"
    assert result.text_present is False


def test_corrupt_pdf_is_a_safe_failure():
    result = extraction.extract_receipt_text(b"not-a-pdf", "application/pdf")

    assert result.extraction_status == "extraction_failed"
    assert result.extraction_method == "pdf_text"
    assert result.text_present is False


def test_corrupt_pdf_does_not_emit_input_marker(capfd, caplog):
    marker = "PII42"
    with caplog.at_level(logging.DEBUG):
        result = extraction.extract_receipt_text(
            f"{marker} malformed pdf".encode(), "application/pdf"
        )

    captured = capfd.readouterr()
    logging_text = " ".join(record.getMessage() for record in caplog.records)
    assert marker not in captured.out
    assert marker not in captured.err
    assert marker not in logging_text
    assert result.extraction_status == "extraction_failed"
    assert result.text_present is False


def test_concurrent_pdf_suppression_is_serial_and_restores_process_state(
    monkeypatch, capfd, caplog
):
    markers = ("PDF_THREAD_MARKER_A", "PDF_THREAD_MARKER_B")
    first_reader_entered = threading.Event()
    release_first_reader = threading.Event()
    second_lock_attempted = threading.Event()
    second_lock_acquired = threading.Event()
    results = {}
    errors = []

    class ObservedLock:
        def __init__(self):
            self.delegate = threading.Lock()

        def __enter__(self):
            is_second = threading.current_thread().name == "receipt-pdf-second"
            if is_second:
                second_lock_attempted.set()
            self.delegate.acquire()
            if is_second:
                second_lock_acquired.set()
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.delegate.release()

    class NoisyPage:
        def __init__(self, marker):
            self.marker = marker

        def extract_text(self):
            logging.getLogger("pypdf._page").warning(self.marker)
            print(self.marker, file=sys.stderr)
            return "レシート 商品 小計 100円 現金 100円"

    class NoisyReader:
        is_encrypted = False

        def __init__(self, marker):
            self.pages = [NoisyPage(marker)]

    def fake_reader(stream):
        content = stream.getvalue()
        marker = markers[0] if content == b"first" else markers[1]
        logging.getLogger("pypdf._reader").warning(marker)
        print(marker, file=sys.stderr)
        if content == b"first":
            first_reader_entered.set()
            if not release_first_reader.wait(2):
                raise RuntimeError("controlled reader timeout")
        return NoisyReader(marker)

    def run(name, content):
        try:
            results[name] = extraction.extract_receipt_text(content, "application/pdf")
        except Exception as exc:  # pragma: no cover - asserted through errors below
            errors.append(type(exc).__name__)

    observed_lock = ObservedLock()
    monkeypatch.setattr(extraction, "_PYPDF_EXTRACTION_LOCK", observed_lock)
    monkeypatch.setattr("pypdf.PdfReader", fake_reader)

    pypdf_logger = logging.getLogger("pypdf")
    original_logger_state = (
        list(pypdf_logger.handlers),
        pypdf_logger.propagate,
        pypdf_logger.disabled,
    )
    original_stderr = sys.stderr
    first = threading.Thread(target=run, args=("first", b"first"), name="receipt-pdf-first")
    second = threading.Thread(
        target=run, args=("second", b"second"), name="receipt-pdf-second"
    )

    with caplog.at_level(logging.DEBUG):
        first.start()
        assert first_reader_entered.wait(2)
        second.start()
        try:
            assert second_lock_attempted.wait(2)
            assert not second_lock_acquired.is_set()
        finally:
            release_first_reader.set()
        first.join(2)
        second.join(2)

    captured = capfd.readouterr()
    logging_text = " ".join(record.getMessage() for record in caplog.records)
    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert second_lock_acquired.is_set()
    assert set(results) == {"first", "second"}
    assert all(result.extraction_status == "extracted" for result in results.values())
    for marker in markers:
        assert marker not in captured.out
        assert marker not in captured.err
        assert marker not in logging_text
    assert sys.stderr is original_stderr
    assert pypdf_logger.handlers == original_logger_state[0]
    assert pypdf_logger.propagate == original_logger_state[1]
    assert pypdf_logger.disabled == original_logger_state[2]


def test_empty_image_ocr_is_a_safe_failure(monkeypatch):
    monkeypatch.setattr(extraction, "_run_image_ocr", lambda image: "")

    result = extraction.extract_receipt_text(_synthetic_png(), "image/png")

    assert result.extraction_status == "ocr_empty"
    assert result.text_present is False


def test_whitespace_image_ocr_is_a_safe_failure(monkeypatch):
    monkeypatch.setattr(extraction, "_run_image_ocr", lambda image: " \n\t ")

    result = extraction.extract_receipt_text(_synthetic_png(), "image/png")

    assert result.extraction_status == "ocr_empty"
    assert result.text_present is False


def test_synthetic_image_uses_bytesio_ocr_path(monkeypatch):
    monkeypatch.setattr(extraction, "_run_image_ocr", lambda image: "レシート 商品 合計 100円")

    result = extraction.extract_receipt_text(_synthetic_png(), "image/png")

    assert result.extraction_status == "extracted"
    assert result.extraction_method == "image_ocr"
    assert result.text_present is True


@pytest.mark.parametrize("mime_type", ["IMAGE/PNG", " image/png ", " ImAgE/JpEg "])
def test_mime_case_and_whitespace_are_normalized(monkeypatch, mime_type):
    monkeypatch.setattr(extraction, "_run_image_ocr", lambda image: "レシート 商品 合計 100円")

    result = extraction.extract_receipt_text(_synthetic_png(), mime_type)

    assert result.extraction_status == "extracted"
    assert result.extraction_method == "image_ocr"
    assert result.text_present is True


def test_pdf_embedded_text_path_is_safe_metadata(monkeypatch):
    monkeypatch.setattr(
        extraction, "_extract_pdf_embedded_text", lambda content: "レシート 商品 合計 100円"
    )

    result = extraction.extract_receipt_text(b"synthetic-pdf", "application/pdf")

    assert result.extraction_status == "extracted"
    assert result.extraction_method == "pdf_text"
    assert result.text_present is True


def test_pdf_without_embedded_text_fails_closed(monkeypatch):
    monkeypatch.setattr(extraction, "_extract_pdf_embedded_text", lambda content: " \n")

    result = extraction.extract_receipt_text(b"synthetic-pdf", "application/pdf")

    assert result.extraction_status == "pdf_text_empty"
    assert result.extraction_method == "pdf_text"
    assert result.text_present is False


def test_public_extraction_result_has_no_text_field():
    result = extraction.extract_receipt_text(b"synthetic", "text/plain")

    assert "text" not in type(result).model_fields
    assert "raw_text" not in type(result).model_fields
    assert "snippet" not in type(result).model_fields


@pytest.mark.parametrize(
    "mime_type", ["application/octet-stream", "text/plain", "", None]
)
def test_other_mime_values_fail_closed(mime_type):
    result = extraction.extract_receipt_text(b"synthetic", mime_type)

    assert result.extraction_status == "unsupported_mime_type"
    assert result.extraction_method == "none"
    assert result.text_present is False


def test_low_level_exception_message_is_not_exposed(monkeypatch, capfd, caplog):
    marker = "SYNTHETIC_PRIVATE_MARKER"

    def fail(content):
        raise RuntimeError(marker)

    monkeypatch.setattr(extraction, "_extract_pdf_embedded_text", fail)
    with caplog.at_level(logging.DEBUG):
        result = extraction.extract_receipt_text(b"synthetic", "application/pdf")

    captured = capfd.readouterr()
    exposed = " ".join(
        (repr(result), str(result.model_dump()), result.model_dump_json(), captured.out,
         captured.err, " ".join(record.getMessage() for record in caplog.records))
    )
    assert marker not in exposed
    assert result.extraction_status == "extraction_failed"


@pytest.mark.parametrize(
    "update",
    [
        {"extraction_status": "SYNTHETIC_PRIVATE_MARKER"},
        {"raw_text": "SYNTHETIC_PRIVATE_MARKER"},
        {"text_present": True},
    ],
)
def test_extraction_result_rejects_update_copy(update):
    result = extraction.extract_receipt_text(b"synthetic", "text/plain")

    with pytest.raises(extraction.SafeExtractionModelError):
        result.model_copy(update=update)
    with pytest.raises(extraction.SafeExtractionModelError):
        result.copy(update=update)


def test_extraction_result_allows_exact_copy():
    result = extraction.extract_receipt_text(b"synthetic", "text/plain")

    assert result.model_copy() == result
    assert result.copy() == result


@pytest.mark.parametrize(
    "payload",
    [
        {
            "extraction_status": "extracted",
            "extraction_method": "none",
            "text_present": True,
        },
        {
            "extraction_status": "extracted",
            "extraction_method": "pdf_text",
            "text_present": False,
        },
        {
            "extraction_status": "extraction_failed",
            "extraction_method": "image_ocr",
            "text_present": True,
        },
    ],
)
def test_extraction_result_rejects_contradictory_constructor_state(payload):
    with pytest.raises(ValueError):
        extraction.ReceiptTextExtractionResult(**payload)


@pytest.mark.parametrize("method", ["pdf_text", "pdf_ocr", "image_ocr"])
def test_extraction_result_accepts_valid_extracted_state(method):
    result = extraction.ReceiptTextExtractionResult(
        extraction_status="extracted",
        extraction_method=method,
        text_present=True,
    )

    assert result.extraction_status == "extracted"
    assert result.extraction_method == method
    assert result.text_present is True
