"""Local, bytes-only receipt text extraction with safe public metadata."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from contextlib import contextmanager, redirect_stderr
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator


ExtractionStatus = Literal[
    "extracted",
    "unsupported_mime_type",
    "empty_content",
    "pdf_text_empty",
    "pdf_ocr_empty",
    "pdf_render_failed",
    "pdf_ocr_failed",
    "pdf_page_limit_exceeded",
    "ocr_empty",
    "extraction_failed",
]
ExtractionMethod = Literal["pdf_text", "image_ocr", "pdf_ocr", "none"]

_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/jpg"})
_PYPDF_EXTRACTION_LOCK = threading.Lock()
_MAX_PDF_OCR_PAGES = 3
_PDF_OCR_RENDER_SCALE = 3


class _PdfRenderFailed(Exception):
    """Internal, data-free signal for an unsafe PDF render failure."""


class _PdfOcrFailed(Exception):
    """Internal, data-free signal for an unsafe PDF OCR failure."""


class _PdfPageLimitExceeded(Exception):
    """Internal signal that prevents partial PDF OCR classification."""


class _PdfEmbeddedTextUnavailable(Exception):
    """Internal signal for PDFs that must not enter the renderer fallback."""


class SafeExtractionModelError(ValueError):
    """Fixed, data-free error for unsafe extraction metadata operations."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("safe extraction model operation rejected")


