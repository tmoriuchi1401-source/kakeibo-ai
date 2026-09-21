"""Owner-selected routing for unruled receipt images, not a medical diagnosis."""
from io import BytesIO
import os
import re
import unicodedata

POLICY = 'owner-unruled-v1'
REASON = 'owner_unruled_receipt_policy'


def enabled():
    return os.environ.get('RECEIPT_UNRULED_POLICY') == POLICY


def text_compatible(text):
    """A drugstore name alone does not establish a medical document."""
    from .medical_receipt_privacy import (
        _MEDICAL_SIGNALS, _PAYROLL_SIGNALS, _AMBIGUOUS_SENSITIVE_SIGNALS, _matched_signals,
    )
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or ''))
    medical = tuple(x for x in _MEDICAL_SIGNALS if x != '薬局')
    ambiguous = tuple(x for x in _AMBIGUOUS_SENSITIVE_SIGNALS if x != '薬局')
    # Explicit treatment/dispensing terms supplement the existing taxonomy.
    clinical = ('処方', '調剤', '診察', '療養', '負担割合', '診療点数', '保険薬局')
    return bool(compact) and not any(_matched_signals(compact, signals)
        for signals in (medical, ambiguous, _PAYROLL_SIGNALS, clinical))


def has_ruled_table(content, mime_type):
    """True=intersecting table rules, False=none found, None=not inspected.

    Whole image only; separators and barcode strokes are not table cells.
    Large ambiguous line collections stay unresolved. Images only in v1.
    """
    if (mime_type or '').strip().lower() not in {'image/png', 'image/jpeg', 'image/jpg'}:
        return None
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps

        with Image.open(BytesIO(content)) as raw:
            if raw.width < 150 or raw.height < 150 or raw.width * raw.height > 20_000_000:
                return None
            with ImageOps.exif_transpose(raw) as image:
                image.thumbnail((1200, 3000))
                gray = np.asarray(image.convert('L'))
        height, width = gray.shape
        if min(height, width) < 150:
            return None
        edges = cv2.Canny(gray, 50, 150)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=45,
            minLineLength=max(35, round(width * .12)), maxLineGap=8)
        if lines is None:
            return False
        if len(lines) > 1500:
            return None
        horizontal, vertical = [], []
        for x1, y1, x2, y2 in lines.reshape(-1, 4).tolist():
            dx, dy = x2 - x1, y2 - y1
            if abs(dx) >= width * .28 and abs(dy) <= abs(dx) * .30:
                horizontal.append((x1, y1, x2, y2))
            elif abs(dy) >= max(40, height * .035) and abs(dx) <= abs(dy) * .30:
                vertical.append((x1, y1, x2, y2))
        if len(horizontal) > 120 or len(vertical) > 120:
            return None
        tolerance = max(6, width * .01)
        intersections = []
        for v in vertical:
            crossings = {}
            for index, h in enumerate(horizontal):
                x1, y1, x2, y2 = h
                x3, y3, x4, y4 = v
                denominator = (x1-x2)*(y3-y4) - (y1-y2)*(x3-x4)
                if not denominator:
                    continue
                px = ((x1*y2-y1*x2)*(x3-x4)-(x1-x2)*(x3*y4-y3*x4)) / denominator
                py = ((x1*y2-y1*x2)*(y3-y4)-(y1-y2)*(x3*y4-y3*x4)) / denominator
                if all(min(a, b)-tolerance <= n <= max(a, b)+tolerance
                    for a, b, n in ((x1,x2,px), (y1,y2,py), (x3,x4,px), (y3,y4,py))):
                    crossings[index] = (px, py)
            intersections.append(crossings)
        # A cell needs two distinct vertical boundaries and two shared rules.
        # A single text stroke or paper edge crossing separators is insufficient.
        for i, left in enumerate(intersections):
            for right in intersections[i + 1:]:
                shared = [index for index in left.keys() & right.keys()
                          if abs(left[index][0] - right[index][0]) >= max(35, width * .12)]
                for j, top in enumerate(shared):
                    for bottom in shared[j + 1:]:
                        if (abs(left[top][1] - left[bottom][1]) >= max(35, height * .035)
                                and abs(right[top][1] - right[bottom][1]) >= max(35, height * .035)):
                            return True
        return False
    except Exception:
        return None


def allows(content, mime_type, texts):
    return (enabled() and bool(texts) and all(text and text.strip() for text in texts)
            and text_compatible('\n'.join(texts))
            and has_ruled_table(content, mime_type) is False)
