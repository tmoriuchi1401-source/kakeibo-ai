import hashlib
import json

import pytest

from app.paypay_evidence_bundle import (
    PayPayEvidenceBundle,
    PayPayEvidencePayload,
    calculate_evidence_id,
    canonical_payload,
    load_evidence_bundle,
    verify_evidence_bundle,
)
from app.payment_coverage_manifest import preview_payment_coverage_manifests


class MockSignatureVerifier:
    def __init__(self, valid=True):
        self.valid = valid

    def verify(self, canonical, signature, device_key_id):
        return self.valid and signature == "mock-valid" and device_key_id == "device-key-1"


def _files(tmp_path):
    csv_path = tmp_path / "Transactions.csv"
    csv_path.write_text(
        "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
        "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
        "2026/08/10 12:00,100,,,,,,支払い,店,PayPay残高,一回払い,本人,TX-1\n",
        encoding="utf-8-sig",
    )
    image_path = tmp_path / "status.png"
    image_path.write_bytes(b"mock status image")
    return csv_path, image_path


def _payload(csv_path, image_path, **overrides):
    values = {
        "format_version": "1",
        "trust_tier": "captured",
        "acquisition_method": "same_flow_capture_helper",
        "requested_start": "2026-08-01",
        "requested_end": "2026-08-31",
        "captured_status": "download_available",
        "captured_at": "2026-08-29T06:00:00Z",
        "csv_filename": csv_path.name,
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "status_image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
        "capture_helper_version": "test-1",
        "device_key_id": "device-key-1",
    }
    values.update(overrides)
    return PayPayEvidencePayload(**values)


def _bundle(csv_path, image_path, *, signature="mock-valid", **overrides):
    payload = _payload(csv_path, image_path, **overrides)
    return PayPayEvidenceBundle(payload, calculate_evidence_id(payload), signature)


def _verify(tmp_path, *, verifier=None, signature="mock-valid", **overrides):
    csv_path, image_path = _files(tmp_path)
    bundle = _bundle(csv_path, image_path, signature=signature, **overrides)
    result = verify_evidence_bundle(
        bundle, csv_path=csv_path, status_image_path=image_path,
        signature_verifier=verifier,
    )
    return result, csv_path, image_path, bundle


def test_canonicalization_is_deterministic_and_key_order_independent(tmp_path):
    csv_path, image_path = _files(tmp_path)
    payload = _payload(csv_path, image_path)
    reversed_dict = dict(reversed(list(payload.__dict__.items())))
    assert canonical_payload(payload) == canonical_payload(reversed_dict)
    assert calculate_evidence_id(payload) == calculate_evidence_id(reversed_dict)
    assert b" " not in canonical_payload(payload)
    assert canonical_payload(payload).decode("utf-8").startswith('{"acquisition_method"')


def test_payload_change_changes_evidence_id(tmp_path):
    csv_path, image_path = _files(tmp_path)
    first = _payload(csv_path, image_path)
    second = _payload(csv_path, image_path, requested_end="2026-08-30")
    assert calculate_evidence_id(first) != calculate_evidence_id(second)


def test_evidence_id_tampering_is_detected(tmp_path):
    result, csv_path, image_path, bundle = _verify(tmp_path, verifier=MockSignatureVerifier())
    tampered = PayPayEvidenceBundle(bundle.payload, "0" * 64, bundle.signature)
    result = verify_evidence_bundle(
        tampered, csv_path=csv_path, status_image_path=image_path,
        signature_verifier=MockSignatureVerifier(),
    )
    assert result.reason == "evidence_id_mismatch"


def test_csv_hash_match_and_mismatch(tmp_path):
    result, csv_path, image_path, bundle = _verify(tmp_path, verifier=MockSignatureVerifier())
    assert result.csv_hash_valid is True
    csv_path.write_bytes(csv_path.read_bytes() + b"changed")
    result = verify_evidence_bundle(
        bundle, csv_path=csv_path, status_image_path=image_path,
        signature_verifier=MockSignatureVerifier(),
    )
    assert result.reason == "csv_hash_mismatch"


def test_status_image_hash_match_and_mismatch(tmp_path):
    result, csv_path, image_path, bundle = _verify(tmp_path, verifier=MockSignatureVerifier())
    assert result.status_image_hash_valid is True
    image_path.write_bytes(b"changed")
    result = verify_evidence_bundle(
        bundle, csv_path=csv_path, status_image_path=image_path,
        signature_verifier=MockSignatureVerifier(),
    )
    assert result.reason == "status_image_hash_mismatch"


