"""Owner-complete general inputs for the existing shared PDF review UI.

No OCR, AI, candidate prefill, authority store or accounting writer.
The host independently binds these fields to its current Drive Unit proof.
"""
from .drive_run_state import StateError
from .receipt_reimport import _date, _money

FIELDS = {'date', 'amount', 'category', 'merchant', 'payment', 'memo', 'manual_action'}
CONFIRM_ACTION = '一般支出を確定'
# Preserve already-captured explicit owner intent when the display label changes.
CONFIRM_ACTIONS = (CONFIRM_ACTION, '一般手入力を確定')


def category_choices(categories):
    return sorted({'｜'.join(pair) for pair in categories})


def manual_values(fields, categories):
    try:
        day = _date(fields.get('date', ''))
        amount = _money(fields.get('amount', ''))
        pair = str(fields.get('category', '')).split('｜')
        merchant, payment, note = (fields.get(k, '') for k in ('merchant', 'payment', 'memo'))
        if (not day or amount is None or not 1 <= amount <= 99999999 or int(amount) != amount
                or len(pair) != 2 or tuple(pair) not in set(categories)
                or not all(isinstance(x, str) for x in (merchant, payment, note))
                or len(merchant) > 100 or len(payment) > 50 or len(note) > 300
                or fields.get('manual_action') not in ('保留', *CONFIRM_ACTIONS)):
            raise ValueError()
        return {'date': day, 'amount': int(amount), 'major': pair[0], 'minor': pair[1],
                'merchant': merchant, 'payment': payment, 'note': note}
    except Exception:
        raise StateError('pdf_general_complete_manual_input_required') from None
