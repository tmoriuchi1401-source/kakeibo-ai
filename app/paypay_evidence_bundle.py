from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol, TypeAlias


TrustTier: TypeAlias = Literal["self_reported", "captured", "provider_verified"]

_BUNDLE_FIELDS = {"payload", "evidence_id", "signature"}
_PAYLOAD_FIELDS = {
    "format_version", "trust_tier", "acquisition_method", "requested_start",
    "requested_end", "captured_status", "captured_at", "csv_filename",
    "csv_sha256", "status_image_sha256", "capture_helper_version",
    "device_key_id",
}
_METHODS = {
    "self_reported": {"manual_json", "user_attestation", "manual_screenshot"},
    "captured": {"same_flow_capture_helper"},
    "provider_verified": {"paypay_official_api", "paypay_signed_receipt"},
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class SignatureVerifier(Protocol):
    def verify(self, canonical_payload: bytes, signature: str,
               device_key_id: str) -> bool: ...


class RejectingSignatureVerifier:
    """Safe default until a real device/provider key registry exists."""

    def verify(self, canonical_payload: bytes, signature: str,
               device_key_id: str) -> bool:
        return False


@dataclass(frozen=True)
class PayPayEvidencePayload:
    format_version: str
    trust_tier: TrustTier
    acquisition_method: str
    requested_start: str
    requested_end: str
    captured_status: str
    captured_at: str
    csv_filename: str
    csv_sha256: str
    status_image_sha256: str
    capture_helper_version: str
    device_key_id: str


@dataclass(frozen=True)
class PayPayEvidenceBundle:
    payload: PayPayEvidencePayload
    evidence_id: str
    signature: str | None


@dataclass(frozen=True)
class EvidenceVerificationResult:
    accepted: bool
    reason: str
    trust_tier: str | None = None
    evidence_id: str | None = None
    requested_start: str | None = None
    requested_end: str | None = None
    evidence_id_valid: bool = False
    csv_hash_valid: bool = False
    status_image_hash_valid: bool = False
    signature_valid: bool = False
    candidate_complete: bool = False
    completeness_proven: bool = False


def canonical_payload(payload: PayPayEvidencePayload | dict[str, str]) -> bytes:
    """Canonical JSON v1: UTF-8, sorted keys, no whitespace, unescaped Unicode.

    Version 1 deliberately accepts exactly the schema's string fields.  JSON
    numbers, booleans and null are rejected instead of inventing number rules.
    """
    data = asdict(payload) if isinstance(payload, PayPayEvidencePayload) else payload
    if set(data) != _PAYLOAD_FIELDS:
        raise ValueError("unsupported_payload_fields")
    if any(not isinstance(value, str) for value in data.values()):
        raise ValueError("payload_values_must_be_strings")
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def calculate_evidence_id(payload: PayPayEvidencePayload | dict[str, str]) -> str:
    return hashlib.sha256(canonical_payload(payload)).hexdigest()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def load_evidence_bundle(path: str | Path) -> PayPayEvidenceBundle:
    try:
        data = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"unsupported_json_constant:{value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed_evidence_bundle") from exc
    if not isinstance(data, dict) or set(data) != _BUNDLE_FIELDS:
        raise ValueError("unsupported_bundle_fields")
    raw_payload = data["payload"]
    if not isinstance(raw_payload, dict):
        raise ValueError("malformed_payload")
    canonical_payload(raw_payload)
    signature = data["signature"]
    if signature is not None and not isinstance(signature, str):
        raise ValueError("malformed_signature")
    if not isinstance(data["evidence_id"], str):
        raise ValueError("malformed_evidence_id")
    return PayPayEvidenceBundle(
        payload=PayPayEvidencePayload(**raw_payload),
        evidence_id=data["evidence_id"], signature=signature,
    )


