import io

import pytest
from pypdf import PdfWriter

from app.medical_receipt import MedicalReceiptAnalysis
from app.medical_receipt_input import MedicalReceiptScreening, screen_medical_receipt
from app.models import ReceiptItem, ReceiptResult
from app.receipt_pipeline import ReceiptPipeline


MEDICAL_CATEGORY = ("医療・保険", "病院")


class FakeDB:
    def __init__(self, *, duplicate=False):
        self.duplicate = duplicate
        self.rows = {}
        self.category_calls = 0

    def import_ids(self):
        return {"receipt:file-1"} if self.duplicate else set()

    def categories(self):
        self.category_calls += 1
        return [MEDICAL_CATEGORY, ("食費", "食品")]

    def append(self, sheet, rows):
        self.rows.setdefault(sheet, []).extend(rows)

    def ensure_expense_status_column(self):
        pass


class FakeAI:
    def __init__(self):
        self.calls = []

    def analyze_receipt(self, data, mime, categories):
        self.calls.append((data, mime, categories))
        return ReceiptResult(
            merchant="架空商店", date="2026-08-01", total=500,
            items=[ReceiptItem(name="商品", amount=500, major_category="食費", minor_category="食品")],
        )


def analysis(classification, amount=None, label=None):
    return MedicalReceiptScreening(
        MedicalReceiptAnalysis(classification, amount, label,
                               "high" if classification == "medical" else "none", (), "fixture"),
        "fixture", "fixture_reason",
    )


class Screener:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, data, mime):
        self.calls.append((data, mime))
        return self.result


def pipeline_for(result, *, duplicate=False):
    db = FakeDB(duplicate=duplicate)
    ai = FakeAI()
    screener = Screener(result)
    return ReceiptPipeline(db, ai, screener), db, ai, screener


def test_duplicate_runs_neither_screening_nor_gemini():
    pipeline, _, ai, screener = pipeline_for(analysis("medical", 3000), duplicate=True)
    assert pipeline.process_bytes(b"private", "application/pdf", "file-1") == {
        "status": "skipped", "reason": "already_imported"}
    assert screener.calls == []
    assert ai.calls == []


def test_medical_amount_uses_local_value_and_category_without_auto_expense():
    pipeline, db, ai, _ = pipeline_for(analysis("medical", 3000, "領収金額"))
    result = pipeline.process_bytes(b"private-pdf", "application/pdf", "new")
    assert result["status"] == "needs_review"
    assert result["receipt"]["total"] == 3000
    assert result["receipt"]["items"][0]["major_category"] == "医療・保険"
    assert result["receipt"]["items"][0]["minor_category"] == "病院"
    assert ai.calls == []
    assert "支出明細" not in db.rows
    assert db.rows["レシート"][0][3] == 3000
    assert db.rows["取込データ"][0][6] == 3000


@pytest.mark.parametrize("classification", ["medical", "suspected_medical", "unknown"])
def test_blocked_classifications_are_reviewed_without_gemini(classification):
    pipeline, db, ai, _ = pipeline_for(analysis(classification))
    result = pipeline.process_bytes(b"private", "application/pdf", "new")
    assert result["status"] == "needs_review"
    assert ai.calls == []
    assert "支出明細" not in db.rows
    assert db.rows["レシート"][0][3] == ""
    assert db.rows["取込データ"][0][6] == ""


@pytest.mark.parametrize("mime", ["image/jpeg", "image/png"])
def test_b2_images_are_unknown_and_never_sent_to_gemini(mime):
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"image-private", mime, "new")
    assert result["classification"] == "unknown"
    assert result["status"] == "needs_review"
    assert ai.calls == []


def _pdf(*, encrypted=False):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    if encrypted:
        writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [(_pdf(encrypted=True), "encrypted_pdf"), (_pdf(), "empty_pdf_text"),
     (b"not-a-pdf", "pdf_text_extraction_failed")],
)
def test_unreadable_pdfs_are_unknown_and_never_sent_to_gemini(payload, reason):
    screening = screen_medical_receipt(payload, "application/pdf")
    assert screening.analysis.classification == "unknown"
    assert screening.reason_code == reason
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(payload, "application/pdf", "new")
    assert result["status"] == "needs_review"
    assert ai.calls == []


def test_clear_non_medical_text_pdf_is_the_only_path_to_gemini(monkeypatch):
    class Page:
        def extract_text(self):
            return "領収書\n通常の商品\n合計 500円"

    class Reader:
        is_encrypted = False
        pages = [Page()]

        def __init__(self, stream):
            assert stream.read() == b"ordinary-receipt"

    monkeypatch.setattr("pypdf.PdfReader", Reader)
    payload = b"ordinary-receipt"
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(payload, "application/pdf", "new")
    assert result == {"status": "imported", "items": 1, "total": 500}
    assert ai.calls[0][0] is payload
    assert len(ai.calls) == 1
    assert len(db.rows["支出明細"]) == 1


def test_medical_bytes_are_not_passed_to_gemini_and_note_is_diagnostic_only():
    payload = b"patient-name-and-private-medical-text"
    pipeline, db, ai, _ = pipeline_for(analysis("medical", 3000, "領収金額"))
    pipeline.process_bytes(payload, "application/pdf", "new")
    assert ai.calls == []
    saved = " ".join(str(cell) for rows in db.rows.values() for row in rows for cell in row)
    assert "patient-name" not in saved
    assert "private-medical-text" not in saved
    assert "medical_local" in saved
    assert "classification=medical" in saved


def test_medical_missing_date_and_merchant_never_auto_posts():
    pipeline, db, _, _ = pipeline_for(analysis("medical", 3000, "領収金額"))
    result = pipeline.process_bytes(b"private", "application/pdf", "new")
    assert result["receipt"]["date"] == ""
    assert result["receipt"]["merchant"] == ""
    assert {"日付不明", "店舗不明"}.issubset(result["issues"])
    assert "支出明細" not in db.rows
