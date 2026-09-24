from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from app import receipt_pipeline as pipeline_module
from app.medical_inbox_handoff_shadow import MedicalInboxHandoffShadow
from app.models import ReceiptItem, ReceiptResult
from app.receipt_privacy_gate import ReceiptPrivacyGateResult
from app.bank_income import INCOME_HEADERS, INCOME_SHEET, income_id, monthly_income


class FakeDB:
    def __init__(self, import_ids=(), *, fail_after_commit=None):
        self._import_ids = set(import_ids)
        self._receipt_ids = set()
        self._expense_ids = set()
        self.income_rows = []
        self.category_calls = 0
        self.append_calls = []
        self.ensure_expense_status_column_calls = 0
        self.fail_after_commit = fail_after_commit
        self._failure_raised = False

    def import_ids(self):
        return self._import_ids

    def categories(self):
        self.category_calls += 1
        return [("食費", "食品")]

    def append(self, sheet, rows):
        if not rows:
            return
        self.append_calls.append((sheet, rows))
        ids = {row[0] for row in rows}
        if sheet == "レシート":
            self._receipt_ids.update(ids)
        elif sheet == "取込データ":
            self._import_ids.update(ids)
        elif sheet == "支出明細":
            self._expense_ids.update(ids)
        if self.fail_after_commit == sheet and not self._failure_raised:
            self._failure_raised = True
            raise RuntimeError(f"synthetic {sheet} response failure")

    def receipt_ids(self):
        return self._receipt_ids

    def expense_index(self):
        return {value: index for index, value in enumerate(self._expense_ids, 2)}

    def ensure_expense_status_column(self):
        self.ensure_expense_status_column_calls += 1

    def sheet_titles(self):
        return [INCOME_SHEET]

    def get(self, range_name):
        if range_name == f"{INCOME_SHEET}!A1:J1":
            return [INCOME_HEADERS]
        assert range_name == f"{INCOME_SHEET}!A2:J"
        return [row.copy() for row in self.income_rows]

    def append_raw(self, sheet, rows):
        assert sheet == INCOME_SHEET
        self.income_rows.extend(row.copy() for row in rows)
        self.append_calls.append((sheet, rows))
        if self.fail_after_commit == sheet and not self._failure_raised:
            self._failure_raised = True
            raise RuntimeError("synthetic income response failure")


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
    assert [sheet for sheet, _ in db.append_calls] == ["レシート", "支出明細", "取込データ"]
    assert db.ensure_expense_status_column_calls == 1
    assert result == {"status": "imported", "items": 1, "total": 100}


def _buyback_result(kind="buyback"):
    return ReceiptResult(
        merchant="合成リユース店", date="2026-08-21", total=155,
        transaction_kind=kind, payment_method="現金",
        items=[ReceiptItem(name="中古品A", amount=5, major_category="食費", minor_category="食品"),
               ReceiptItem(name="中古品B", amount=100, major_category="食費", minor_category="食品"),
               ReceiptItem(name="中古品C", amount=50, major_category="食費", minor_category="食品")],
    )


def test_explicit_buyback_posts_one_cash_income_and_no_expense(monkeypatch):
    db = FakeDB()
    gate = _normal_gate().model_dump(exclude={"gemini_allowed"})
    gate["buyback_evidence"] = True
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy",
                        Mock(return_value=ReceiptPrivacyGateResult(**gate)))
    result = pipeline_module.ReceiptPipeline(db, FakeAI(_buyback_result())).process_bytes(
        b"synthetic buyback", "image/png", "source-buyback")
    assert result == {"status": "imported", "kind": "buyback", "items": 3, "total": 155}
    assert [sheet for sheet, _ in db.append_calls] == ["レシート", INCOME_SHEET, "取込データ"]
    assert db.ensure_expense_status_column_calls == 0
    assert db.income_rows[0][:8] == [income_id("receipt:source-buyback"), "2026-08-21", 155,
                                   "その他確認済収入", "合成リユース店", "", "receipt:source-buyback", "receipt_buyback"]
    assert monthly_income(db.income_rows) == {"2026-08": 155}
    assert pipeline_module.ReceiptPipeline(db, FakeAI(_buyback_result())).process_bytes(
        b"synthetic buyback", "image/png", "source-buyback") == {
            "status": "skipped", "reason": "already_imported"}


