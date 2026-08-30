from __future__ import annotations

import io
import re
from dataclasses import dataclass, replace

from .medical_receipt import (
    CLINICAL_TERMS, EXCLUDED_AMOUNT_LABELS, INSTITUTION_TERMS, INSURANCE_TERMS,
    MEDIUM_AMOUNT_LABELS, RECEIPT_TERMS, STRONG_AMOUNT_LABELS,
    MedicalReceiptAnalysis, _paired, analyze_medical_receipt, normalize_text,
    yen_amount_tokens,
)

MAX_PDF_OCR_PAGES = 5
MIN_MEANINGFUL_TEXT_CHARS = 30
MIN_OCR_TOKEN_CONFIDENCE = 60.0
MIN_OCR_AMOUNT_CONFIDENCE = 70.0
PRIVACY_SUSPICION_CONFIDENCE = 25.0
MIN_OCR_CLASSIFICATION_TOKENS = 3
OCR_RENDER_SCALE = 3
OCR_UPSCALE_MIN_WIDTH = 1800
MAX_LABEL_TOKEN_GAP = 40.0
MAX_LABEL_TOKEN_OVERLAP = 5.0
MAX_JOINED_LABEL_TOKENS = 5
MIN_FUZZY_LABEL_LENGTH = 3

STRONG_PRIVACY_MEDICAL_TERMS = {
    "診療報酬", "一部負担金", "自己負担額", "自己負担", "処方箋", "調剤",
    "公費負担", "保険点数", "診療点数",
}
WEAK_PRIVACY_MEDICAL_GROUPS = {
    "clinical": {"患者", "医療費"},
    "institution": {"病院", "クリニック", "医院", "診療所", "薬局"},
    "insurance": {"保険"},
}


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
    privacy_tokens: tuple[OCRToken, ...] = ()


@dataclass(frozen=True)
class MedicalReceiptScreening:
    analysis: MedicalReceiptAnalysis
    extraction: str
    reason_code: str


@dataclass(frozen=True)
class OCRAmountMatch:
    amount: int | None
    label: str | None
    match_type: str | None
    reason: str


@dataclass(frozen=True)
class _LabelCandidate:
    label: str
    token: OCRToken
    match_type: str


class TesseractUnavailable(RuntimeError):
    pass


class LocalOCRFailed(RuntimeError):
    pass


class PartialOCRFailure(LocalOCRFailed):
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
    privacy_tokens = []
    for index, raw_word in enumerate(data.get("text", [])):
        word = normalize_text(str(raw_word)).strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if confidence < PRIVACY_SUSPICION_CONFIDENCE:
            continue
        line_key = (
            page, int(data.get("block_num", [0] * (index + 1))[index]),
            int(data.get("par_num", [0] * (index + 1))[index]),
            int(data.get("line_num", [0] * (index + 1))[index]),
        )
        token = OCRToken(
            word, page, float(data["left"][index]), float(data["top"][index]),
            float(data["width"][index]), float(data["height"][index]), confidence,
            line_key,
        )
        privacy_tokens.append(token)
        if confidence >= MIN_OCR_TOKEN_CONFIDENCE:
            tokens.append(token)
    lines: dict[tuple[int, int, int, int], list[OCRToken]] = {}
    for token in tokens:
        lines.setdefault(token.line_key, []).append(token)
    text = "\n".join(
        " ".join(token.text for token in sorted(line, key=lambda item: item.x))
        for _, line in sorted(lines.items())
    )
    return OCRExtraction(text, tuple(tokens), tuple(privacy_tokens))


def _ocr_pdf(data: bytes, page_count: int) -> OCRExtraction:
    try:
        import pypdfium2 as pdfium
        document = pdfium.PdfDocument(data)
        if len(document) != page_count:
            raise LocalOCRFailed("PDF page count changed while rendering")
        results = []
        for index, page in enumerate(document):
            try:
                results.append(_ocr_image(
                    page.render(scale=OCR_RENDER_SCALE).to_pil(), index + 1,
                ))
            except TesseractUnavailable:
                raise
            except Exception as exc:
                if page_count > 1:
                    raise PartialOCRFailure from exc
                raise LocalOCRFailed from exc
    except (TesseractUnavailable, LocalOCRFailed):
        raise
    except Exception as exc:
        raise LocalOCRFailed from exc
    return OCRExtraction(
        "\n".join(result.text for result in results),
        tuple(token for result in results for token in result.tokens),
        tuple(token for result in results for token in result.privacy_tokens),
    )


