"""Local issuer-contact evidence. No phone number or pixels are persisted.

A TEL/FAX field or one standalone formatted telephone number must be directly
adjacent to the already selected issuer. Patient/referrer blocks and plain
numeric identifiers are excluded. A second OCR engine must independently
agree with every digit of a complete number.
"""
import re

from .medical_anonymization import compact

LABEL = re.compile(r'(tel[.]?|電話(?:番号)?|fax[.]?|fax番号|ファックス)[:：]?', re.I)
NUMBER = re.compile(r'0[0-9]{1,3}[-ー−][0-9]{1,4}[-ー−][0-9]{4}')
FULL = re.compile(LABEL.pattern + '(' + NUMBER.pattern + ')', re.I)
BOUNDARY = re.compile(r'患者|氏名|住所|連絡先|紹介|依頼|処方元|発行元以外')


def _kind(label):
    return 'fax' if label.lower().startswith(('fax', 'ファックス')) else 'telephone'


def _number(text, kind):
    value = compact(text)
    match = FULL.fullmatch(value)
    if match:
        if _kind(match[1]) != kind: return None
        value = match[2]
    if not NUMBER.fullmatch(value): return None
    digits = re.sub(r'[-ー−]', '', value)
    return digits if len(digits) == 10 else None


def contact_regions(observations, issuer):
    def name(value): return compact(value).replace('·', '・')
    anchors = [t for t in observations if name(t['text']) == name(issuer) and t['confidence'] >= 90]
    if len(anchors) != 1: return {}
    anchor = anchors[0]; a = anchor['box']; ah = a[3] - a[1]
    candidates = {}; claimed = set()
    # Reserve separately detected values for their explicit labels, including
    # low-confidence labels. They must not be promoted via the standalone path.
    for label in observations:
        if not LABEL.fullmatch(compact(label['text'])): continue
        a_label = label['box']; h = a_label[3]-a_label[1]
        for n, t in enumerate(observations):
            b = t['box']
            if (-.25*h <= b[0]-a_label[2] <= 2*h
                    and abs((b[1]+b[3]-a_label[1]-a_label[3])/2) <= .6*max(h,b[3]-b[1])):
                claimed.add(n)
    for ordinal, token in enumerate(observations):
        text = compact(token['text']); full = FULL.fullmatch(text); label = LABEL.fullmatch(text)
        standalone = NUMBER.fullmatch(text) and ordinal not in claimed
        if not full and not label and not standalone: continue
        kind = _kind((full or label)[1]) if full or label else 'telephone'; participants = [token]
        box = token['box']; read_box = box; value = full[2] if full else text if standalone else ''
        if label:
            h = box[3] - box[1]
            nearby = [t for t in observations if NUMBER.fullmatch(compact(t['text']))
                and -.25 * h <= t['box'][0] - box[2] <= 2 * h
                and abs((t['box'][1] + t['box'][3] - box[1] - box[3]) / 2) <= .6 * max(h, t['box'][3]-t['box'][1])]
            if len(nearby) != 1: continue
            number = nearby[0]; b = number['box']; participants.append(number); value = number['text']; read_box = b
            if any(t not in participants and box[2] < t['box'][0] < b[0]
                   and abs((t['box'][1]+t['box'][3]-box[1]-box[3])/2) <= .6*h for t in observations):
                continue
            box = (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
        if not (-.3 * ah <= box[1] - a[3] <= (1.5 if standalone else 2) * ah
                and max(a[0], box[0]) < min(a[2], box[2])):
            continue
        # A nearby owner/referrer block cannot borrow the issuer above it.
        if any(t is not anchor and t not in participants and BOUNDARY.search(compact(t['text']))
               and a[1] <= t['box'][1] <= box[3]
               and max(min(a[0], box[0]), t['box'][0]) < min(max(a[2], box[2]), t['box'][2])
               for t in observations):
            continue
        digits = _number(value, kind)
        # A separately detected label binds the number's meaning, but need not
        # contaminate the independent numeric crop on slanted forms.
        candidates.setdefault(kind, []).append((digits, tuple(read_box), min(t['confidence'] for t in participants)))
    return {kind: (values[0][0], values[0][1]) for kind, values in candidates.items()
            if len(values) == 1 and values[0][0] and values[0][2] >= 90}


def confirm_contact(image, box, expected, kind):
    import cv2
    import numpy as np
    import pytesseract
    from PIL import Image, ImageOps
    original = ImageOps.grayscale(image.crop(box))
    for height, binary in ((64, False), (48, True)):
        patch = original.resize((max(1, round(original.width*height/original.height)), height))
        if binary:
            _, pixels = cv2.threshold(np.asarray(patch), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            patch = Image.fromarray(pixels)
        text = pytesseract.image_to_string(ImageOps.expand(patch, border=16, fill=255),
                                           lang='eng', config='--psm 7', timeout=20)
        # The first reader supplies the field's role; this reader independently
        # verifies the complete numeric suffix without correcting any digit.
        suffix = re.fullmatch(r'[^0-9]*(' + NUMBER.pattern + ')', compact(text))
        actual = _number(suffix[1], kind) if suffix else None
        if actual is not None: return actual == expected
    return False


def read_contacts(image, observations, issuer, reference):
    result = {}
    for kind, (number, box) in contact_regions(observations, issuer).items():
        try:
            if confirm_contact(image, box, number, kind):
                result[kind] = reference('issuer-contact', number)
        except (RuntimeError, TimeoutError):
            pass
    return result
