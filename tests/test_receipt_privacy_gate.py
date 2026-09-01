from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from app import receipt_privacy_gate as gate
from app.medical_receipt_privacy import _StructuredOcrToken
from app.receipt_text_extraction import _ReceiptTextExtraction


def _extracted(text: str, method: str = "image_ocr") -> _ReceiptTextExtraction:
    return _ReceiptTextExtraction("extracted", method, text)


def _failed(status: str = "extraction_failed") -> _ReceiptTextExtraction:
    return _ReceiptTextExtraction(status, "image_ocr", None)


def _gate_for(monkeypatch, text: str):
    monkeypatch.setattr(gate, "_extract_receipt_text", lambda content, mime_type: _extracted(text))
    return gate.evaluate_receipt_privacy(b"synthetic-document", "image/png")


def _scan_pdf_gate_for(monkeypatch, text: str):
    monkeypatch.setattr(
        gate,
        "_extract_receipt_text",
        lambda content, mime_type: _extracted(text, "pdf_ocr"),
    )
    return gate.evaluate_receipt_privacy(b"synthetic-scan-pdf", "application/pdf")


def _ocr_token(text: str, x: float) -> _StructuredOcrToken:
    return _StructuredOcrToken(text, 1, x, 20, 50, 12, 96, (1, 1, 1, 5))


def test_normal_text_is_the_only_gemini_allowed_result(monkeypatch):
    result = _gate_for(monkeypatch, "レシート 商品 小計 1000円 現金 1000円")

    assert result.classification == "normal"
    assert result.status == "ready_for_gemini"
    assert result.gemini_allowed is True
    assert result.medical_payment_amount is None
    assert result.category is None


def test_medical_text_stays_local_and_extracts_payment(monkeypatch):
    result = _gate_for(monkeypatch, "診療 領収書 患者負担額 3,250円")

    assert result.classification == "medical"
    assert result.status == "confirmed"
    assert result.gemini_allowed is False
    assert result.medical_payment_amount == 3250
    assert result.medical_candidate_count == 1
    assert result.category == "医療費"


def test_scan_pdf_ocr_medical_text_stays_local_and_extracts_payment(monkeypatch):
    result = _scan_pdf_gate_for(monkeypatch, "診療 領収書 患者負担額 3,250円")

    assert result.classification == "medical"
    assert result.extraction_method == "pdf_ocr"
    assert result.status == "confirmed"
    assert result.gemini_allowed is False
    assert result.medical_payment_amount == 3250


def test_structured_ocr_stays_private_and_can_confirm_a_local_payment(monkeypatch):
    tokens = (_ocr_token("支額", 10), _ocr_token("3,250円", 100))
    monkeypatch.setattr(
        gate,
        "_extract_receipt_text",
        lambda content, mime_type: _ReceiptTextExtraction(
            "extracted", "image_ocr", "病院 診療", tokens
        ),
    )

    result = gate.evaluate_receipt_privacy(b"synthetic-document", "image/png")

    assert result.classification == "medical"
    assert result.status == "confirmed"
    assert result.medical_payment_amount == 3250
    assert set(result.model_dump()) == {
        "classification",
        "extraction_status",
        "extraction_method",
        "text_present",
        "status",
        "reason_code",
        "medical_payment_amount",
        "medical_candidate_count",
        "category",
        "gemini_allowed",
    }


def test_payroll_text_is_blocked(monkeypatch):
    result = _gate_for(monkeypatch, "給与明細 基本給 控除合計 差引支給額")

    assert result.classification == "payroll"
    assert result.status == "blocked"
    assert result.gemini_allowed is False


def test_scan_pdf_ocr_normal_text_is_gemini_allowed(monkeypatch):
    result = _scan_pdf_gate_for(monkeypatch, "レシート 商品 小計 1,000円 現金 1,000円")

    assert result.classification == "normal"
    assert result.extraction_method == "pdf_ocr"
    assert result.status == "ready_for_gemini"
    assert result.gemini_allowed is True


