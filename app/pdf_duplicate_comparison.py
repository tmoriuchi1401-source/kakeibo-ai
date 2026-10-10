"""Local original comparison for a held normal receipt, never amount-only bypass.

Evidence is transient/private. Every permitted receipt is independently tied to
its import, active items and unchanged original bytes. Unrelated candidates hold.
No external AI, writer, Medical or move capability is used here.
"""
from contextlib import closing
from hashlib import sha256
import re
import unicodedata

import pypdfium2 as pdfium

from .drive_run_state import StateError
from .pdf_bounded_rendering import render_scale
from .pdf_receipt_write_canary import table_rows
from .receipt_pdf_units import _render_png, MAX_PAGE_PIXELS, MAX_SOURCE_BYTES
from .receipt_reimport import _date, _money
from .receipt_reimport_production import digest
from .receipt_text_extraction import _extract_receipt_text
from .receipt_privacy_gate import evaluate_receipt_privacy


def normalized(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or '')).casefold()


def date_seen(text, day):
    y, m, d = map(int, day.split('-'))
    return bool(re.search(str(y)+r'(?:年|/|-)0?'+str(m)+r'(?:月|/|-)0?'+str(d)+r'(?:日|\D|$)', normalized(text)))


def snapshot(db, receipt_id):
    receipts = [r for r in table_rows(db, 'レシート') if r and r[0] == receipt_id]
    if len(receipts) != 1 or len(receipts[0]) < 6 or not receipt_id.startswith('R-'):
        raise StateError('duplicate_comparison_identity_held')
    sid = receipt_id[2:]
    imports = [r for r in table_rows(db, '取込データ') if r and r[0] == 'receipt:'+sid]
    expenses = [r for r in table_rows(db, '支出明細') if len(r) > 12 and r[9] == receipt_id and r[12] == 'active']
    if (len(imports) != 1 or len(imports[0]) < 11 or imports[0][2:4] != ['receipt', sid]
            or not expenses or any(r[8:11] != ['receipt', receipt_id, 'receipt:'+sid] for r in expenses)
            or _money(receipts[0][3]) != _money(imports[0][6])
            or sum(_money(r[4]) for r in expenses) != _money(receipts[0][3])):
        raise StateError('duplicate_comparison_identity_held')
    if '/file/d/'+sid+'/' not in receipts[0][5]:
        raise StateError('duplicate_comparison_original_link_held')
    return receipts[0], expenses, imports[0]


def original_png(content, mime):
    if not isinstance(content, bytes) or not content or len(content) > MAX_SOURCE_BYTES:
        raise StateError('duplicate_comparison_original_held')
    if mime == 'application/pdf':
        try:
            with closing(pdfium.PdfDocument(content)) as doc:
                # Multiple pages could represent multiple transactions: hold.
                if len(doc) != 1: raise StateError('duplicate_comparison_original_held')
                with closing(doc[0]) as page:
                    scale, _ = render_scale(page.get_width(), page.get_height(), MAX_PAGE_PIXELS)
                    return _render_png(page, scale, MAX_PAGE_PIXELS), 'image/png'
        except StateError: raise
        except Exception: raise StateError('duplicate_comparison_original_held') from None
    if not mime.startswith('image/'):
        raise StateError('duplicate_comparison_original_held')
    return content, mime


def compare_distinct(db, receipt_id, result, payload, get_original, *, merchant_terms):
    """merchant_terms are reviewed literal original labels, never inferred names."""
    receipt, expenses, imported = snapshot(db, receipt_id)
    sid = receipt_id[2:]
    content, mime = get_original(sid)
    original_hash = sha256(content).hexdigest()
    image, image_mime = original_png(content, mime)
    old = _extract_receipt_text(image, image_mime)
    new = _extract_receipt_text(payload, 'image/png')
    old_gate = evaluate_receipt_privacy(image, image_mime)
    new_gate = evaluate_receipt_privacy(payload, 'image/png')
    old_day = _date(receipt[1])
    if (not old_day or old_day == result.date or normalized(receipt[2]) == normalized(result.merchant)
            or _money(receipt[3]) != result.total
            or sorted(_money(r[4]) for r in expenses) == sorted(r.amount for r in result.items)
            or not all(x.status == 'extracted' and x.observation_complete for x in (old, new))
            or not all(x.classification == 'normal' and x.gemini_allowed for x in (old_gate, new_gate))
            or not date_seen(old.text, old_day) or not date_seen(new.text, result.date)
            or len(merchant_terms) != 2 or not all(merchant_terms)
            or not any(normalized(t) in normalized(old.text) for t in merchant_terms[0])
            or not any(normalized(t) in normalized(new.text) for t in merchant_terms[1])):
        raise StateError('duplicate_comparison_requires_review')
    evidence = {'schema': 'receipt-distinct-originals-v1', 'decision': 'distinct_transactions',
        'receipt_id': receipt_id, 'import_id': imported[0], 'source_file_id': sid,
        'source_content_hash': original_hash, 'rows_digest': digest([receipt, expenses, imported]),
        'candidate_digest': digest(result.model_dump()), 'payload_sha256': sha256(payload).hexdigest(),
        'old_date': old_day, 'new_date': result.date, 'old_item_count': len(expenses),
        'new_item_count': len(result.items), 'date_original_verified': True, 'merchant_original_verified': True}
    verify_comparison(db, evidence, result, payload, get_original)
    return evidence


def verify_comparison(db, evidence, result, payload, get_original, *, replay=False):
    if (evidence.get('schema') != 'receipt-distinct-originals-v1' or evidence.get('decision') != 'distinct_transactions'
            or evidence.get('candidate_digest') != digest(result.model_dump())
            or (not replay and evidence.get('payload_sha256') != sha256(payload).hexdigest())
            or evidence.get('date_original_verified') is not True or evidence.get('merchant_original_verified') is not True
            or digest(list(snapshot(db, evidence['receipt_id']))) != evidence['rows_digest']):
        raise StateError('duplicate_comparison_changed')
    content, _ = get_original(evidence['source_file_id'])
    if sha256(content).hexdigest() != evidence['source_content_hash']:
        raise StateError('duplicate_comparison_changed')
    return {evidence['receipt_id']}
