import io
import shutil

import pytest
from PIL import Image, ImageDraw

from app.medical_receipt_input import (
    MAX_PDF_OCR_PAGES,
    OCRExtraction,
    OCRToken,
    LocalOCRFailed,
    PartialOCRFailure,
    TesseractUnavailable,
    _analyze_ocr,
    _ocr_image,
    pdf_text_is_sufficient,
    screen_medical_receipt,
)
from app.models import ReceiptItem, ReceiptResult
from app.receipt_pipeline import ReceiptPipeline


def image_bytes(fmt):
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format=fmt)
    return output.getvalue()


def token(text, x, y, *, page=1, confidence=95, line=1, width=60):
    return OCRToken(text, page, x, y, width, 12, confidence, (page, 1, 1, line))


def medical_ocr(amount=3000):
    tokens = (
        token("領収書", 10, 10, line=1), token("患者", 10, 30, line=2),
        token("診療", 80, 30, line=2), token("病院", 140, 30, line=2),
        token("領収金額", 10, 50, line=3), token(f"{amount:,}円", 100, 50, line=3),
    )
    return OCRExtraction("領収書\n患者 診療 病院\n領収金額 %s円" % f"{amount:,}", tokens)


def confidence_fixture(*low_words, amount_confidence=95):
    normal = (
        token("領収書", 10, 10, confidence=95),
        token("架空商品", 10, 30, confidence=95, line=2),
        token("3,000円", 100, 50, confidence=amount_confidence, line=3),
    )
    privacy = normal + tuple(
        token(word, 10, 70 + index * 20, confidence=40, line=4 + index)
        for index, word in enumerate(low_words)
    )
    accepted = tuple(item for item in normal if item.confidence >= 60)
    text = "\n".join(item.text for item in accepted)
    return OCRExtraction(text, accepted, privacy)


def non_medical_ocr():
    return OCRExtraction(
        "領収書\n架空商品\n合計 500円",
        (token("領収書", 10, 10), token("架空商品", 10, 30, line=2),
         token("合計", 10, 50, line=3), token("500円", 100, 50, line=3)),
    )


class FakeDB:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate
        self.rows = {}

    def import_ids(self):
        return {"receipt:same"} if self.duplicate else set()

    def categories(self):
        return [("医療・保険", "病院"), ("食費", "食品")]

    def append(self, sheet, rows):
        self.rows.setdefault(sheet, []).extend(rows)

    def ensure_expense_status_column(self):
        pass


class FakeAI:
    def __init__(self):
        self.calls = []

    def analyze_receipt(self, data, mime, categories):
        self.calls.append((data, mime))
        return ReceiptResult(
            merchant="架空商店", date="2026-08-01", total=500,
            items=[ReceiptItem(name="商品", amount=500,
                               major_category="食費", minor_category="食品")],
        )


def patch_pdf_reader(monkeypatch, texts, *, encrypted=False):
    class Page:
        def __init__(self, text): self.text = text
        def extract_text(self): return self.text

    class Reader:
        def __init__(self, stream):
            self.is_encrypted = encrypted
            self.pages = [Page(text) for text in texts]

    monkeypatch.setattr("pypdf.PdfReader", Reader)


@pytest.mark.parametrize(("mime", "fmt"), [("image/jpeg", "JPEG"), ("image/png", "PNG")])
def test_image_ocr_medical_never_calls_gemini(monkeypatch, mime, fmt):
    monkeypatch.setattr("app.medical_receipt_input._ocr_image", lambda image: medical_ocr())
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(image_bytes(fmt), mime, "new")
    assert result["classification"] == "medical"
    assert result["receipt"]["total"] == 3000
    assert ai.calls == []


def test_image_ocr_clear_non_medical_calls_gemini_once(monkeypatch):
    monkeypatch.setattr("app.medical_receipt_input._ocr_image", lambda image: non_medical_ocr())
    payload = image_bytes("JPEG")
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(payload, "image/jpeg", "new")
    assert result["status"] == "imported"
    assert ai.calls == [(payload, "image/jpeg")]