def _normalized_ocr_word(value: str) -> str:
    value = re.sub(r"[\s　]+", "", normalize_text(value))
    return value.strip(".,:;!?()[]{}<>「」『』【】、。・:;")


def _privacy_suspicion(extracted: OCRExtraction) -> tuple[bool, tuple[str, ...]]:
    candidates = extracted.privacy_tokens or extracted.tokens
    words = {
        _normalized_ocr_word(token.text)
        for token in candidates if token.confidence >= PRIVACY_SUSPICION_CONFIDENCE
    }
    words.discard("")
    strong = sorted(words & STRONG_PRIVACY_MEDICAL_TERMS)
    if strong:
        return True, tuple(f"privacy_strong:{word}" for word in strong)
    weak_groups = {
        group for group, terms in WEAK_PRIVACY_MEDICAL_GROUPS.items() if words & terms
    }
    if len(weak_groups) >= 2:
        return True, tuple(f"privacy_weak_group:{group}" for group in sorted(weak_groups))
    has_receipt = bool(words & set(RECEIPT_TERMS))
    has_money = any(yen_amount_tokens(word) for word in words)
    if weak_groups and has_receipt and has_money:
        return True, tuple(f"privacy_weak_group:{group}" for group in sorted(weak_groups))
    return False, ()


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _unique_fuzzy_label(
    value: str, labels: tuple[str, ...] | None = None,
) -> str | None:
    value = _normalized_ocr_word(value)
    if len(value) < MIN_FUZZY_LABEL_LENGTH:
        return None
    known = labels or tuple(STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS)
    matches = [label for label in known if _edit_distance(value, label) == 1]
    return matches[0] if len(matches) == 1 else None


def _combined_label_token(parts: tuple[OCRToken, ...], label: str) -> OCRToken:
    first = min(parts, key=lambda token: token.x)
    left = min(token.x for token in parts)
    right = max(token.x + token.width for token in parts)
    top = min(token.y for token in parts)
    bottom = max(token.y + token.height for token in parts)
    return OCRToken(
        label, first.page, left, top, right - left, bottom - top,
        min(token.confidence for token in parts), first.line_key,
    )


def _joined_token_windows(tokens: tuple[OCRToken, ...]):
    lines: dict[tuple[int, int, int, int], list[OCRToken]] = {}
    for token in tokens:
        lines.setdefault(token.line_key, []).append(token)
    for line in lines.values():
        ordered = sorted(line, key=lambda token: token.x)
        for size in range(2, min(MAX_JOINED_LABEL_TOKENS, len(ordered)) + 1):
            for start in range(len(ordered) - size + 1):
                parts = tuple(ordered[start:start + size])
                gaps = [
                    right.x - (left.x + left.width)
                    for left, right in zip(parts, parts[1:])
                ]
                if all(-MAX_LABEL_TOKEN_OVERLAP <= gap <= MAX_LABEL_TOKEN_GAP
                       for gap in gaps):
                    yield parts, "".join(_normalized_ocr_word(token.text) for token in parts)


def _label_candidates(tokens: tuple[OCRToken, ...]):
    payment_labels = tuple(STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS)
    exact_joined = []
    fuzzy_joined = []
    for parts, joined in _joined_token_windows(tokens):
        if joined in payment_labels:
            exact_joined.append(_LabelCandidate(
                joined, _combined_label_token(parts, joined), "exact_joined",
            ))
        else:
            corrected = _unique_fuzzy_label(joined, payment_labels)
            if corrected:
                fuzzy_joined.append(_LabelCandidate(
                    corrected, _combined_label_token(parts, corrected), "edit_distance_1",
                ))
    exact_single = [
        _LabelCandidate(word, token, "exact_single")
        for token in tokens
        if (word := _normalized_ocr_word(token.text)) in payment_labels
    ]
    fuzzy_single = []
    for token in tokens:
        word = _normalized_ocr_word(token.text)
        if word in payment_labels:
            continue
        corrected = _unique_fuzzy_label(word, payment_labels)
        if corrected:
            fuzzy_single.append(_LabelCandidate(corrected, token, "edit_distance_1"))
    return exact_joined, exact_single, fuzzy_joined + fuzzy_single