def _sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_evidence_bundle(
    bundle: PayPayEvidenceBundle,
    *, csv_path: str | Path,
    status_image_path: str | Path,
    signature_verifier: SignatureVerifier | None = None,
) -> EvidenceVerificationResult:
    payload = bundle.payload
    base = {
        "trust_tier": payload.trust_tier,
        "evidence_id": bundle.evidence_id,
        "requested_start": payload.requested_start,
        "requested_end": payload.requested_end,
    }
    try:
        canonical = canonical_payload(payload)
        start = date.fromisoformat(payload.requested_start)
        end = date.fromisoformat(payload.requested_end)
        datetime.strptime(payload.captured_at, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return EvidenceVerificationResult(False, "malformed_evidence", **base)
    if payload.format_version != "1":
        return EvidenceVerificationResult(False, "unsupported_format_version", **base)
    if payload.trust_tier not in _METHODS:
        return EvidenceVerificationResult(False, "unsupported_trust_tier", **base)
    if payload.acquisition_method not in _METHODS[payload.trust_tier]:
        return EvidenceVerificationResult(False, "unsupported_acquisition_method", **base)
    if payload.captured_status != "download_available":
        return EvidenceVerificationResult(False, "export_not_completed", **base)
    if start > end or not _UTC_TIMESTAMP.fullmatch(payload.captured_at):
        return EvidenceVerificationResult(False, "malformed_evidence", **base)
    if not _SHA256.fullmatch(payload.csv_sha256):
        return EvidenceVerificationResult(False, "malformed_csv_hash", **base)
    if not _SHA256.fullmatch(payload.status_image_sha256):
        return EvidenceVerificationResult(False, "malformed_status_image_hash", **base)
    if Path(csv_path).name != payload.csv_filename:
        return EvidenceVerificationResult(False, "csv_filename_mismatch", **base)

    expected_id = hashlib.sha256(canonical).hexdigest()
    id_valid = bundle.evidence_id == expected_id
    if not id_valid:
        return EvidenceVerificationResult(
            False, "evidence_id_mismatch", evidence_id_valid=False, **base,
        )
    try:
        csv_valid = _sha256_file(csv_path) == payload.csv_sha256
        image_valid = _sha256_file(status_image_path) == payload.status_image_sha256
    except OSError:
        return EvidenceVerificationResult(
            False, "evidence_file_unreadable", evidence_id_valid=True, **base,
        )
    if not csv_valid:
        return EvidenceVerificationResult(
            False, "csv_hash_mismatch", evidence_id_valid=True,
            csv_hash_valid=False, status_image_hash_valid=image_valid, **base,
        )
    if not image_valid:
        return EvidenceVerificationResult(
            False, "status_image_hash_mismatch", evidence_id_valid=True,
            csv_hash_valid=True, status_image_hash_valid=False, **base,
        )

    if payload.trust_tier == "self_reported":
        return EvidenceVerificationResult(
            True, "self_reported_not_completion_evidence", evidence_id_valid=True,
            csv_hash_valid=True, status_image_hash_valid=True, **base,
        )
    if not bundle.signature:
        return EvidenceVerificationResult(
            False, "signature_missing", evidence_id_valid=True, csv_hash_valid=True,
            status_image_hash_valid=True, **base,
        )
    verifier = signature_verifier or RejectingSignatureVerifier()
    signature_valid = verifier.verify(
        canonical, bundle.signature, payload.device_key_id,
    )
    if not signature_valid:
        return EvidenceVerificationResult(
            False, "signature_invalid", evidence_id_valid=True, csv_hash_valid=True,
            status_image_hash_valid=True, signature_valid=False, **base,
        )
    if payload.trust_tier == "captured":
        return EvidenceVerificationResult(
            True, "captured_not_provider_verified", evidence_id_valid=True,
            csv_hash_valid=True, status_image_hash_valid=True, signature_valid=True,
            candidate_complete=True, completeness_proven=False, **base,
        )
    return EvidenceVerificationResult(
        True, "provider_verification_not_implemented", evidence_id_valid=True,
        csv_hash_valid=True, status_image_hash_valid=True, signature_valid=True,
        candidate_complete=True, completeness_proven=False, **base,
    )