@pytest.mark.parametrize(
    "text, classification",
    [
        ("給与明細 基本給 控除合計 差引支給額", "payroll"),
        ("診療 給与", "sensitive_unknown"),
    ],
)
def test_scan_pdf_ocr_sensitive_text_is_blocked(monkeypatch, text, classification):
    result = _scan_pdf_gate_for(monkeypatch, text)

    assert result.classification == classification
    assert result.extraction_method == "pdf_ocr"
    assert result.status == "blocked"
    assert result.gemini_allowed is False


def test_scan_pdf_ocr_failure_is_sensitive_unknown_and_blocked(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_extract_receipt_text",
        lambda content, mime_type: _ReceiptTextExtraction("pdf_ocr_failed", "pdf_ocr", None),
    )

    result = gate.evaluate_receipt_privacy(b"synthetic-scan-pdf", "application/pdf")

    assert result.classification == "sensitive_unknown"
    assert result.extraction_status == "pdf_ocr_failed"
    assert result.extraction_method == "pdf_ocr"
    assert result.status == "blocked"
    assert result.gemini_allowed is False


@pytest.mark.parametrize("text", ["", "  \n\t", "領収書"])
def test_insufficient_text_is_blocked(monkeypatch, text):
    result = _gate_for(monkeypatch, text)

    assert result.classification == "sensitive_unknown"
    assert result.status == "blocked"
    assert result.gemini_allowed is False


def test_extraction_failure_is_sensitive_unknown_without_exception_detail(monkeypatch):
    monkeypatch.setattr(gate, "_extract_receipt_text", lambda content, mime_type: _failed())

    result = gate.evaluate_receipt_privacy(b"synthetic-document", "image/png")

    assert result.classification == "sensitive_unknown"
    assert result.extraction_status == "extraction_failed"
    assert result.gemini_allowed is False
    assert "synthetic-document" not in repr(result)


def test_unsupported_mime_is_never_normal():
    result = gate.evaluate_receipt_privacy(b"synthetic-document", "application/zip")

    assert result.classification == "sensitive_unknown"
    assert result.extraction_status == "unsupported_mime_type"
    assert result.gemini_allowed is False


def test_corrupt_pdf_gate_does_not_emit_input_marker(capfd, caplog):
    marker = "PII42"
    with caplog.at_level(logging.DEBUG):
        result = gate.evaluate_receipt_privacy(
            f"{marker} malformed pdf".encode(), "application/pdf"
        )

    captured = capfd.readouterr()
    logging_text = " ".join(record.getMessage() for record in caplog.records)
    assert marker not in captured.out
    assert marker not in captured.err
    assert marker not in logging_text
    assert result.classification == "sensitive_unknown"
    assert result.gemini_allowed is False
    assert result.extraction_status == "extraction_failed"


def test_medical_amount_ambiguity_remains_needs_review(monkeypatch):
    result = _gate_for(monkeypatch, "診療 患者負担額 5000円\nご請求額 7000円")

    assert result.classification == "medical"
    assert result.status == "needs_review"
    assert result.medical_payment_amount is None
    assert result.medical_candidate_count == 2
    assert result.gemini_allowed is False


def test_medical_same_amount_candidates_remain_confirmed(monkeypatch):
    result = _gate_for(monkeypatch, "診療 患者 領収金額 5000円\nご請求額 5000円")

    assert result.classification == "medical"
    assert result.status == "confirmed"
    assert result.medical_payment_amount == 5000
    assert result.gemini_allowed is False


def test_gate_result_never_retains_synthetic_sensitive_text(monkeypatch):
    sensitive = "山田太郎 患者番号ABC123 保険者番号99999999 胃炎 テスト病院 診療 患者負担額 3250円"
    result = _gate_for(monkeypatch, sensitive)

    exposed = "\n".join((repr(result), str(result.model_dump()), result.model_dump_json()))
    json.loads(result.model_dump_json())
    for forbidden in ("山田太郎", "患者番号ABC123", "保険者番号99999999", "胃炎", "テスト病院"):
        assert forbidden not in exposed
    for field_name in ("raw_text", "extracted_text", "snippet", "matched_line", "patient_name"):
        assert field_name not in type(result).model_fields


