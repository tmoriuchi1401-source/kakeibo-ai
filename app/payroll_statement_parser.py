from __future__ import annotations

import re
from pathlib import Path

from .payroll_models import PayrollItem, PayrollPreview
from .payroll_ocr import extract_payroll_text
from .payroll_parser import amounts, compact, parse_positioned_items, parse_period_and_date


_COMPANY_MARKERS = ("株式会社", "有限会社", "合同会社", "合資会社", "合名会社")
_SENSITIVE_MARKERS = ("氏名", "社員番号", "従業員番号", "住所", "口座", "メール")
_SUMMARY_CANDIDATES = ("gross_pay", "total_deductions", "net_pay")
_COMPLETE_MONEY_TOKEN = re.compile(
    r"\d+|\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+"
)


def _company_name(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if (line and any(marker in line for marker in _COMPANY_MARKERS)
                and not any(marker in line for marker in _SENSITIVE_MARKERS)):
            return line
    return None


def _confirmed_summary_values(items: list[PayrollItem]) -> dict[str, int]:
    grouped = {
        candidate: {
            item.value for item in items
            if item.standard_item_candidate == candidate
            and item.section == "reference"
            and isinstance(item.value, int)
            and not isinstance(item.value, bool)
            and not item.needs_review
        }
        for candidate in _SUMMARY_CANDIDATES
    }
    return {
        candidate: next(iter(values))
        for candidate, values in grouped.items()
        if len(values) == 1
    }


def _supplement_totals_from_items(
    gross: int | None,
    deductions: int | None,
    net: int | None,
    items: list[PayrollItem],
) -> tuple[int | None, int | None, int | None]:
    existing = {
        "gross_pay": gross,
        "total_deductions": deductions,
        "net_pay": net,
    }
    confirmed = _confirmed_summary_values(items)
    supplements = {
        candidate: value for candidate, value in confirmed.items()
        if existing[candidate] is None
    }
    proposed = existing | supplements
    if (all(isinstance(proposed[candidate], int) for candidate in _SUMMARY_CANDIDATES)
            and proposed["gross_pay"] - proposed["total_deductions"]
            != proposed["net_pay"]):
        # Do not make a contradictory three-value header authoritative using items.
        supplements = {}
    resolved = existing | supplements
    return (resolved["gross_pay"], resolved["total_deductions"], resolved["net_pay"])


def _anchored_money_token(text: str, label_pattern: str) -> int | None:
    """Read one complete money token after a known total label without repair."""
    match = re.search(rf"{label_pattern}\s*(\S+)", text)
    if match is None:
        return None
    token = compact(match.group(1))
    if _COMPLETE_MONEY_TOKEN.fullmatch(token) is None:
        return None
    return int(token.replace(",", "").replace(".", ""))


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
    gross, deductions, net = _supplement_totals_from_items(
        gross, deductions, net, items,
    )
    # OCR often retains these three robust anchors even when the summary row loses
    # its labels: taxable earnings + non-taxable earnings = gross; transfer = net.
    if gross is None:
        taxable = _anchored_money_token(text, r"課税対象支給額")
        non_taxable = _anchored_money_token(text, r"非課税合計")
        insured_total = _anchored_money_token(text, r"[屋雇]用保険対象額")
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
