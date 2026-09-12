from __future__ import annotations

import json

import pytest

from app.medical_inbox_handoff_shadow import MedicalInboxHandoffShadow
from app.medical_review_workflow_shadow import SafeReviewValidationError
from app.receipt_privacy_gate import ReceiptPrivacyGateResult


KEY = b"synthetic-medical-inbox-key"


def medical_gate(*, status="needs_review") -> ReceiptPrivacyGateResult:
    confirmed = status == "confirmed"
    return ReceiptPrivacyGateResult(
        classification="medical",
        extraction_status="extracted",
        extraction_method="image_ocr",
        text_present=True,
        status=status,
        reason_code="unique_strong_candidate" if confirmed else "no_candidate",
        medical_payment_amount=1200 if confirmed else None,
        medical_candidate_count=1 if confirmed else 0,
        category="医療費",
    )


def sensitive_unknown_gate() -> ReceiptPrivacyGateResult:
    return ReceiptPrivacyGateResult(
        classification="sensitive_unknown",
        extraction_status="extracted",
        extraction_method="image_ocr",
        text_present=True,
        status="blocked",
        reason_code="insufficient_evidence",
    )


def test_unresolved_medical_creates_safe_review_item_once():
    handoff = MedicalInboxHandoffShadow(identity_key=KEY)

    first = handoff.observe(source_id="private-drive-file-id", gate=medical_gate())
    second = handoff.observe(source_id="private-drive-file-id", gate=medical_gate())

    assert first is not None and first.action == "new_item"
    assert second is not None and second.action == "duplicate_suppressed"
    assert len(handoff.items()) == 1
    serialized = handoff.items()[0].model_dump_json()
    assert "private-drive-file-id" not in serialized
    for forbidden in ("raw_ocr", "amount", "patient", "facility", "coordinates"):
        assert forbidden not in serialized.lower()


def test_different_source_identity_creates_different_review_item():
    handoff = MedicalInboxHandoffShadow(identity_key=KEY)
    handoff.observe(source_id="medical-source-1", gate=medical_gate())
    handoff.observe(source_id="medical-source-2", gate=medical_gate())

    assert len(handoff.items()) == 2
    assert handoff.items()[0].review_item_id != handoff.items()[1].review_item_id


def test_sensitive_unknown_remains_hold_and_does_not_enter_medical_queue():
    handoff = MedicalInboxHandoffShadow(identity_key=KEY)

    result = handoff.observe(source_id="unknown-source", gate=sensitive_unknown_gate())

    assert result is None
    assert handoff.items() == ()


def test_confirmed_medical_uses_existing_local_preview_without_unresolved_item():
    handoff = MedicalInboxHandoffShadow(identity_key=KEY)

    result = handoff.observe(source_id="confirmed-source", gate=medical_gate(status="confirmed"))

    assert result is None
    assert handoff.items() == ()


def test_persistent_store_suppresses_duplicate_after_restart(tmp_path):
    path = tmp_path / "medical-review.json"
    first_process = MedicalInboxHandoffShadow(identity_key=KEY, store_path=path)
    first = first_process.observe(source_id="restart-source", gate=medical_gate())
    first_id = first_process.items()[0].review_item_id

    restarted = MedicalInboxHandoffShadow(identity_key=KEY, store_path=path)
    second = restarted.observe(source_id="restart-source", gate=medical_gate())

    assert first is not None and first.action == "new_item"
    assert second is not None and second.action == "duplicate_suppressed"
    assert len(restarted.items()) == 1
    assert restarted.items()[0].review_item_id == first_id


def test_persistent_schema_contains_only_safe_review_metadata(tmp_path):
    path = tmp_path / "medical-review.json"
    handoff = MedicalInboxHandoffShadow(identity_key=KEY, store_path=path)
    handoff.observe(source_id="opaque-source", gate=medical_gate())

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert len(document["items"]) == 1
    serialized = path.read_text(encoding="utf-8").lower()
    for forbidden in ("opaque-source", "raw_ocr", "amount", "patient", "facility", "bytes"):
        assert forbidden not in serialized


def test_corrupted_store_fails_closed(tmp_path):
    path = tmp_path / "medical-review.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(SafeReviewValidationError):
        MedicalInboxHandoffShadow(identity_key=KEY, store_path=path)


def test_missing_identity_key_fails_closed(tmp_path):
    with pytest.raises(SafeReviewValidationError):
        MedicalInboxHandoffShadow(identity_key=b"", store_path=tmp_path / "review.json")


def test_key_change_never_aliases_existing_item(tmp_path):
    path = tmp_path / "medical-review.json"
    first = MedicalInboxHandoffShadow(identity_key=KEY, store_path=path)
    first.observe(source_id="key-rotation-source", gate=medical_gate())

    rotated = MedicalInboxHandoffShadow(identity_key=b"rotated-medical-key-20260912", store_path=path)
    result = rotated.observe(source_id="key-rotation-source", gate=medical_gate())

    assert result is not None and result.action == "new_item"
    assert len(rotated.items()) == 2
    assert rotated.items()[0].review_item_id != rotated.items()[1].review_item_id


def test_atomic_persistence_does_not_leave_partial_store(tmp_path, monkeypatch):
    path = tmp_path / "medical-review.json"
    handoff = MedicalInboxHandoffShadow(identity_key=KEY, store_path=path)
    original_replace = __import__("app.medical_inbox_handoff_shadow", fromlist=["os"]).os.replace

    def fail_replace(*args, **kwargs):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("app.medical_inbox_handoff_shadow.os.replace", fail_replace)
    with pytest.raises(SafeReviewValidationError):
        handoff.observe(source_id="atomic-source", gate=medical_gate())
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr("app.medical_inbox_handoff_shadow.os.replace", original_replace)