@pytest.mark.parametrize("kind,evidence", [
    ("purchase", True), ("buyback", False), ("unknown", True),
])
def test_disputed_receipt_kind_never_posts_expense_or_income(monkeypatch, kind, evidence):
    gate = _normal_gate().model_dump(exclude={"gemini_allowed"})
    gate["buyback_evidence"] = evidence
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy",
                        Mock(return_value=ReceiptPrivacyGateResult(**gate)))
    db = FakeDB()
    result = pipeline_module.ReceiptPipeline(db, FakeAI(_buyback_result(kind))).process_bytes(
        b"synthetic disputed", "image/png", "source-disputed")
    assert result["status"] == "needs_review"
    assert [sheet for sheet, _ in db.append_calls] == ["レシート", "取込データ"]


def test_buyback_replay_after_uncertain_income_write_does_not_duplicate(monkeypatch):
    gate = _normal_gate().model_dump(exclude={"gemini_allowed"})
    gate["buyback_evidence"] = True
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy",
                        Mock(return_value=ReceiptPrivacyGateResult(**gate)))
    db = FakeDB(fail_after_commit=INCOME_SHEET)
    pipeline = pipeline_module.ReceiptPipeline(db, FakeAI(_buyback_result()))
    with pytest.raises(RuntimeError, match="synthetic income response failure"):
        pipeline.process_bytes(b"synthetic", "image/png", "source-buyback")
    assert len(db.income_rows) == 1
    assert pipeline.process_bytes(b"synthetic", "image/png", "source-buyback")["status"] == "imported"
    assert len(db.income_rows) == 1


def test_buyback_requires_existing_income_schema_before_any_write(monkeypatch):
    gate = _normal_gate().model_dump(exclude={"gemini_allowed"})
    gate["buyback_evidence"] = True
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy",
                        Mock(return_value=ReceiptPrivacyGateResult(**gate)))
    db = FakeDB()
    db.sheet_titles = lambda: []
    with pytest.raises(RuntimeError, match="receipt_income_sheet_missing"):
        pipeline_module.ReceiptPipeline(db, FakeAI(_buyback_result())).process_bytes(
            b"synthetic", "image/png", "source-buyback")
    assert db.append_calls == []


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


def test_medical_gate_does_not_resolve_lazy_gemini(monkeypatch):
    db = FakeDB()
    gemini_factory = Mock(side_effect=AssertionError("Gemini must remain unused"))
    monkeypatch.setattr(
        pipeline_module, "evaluate_receipt_privacy", Mock(return_value=_medical_gate())
    )

    result = pipeline_module.ReceiptPipeline(
        db, None, gemini_factory=gemini_factory
    ).process_bytes(b"private medical bytes", "image/png", "medical-source")

    assert result["status"] == "privacy_blocked"
    assert result["gemini_allowed"] is False
    gemini_factory.assert_not_called()
    assert db.category_calls == 0
    assert db.append_calls == []


def test_normal_gate_without_gemini_key_fails_closed_at_use_boundary(monkeypatch):
    db = FakeDB()
    gemini_factory = Mock(side_effect=RuntimeError("未設定: GEMINI_API_KEY"))
    monkeypatch.setattr(
        pipeline_module, "evaluate_receipt_privacy", Mock(return_value=_normal_gate())
    )

    with pytest.raises(RuntimeError, match="未設定: GEMINI_API_KEY"):
        pipeline_module.ReceiptPipeline(
            db, None, gemini_factory=gemini_factory
        ).process_bytes(b"normal receipt", "image/png", "normal-source")

    gemini_factory.assert_called_once_with()
    assert db.category_calls == 0
    assert db.append_calls == []


def test_medical_needs_review_is_observed_without_gemini_or_sheets(monkeypatch):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    observer = Mock()
    gate_result = _medical_gate("needs_review")
    monkeypatch.setattr(
        pipeline_module, "evaluate_receipt_privacy", Mock(return_value=gate_result)
    )

    result = pipeline_module.ReceiptPipeline(
        db, ai, medical_review_observer=observer
    ).process_bytes(b"private medical bytes", "image/png", "medical-source")

    observer.observe.assert_called_once_with(source_id="medical-source", gate=gate_result)
    assert result["status"] == "privacy_blocked"
    ai.analyze_receipt.assert_not_called()
    assert db.append_calls == []


