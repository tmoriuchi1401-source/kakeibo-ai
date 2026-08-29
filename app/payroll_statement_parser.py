from __future__ import annotations

import re
from pathlib import Path

from .payroll_models import PayrollItem, PayrollPreview
from .payroll_ocr import extract_payroll_text
from .payroll_parser import amounts, compact, parse_positioned_items, parse_period_and_date


_COMPANY_MARKERS = ("株式会社", "有限会社", "合同会社", "合資会社", "合名会社")
_SENSITIVE_MARKERS = ("氏名", "社員番号", "従業員番号", "住所", "口座", "メール")


def _company_name(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (line and any(marker in line for marker in _COMPANY_MARKERS)
                and not any(marker in line for marker in _SENSITIVE_MARKERS)):
            return line
    return None


def _totals(text: str, items: list[PayrollItem]) -> tuple[int | None, int | None, int | None]:
    normalized = compact(text)
    gross = deductions = net = None
    # Common compact summary row: gross, deductions, net.
    for line in text.splitlines():
        vals = amounts(line)
        vals.extend(int(value) for value in re.findall(r"(?<![\d,.])\d{5,7}(?![\d,.])", line))
        if len(vals) >= 3 and vals[-3] - vals[-2] == vals[-1]:
            gross, deductions, net = vals[-3:]
    # Older statements place gross/net together and deductions on a separate row.
    match = re.search(r"総支給額\s+差引支給額[^\n]*\n\s*([\d,]+)\s+([\d,]+)", text)
    if match:
        gross, net = (int(value.replace(",", "")) for value in match.groups())
    match = re.search(r"控除合計[^\n]*\n\s*([\d,]+)", text)
    if match: deductions = int(match.group(1).replace(",", ""))
    for item in items:
        if not isinstance(item.value, int): continue
        if item.standard_item_candidate == "gross_pay": gross = item.value
        elif item.standard_item_candidate == "total_deductions": deductions = item.value
        elif item.standard_item_candidate == "net_pay": net = item.value
    # OCR often retains these three robust anchors even when the summary row loses
    # its labels: taxable earnings + non-taxable earnings = gross; transfer = net.
    if gross is None:
        tax = re.search(r"課税対象支給額\s*([\d,.]+)", text)
        non_tax = re.search(r"非課税合計\s*([\d,.]+)", text)
        insured = re.search(r"[屋雇]用保険対象額\s*([\d,.]+)", text)
        def number(match):
            return int(re.sub(r"\D", "", match.group(1))) if match else None
        taxable, non_taxable, insured_total = number(tax), number(non_tax), number(insured)
        if taxable is not None and non_taxable is not None:
            gross = taxable + non_taxable
        elif insured_total is not None:
            gross = insured_total
    if net is None:
        # The transfer amount is normally the last salary-sized value on the bank row.
        for line in reversed(text.splitlines()):
            vals = amounts(line)
            if not vals:
                repaired = line.replace("B", "8").replace("Z", "2")
                vals = [int(a + b) for a, b in re.findall(r"(?<!\d)(\d{3})[,. ](\d{3})(?!\d)", repaired)]
            if vals and any(marker in line for marker in ("じ", "銀行", "振込")):
                net = vals[-1]
                break
    if gross is not None and net is not None and deductions is None:
        deductions = gross - net
    if gross is not None and deductions is not None and net is None:
        net = gross - deductions
    return gross, deductions, net


def preview_payroll_file(path: str | Path) -> PayrollPreview:
    extracted = extract_payroll_text(path)
    company_name = _company_name(extracted.text)
    period, pay_date = parse_period_and_date(extracted.text)
    items = parse_positioned_items(extracted.tokens, ocr=extracted.extraction_method == "ocr")
    if extracted.extraction_method == "ocr":
        safe_terms = ("給", "手当", "保険", "年金", "税", "控除", "支給", "勤", "日数", "時間")
        items = [item for item in items if item.standard_item_candidate or
                 any(term in item.raw_item_name for term in safe_terms)]
    gross, deductions, net = _totals(extracted.text, items)
    status = "success" if period and gross is not None and deductions is not None and net is not None else "partial"
    return PayrollPreview(file_type=extracted.file_type,
                          extraction_method=extracted.extraction_method,
                          company_name=company_name,
                          company_present=bool(company_name),
                          pay_period=period, pay_date=pay_date, gross_pay=gross,
                          total_deductions=deductions, net_pay=net, items=items,
                          parse_status=status)
