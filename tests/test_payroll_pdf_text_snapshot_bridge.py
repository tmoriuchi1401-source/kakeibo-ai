from pypdf import PdfWriter

from app.payroll_ocr import ExtractedPayrollText, PositionedText
from app.payroll_pdf_text_snapshot_bridge import (
    capture_pdf_text_ownership_snapshot,
)
from app.payroll_ownership_provenance import reconstruct_consumption


KEY = b"synthetic-pdf-text-bridge-key-000"


def extracted(*, method="pdf_text", value="1,234"):
    tokens = (
        PositionedText("基本給", 1, 10, 20, 30, 10, 100),
        PositionedText(value, 1, 50, 20, 30, 10, 100),
    )
    return ExtractedPayrollText(
        "基本給 " + value, "pdf", method, tokens,
    )


def pdf_path(tmp_path):
    path = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_pdf_text_capture_binds_stable_production_tokens(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.payroll_pdf_text_snapshot_bridge.extract_payroll_text",
        lambda _path: extracted(),
    )

    result = capture_pdf_text_ownership_snapshot(
        pdf_path(tmp_path), local_key=KEY,
    )

    assert result.ownership_ready
    assert result.page_scope_complete
    assert result.rerun_stable
    assert result.snapshot.parser_mode == "pdf"
    assert reconstruct_consumption(result.snapshot).complete


def test_pdf_text_capture_rejects_mode_or_rerun_change(monkeypatch, tmp_path):
    values = iter((extracted(), extracted(value="2,345")))
    monkeypatch.setattr(
        "app.payroll_pdf_text_snapshot_bridge.extract_payroll_text",
        lambda _path: next(values),
    )
    changed = capture_pdf_text_ownership_snapshot(
        pdf_path(tmp_path), local_key=KEY,
    )
    assert not changed.ownership_ready
    assert changed.reason == "pdf_text_rerun_unstable"

    monkeypatch.setattr(
        "app.payroll_pdf_text_snapshot_bridge.extract_payroll_text",
        lambda _path: extracted(method="ocr"),
    )
    wrong_mode = capture_pdf_text_ownership_snapshot(
        pdf_path(tmp_path), local_key=KEY,
    )
    assert not wrong_mode.ownership_ready
    assert wrong_mode.reason == "production_did_not_select_pdf_text"