def _exclusion_candidates(tokens: tuple[OCRToken, ...]) -> tuple[OCRToken, ...]:
    candidates = [
        token for token in tokens
        if _normalized_ocr_word(token.text) in EXCLUDED_AMOUNT_LABELS
    ]
    for parts, joined in _joined_token_windows(tokens):
        if joined in EXCLUDED_AMOUNT_LABELS:
            candidates.append(_combined_label_token(parts, joined))
    return tuple(candidates)


def _resolve_label_tier(
    candidates: list[_LabelCandidate], amounts: tuple[tuple[OCRToken, int], ...],
    exclusions: tuple[OCRToken, ...],
) -> OCRAmountMatch | None:
    if not candidates:
        return None
    resolved = []
    for candidate in candidates:
        nearby = []
        for number, amount in amounts:
            relation = _paired(candidate.token, number)
            if relation is None:
                continue
            conflicts = [
                excluded for excluded in exclusions
                if (excluded_relation := _paired(excluded, number)) is not None
                and excluded_relation[0] == relation[0]
            ]
            if not conflicts:
                nearby.append(amount)
        unique = set(nearby)
        if len(unique) != 1:
            return OCRAmountMatch(None, None, None, "payment label amount is not unique")
        resolved.append((candidate, next(iter(unique))))
    values = {amount for _, amount in resolved}
    if len(values) != 1:
        return OCRAmountMatch(None, None, None, "payment labels resolve to different amounts")
    amount = next(iter(values))
    best = min(resolved, key=lambda item: (
        (STRONG_AMOUNT_LABELS + MEDIUM_AMOUNT_LABELS).index(item[0].label),
        -item[0].token.confidence,
    ))[0]
    return OCRAmountMatch(amount, best.label, best.match_type,
                          f"OCR payment label matched by {best.match_type}")


def _extract_ocr_payment_amount(tokens: tuple[OCRToken, ...]) -> OCRAmountMatch:
    safe_tokens = tuple(
        token for token in tokens if token.confidence >= MIN_OCR_AMOUNT_CONFIDENCE
    )
    amounts = []
    for token in safe_tokens:
        values = yen_amount_tokens(token.text, label_context=True)
        if len(set(values)) == 1:
            amounts.append((token, values[0]))
    amount_candidates = tuple(amounts)
    exclusions = _exclusion_candidates(safe_tokens)
    for tier in _label_candidates(safe_tokens):
        result = _resolve_label_tier(tier, amount_candidates, exclusions)
        if result is not None:
            return result
    return OCRAmountMatch(None, None, None, "no safe OCR payment label match")


def _analyze_ocr(extracted: OCRExtraction, method: str) -> MedicalReceiptScreening:
    if not extracted.text.strip():
        return _unknown(method, "ocr_empty")
    if len(extracted.tokens) < MIN_OCR_CLASSIFICATION_TOKENS:
        return _unknown(method, "insufficient_ocr_quality")
    amount_tokens = tuple(token for token in extracted.tokens
                          if token.confidence >= MIN_OCR_AMOUNT_CONFIDENCE)
    analysis = analyze_medical_receipt(extracted.text, amount_tokens)
    if analysis.classification == "non_medical":
        suspected, privacy_evidence = _privacy_suspicion(extracted)
        if suspected:
            analysis = replace(
                analysis, classification="suspected_medical", certainty="low",
                evidence=analysis.evidence + privacy_evidence,
                reason=analysis.reason + "; low-confidence medical privacy evidence",
            )
    positioned_amount = _extract_ocr_payment_amount(amount_tokens)
    if positioned_amount.amount is not None:
        analysis = replace(
            analysis, amount=positioned_amount.amount, amount_label=positioned_amount.label,
            certainty="high" if analysis.classification == "medical" else analysis.certainty,
            evidence=analysis.evidence + (f"label_match:{positioned_amount.match_type}",),
            reason=analysis.reason + f"; {positioned_amount.reason}",
        )
    elif analysis.amount is not None:
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
    # The page limit controls local OCR cost only. Text PDFs are screened above
    # regardless of page count.
    if page_count > MAX_PDF_OCR_PAGES:
        return _unknown("pdf_ocr", "too_many_pages_for_ocr")
    try:
        extracted = _ocr_pdf(data, page_count)
    except TesseractUnavailable:
        return _unknown("pdf_ocr", "tesseract_unavailable")
    except PartialOCRFailure:
        return _unknown("pdf_ocr", "partial_ocr_failure")
    except LocalOCRFailed:
        return _unknown("pdf_ocr", "ocr_failed")
    return _analyze_ocr(extracted, "pdf_ocr")