@pytest.mark.parametrize("extracted", [medical_ocr(), non_medical_ocr()])
def test_scanned_pdf_falls_back_to_ocr(monkeypatch, extracted):
    patch_pdf_reader(monkeypatch, [""])
    calls = []
    monkeypatch.setattr(
        "app.medical_receipt_input._ocr_pdf",
        lambda data, pages: calls.append((data, pages)) or extracted,
    )
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"scanned", "application/pdf", "new")
    assert calls == [(b"scanned", 1)]
    assert len(ai.calls) == (1 if "架空商品" in extracted.text else 0)
    assert result["status"] in {"imported", "needs_review"}


def test_sufficient_text_pdf_does_not_call_ocr(monkeypatch):
    patch_pdf_reader(monkeypatch, ["領収書 通常商品 合計 500円"])
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda *args: pytest.fail("OCR must not run"))
    assert screen_medical_receipt(b"text-pdf", "application/pdf").analysis.classification == "non_medical"


def test_short_unusable_pdf_text_calls_ocr(monkeypatch):
    patch_pdf_reader(monkeypatch, ["abc"])
    calls = []
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda data, pages: calls.append(pages) or medical_ocr())
    result = screen_medical_receipt(b"short", "application/pdf")
    assert calls == [1]
    assert result.extraction == "pdf_ocr"


def test_encrypted_pdf_calls_neither_ocr_nor_gemini(monkeypatch):
    patch_pdf_reader(monkeypatch, [""], encrypted=True)
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda *args: pytest.fail("OCR must not run"))
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"encrypted", "application/pdf", "new")
    assert result["issues"] == ["encrypted_pdf"]
    assert ai.calls == []


def test_tesseract_unavailable_is_unknown_review(monkeypatch):
    def unavailable(image):
        raise TesseractUnavailable
    monkeypatch.setattr("app.medical_receipt_input._ocr_image", unavailable)
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(image_bytes("PNG"), "image/png", "new")
    assert result["classification"] == "unknown"
    assert result["issues"] == ["tesseract_unavailable"]
    assert ai.calls == []


def test_pdf_tesseract_unavailable_is_unknown(monkeypatch):
    patch_pdf_reader(monkeypatch, [""])
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda data, pages: (_ for _ in ()).throw(TesseractUnavailable()))
    result = screen_medical_receipt(b"scan", "application/pdf")
    assert (result.analysis.classification, result.reason_code) == (
        "unknown", "tesseract_unavailable")


def test_pdf_ocr_failure_is_unknown(monkeypatch):
    patch_pdf_reader(monkeypatch, [""])
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda data, pages: (_ for _ in ()).throw(LocalOCRFailed()))
    result = screen_medical_receipt(b"scan", "application/pdf")
    assert (result.analysis.classification, result.reason_code) == ("unknown", "ocr_failed")


def test_image_decode_failure_is_unknown():
    result = screen_medical_receipt(b"broken", "image/jpeg")
    assert (result.analysis.classification, result.reason_code) == ("unknown", "image_decode_failed")


def test_empty_ocr_is_unknown(monkeypatch):
    monkeypatch.setattr("app.medical_receipt_input._ocr_image",
                        lambda image: OCRExtraction("", ()))
    result = screen_medical_receipt(image_bytes("PNG"), "image/png")
    assert (result.analysis.classification, result.reason_code) == ("unknown", "ocr_empty")


def test_low_confidence_amount_is_not_confirmed():
    extracted = medical_ocr()
    low = tuple(OCRToken(t.text, t.page, t.x, t.y, t.width, t.height,
                         65 if "円" in t.text else t.confidence, t.line_key)
                for t in extracted.tokens)
    result = _analyze_ocr(OCRExtraction(extracted.text, low), "image_ocr")
    assert result.analysis.classification == "medical"
    assert result.analysis.amount is None


def test_ocr_discards_empty_and_low_confidence_tokens(monkeypatch):
    data = {
        "text": ["", "患者", "領収書"], "conf": [95, 20, 90],
        "left": [0, 10, 20], "top": [0, 10, 20], "width": [1, 20, 30],
        "height": [1, 10, 10], "block_num": [0, 1, 1], "par_num": [0, 1, 1],
        "line_num": [0, 1, 2],
    }
    monkeypatch.setattr("pytesseract.image_to_data", lambda *args, **kwargs: data)
    extracted = _ocr_image(Image.new("RGB", (20, 20), "white"))
    assert [item.text for item in extracted.tokens] == ["領収書"]
    assert [item.text for item in extracted.privacy_tokens] == ["領収書"]


