from types import SimpleNamespace

import pytest

from app.payroll_ocr import EncryptedPayrollPdfError
from app.payroll_statement_parser import preview_payroll_file


def test_preview_uses_extracted_text_and_calculates_summary(monkeypatch):
    text = """2024年11月分 給与明細書
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


def test_encrypted_pdf_stops_safely(monkeypatch):
    def stop(path):
        raise EncryptedPayrollPdfError("暗号化PDFは処理できません")
    monkeypatch.setattr("app.payroll_statement_parser.extract_payroll_text", stop)
    with pytest.raises(EncryptedPayrollPdfError):
        preview_payroll_file("encrypted.pdf")
