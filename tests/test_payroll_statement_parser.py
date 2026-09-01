import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.payroll_ocr import EncryptedPayrollPdfError, ExtractedPayrollText, PositionedText
from app.payroll_models import PayrollItem
from app.payroll_statement_parser import _totals, preview_payroll_file
from app.payroll_storage import phase_a_to_storage_candidate


def summary_item(candidate, value, *, section="reference", needs_review=False):
    return PayrollItem(
        raw_item_name=candidate,
        section=section,
        value=value,
        standard_item_candidate=candidate,
        needs_review=needs_review,
    )


def anonymized_ocr_fixture() -> ExtractedPayrollText:
    path = Path(__file__).with_name("fixtures") / "payroll_ocr_anonymized_reference_layout.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert all(payload["anonymization"][field] is None for field in (
        "company_name", "employee_name", "employee_id", "account_information",
    ))
    return ExtractedPayrollText(
        text=payload["text"], file_type=payload["file_type"],
        extraction_method=payload["extraction_method"],
        tokens=tuple(PositionedText(**token) for token in payload["tokens"]),
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


def test_anonymized_ocr_fixture_covers_preview_totals_and_storage_review(monkeypatch):
    extracted = anonymized_ocr_fixture()
    monkeypatch.setattr("app.payroll_statement_parser.extract_payroll_text",
                        lambda path: extracted)

    separator = next(token for token in extracted.tokens if token.text == "|")
    assert (separator.x, separator.y, separator.height) == (238, 8, 78)

    preview = preview_payroll_file("anonymized-payroll.pdf")

    assert preview.company_name is None
    assert not preview.company_present
    assert preview.extraction_method == "ocr"
    assert preview.parse_status == "success"
    assert (preview.gross_pay, preview.total_deductions, preview.net_pay) == (
        740669, 180149, 560520,
    )
    parsed = {item.raw_item_name: item for item in preview.items}
    assert (parsed["非課税合計"].raw_value, parsed["非課税合計"].value,
            parsed["非課税合計"].needs_review, parsed["非課税合計"].confidence) == (
        "38.430", 38430, False, 83,
    )
    assert (parsed["非課税合計"].x, parsed["非課税合計"].y,
            parsed["総支給額累計"].x, parsed["総支給額累計"].value) == (
        20, 52, 260, 7087172,
    )

    candidate = phase_a_to_storage_candidate(preview, statement_label="給与明細")

    stored = {item.raw_item_name: item for item in candidate.items}
    assert candidate.statement.needs_review
    assert (stored["非課税合計"].standard_item_id, stored["非課税合計"].raw_value,
            stored["非課税合計"].value, stored["非課税合計"].needs_review) == (
        "non_taxable_total", "38.430", None, True,
    )
    assert (stored["標準報酬月額"].standard_item_id,
            stored["標準報酬月額"].value, stored["標準報酬月額"].needs_review) == (
        "standard_monthly_remuneration", None, True,
    )


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