def test_unsupported_acquisition_method(tmp_path):
    result, *_ = _verify(
        tmp_path, verifier=MockSignatureVerifier(), acquisition_method="manual_json",
    )
    assert result.reason == "unsupported_acquisition_method"


def test_unsupported_format_version(tmp_path):
    result, *_ = _verify(
        tmp_path, verifier=MockSignatureVerifier(), format_version="2",
    )
    assert result.reason == "unsupported_format_version"


def test_export_must_be_completed(tmp_path):
    result, *_ = _verify(
        tmp_path, verifier=MockSignatureVerifier(), captured_status="processing",
    )
    assert result.reason == "export_not_completed"


def test_csv_filename_must_match(tmp_path):
    result, *_ = _verify(
        tmp_path, verifier=MockSignatureVerifier(), csv_filename="another.csv",
    )
    assert result.reason == "csv_filename_mismatch"


def test_unsigned_captured_evidence(tmp_path):
    result, *_ = _verify(tmp_path, verifier=MockSignatureVerifier(), signature=None)
    assert result.reason == "signature_missing"


def test_invalid_and_valid_mocked_signature(tmp_path):
    invalid, *_ = _verify(tmp_path, verifier=MockSignatureVerifier(False))
    valid, *_ = _verify(tmp_path, verifier=MockSignatureVerifier(True))
    assert invalid.reason == "signature_invalid"
    assert valid.reason == "captured_not_provider_verified"
    assert valid.signature_valid is True


def test_self_reported_is_unknown_even_when_files_are_bound(tmp_path):
    result, *_ = _verify(
        tmp_path, signature=None, trust_tier="self_reported",
        acquisition_method="manual_screenshot",
    )
    assert result.reason == "self_reported_not_completion_evidence"
    assert result.candidate_complete is False
    assert result.completeness_proven is False


def test_captured_valid_signature_is_candidate_but_never_proven(tmp_path):
    result, *_ = _verify(tmp_path, verifier=MockSignatureVerifier())
    assert result.accepted is True
    assert result.candidate_complete is True
    assert result.completeness_proven is False


def test_provider_verified_tier_is_still_not_proven(tmp_path):
    result, *_ = _verify(
        tmp_path, verifier=MockSignatureVerifier(), trust_tier="provider_verified",
        acquisition_method="paypay_signed_receipt",
    )
    assert result.reason == "provider_verification_not_implemented"
    assert result.completeness_proven is False


def test_payload_rejects_numbers_booleans_null_and_extra_fields(tmp_path):
    csv_path, image_path = _files(tmp_path)
    data = _payload(csv_path, image_path).__dict__
    for value in (1, True, None):
        malformed = dict(data, format_version=value)
        with pytest.raises(ValueError, match="payload_values_must_be_strings"):
            canonical_payload(malformed)
    with pytest.raises(ValueError, match="unsupported_payload_fields"):
        canonical_payload(dict(data, extra="no"))


def test_loader_rejects_duplicate_keys_and_malformed_bundle(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text('{"payload":{},"payload":{},"evidence_id":"x","signature":null}')
    with pytest.raises(ValueError, match="duplicate_json_key"):
        load_evidence_bundle(path)
    path.write_text("not json")
    with pytest.raises(ValueError, match="malformed_evidence_bundle"):
        load_evidence_bundle(path)


def test_preview_reports_verification_and_keeps_manifest_unknown(tmp_path):
    csv_path, image_path = _files(tmp_path)
    bundle = _bundle(csv_path, image_path)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps({
        "payload": bundle.payload.__dict__,
        "evidence_id": bundle.evidence_id,
        "signature": bundle.signature,
    }), encoding="utf-8")
    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(csv_path)],
        paypay_export_evidence_files=[str(bundle_path)],
        paypay_status_image_files=[str(image_path)],
        signature_verifier=MockSignatureVerifier(),
    )
    verification = result["paypay_evidence_verifications"][0]
    manifest = next(row for row in result["manifests"] if row["source"] == "paypay")
    assert verification["reason"] == "captured_not_provider_verified"
    assert manifest["completion_status"] == "unknown"
    assert manifest["completeness_proven"] is False
    assert (manifest["coverage_start"], manifest["coverage_end"]) == (
        "2026-08-01", "2026-08-31",
    )
