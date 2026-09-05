"""Opt-in local bytes -> OCR -> shadow counters; no production callers or writes.

Uses the existing local OCR engine. Text/tokens/frames are transient and never
returned. The PDF path intentionally observes every rendered page, even if the
document has embedded text. It is not a simulation of production PDF routing.
"""
from __future__ import annotations

import math
from io import BytesIO

from . import receipt_text_extraction as extraction
from .medical_layout_evaluation import _empty_summary, evaluate_medical_layout
from .medical_layout_shadow import PageFrame

_MAX_BYTES = 20 * 1024 * 1024
_MAX_PIXELS = 20_000_000
_MAX_PAGES = 3
_RENDER_SCALE = 3


class _InputRejected(Exception):
    pass


class _PageLimitExceeded(Exception):
    pass


def _summary():
    return {**_empty_summary(), "local_input_rejected": 0,
            "local_observation_failed": 0, "local_page_limit_exceeded": 0}


def _check_size(width, height):
    if (type(width) not in (int, float) or type(height) not in (int, float)
            or not math.isfinite(width) or not math.isfinite(height)
            or width <= 0 or height <= 0 or width * height > _MAX_PIXELS):
        raise _InputRejected()


def _observe_image(image, page):
    width, height = image.size
    _check_size(width, height)
    text = extraction._run_image_ocr(image)
    tokens = extraction._run_image_ocr_tokens(image, page)
    if type(text) is not str or not text.strip() or not tokens:
        raise ValueError()
    # Neither OCR pass is an independent vote. Both use this exact image/frame.
    if any(t.page != page for t in tokens):
        raise ValueError()
    return text, tokens, PageFrame(page, width, height)


def _image_input(content, mime):
    from PIL import Image

    with Image.open(BytesIO(content)) as image:
        expected = "PNG" if mime == "image/png" else "JPEG"
        if (image.format != expected or getattr(image, "n_frames", 1) != 1
                or image.getexif().get(274, 1) != 1):
            # Do not silently use frame one or invent an orientation transform.
            raise _InputRejected()
        _check_size(*image.size)
        image.load()
        text, tokens, frame = _observe_image(image, 1)
        return text, tokens, (frame,)


def _pdf_input(content):
    import pypdfium2 as pdfium

    # Existing validator rejects encrypted/malformed input and suppresses parser
    # diagnostics. Embedded content is discarded, never used as OCR coverage.
    extraction._extract_pdf_embedded_text(content)
    document = pdfium.PdfDocument(content)
    try:
        count = len(document)
        if count > _MAX_PAGES:
            raise _PageLimitExceeded()
        if count < 1:
            raise _InputRejected()
        texts, tokens, frames = [], [], []
        for index in range(count):
            page = bitmap = image = None
            try:
                page = document[index]
                width, height = page.get_size()
                # Bound allocation before rendering and verify actual pixels too.
                _check_size(math.ceil(width * _RENDER_SCALE), math.ceil(height * _RENDER_SCALE))
                bitmap = page.render(scale=_RENDER_SCALE)
                image = bitmap.to_pil()
                text, observed, frame = _observe_image(image, index + 1)
                texts.append(text)
                tokens.extend(observed)
                frames.append(frame)
            finally:
                extraction._close_if_possible(image)
                extraction._close_if_possible(bitmap)
                extraction._close_if_possible(page)
        return "\n".join(texts), tuple(tokens), tuple(frames)
    finally:
        extraction._close_if_possible(document)


def evaluate_local_medical_bytes(content: bytes, mime_type: str) -> dict[str, int]:
    """Evaluate safely obtained local input; never fetch a URL or return content.

    Errors discard the entire input observation, not merely the failing page.
    Immutable bytes prevent caller buffer mutation between inspection and OCR.
    The caller must not log arguments, locals, or original input on failure.
    """
    result = _summary()
    try:
        if type(content) is not bytes or not content or len(content) > _MAX_BYTES or type(mime_type) is not str:
            raise _InputRejected()
        mime = mime_type.strip().lower()
        if mime == "application/pdf":
            text, tokens, frames = _pdf_input(content)
        elif mime in {"image/png", "image/jpeg", "image/jpg"}:
            text, tokens, frames = _image_input(content, mime)
        else:
            raise _InputRejected()
        result.update(evaluate_medical_layout(text, tokens, frames,
            expected_pages=len(frames), observation_complete=True))
        return result
    except _InputRejected:
        result["local_input_rejected"] = 1
    except _PageLimitExceeded:
        result["local_page_limit_exceeded"] = 1
    except Exception:
        result["local_observation_failed"] = 1
    result["evaluation_failed"] = 1
    return result
