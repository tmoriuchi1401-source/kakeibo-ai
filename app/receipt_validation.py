"""Accounting checks shared by extraction, correction and posting."""
from datetime import date
import re
import unicodedata

POLICY_VERSION = 'receipt-required-date-total-category-v1'


def validate_receipt_result(result, categories):
    notes=[]
    if any((x.major_category,x.minor_category) not in set(categories) for x in result.items):
        notes.append('カテゴリ不正')
    item_sum=sum(x.amount for x in result.items)
    # Yen are integers. A tolerance used to silently admit missing small
    # discounts/taxes. Never manufacture a balancing item to pass this check.
    if item_sum!=result.total:notes.append(f'明細合計{item_sum}≠レシート合計{result.total}')
    if not result.date:notes.append('日付不明')
    else:
        try:
            if date.fromisoformat(result.date).isoformat()!=result.date:raise ValueError
        except (ValueError,TypeError):notes.append('日付不正')
    if not result.items:notes.append('明細なし')
    if result.total<=0:notes.append('合計金額不正')
    if any(not x.name.strip() or (x.quantity is not None and x.quantity<=0) for x in result.items):notes.append('明細不正')
    return not notes,notes


def apply_receipt_policy(result, categories, *, text='', gate=None, readings=()):
    """Shared posting/read-only policy. Unsupported optional values stay blank.

    Text is local exact-image extraction, never filenames or prior history.
    A missing payment label is not a validation error. Explicit contradictory
    payment labels remain reviewable; no payment method is inferred here.
    """
    _, issues = validate_receipt_result(result, categories)
    if result.transaction_kind == 'unknown':
        issues.append('購入・買取の別が不明')
    elif gate is not None and gate.buyback_evidence != (result.transaction_kind == 'buyback'):
        issues.append('原本の買取表示と取引種別が不一致')
    stable = len({(r.date, r.total, r.transaction_kind) for r in readings}) <= 1
    if not stable: issues.append('再読取で日付・合計・取引種別が変化')
    adjustment = any(re.search('調整|差額補正|差額調整', x.name) and x.name not in text for x in result.items)
    if adjustment: issues.append('原本で確認できない調整明細')
    normalized = unicodedata.normalize('NFKC', text).casefold()
    payment = unicodedata.normalize('NFKC', result.payment_method).casefold().strip()
    patterns = {'現金': r'現金|お預[りか]|預り|釣銭|お釣',
                'クレジット': r'クレジット|カード|visa|master|jcb|amex',
                'クレジットカード': r'クレジット|カード|visa|master|jcb|amex',
                'カード': r'クレジット|カード|visa|master|jcb|amex'}
    supported = not payment or bool(re.search(patterns.get(payment, re.escape(payment)), normalized))
    # Only an explicit payment-field statement can establish contradiction.
    labels = re.findall(r'(?:支払方法|お支払方法|決済方法)\s*[:：]?\s*(現金|クレジットカード|カード|PayPay)', text, re.I)
    family = lambda x: 'card' if x in {'カード','クレジット','クレジットカード'} else x.casefold()
    if payment and labels and all(family(unicodedata.normalize('NFKC', x)) != family(payment) for x in labels):
        issues.append('原本の明示的な支払方法と解析結果が矛盾')
    if not supported: result.payment_method = ''
    return list(dict.fromkeys(issues)), {
        'policy_version': POLICY_VERSION, 'date_present': bool(result.date),
        'merchant_present': bool(result.merchant.strip()), 'total_present': result.total > 0,
        'item_sum': sum(x.amount for x in result.items),
        'total_matches': sum(x.amount for x in result.items) == result.total,
        'category_valid': all((x.major_category,x.minor_category) in categories for x in result.items),
        'reread_stable': stable, 'reread_count': len(readings),
        'unverified_adjustment': adjustment, 'payment_evidence_supported': supported,
        'payment_saved': bool(result.payment_method)}
