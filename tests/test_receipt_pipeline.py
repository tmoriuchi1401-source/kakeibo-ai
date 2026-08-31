from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app import receipt_pipeline as pipeline_module
from app.models import ReceiptItem, ReceiptResult
from app.receipt_privacy_gate import ReceiptPrivacyGateResult


class FakeDB:
    def __init__(self, import_ids=()):
        self._import_ids = set(import_ids)
        self.category_calls = 0
        self.append_calls = []
        self.ensure_expense_status_column_calls = 0

    def import_ids(self):
        return self._import_ids

    def categories(self):
        self.category_calls += 1
        return [("食費", "食品")]

    def append(self, sheet, rows):
        self.append_calls.append((sheet, rows))

    def ensure_expense_status_column(self):
        self.ensure_expense_status_column_calls += 1


class FakeAI:
    def __init__(self, result=None):
        self.analyze_receipt = Mock(return_value=result)


def _normal_gate() -> ReceiptPrivacyGateResult:
    return ReceiptPrivacyGateResult(
        classification="normal",
        extraction_status="extracted",
        extraction_method="image_ocr",
        text_present=True,
        status="ready_for_gemini",
        reason_code="normal_receipt_evidence",
    )


def _medical_gate(status="confirmed") -> ReceiptPrivacyGateResult:
    confirmed = status == "confirmed"
    return ReceiptPrivacyGateResult(
        classification="medical",
        extraction_status="extracted",
        extraction_method="pdf_ocr",
        text_present=True,
        status=status,
        reason_code="unique_strong_candidate" if confirmed else "conflicting_candidates",
        medical_payment_amount=3250 if confirmed else None,
        medical_candidate_count=1 if confirmed else 2,
        category="医療費",
    )


def _payroll_gate() -> ReceiptPrivacyGateResult:
    return ReceiptPrivacyGateResult(
        classification="payroll",
        extraction_status="extracted",
        extraction_method="pdf_ocr",
        text_present=True,
        status="blocked",
        reason_code="payroll_strong_signal",
    )


def _sensitive_gate(extraction_status="extraction_failed") -> ReceiptPrivacyGateResult:
    return ReceiptPrivacyGateResult(
        classification="sensitive_unknown",
        extraction_status=extraction_status,
        extraction_method="pdf_ocr" if extraction_status != "unsupported_mime_type" else "none",
        text_present=False,
        status="blocked",
        reason_code="ocr_or_text_extraction_failed",
    )


def _normal_receipt_result() -> ReceiptResult:
    return ReceiptResult(
        merchant="テスト商店",
        date="2026-01-02",
        total=100,
        payment_method="現金",
        items=[
            ReceiptItem(
                name="商品",
                amount=100,
                major_category="食費",
                minor_category="食品",
            )
        ],
    )


def test_normal_gate_runs_existing_receipt_pipeline_once(monkeypatch):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    gate = Mock(return_value=_normal_gate())
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy", gate)

    result = pipeline_module.ReceiptPipeline(db, ai).process_bytes(
        b"normal receipt", "image/png", "source-1", "https://example.invalid/receipt"
    )

    gate.assert_called_once_with(b"normal receipt", "image/png")
    assert db.category_calls == 1
    ai.analyze_receipt.assert_called_once_with(
        b"normal receipt", "image/png", [("食費", "食品")]
    )
    assert [sheet for sheet, _ in db.append_calls] == ["レシート", "取込データ", "支出明細"]
    assert db.ensure_expense_status_column_calls == 1
    assert result == {"status": "imported", "items": 1, "total": 100}


@pytest.mark.parametrize(
    "gate_result, expected_classification, expected_amount",
    [
        (_medical_gate("confirmed"), "medical", 3250),
        (_medical_gate("needs_review"), "medical", None),
        (_payroll_gate(), "payroll", None),
        (_sensitive_gate(), "sensitive_unknown", None),
        (_sensitive_gate("pdf_ocr_empty"), "sensitive_unknown", None),
    ],
)
def test_non_normal_gate_never_calls_gemini_or_sheets(
    monkeypatch, gate_result, expected_classification, expected_amount
):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    gate = Mock(return_value=gate_result)
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy", gate)

    result = pipeline_module.ReceiptPipeline(db, ai).process_bytes(
        b"SYNTHETIC_PRIVATE_OCR", "application/pdf", "source-2"
    )

    gate.assert_called_once_with(b"SYNTHETIC_PRIVATE_OCR", "application/pdf")
    ai.analyze_receipt.assert_not_called()
    assert db.category_calls == 0
    assert db.append_calls == []
    assert db.ensure_expense_status_column_calls == 0
    assert result["status"] == "privacy_blocked"
    assert result["classification"] == expected_classification
    assert result["gemini_allowed"] is False
    assert result["medical_payment_amount"] == expected_amount


def test_blocked_result_never_contains_synthetic_private_text(monkeypatch):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy", lambda content, mime: _medical_gate())

    result = pipeline_module.ReceiptPipeline(db, ai).process_bytes(
        "山田太郎 患者番号ABC123 保険者番号99999999 胃炎".encode(),
        "application/pdf",
        "private-source",
    )

    exposed = " ".join((repr(result), str(result), json.dumps(result, ensure_ascii=False)))
    for marker in ("山田太郎", "患者番号ABC123", "保険者番号99999999", "胃炎"):
        assert marker not in exposed


def test_duplicate_skips_privacy_gate_and_preserves_existing_result(monkeypatch):
    db = FakeDB({"receipt:duplicate-source"})
    ai = FakeAI(_normal_receipt_result())
    gate = Mock()
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy", gate)

    result = pipeline_module.ReceiptPipeline(db, ai).process_bytes(
        b"duplicate", "image/png", "duplicate-source"
    )

    assert result == {"status": "skipped", "reason": "already_imported"}
    gate.assert_not_called()
    ai.analyze_receipt.assert_not_called()
    assert db.category_calls == 0
    assert db.append_calls == []


def test_unexpected_gate_failure_stops_before_gemini_or_sheets(monkeypatch):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    gate = Mock(side_effect=RuntimeError("synthetic gate failure"))
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy", gate)

    with pytest.raises(RuntimeError, match="synthetic gate failure"):
        pipeline_module.ReceiptPipeline(db, ai).process_bytes(b"synthetic", "image/png", "source-3")

    ai.analyze_receipt.assert_not_called()
    assert db.category_calls == 0
    assert db.append_calls == []