def test_medical_pipeline_to_canonical_review_handoff_is_idempotent(monkeypatch):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    handoff = MedicalInboxHandoffShadow(identity_key=b"synthetic-pipeline-review-key")
    gate = Mock(return_value=_medical_gate("needs_review"))
    monkeypatch.setattr(pipeline_module, "evaluate_receipt_privacy", gate)
    pipeline = pipeline_module.ReceiptPipeline(
        db, ai, medical_review_observer=handoff
    )

    first = pipeline.process_bytes(b"private medical bytes", "image/png", "medical-source")
    second = pipeline.process_bytes(b"private medical bytes", "image/png", "medical-source")

    assert first["status"] == second["status"] == "privacy_blocked"
    assert len(handoff.items()) == 1
    assert gate.call_count == 2
    ai.analyze_receipt.assert_not_called()
    assert db.append_calls == []


def test_sensitive_unknown_is_not_promoted_to_medical_review(monkeypatch):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    observer = Mock()
    monkeypatch.setattr(
        pipeline_module, "evaluate_receipt_privacy", Mock(return_value=_sensitive_gate())
    )

    result = pipeline_module.ReceiptPipeline(
        db, ai, medical_review_observer=observer
    ).process_bytes(b"unknown private bytes", "image/png", "unknown-source")

    observer.observe.assert_not_called()
    assert result["status"] == "privacy_blocked"
    ai.analyze_receipt.assert_not_called()
    assert db.append_calls == []


def test_medical_review_observer_failure_preserves_privacy_block(monkeypatch):
    db = FakeDB()
    ai = FakeAI(_normal_receipt_result())
    observer = Mock()
    observer.observe.side_effect = RuntimeError("synthetic shadow failure")
    monkeypatch.setattr(
        pipeline_module,
        "evaluate_receipt_privacy",
        Mock(return_value=_medical_gate("needs_review")),
    )

    result = pipeline_module.ReceiptPipeline(
        db, ai, medical_review_observer=observer
    ).process_bytes(b"private medical bytes", "image/png", "medical-source")

    assert result["status"] == "privacy_blocked"
    assert result["classification"] == "medical"
    assert result["medical_shadow_status"] == "handoff_failed"
    ai.analyze_receipt.assert_not_called()
    assert db.append_calls == []


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


@pytest.mark.parametrize("failed_sheet", ["レシート", "支出明細", "取込データ"])
def test_partial_multi_sheet_failure_replay_does_not_duplicate_rows(monkeypatch, failed_sheet):
    db = FakeDB(fail_after_commit=failed_sheet)
    ai = FakeAI(_normal_receipt_result())
    monkeypatch.setattr(
        pipeline_module, "evaluate_receipt_privacy", Mock(return_value=_normal_gate())
    )
    pipeline = pipeline_module.ReceiptPipeline(db, ai)

    with pytest.raises(RuntimeError, match="synthetic"):
        pipeline.process_bytes(b"normal receipt", "image/png", "replay-source")

    replay = pipeline.process_bytes(b"normal receipt", "image/png", "replay-source")

    assert replay["status"] in {"imported", "skipped"}
    assert db._receipt_ids == {"R-replay-source"}
    assert db._expense_ids == {"R-replay-source-01"}
    assert db._import_ids == {"receipt:replay-source"}
    for sheet in ("レシート", "支出明細", "取込データ"):
        stored_ids = [row[0] for called_sheet, rows in db.append_calls
                      if called_sheet == sheet for row in rows]
        assert len(stored_ids) == len(set(stored_ids)) == 1


@pytest.mark.parametrize("failed_sheet", ["レシート", "取込データ"])
def test_needs_review_partial_failure_replay_is_also_idempotent(monkeypatch, failed_sheet):
    result = _normal_receipt_result().model_copy(update={"date": ""})
    db = FakeDB(fail_after_commit=failed_sheet)
    ai = FakeAI(result)
    monkeypatch.setattr(
        pipeline_module, "evaluate_receipt_privacy", Mock(return_value=_normal_gate())
    )
    pipeline = pipeline_module.ReceiptPipeline(db, ai)

    with pytest.raises(RuntimeError, match="synthetic"):
        pipeline.process_bytes(b"normal receipt", "image/png", "review-replay-source")

    replay = pipeline.process_bytes(
        b"normal receipt", "image/png", "review-replay-source"
    )

    assert replay["status"] in {"needs_review", "skipped"}
    assert db._receipt_ids == {"R-review-replay-source"}
    assert db._expense_ids == set()
    assert db._import_ids == {"receipt:review-replay-source"}
    for sheet in ("レシート", "取込データ"):
        stored_ids = [row[0] for called_sheet, rows in db.append_calls
                      if called_sheet == sheet for row in rows]
        assert len(stored_ids) == len(set(stored_ids)) == 1
