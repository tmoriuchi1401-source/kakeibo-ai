"""Issuer-adjacent ward evidence read locally; never a name/address allowlist.

Only an opaque ward reference leaves this reader. It cannot link receipts or
prove distinct spending alone. The comparison also needs different verified
bill serials/dates, different selected issuers and disjoint verified contacts.
"""
import re

from .medical_anonymization import compact

POLICY = 'medical-issuer-ward-v1'
ADDRESS = re.compile(r'[一-龯]{2,3}[都道府県][一-龯]{1,10}市([一-龯]{1,8}区)')
WARD = re.compile(r'市([一-龯]{1,8}区)')
BOUNDARY = re.compile(r'患者|氏名|様|紹介|依頼|処方元|送付先|連絡先|発行元以外')


def address_region(observations, issuer):
    def name(value): return compact(value).replace('·', '・')
    anchors = [t for t in observations if name(t['text']) == name(issuer)]
    if len(anchors) != 1 or anchors[0]['confidence'] < 90: return None
    anchor = anchors[0]; a = anchor['box']; height = a[3] - a[1]
    choices = []
    for token in observations:
        text = compact(token['text']); matches = ADDRESS.findall(text); b = token['box']
        if not matches: continue
        separation = (a[1] + a[3] - b[1] - b[3]) / 2
        if not (.3 * min(height, b[3] - b[1]) <= separation <= 2 * height
                and max(a[0], b[0]) < min(a[2], b[2])): continue
        # A selected provider cannot borrow a patient's/referrer's address.
        if len(matches) != 1 or token['confidence'] < 90 or BOUNDARY.search(text): return None
        if any(t is not anchor and t is not token and BOUNDARY.search(compact(t['text']))
               and b[1] - 1.5 * (b[3]-b[1]) <= t['box'][1] <= a[3]
               and max(min(a[0], b[0]), t['box'][0]) < min(max(a[2], b[2]), t['box'][2])
               for t in observations): return None
        if any(t is not anchor and t is not token
               and (b[1]+b[3])/2 < (t['box'][1]+t['box'][3])/2 < (a[1]+a[3])/2
               and max(a[0], b[0]) <= (t['box'][0]+t['box'][2])/2 <= min(a[2], b[2])
               for t in observations): return None
        choices.append((matches[0], token))
    return choices[0] if len(choices) == 1 else None


def line_image(image, token):
    """Rectify only the detector's original convex quadrilateral, never expand."""
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    q = np.asarray(token.get('quad', []), dtype=np.float32)
    if q.shape != (4, 2) or not np.isfinite(q).all(): raise ValueError('locality_geometry')
    if (np.any(q < 0) or np.any(q[:, 0] >= image.width) or np.any(q[:, 1] >= image.height)
            or q[1, 0] <= q[0, 0] or q[3, 1] <= q[0, 1]): raise ValueError('locality_geometry')
    edges = np.roll(q, -1, axis=0) - q
    following = np.roll(edges, -1, axis=0)
    if not np.all(edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0] > 0):
        raise ValueError('locality_geometry')
    bounds = tuple(np.floor(q.min(axis=0)).astype(int)) + tuple(np.ceil(q.max(axis=0)).astype(int))
    if bounds != tuple(token['box']): raise ValueError('locality_geometry')
    width = round(max(np.linalg.norm(q[1]-q[0]), np.linalg.norm(q[2]-q[3])))
    height = round(max(np.linalg.norm(q[3]-q[0]), np.linalg.norm(q[2]-q[1])))
    if not 2 <= height <= width or width * height > 1_000_000: raise ValueError('locality_geometry')
    matrix = cv2.getPerspectiveTransform(q, np.array([[0,0],[width-1,0],[width-1,height-1],[0,height-1]], dtype=np.float32))
    return ImageOps.grayscale(Image.fromarray(cv2.warpPerspective(
        np.asarray(image.convert('RGB')), matrix, (width, height), borderValue=(255,255,255))))


def recognize_line(image, directory):
    # Pass every argument separately. Pytesseract's config string preserves
    # literal quotes on Windows; no shell or process-wide environment change
    # is needed here. Pixels stay in stdin, OCR text stays in stdout memory.
    from io import BytesIO
    import subprocess
    from pytesseract.pytesseract import tesseract_cmd
    pixels = BytesIO(); image.save(pixels, format='PNG')
    try:
        result = subprocess.run([tesseract_cmd, 'stdin', 'stdout', '-l', 'jpn+eng',
            '--tessdata-dir', str(directory), '--oem', '1', '--psm', '7'],
            input=pixels.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=20, check=False, shell=False)
    except subprocess.TimeoutExpired:
        raise TimeoutError('locality_reader_timeout') from None
    if result.returncode: raise RuntimeError('locality_reader_unavailable')
    return result.stdout.decode('utf-8')


def confirm_ward(image, token, expected):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    from .medical_locality_models import verified_directory
    directory = verified_directory()
    original = line_image(image, token)
    for height, binary in ((64, False), (48, True)):
        patch = original.resize((max(1, round(original.width * height / original.height)), height))
        if binary:
            _, pixels = cv2.threshold(np.asarray(patch), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            patch = Image.fromarray(pixels)
        text = recognize_line(ImageOps.expand(patch, border=16, fill=255), directory)
        wards = WARD.findall(compact(text))
        # Any valid disagreement or multiple regions ends the attempt. No
        # dictionary corrections, expected text prompt or majority vote.
        if wards: return len(wards) == 1 and wards[0] == expected
    return False


def read_locality(image, observations, issuer, reference):
    region = address_region(observations, issuer)
    if region is None: return {}
    ward, token = region
    try:
        if confirm_ward(image, token, ward):
            return {'policy': POLICY, 'readers': 2, 'ward_ref': reference('issuer-ward', ward)}
    except (ValueError, OSError, RuntimeError, TimeoutError):
        pass
    return {}


def distinct_locations(left, right):
    """Corroboration only: never grant equality/link authority."""
    from datetime import date
    try:
        if date.fromisoformat(left['date']) == date.fromisoformat(right['date']): return False
    except (KeyError, TypeError, ValueError): return False
    regions = [x.get('issuer_locality', {}) for x in (left, right)]
    if any(not isinstance(r, dict) or r.get('policy') != POLICY or r.get('readers') != 2
           or not re.fullmatch('[a-f0-9]{64}', str(r.get('ward_ref', ''))) for r in regions): return False
    if regions[0]['ward_ref'] == regions[1]['ward_ref']: return False
    contacts = [x.get('issuer_contacts', {}) for x in (left, right)]
    if any(not isinstance(c, dict) or not c or not set(c) <= {'telephone', 'fax'}
           or any(not re.fullmatch('[a-f0-9]{64}', str(v)) for v in c.values())
           or len(set(c.values())) != len(c) for c in contacts): return False
    return not set(contacts[0].values()) & set(contacts[1].values())