def test_scan_pdf_gate_result_never_retains_synthetic_sensitive_text(monkeypatch):
    sensitive = "山田太郎 患者番号ABC123 保険者番号99999999 胃炎 テスト病院 診療 患者負担額 3250円"
    result = _scan_pdf_gate_for(monkeypatch, sensitive)

    exposed = "\n".join((repr(result), str(result.model_dump()), result.model_dump_json()))
    for forbidden in ("山田太郎", "患者番号ABC123", "保険者番号99999999", "胃炎", "テスト病院"):
        assert forbidden not in exposed
    assert result.classification == "medical"
    assert result.extraction_method == "pdf_ocr"
    assert result.gemini_allowed is False


def test_gate_result_rejects_unsafe_update_copy(monkeypatch):
    result = _gate_for(monkeypatch, "診療 領収書 患者負担額 3250円")

    with pytest.raises(gate.SafeModelValidationError):
        result.model_copy(update={"classification": "normal"})


def test_gate_safe_validate_hides_sensitive_invalid_input():
    sensitive = {"classification": "normal", "raw_text": "山田太郎 患者番号ABC123"}

    with pytest.raises(gate.SafeModelValidationError) as caught:
        gate.ReceiptPrivacyGateResult.safe_validate(sensitive)

    exposed = f"{caught.value!r} {caught.value!s} {vars(caught.value)}"
    assert "山田太郎" not in exposed
    assert "患者番号ABC123" not in exposed


def _valid_normal_payload():
    return {
        "classification": "normal",
        "extraction_status": "extracted",
        "extraction_method": "image_ocr",
        "text_present": True,
        "status": "ready_for_gemini",
        "reason_code": "normal_receipt_evidence",
        "medical_payment_amount": None,
        "medical_candidate_count": 0,
        "category": None,
    }


@pytest.mark.parametrize(
    "updates",
    [
        {"extraction_status": "extraction_failed", "text_present": False},
        {"extraction_status": "unsupported_mime_type", "extraction_method": "none",
         "text_present": False},
        {"text_present": False},
        {"status": "blocked"},
    ],
)
def test_normal_gate_constructor_rejects_extraction_contradictions(updates):
    payload = {**_valid_normal_payload(), **updates}

    with pytest.raises(ValidationError):
        gate.ReceiptPrivacyGateResult(**payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"extraction_status": "extraction_failed", "text_present": False},
        {"extraction_status": "unsupported_mime_type", "extraction_method": "none",
         "text_present": False},
        {"text_present": False},
        {"status": "blocked"},
    ],
)
def test_normal_gate_safe_validate_rejects_extraction_contradictions(updates):
    payload = {**_valid_normal_payload(), **updates}

    with pytest.raises(gate.SafeModelValidationError):
        gate.ReceiptPrivacyGateResult.safe_validate(payload)


def test_valid_normal_gate_state_is_accepted():
    result = gate.ReceiptPrivacyGateResult(**_valid_normal_payload())

    assert result.classification == "normal"
    assert result.extraction_status == "extracted"
    assert result.text_present is True
    assert result.gemini_allowed is True


def test_gate_low_level_exception_message_is_not_exposed(monkeypatch, capfd, caplog):
    marker = "SYNTHETIC_PRIVATE_MARKER"

    def fail(content):
        raise RuntimeError(marker)

    monkeypatch.setattr(
        "app.receipt_text_extraction._extract_pdf_embedded_text", fail
    )
    with caplog.at_level(logging.DEBUG):
        result = gate.evaluate_receipt_privacy(b"synthetic", "application/pdf")

    captured = capfd.readouterr()
    exposed = " ".join(
        (repr(result), str(result.model_dump()), result.model_dump_json(), captured.out,
         captured.err, " ".join(record.getMessage() for record in caplog.records))
    )
    assert marker not in exposed
    assert result.classification == "sensitive_unknown"
    assert result.gemini_allowed is False
