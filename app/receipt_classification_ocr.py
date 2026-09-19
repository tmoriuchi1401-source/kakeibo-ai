"""Bounded local re-reading for document kind only, never payment extraction.

Whole pages are read: no region is removed to make a document appear ordinary.
The original OCR observation is retained when reconciling all passes. No OCR
text, filename or image is logged, persisted, or returned in the public result.
"""
from io import BytesIO
from contextlib import closing

from .medical_receipt_privacy import ClassificationDecision, classify_receipt_text


def _read_page(image):
    import pytesseract
    from PIL import Image, ImageOps

    # Explicitly bound memory and subprocess time. Resize without cropping.
    width, height = image.size
    scale = min(2.0, (12_000_000 / (width * height)) ** 0.5)
    with ImageOps.grayscale(image) as gray:
        with gray.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BICUBIC) as enlarged:
            return tuple(pytesseract.image_to_string(
                enlarged, lang='jpn', config=config, timeout=25,
            ) for config in ('--psm 6', '--psm 4 -c thresholding_method=2'))


def reread_classification(content, mime_type, original_text):
    """Return a data-free decision only after every bounded pass completes."""
    from PIL import Image

    texts = [original_text or '']
    try:
        if (mime_type or '').strip().lower() == 'application/pdf':
            import pypdfium2 as pdfium

            with closing(pdfium.PdfDocument(content)) as document:
                if not 0 < len(document) <= 3:
                    return None
                for n in range(len(document)):
                    with closing(document[n]) as page:
                        with closing(page.render(scale=3)) as bitmap:
                            with bitmap.to_pil() as image:
                                observations = _read_page(image)
                            if not all(t.strip() for t in observations):
                                return None
                            texts.extend(observations)
        elif (mime_type or '').strip().lower() in {'image/png', 'image/jpeg', 'image/jpg'}:
            with Image.open(BytesIO(content)) as image:
                image.load()
                observations = _read_page(image)
            if not all(t.strip() for t in observations):
                return None
            texts.extend(observations)
        else:
            return None
        # Sensitive evidence from ANY reading wins. Combine negative evidence,
        # but require one complete reading to establish the sale structure:
        # duplicated passes must not manufacture multiple purchased items.
        combined = classify_receipt_text('\n'.join(texts))
        if combined.classification in {'medical', 'payroll'} or combined.reason_code in {
            'conflicting_sensitive_evidence', 'sensitive_signal_insufficient',
        }:
            return combined
        for text in texts:
            decision = classify_receipt_text(text)
            if decision.classification == 'normal':
                return decision
        return ClassificationDecision(classification='sensitive_unknown', reason_code='insufficient_evidence')
    except Exception:
        # An incomplete pass never promotes a previously blocked original.
        return None
