from types import SimpleNamespace

import pytest

from app.payroll_ocr import EncryptedPayrollPdfError
from app.payroll_models import PayrollItem
from app.payroll_statement_parser import _totals, preview_payroll_file


def summary_item(candidate, value, *, section="reference", needs_review=False):
    return PayrollItem(
        raw_item_name=candidate,
        section=section,
        value=value,
        standard_item_candidate=candidate,
        needs_review=needs_review,
    )


def test_preview_uses_extracted_text_and_calculates_summary(monkeypatch):
    text = """秘密サンプル株式会社
2024年11月分 給与明細書
基本給 独自手当 支給合計
300,000 20,000 320,000
控除合計
50,000
総支給額 差引支給額
320,000 270,000
"""
    monkeypatch.setattr("app.payroll_statement_parser.extract_payroll_text",
                        lambda path: SimpleNamespace(text=text, file_type="pdf",
                                                     extraction_method="pdf_text", tokens=()))
    result = preview_payroll_file("statement.pdf")
    assert (result.gross_pay, result.total_deductions, result.net_pay) == (320000, 50000, 270000)
    assert result.parse_status == "success"
    assert result.company_name == "秘密サンプル株式会社"
    assert result.company_present
    assert "秘密サンプル株式会社" not in result.model_dump()
    assert "秘密サンプル株式会社" not in result.model_dump_json()


def test_encrypted_pdf_stops_safely(monkeypatch):
    def stop(path):
        raise EncryptedPayrollPdfError("暗号化PDFは処理できません")
    monkeypatch.setattr("app.payroll_statement_parser.extract_payroll_text", stop)
    with pytest.raises(EncryptedPayrollPdfError):
        preview_payroll_file("encrypted.pdf")


def test_duplicate_equal_gross_items_are_one_safe_header_candidate():
    items = [summary_item("gross_pay", 320000), summary_item("gross_pay", 320000)]

    assert _totals("", items) == (320000, None, None)


def test_conflicting_gross_items_do_not_supplement_header():
    items = [summary_item("gross_pay", 320000), summary_item("gross_pay", 330000)]

    assert _totals("", items) == (None, None, None)


def test_item_equal_to_existing_header_value_keeps_header():
    text = "320,000 50,000 270,000"

    assert _totals(text, [summary_item("gross_pay", 320000)]) == (
        320000, 50000, 270000)


def test_item_different_from_existing_header_value_does_not_overwrite():
    text = "320,000 50,000 270,000"

    assert _totals(text, [summary_item("gross_pay", 999999)]) == (
        320000, 50000, 270000)


def test_consistent_confirmed_summary_items_still_populate_header():
    items = [
        summary_item("gross_pay", 320000),
        summary_item("total_deductions", 50000),
        summary_item("net_pay", 270000),
    ]

    assert _totals("", items) == (320000, 50000, 270000)


def test_inconsistent_items_do_not_make_three_value_header_authoritative():
    items = [
        summary_item("gross_pay", 320000),
        summary_item("net_pay", 260000),
    ]

    assert _totals("控除合計\n50,000", items) == (None, 50000, None)


def test_non_reference_reviewed_or_missing_value_items_are_not_candidates():
    items = [
        summary_item("gross_pay", 320000, section="earning"),
        summary_item("total_deductions", 50000, needs_review=True),
        summary_item("net_pay", None),
    ]

    assert _totals("", items) == (None, None, None)


@pytest.mark.parametrize(
    ("taxable", "non_taxable", "expected_gross"),
    (
        ("38.430", "0", 38430),
        ("53.985", "0", 53985),
        ("51.500", "0", 51500),
        ("702.239", "560,520", 1262759),
        ("38430", "0", 38430),
        ("560520", "0", 560520),
    ),
)
def test_total_anchor_fallback_accepts_complete_money_tokens(
    taxable, non_taxable, expected_gross,
):
    text = f"課税対象支給額 {taxable}\n非課税合計 {non_taxable}"

    assert _totals(text, []) == (expected_gross, None, None)


@pytest.mark.parametrize(
    "malformed_taxable",
    (
        "2011.300",
        "|38.430",
        "409.257|",
        ".38.430",
        "38.430.",
        "38..430",
        "38,43",
        "38,430,00",
        "38,430.000",
    ),
)
def test_total_anchor_fallback_rejects_malformed_amount_tokens(malformed_taxable):
    text = f"課税対象支給額 {malformed_taxable}\n非課税合計 38.430"

    assert _totals(text, []) == (None, None, None)
