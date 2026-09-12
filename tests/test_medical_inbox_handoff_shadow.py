from __future__ import annotations

from app.medical_inbox_handoff_shadow import MedicalInboxHandoffShadow
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
