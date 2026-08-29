from __future__ import annotations

import io
import re
from dataclasses import dataclass, replace

from .medical_receipt import (
    CLINICAL_TERMS, INSTITUTION_TERMS, INSURANCE_TERMS, MEDIUM_AMOUNT_LABELS,
    RECEIPT_TERMS, STRONG_AMOUNT_LABELS, MedicalReceiptAnalysis,
    analyze_medical_receipt, extract_positioned_payment_amount, normalize_text,
    yen_amount_tokens,
)

MAX_PDF_OCR_PAGES = 5
MIN_MEANINGFUL_TEXT_CHARS = 30
MIN_OCR_TOKEN_CONFIDENCE = 60.0
MIN_OCR_AMOUNT_CONFIDENCE = 70.0
OCR_RENDER_SCALE = 3
OCR_UPSCALE_MIN_WIDTH = 1800


@dataclass(frozen=True)
class OCRToken:
    text: str
    page: int
    x: float
    y: float
    width: float
    height: float
    confidence: float
    line_key: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass(frozen=True)
class OCRExtraction:
    text: str
    tokens: tuple[OCRToken, ...]


@dataclass(frozen=True)
class MedicalReceiptScreening:
    analysis: MedicalReceiptAnalysis
    extraction: str
    reason_code: str


class TesseractUnavailable(RuntimeError):
    pass


class LocalOCRFailed(RuntimeError):
    pass


def _unknown(extraction: str, reason_code: str) -> MedicalReceiptScreening:
    return MedicalReceiptScreening(
        MedicalReceiptAnalysis(
            classification="unknown", amount=None, amount_label=None,
            certainty="none", evidence=(), reason=reason_code,
        ), extraction, reason_code,
    )


def pdf_text_is_sufficient(text: str) -> bool:
    normalized = normalize_text(text)
    meaningful = re.findall(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff]", normalized)
    if len(meaningful) >= MIN_MEANINGFUL_TEXT_CHARS:
        return True
    useful_terms = (
        RECEIPT_TERMS + CLINICAL_TERMS + INSTITUTION_TERMS + INSURANCE_TERMS
        + STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS
    )
    return bool(yen_amount_tokens(normalized)) and any(term in normalized for term in useful_terms)


def _prepare_image(image):
    from PIL import ImageEnhance, ImageOps

    image = ImageOps.exif_transpose(image)
    image = ImageOps.grayscale(image)
    image = ImageEnhance.Contrast(image).enhance(1.7)
    if image.width < OCR_UPSCALE_MIN_WIDTH:
        factor = min(3, max(2, (OCR_UPSCALE_MIN_WIDTH + image.width - 1) // image.width))
        image = image.resize((image.width * factor, image.height * factor))
    return image


def _ocr_image(image, page: int = 1) -> OCRExtraction:
    try:
        import pytesseract
        from pytesseract import Output
    except (ImportError, ModuleNotFoundError) as exc:
        raise TesseractUnavailable from exc
    prepared = _prepare_image(image)
    try:
        data = pytesseract.image_to_data(
            prepared, lang="jpn+eng", config="--psm 6", output_type=Output.DICT,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise TesseractUnavailable from exc
    except Exception as exc:
        raise LocalOCRFailed from exc
    tokens = []
    for index, raw_word in enumerate(data.get("text", [])):
        word = normalize_text(str(raw_word)).strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if confidence < MIN_OCR_TOKEN_CONFIDENCE:
            continue
        line_key = (
            page, int(data.get("block_num", [0] * (index + 1))[index]),
            int(data.get("par_num", [0] * (index + 1))[index]),
            int(data.get("line_num", [0] * (index + 1))[index]),
        )
        tokens.append(OCRToken(
            word, page, float(data["left"][index]), float(data["top"][index]),
            float(data["width"][index]), float(data["height"][index]), confidence,
            line_key,
        ))
    lines: dict[tuple[int, int, int, int], list[OCRToken]] = {}
    for token in tokens:
        lines.setdefault(token.line_key, []).append(token)
    text = "\n".join(
        " ".join(token.text for token in sorted(line, key=lambda item: item.x))
        for _, line in sorted(lines.items())
    )
    return OCRExtraction(text, tuple(tokens))


def _ocr_pdf(data: bytes, page_count: int) -> OCRExtraction:
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(data)
        if len(document) != page_count:
            raise LocalOCRFailed("PDF page count changed while rendering")
        results = [_ocr_image(page.render(scale=OCR_RENDER_SCALE).to_pil(), index + 1)
                   for index, page in enumerate(document)]
    except (TesseractUnavailable, LocalOCRFailed):
        raise
    except Exception as exc:
        raise LocalOCRFailed from exc
    return OCRExtraction(
        "\n".join(result.text for result in results),
        tuple(token for result in results for token in result.tokens),
    )


def _analyze_ocr(extracted: OCRExtraction, method: str) -> MedicalReceiptScreening:
    if not extracted.text.strip():
        return _unknown(method, "ocr_empty")
    amount_tokens = tuple(token for token in extracted.tokens
                          if token.confidence >= MIN_OCR_AMOUNT_CONFIDENCE)
    analysis = analyze_medical_receipt(extracted.text, amount_tokens)
    positioned_amount = extract_positioned_payment_amount(amount_tokens)
    if analysis.amount is not None and positioned_amount.amount != analysis.amount:
        analysis = replace(
            analysis, amount=None, amount_label=None,
            certainty="medium" if analysis.classification == "medical" else analysis.certainty,
            reason=analysis.reason + "; OCR amount lacks high-confidence positioned confirmation",
        )
    return MedicalReceiptScreening(analysis, method, "ocr_extracted")


def _screen_image(data: bytes, mime_type: str) -> MedicalReceiptScreening:
    if mime_type not in {"image/jpeg", "image/png"}:
        return _unknown("unsupported", "unsupported_image_format")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            extracted = _ocr_image(image)
    except TesseractUnavailable:
        return _unknown("image_ocr", "tesseract_unavailable")
    except LocalOCRFailed:
        return _unknown("image_ocr", "ocr_failed")
    except Exception:
        return _unknown("image_ocr", "image_decode_failed")
    return _analyze_ocr(extracted, "image_ocr")


def screen_medical_receipt(data: bytes, mime_type: str) -> MedicalReceiptScreening:
    """Screen locally; every local failure stays unknown and never implies fallback."""
    if mime_type != "application/pdf":
        if mime_type.startswith("image/"):
            return _screen_image(data, mime_type)
        return _unknown("unsupported", "unsupported_mime_type")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return _unknown("pdf_text", "encrypted_pdf")
        page_count = len(reader.pages)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return _unknown("pdf_text", "pdf_text_extraction_failed")
    if pdf_text_is_sufficient(text):
        return MedicalReceiptScreening(
            analyze_medical_receipt(text), "pdf_text", "pdf_text_extracted",
        )
    if page_count > MAX_PDF_OCR_PAGES:
        return _unknown("pdf_ocr", "too_many_pages")
    try:
        extracted = _ocr_pdf(data, page_count)
    except TesseractUnavailable:
        return _unknown("pdf_ocr", "tesseract_unavailable")
    except LocalOCRFailed:
        return _unknown("pdf_ocr", "ocr_failed")
    return _analyze_ocr(extracted, "pdf_ocr")
