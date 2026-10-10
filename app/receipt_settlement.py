"""Printed settlement facts, never a balancing adjustment or payment guess.

Local exact-unit text is transient. Only the bounded amounts and evidence
digest survive. A voucher is ambiguous unless its discount/tender purpose is
explicit; earned points and prepaid-card payments are not negative expenses.
"""
from copy import deepcopy
from dataclasses import dataclass
import re
import unicodedata

from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_plan.models import ReceiptItem, ReceiptResult


@dataclass(frozen=True)
class SettlementFacts:
    net: int
    gross: int
    tax: int
    payable: int
    method: str
    evidence_digest: str
    voucher_decision: str | None = None


def _text(text):
    return unicodedata.normalize('NFKC', text).replace('￥', '¥')


def _amount(text, label):
    """Require one unambiguous anchored line; numbers in counts/rates ignored."""
    amounts = []
    for line in _text(text).splitlines():
        if not re.match(r'^\s*(?:'+label+r')', line):
            continue
        match = re.search(r'([0-9][0-9,]*)\s*円?\s*$', line)
        if match:
            amounts.append(int(match[1].replace(',', '')))
    return amounts[0] if len(amounts) == 1 else None


def voucher_kind(text):
    normalized = _text(text)
    if re.search(r'(ポイント獲得|獲得ポイント|累計ポイント)', normalized) and not re.search(
            r'(ポイント利用|ポイントお買物券|ポイント券|お買物券)', normalized):
        return 'none'
    if re.search(r'(ポイント利用|ポイントお買物券|ポイント券|お買物券|商品券|ギフト券|クーポン)', normalized):
        # Product wording alone cannot establish a monetary instrument's origin.
        return 'unresolved'
    return 'none'


def settlement_issues(text):
    return ['ポイント券の値引き・支払手段の確認が必要'] if voucher_kind(text) == 'unresolved' else []


def printed_facts(text, *, voucher_decision=None):
    """No receipt-total-minus-items shortcut. Missing evidence fails closed.

    The optional decision must come from the trusted original-review adapter,
    never Gemini or a bare cell. Tender needs a separate settlement model;
    this expense writer deliberately refuses to turn it into a discount.
    """
    if voucher_decision not in (None, 'discount', 'tender'):
        raise StateError('settlement_voucher_decision_invalid')
    if voucher_kind(text) == 'unresolved':
        if voucher_decision is None:
            raise StateError('settlement_voucher_confirmation_required')
        if voucher_decision == 'tender':
            raise StateError('settlement_tender_model_required')
    normalized = _text(text)
    if re.search(r'(内税|税含|税込価格)', normalized):
        raise StateError('settlement_included_tax_no_addition')
    net = _amount(text, r'本体合計(?:\s*\([^\n]*\))?\s*|小計(?:\s*\d+点)?\s*')
    gross = _amount(text, r'税込金額合計\s*|総合計\s*')
    payable = _amount(text, r'合計\s*')
    if payable is None:
        payable = gross
    if not all(type(x) is int and x > 0 for x in (net, gross, payable)):
        raise StateError('settlement_printed_subtotals_required')
    tax = _amount(text, r'(?:[0-9]+%\s*)?(?:消費税|税額)\s*')
    method = 'printed_external_tax'
    if tax is None or net + tax != gross:
        # A pre-discount tax bridge needs BOTH printed subtotals, one explicit
        # rate and its separately printed final tax base/tax. Never combine
        # the pre-discount bridge with the final tax a second time.
        rates = set(re.findall(r'(\d+)%', normalized))
        rate = int(next(iter(rates))) if len(rates) == 1 else 0
        final_net = _amount(text, r'[0-9]+%\s*税抜対象額\s*')
        if (voucher_decision != 'discount' or rate not in (8, 10) or tax is None
                or final_net is None or final_net * rate // 100 != tax
                or net * rate // 100 != gross - net
                or final_net + tax != payable):
            raise StateError('settlement_external_tax_evidence_conflict')
        tax = gross - net
        method = 'printed_subtotal_tax_bridge'
    if tax <= 0 or tax > net:
        raise StateError('settlement_external_tax_invalid')
    evidence = [net, gross, tax, payable, method, voucher_decision]
    return SettlementFacts(net, gross, tax, payable, method, digest(evidence), voucher_decision)


def add_external_tax(result, facts, categories):
    """Idempotent explicit tax row, assigned only to one proven category.

    Multi-category/tax-rate allocation remains held until its taxable bases
    are known. Existing printed discounts retain their amounts and categories.
    """
    result = ReceiptResult.model_validate(result.model_dump())
    positives = [i for i in result.items if i.amount >= 0 and not re.search(r'外税|消費税|税額', i.name)]
    taxes = [i for i in result.items if re.search(r'外税|消費税|税額', i.name)]
    negatives = sum(i.amount for i in result.items if i.amount < 0)
    if (sum(i.amount for i in positives) != facts.net or facts.gross + negatives != facts.payable
            or result.total != facts.payable):
        raise StateError('settlement_goods_discount_total_conflict')
    pairs = {(i.major_category, i.minor_category) for i in positives}
    if len(pairs) != 1 or not pairs <= set(categories):
        raise StateError('settlement_tax_category_allocation_required')
    if taxes:
        if len(taxes) == 1 and taxes[0].amount == facts.tax and sum(i.amount for i in result.items) == result.total:
            return result
        raise StateError('settlement_existing_tax_conflict')
    major, minor = next(iter(pairs))
    result.items.append(ReceiptItem(name='外税', quantity=1, amount=facts.tax,
        major_category=major, minor_category=minor, confidence=min(i.confidence for i in positives),
        note='原本税根拠:'+facts.method+':'+facts.evidence_digest))
    return result


def normalize_readings(readings, texts, categories):
    """Only stable single-unit shapes may gain an OCR-corroborated tax row.

    For multiple units, adding an unlocated item would weaken segmentation's
    item-box requirement, so leave those receipts held without changing them.
    """
    from .page_receipt_model import ordered
    a, b = [ordered(r) for r in readings]
    if len(a) != 1 or len(b) != 1:
        return readings
    shape = lambda r: [(i.name, i.quantity, i.amount) for i in r.items]
    if shape(a[0].receipt) != shape(b[0].receipt) or a[0].receipt.total != b[0].receipt.total:
        return readings
    try:
        facts = printed_facts(texts.get(1, ''))
        normalized = [add_external_tax(r[0].receipt, facts, categories) for r in (a, b)]
    except StateError:
        return readings
    result = deepcopy(readings)
    for reading, receipt in zip(result, normalized):
        ordered(reading)[0].receipt = receipt
    return result
