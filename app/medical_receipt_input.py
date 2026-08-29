from __future__ import annotations

import io
from dataclasses import dataclass

from .medical_receipt import MedicalReceiptAnalysis, analyze_medical_receipt


@dataclass(frozen=True)
class MedicalReceiptScreening:
    analysis: MedicalReceiptAnalysis
    extraction: str
    reason_code: str


def _unknown(extraction: str, reason_code: str) -> MedicalReceiptScreening:
    return MedicalReceiptScreening(
        MedicalReceiptAnalysis(
            classification="unknown", amount=None, amount_label=None,
            certainty="none", evidence=(), reason=reason_code,
        ),
        extraction,
        reason_code,
    )


def screen_medical_receipt(data: bytes, mime_type: str) -> MedicalReceiptScreening:
    """Screen locally. B2 deliberately does not OCR images or scanned PDFs."""
    if mime_type != "application/pdf":
        if mime_type.startswith("image/"):
            return _unknown("image_no_ocr", "image_ocr_not_available")
        return _unknown("unsupported", "unsupported_mime_type")

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return _unknown("pdf_text", "encrypted_pdf")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return _unknown("pdf_text", "pdf_text_extraction_failed")
    if not text.strip():
        return _unknown("pdf_text", "empty_pdf_text")
    return MedicalReceiptScreening(
        analyze_medical_receipt(text), "pdf_text", "pdf_text_extracted",
    )