@pytest.mark.parametrize("medical_word", ["診療報酬", "一部負担金"])
def test_low_confidence_strong_medical_evidence_blocks_gemini(monkeypatch, medical_word):
    monkeypatch.setattr("app.medical_receipt_input._ocr_image",
                        lambda image: confidence_fixture(medical_word))
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(
        image_bytes("PNG"), "image/png", "new",
    )
    assert result["classification"] == "suspected_medical"
    assert ai.calls == []


@pytest.mark.parametrize("medical_word", ["病院", "薬局"])
def test_single_low_confidence_weak_word_is_not_suspected(medical_word):
    normal = (token("架空商品", 10, 10), token("合計", 10, 30, line=2),
              token("3,000円", 100, 30, line=2))
    weak = token(medical_word, 10, 50, confidence=40, line=3)
    result = _analyze_ocr(
        OCRExtraction("架空商品\n合計 3,000円", normal, normal + (weak,)), "image_ocr",
    )
    assert result.analysis.classification == "non_medical"


def test_two_low_confidence_medical_groups_are_suspected():
    normal = (token("架空商品", 10, 10), token("合計", 10, 30, line=2),
              token("3,000円", 100, 30, line=2))
    privacy = normal + (token("患者", 10, 50, confidence=40, line=3),
                        token("病院", 10, 70, confidence=40, line=4))
    result = _analyze_ocr(
        OCRExtraction("架空商品\n合計 3,000円", normal, privacy), "image_ocr",
    )
    assert result.analysis.classification == "suspected_medical"


def test_receipt_without_medical_evidence_stays_non_medical():
    result = _analyze_ocr(confidence_fixture(), "image_ocr")
    assert result.analysis.classification == "non_medical"


def test_receipt_only_with_insufficient_ocr_quality_is_unknown():
    receipt = token("領収書", 10, 10)
    result = _analyze_ocr(OCRExtraction("領収書", (receipt,), (receipt,)), "image_ocr")
    assert result.analysis.classification == "unknown"


def test_overall_low_ocr_quality_is_unknown():
    privacy = (token("領収書", 10, 10, confidence=40),
               token("3,000円", 100, 10, confidence=40))
    result = _analyze_ocr(OCRExtraction("", (), privacy), "image_ocr")
    assert result.analysis.classification == "unknown"


def test_low_confidence_amount_is_not_accepted_even_with_medical_evidence():
    privacy = (
        token("領収書", 10, 10), token("患者", 10, 30, line=2),
        token("病院", 80, 30, line=2), token("診療", 140, 30, line=2),
        token("領収金額", 10, 50, line=3),
        token("3,000円", 120, 50, confidence=50, line=3),
    )
    normal = tuple(item for item in privacy if item.confidence >= 60)
    result = _analyze_ocr(
        OCRExtraction("領収書\n患者 病院 診療\n領収金額", normal, privacy), "image_ocr",
    )
    assert result.analysis.classification in {"medical", "suspected_medical"}
    assert result.analysis.amount is None


def test_positioned_ocr_confirms_payment_amount():
    assert _analyze_ocr(medical_ocr(), "image_ocr").analysis.amount == 3000


@pytest.mark.parametrize(
    ("text", "tokens", "expected"),
    [
        ("領収書 患者 診療 病院\n総医療費 10,000円\n自己負担額 3,000円",
         (token("総医療費", 10, 50, line=3), token("10,000円", 120, 50, line=3),
          token("自己負担額", 10, 70, line=4), token("3,000円", 120, 70, line=4)), 3000),
        ("領収書 患者 診療 病院\n預り金 5,000円\nおつり 2,000円\n領収金額 3,000円",
         (token("預り金", 10, 50, line=3), token("5,000円", 120, 50, line=3),
          token("おつり", 10, 70, line=4), token("2,000円", 120, 70, line=4),
          token("領収金額", 10, 90, line=5), token("3,000円", 120, 90, line=5)), 3000),
    ],
)
def test_positioned_ocr_excludes_non_payment_values(text, tokens, expected):
    prefix = (token("領収書", 10, 10), token("患者", 10, 30, line=2),
              token("診療", 80, 30, line=2), token("病院", 140, 30, line=2))
    assert _analyze_ocr(OCRExtraction(text, prefix + tokens), "image_ocr").analysis.amount == expected


def test_positioned_competing_amounts_are_ambiguous():
    extracted = medical_ocr()
    tokens = extracted.tokens + (token("4,000円", 180, 50, line=3),)
    assert _analyze_ocr(OCRExtraction(extracted.text + " 4,000円", tokens), "image_ocr").analysis.amount is None