class ReceiptTextExtractionResult(BaseModel):
    """Safe extraction metadata; extracted document text is deliberately absent."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    extraction_status: ExtractionStatus
    extraction_method: ExtractionMethod
    text_present: bool

    @model_validator(mode="after")
    def validate_extraction_state(self) -> Self:
        if self.extraction_status == "extracted":
            if self.extraction_method == "none" or not self.text_present:
                raise ValueError("extracted metadata requires text and method")
        elif self.text_present:
            raise ValueError("failed extraction metadata cannot contain text")

        no_method_statuses = {"empty_content", "unsupported_mime_type"}
        if self.extraction_method == "none":
            if self.extraction_status not in no_method_statuses:
                raise ValueError("none method does not match extraction status")
        elif self.extraction_status in no_method_statuses:
            raise ValueError("empty or unsupported input cannot have extraction method")
        if self.extraction_status == "pdf_text_empty" and self.extraction_method not in {
            "pdf_text",
            "pdf_ocr",
        }:
            raise ValueError("pdf empty status requires a pdf method")
        if self.extraction_status == "ocr_empty" and self.extraction_method not in {
            "image_ocr",
            "pdf_ocr",
        }:
            raise ValueError("ocr empty status requires an ocr method")
        if self.extraction_status in {
            "pdf_ocr_empty",
            "pdf_render_failed",
            "pdf_ocr_failed",
            "pdf_page_limit_exceeded",
        } and self.extraction_method != "pdf_ocr":
            raise ValueError("pdf ocr status requires pdf ocr method")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is not None:
            raise SafeExtractionModelError()
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: object = None,
        exclude: object = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise SafeExtractionModelError()
        return self.model_copy(deep=deep)


@dataclass(frozen=True)
class _ReceiptTextExtraction:
    """Private hand-off for the gate; never return this from a public API."""

    status: ExtractionStatus
    method: ExtractionMethod
    text: str | None = field(repr=False)

    @property
    def text_present(self) -> bool:
        return bool(self.text and self.text.strip())

    def public_result(self) -> ReceiptTextExtractionResult:
        return ReceiptTextExtractionResult(
            extraction_status=self.status,
            extraction_method=self.method,
            text_present=self.text_present,
        )


@contextmanager
def _suppress_pypdf_output():
    """Serialize and discard pypdf diagnostics, restoring all global state."""

    # redirect_stderr and logger mutation are process-global. Serializing this
    # entire context prevents overlapping calls from restoring each other's state.
    with _PYPDF_EXTRACTION_LOCK:
        logger = logging.getLogger("pypdf")
        original_handlers = list(logger.handlers)
        original_propagate = logger.propagate
        original_disabled = logger.disabled
        try:
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
            logger.disabled = False
            with redirect_stderr(StringIO()):
                yield
        finally:
            logger.handlers = original_handlers
            logger.propagate = original_propagate
            logger.disabled = original_disabled


def _extract_pdf_embedded_text(content: bytes) -> str | None:
    from pypdf import PdfReader

    with _suppress_pypdf_output():
        reader = PdfReader(BytesIO(content))
        if reader.is_encrypted:
            raise _PdfEmbeddedTextUnavailable()
        return "\n".join(page.extract_text() or "" for page in reader.pages)


def _close_if_possible(value: object | None) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _extract_pdf_ocr_text(content: bytes) -> str:
    """Render every bounded PDF page in memory and return only transient OCR text."""

    import pypdfium2 as pdfium

    document = None
    try:
        try:
            document = pdfium.PdfDocument(content)
        except Exception:
            raise _PdfRenderFailed() from None

        page_count = len(document)
        if page_count > _MAX_PDF_OCR_PAGES:
            raise _PdfPageLimitExceeded()

        page_texts: list[str] = []
        for page_index in range(page_count):
            page = None
            bitmap = None
            image = None
            try:
                try:
                    page = document[page_index]
                    bitmap = page.render(scale=_PDF_OCR_RENDER_SCALE)
                    image = bitmap.to_pil()
                except Exception:
                    raise _PdfRenderFailed() from None
                try:
                    text = _run_image_ocr(image)
                except Exception:
                    # Do not classify a document from a successful subset of pages.
                    raise _PdfOcrFailed() from None
                if text and text.strip():
                    page_texts.append(text)
            finally:
                _close_if_possible(image)
                _close_if_possible(bitmap)
                _close_if_possible(page)

        return "\n".join(page_texts)
    finally:
        _close_if_possible(document)


def _run_image_ocr(image) -> str:
    import pytesseract

    return pytesseract.image_to_string(image, lang="jpn+eng", config="--psm 6")


def _extract_image_text(content: bytes) -> str | None:
    from PIL import Image

    with Image.open(BytesIO(content)) as image:
        image.load()
        return _run_image_ocr(image)


def _extract_receipt_text(content: bytes | None, mime_type: str | None) -> _ReceiptTextExtraction:
    """Extract only for immediate in-process classification; never log document data."""

    if not content:
        return _ReceiptTextExtraction("empty_content", "none", None)

    normalized_mime = (mime_type or "").strip().lower()
    if normalized_mime == "application/pdf":
        try:
            text = _extract_pdf_embedded_text(content)
        except Exception:
            # Parser errors are untrusted-document failures. No parser detail escapes.
            return _ReceiptTextExtraction("extraction_failed", "pdf_text", None)
        if not text or not text.strip():
            try:
                text = _extract_pdf_ocr_text(content)
            except _PdfPageLimitExceeded:
                return _ReceiptTextExtraction("pdf_page_limit_exceeded", "pdf_ocr", None)
            except _PdfRenderFailed:
                return _ReceiptTextExtraction("pdf_render_failed", "pdf_ocr", None)
            except _PdfOcrFailed:
                return _ReceiptTextExtraction("pdf_ocr_failed", "pdf_ocr", None)
            except Exception:
                # Do not expose or trust an unexpected renderer/OCR failure.
                return _ReceiptTextExtraction("pdf_ocr_failed", "pdf_ocr", None)
            if not text or not text.strip():
                return _ReceiptTextExtraction("pdf_ocr_empty", "pdf_ocr", None)
            return _ReceiptTextExtraction("extracted", "pdf_ocr", text)
        return _ReceiptTextExtraction("extracted", "pdf_text", text)

    if normalized_mime in _IMAGE_MIME_TYPES:
        try:
            text = _extract_image_text(content)
        except Exception:
            # PIL/Tesseract failures are document-processing failures, not normal input.
            return _ReceiptTextExtraction("extraction_failed", "image_ocr", None)
        if not text or not text.strip():
            return _ReceiptTextExtraction("ocr_empty", "image_ocr", None)
        return _ReceiptTextExtraction("extracted", "image_ocr", text)

    return _ReceiptTextExtraction("unsupported_mime_type", "none", None)


def extract_receipt_text(
    content: bytes | None,
    mime_type: str | None,
) -> ReceiptTextExtractionResult:
    """Return only privacy-safe local extraction metadata for a receipt document."""

    return _extract_receipt_text(content, mime_type).public_result()