def test_positioned_amount_on_other_page_is_not_paired():
    extracted = medical_ocr()
    tokens = tuple(t for t in extracted.tokens if "円" not in t.text) + (
        token("3,000円", 100, 50, page=2, line=3),)
    assert _analyze_ocr(OCRExtraction(extracted.text, tokens), "pdf_ocr").analysis.amount is None


def test_duplicate_runs_neither_ocr_nor_gemini(monkeypatch):
    monkeypatch.setattr("app.medical_receipt_input._ocr_image",
                        lambda image: pytest.fail("OCR must not run"))
    db, ai = FakeDB(duplicate=True), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"private", "image/jpeg", "same")
    assert result["status"] == "skipped"
    assert ai.calls == []


def test_unknown_bytes_and_ocr_text_are_not_sent_or_saved(monkeypatch):
    private = "架空患者秘密本文"
    monkeypatch.setattr("app.medical_receipt_input._ocr_image",
                        lambda image: OCRExtraction(private, ()))
    payload = image_bytes("PNG")
    db, ai = FakeDB(), FakeAI()
    ReceiptPipeline(db, ai).process_bytes(payload, "image/png", "new")
    saved = " ".join(str(cell) for rows in db.rows.values() for row in rows for cell in row)
    assert ai.calls == []
    assert private not in saved


def test_heic_is_unsupported_and_never_sent_to_gemini():
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"heic-private", "image/heic", "new")
    assert result["issues"] == ["unsupported_image_format"]
    assert ai.calls == []


def test_pdf_over_page_limit_is_reviewed_without_ocr_or_gemini(monkeypatch):
    patch_pdf_reader(monkeypatch, [""] * (MAX_PDF_OCR_PAGES + 1))
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda *args: pytest.fail("OCR must not run"))
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"large", "application/pdf", "new")
    assert result["issues"] == ["too_many_pages_for_ocr"]
    assert ai.calls == []


def test_sufficient_six_page_pdf_is_screened_without_ocr(monkeypatch):
    patch_pdf_reader(monkeypatch, ["領収書 架空商品 合計 500円"] * 6)
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda *args: pytest.fail("OCR must not run"))
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"six-text-pages", "application/pdf", "new")
    assert result["status"] == "imported"
    assert len(ai.calls) == 1


def test_partial_multi_page_ocr_failure_is_reviewed(monkeypatch):
    patch_pdf_reader(monkeypatch, ["", ""])
    monkeypatch.setattr(
        "app.medical_receipt_input._ocr_pdf",
        lambda *args: (_ for _ in ()).throw(PartialOCRFailure()),
    )
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"two-scans", "application/pdf", "new")
    assert result["issues"] == ["partial_ocr_failure"]
    assert ai.calls == []


def test_text_pdf_medical_end_to_end(monkeypatch):
    patch_pdf_reader(monkeypatch, [
        "領収書\nテスト診療所\n診療報酬\n一部負担金\n領収金額 3,000円",
    ])
    monkeypatch.setattr("app.medical_receipt_input._ocr_pdf",
                        lambda *args: pytest.fail("OCR must not run"))
    db, ai = FakeDB(), FakeAI()
    result = ReceiptPipeline(db, ai).process_bytes(b"fictional-text-pdf", "application/pdf", "new")
    assert result["classification"] == "medical"
    assert result["receipt"]["total"] == 3000
    assert result["receipt"]["merchant"] == ""
    assert result["receipt"]["date"] == ""
    assert ai.calls == []
    assert "支出明細" not in db.rows


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="local Tesseract unavailable")
def test_synthetic_local_tesseract_smoke():
    import pytesseract

    if "jpn" not in pytesseract.get_languages(config=""):
        pytest.skip("Japanese traineddata unavailable")
    image = Image.new("RGB", (600, 120), "white")
    ImageDraw.Draw(image).text((20, 40), "RECEIPT TEST 1280 YEN", fill="black")
    extracted = _ocr_image(image)
    assert isinstance(extracted, OCRExtraction)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("abc", False), ("領収書 合計 500円", True), ("A" * 30, True)],
)
def test_pdf_text_quality_uses_structure_and_meaningful_characters(text, expected):
    assert pdf_text_is_sufficient(text) is expected
