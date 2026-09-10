"""Synthetic-only AI structured extraction shadow for medical receipts.

Private OCR observations are converted locally into a closed anonymous token
schema.  Only a gate-issued capability for an explicitly synthetic fixture may
reach a transport.  Real-medical observations can be evaluated offline, but the
outbound gate never issues a transport capability for them.

AI output is advisory evidence selection only.  It is rebound to the original
local OCR observation and always stops at ``needs_review`` with no write or
production authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import string
import unicodedata
from typing import Literal, Protocol

from .medical_gemini_shadow import (
    EnvironmentMedicalShadowApiKeyProvider,
    HttpRequest,
    UrllibHttpExecutor,
    _GEMINI_GENERATE_CONTENT_URL,
    _HTTP_TIMEOUT_SECONDS,
    _is_allowed_json_content_type,
)
from .medical_ocr_observation_shadow import OcrObservation, TextRegion
from .medical_payment_evidence import _NUMERIC_RUN, _scope
from .medical_payment_level2_shadow import Level2ShadowEvaluation
from .medical_payment_level2_shadow import classify_structural_relation
from .medical_receipt_privacy import (
    _compact_ocr_token,
    _exact_strong_structured_label_match,
    _payment_labels_on_line,
    _structured_amount,
)
from .medical_review_workflow_shadow import MedicalReviewItem


REQUEST_SCHEMA_VERSION = "medical-ai-structured-shadow-v1"
RESPONSE_SCHEMA_VERSION = "medical-ai-structured-shadow-response-v1"
OCR_PROVENANCE_SCHEMA_VERSION = "medical-ocr-observation-v1"
_MAX_TOKENS = 128
_MAX_RESPONSE_BYTES = 4096
_ID = re.compile(r"(?:doc|unit|token|provenance)_[A-Za-z]{32}\Z")
_PLACEHOLDERS = frozenset(
    {
        "<PAYMENT_LABEL>",
        "<NUMERIC>",
        "<NEGATIVE_CONTEXT>",
        "<REDACTED>",
    }
)
_REASON_CODES = frozenset(
    {
        "selected_bound_evidence",
        "no_payment_candidate",
        "ambiguous_payment_candidates",
        "insufficient_evidence",
        "not_medical_document",
    }
)
_RESPONSE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "request_schema_version",
        "document_id",
        "unit_ref",
        "provenance_ref",
        "document_type",
        "payment_amount_token_id",
        "payment_label_token_ids",
        "decision",
        "reason_code",
    ],
    "properties": {
        "schema_version": {"type": "string", "enum": [RESPONSE_SCHEMA_VERSION]},
        "request_schema_version": {"type": "string", "enum": [REQUEST_SCHEMA_VERSION]},
        "document_id": {"type": "string"},
        "unit_ref": {"type": "string"},
        "provenance_ref": {"type": "string"},
        "document_type": {
            "type": "string",
            "enum": ["medical_receipt", "not_medical", "unknown"],
        },
        "payment_amount_token_id": {"type": ["string", "null"]},
        "payment_label_token_ids": {
            "type": "array",
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"type": "string"},
        },
        "decision": {"type": "string", "enum": ["select", "abstain", "unresolved"]},
        "reason_code": {"type": "string", "enum": sorted(_REASON_CODES)},
    },
}
_STATIC_INSTRUCTION = (
    "Return exactly one JSON object matching the supplied schema. Use only the "
    "anonymous token evidence. Never invent a value or token ID. Select only an "
    "existing numeric token and its existing payment-label token(s). If evidence "
    "is absent, conflicting, negative-context, or ambiguous, abstain or return "
    "unresolved. Do not return explanations, markdown, or extra fields."
)
_CAPABILITY = object()


class StructuredShadowRejected(ValueError):
    """Data-free fail-closed rejection."""


@dataclass(frozen=True)
class StructuredShadowPolicy:
    """Explicit opt-in plus a separately engaged-by-default kill switch."""

    transport_enabled: bool = False
    kill_switch_engaged: bool = True

    def __post_init__(self) -> None:
        if type(self.transport_enabled) is not bool or type(self.kill_switch_engaged) is not bool:
            raise TypeError("invalid_policy")


@dataclass(frozen=True)
class _LocalTokenEvidence:
    token_id: str
    ordinal: int
    unit_ref: str
    page: int
    raw_text: str = field(repr=False)
    bbox: tuple[float, float, float, float] = field(repr=False)
    confidence: float = field(repr=False)
    scope: str
    exact_strong_label: bool
    numeric_amount: int | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StructuredShadowBuild:
    payload: dict = field(repr=False)
    source_kind: Literal["synthetic_fixture", "real_medical"]
    tokens: tuple[_LocalTokenEvidence, ...] = field(repr=False)
    source_fingerprint: str = field(repr=False)
    integrity_key: bytes = field(repr=False)
    integrity_seal: str = field(repr=False)


class ValidatedSyntheticRequest:
    """Opaque, single-use request capability issued only for synthetic fixtures."""

    __slots__ = ("__wire", "__used")

    def __init__(self, wire: bytes, capability: object) -> None:
        if capability is not _CAPABILITY or type(wire) is not bytes:
            raise TypeError("validated_synthetic_request_required")
        self.__wire = wire
        self.__used = False

    def __repr__(self) -> str:
        return "ValidatedSyntheticRequest()"

    def __reduce__(self):
        raise TypeError("capability_not_serializable")

    def _claim(self, capability: object) -> bytes:
        if capability is not _CAPABILITY or self.__used:
            raise StructuredShadowRejected("request_reused")
        self.__used = True
        return self.__wire


class StructuredShadowTransport(Protocol):
    def send(self, request: ValidatedSyntheticRequest) -> bytes: ...


@dataclass
class FakeStructuredShadowTransport:
    """No-network test transport; response content is synthetic test data."""

    response: bytes
    invocations: int = 0

    def send(self, request: ValidatedSyntheticRequest) -> bytes:
        wire = request._claim(_CAPABILITY)
        if type(wire) is not bytes:
            raise StructuredShadowRejected("invalid_request")
        self.invocations += 1
        return self.response


class GeminiStructuredSyntheticTransport:
    """Fixed Gemini REST transport accepting only a gate-issued synthetic request."""

    def __init__(self, executor=None, key_provider=None) -> None:
        self._executor = executor if executor is not None else UrllibHttpExecutor()
        self._key_provider = (
            key_provider if key_provider is not None else EnvironmentMedicalShadowApiKeyProvider()
        )

    def send(self, request: ValidatedSyntheticRequest) -> bytes:
        if type(request) is not ValidatedSyntheticRequest:
            raise StructuredShadowRejected("validated_synthetic_request_required")
        key = self._key_provider.get()
        if type(key) is not str or not key.strip():
            raise StructuredShadowRejected("authentication")
        wire = request._claim(_CAPABILITY)
        body = json.dumps(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": _STATIC_INSTRUCTION},
                            {"text": wire.decode("ascii")},
                        ],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": _RESPONSE_JSON_SCHEMA,
                    "candidateCount": 1,
                    "temperature": 0,
                },
            },
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        response = self._executor.execute(
            HttpRequest(
                "POST",
                _GEMINI_GENERATE_CONTENT_URL,
                (("Content-Type", "application/json"), ("x-goog-api-key", key)),
                body,
                _HTTP_TIMEOUT_SECONDS,
            )
        )
        if type(response.status_code) is not int or not 200 <= response.status_code < 300:
            raise StructuredShadowRejected("transport_error")
        if not _is_allowed_json_content_type(response.content_type):
            raise StructuredShadowRejected("invalid_content_type")
        if type(response.body) is not bytes or len(response.body) > 32768:
            raise StructuredShadowRejected("response_too_large")
        try:
            return _extract_structured_gemini_text(response.body)
        except Exception:
            raise StructuredShadowRejected("malformed_response") from None


@dataclass(frozen=True)
class StructuredShadowResult:
    status: Literal["needs_review"]
    reason_code: str
    decision: Literal["select", "abstain", "unresolved"] | None = None
    candidate_amount: int | None = field(default=None, repr=False)
    production_authorized: Literal[False] = False
    write_authorized: Literal[False] = False


@dataclass(frozen=True)
class Level2StructuredPreparation:
    build: StructuredShadowBuild = field(repr=False)
    level2_evidence_state: Literal["complete", "unresolved", "incomplete"]
    production_authorized: Literal[False] = False


@dataclass(frozen=True)
class ExistingReviewFirstHandoff:
    """Value-free link to an already-created review-first item."""

    review_item_id: str
    receipt_unit_ref: str
    status: Literal["pending_existing_review"] = "pending_existing_review"
    ai_value_forwarded: Literal[False] = False
    production_authorized: Literal[False] = False
    write_authorized: Literal[False] = False


def _opaque_id(prefix: str) -> str:
    return prefix + "_" + "".join(secrets.choice(string.ascii_letters) for _ in range(32))


def _fingerprint(observation: OcrObservation) -> str:
    digest = hashlib.sha256()
    digest.update(observation.unit_id.encode("utf-8"))
    digest.update(b"\0" + str(observation.page).encode("ascii"))
    digest.update(b"\0" + observation.image_sha256.encode("ascii"))
    digest.update(b"\0" + observation.engine.encode("utf-8"))
    for model in observation.model_sha256:
        digest.update(b"\0" + model.encode("ascii"))
    for region in observation.regions:
        digest.update(b"\0" + str(region.ordinal).encode("ascii"))
        digest.update(b"\0" + unicodedata.normalize("NFKC", region.text).encode("utf-8"))
        digest.update(b"\0" + repr(region.polygon).encode("ascii"))
        digest.update(b"\0" + repr(region.confidence).encode("ascii"))
    return digest.hexdigest()


def _normalized_geometry(region: TextRegion, observation: OcrObservation) -> dict[str, int]:
    bbox = region.bbox
    if bbox is None:
        raise StructuredShadowRejected("invalid_observation")
    x, y, width, height = bbox
    return {
        "x": round(x / observation.width * 10000),
        "y": round(y / observation.height * 10000),
        "width": round(width / observation.width * 10000),
        "height": round(height / observation.height * 10000),
    }


def _confidence_bucket(confidence: float) -> str:
    if confidence >= 0.9:
        return "high"
    if confidence >= 0.7:
        return "medium"
    return "low"


def _numeric_amount(text: str) -> int | None:
    matches = tuple(_NUMERIC_RUN.finditer(unicodedata.normalize("NFKC", text)))
    if len(matches) != 1:
        return None
    return _structured_amount(matches[0].group().strip())


def _anonymous_text(text: str, scope: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    markers: list[str] = []
    if scope == "excluded":
        markers.append("<NEGATIVE_CONTEXT>")
    if _payment_labels_on_line(normalized):
        markers.append("<PAYMENT_LABEL>")
    if _NUMERIC_RUN.search(normalized):
        markers.append("<NUMERIC>")
    return " ".join(markers) if markers else "<REDACTED>"


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _local_integrity_material(build: StructuredShadowBuild) -> bytes:
    local = [
        {
            "token_id": token.token_id,
            "ordinal": token.ordinal,
            "unit_ref": token.unit_ref,
            "page": token.page,
            "raw_text": token.raw_text,
            "bbox": token.bbox,
            "confidence": token.confidence,
            "scope": token.scope,
            "exact_strong_label": token.exact_strong_label,
            "numeric_amount": token.numeric_amount,
        }
        for token in build.tokens
    ]
    return _canonical(build.payload) + b"\0" + json.dumps(
        local, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\0" + build.source_kind.encode("ascii") + b"\0" + build.source_fingerprint.encode("ascii")


def _seal(build: StructuredShadowBuild) -> str:
    return hmac.new(build.integrity_key, _local_integrity_material(build), hashlib.sha256).hexdigest()


def build_structured_shadow_payload(
    observation: OcrObservation,
    *,
    source_kind: Literal["synthetic_fixture", "real_medical"],
) -> StructuredShadowBuild:
    """Create a PII-free token payload while retaining local-only binding evidence."""
    if type(observation) is not OcrObservation or type(source_kind) is not str or source_kind not in {
        "synthetic_fixture",
        "real_medical",
    }:
        raise StructuredShadowRejected("invalid_source")
    if not observation.complete or not 0 < len(observation.regions) <= _MAX_TOKENS:
        raise StructuredShadowRejected("invalid_observation")
    document_id = _opaque_id("doc")
    unit_ref = _opaque_id("unit")
    provenance_ref = _opaque_id("provenance")
    local_tokens: list[_LocalTokenEvidence] = []
    wire_tokens: list[dict] = []
    for region in observation.regions:
        if region.confidence is None or region.bbox is None:
            raise StructuredShadowRejected("invalid_observation")
        normalized = unicodedata.normalize("NFKC", region.text)
        compact = _compact_ocr_token(normalized)
        scope = _scope(compact, bool(_payment_labels_on_line(normalized)))
        token_id = _opaque_id("token")
        geometry = _normalized_geometry(region, observation)
        anonymous = _anonymous_text(normalized, scope)
        wire_tokens.append(
            {
                "token_id": token_id,
                "unit_ref": unit_ref,
                "anonymous_text": anonymous,
                "normalized_geometry": geometry,
                "confidence_bucket": _confidence_bucket(region.confidence),
            }
        )
        x, y, width, height = region.bbox
        local_tokens.append(
            _LocalTokenEvidence(
                token_id,
                region.ordinal,
                unit_ref,
                observation.page,
                normalized,
                (x / observation.width, y / observation.height,
                 width / observation.width, height / observation.height),
                region.confidence,
                scope,
                _exact_strong_structured_label_match(normalized) is not None,
                _numeric_amount(normalized),
            )
        )
    payload = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "synthetic_document_id": document_id,
        "unit_ref": unit_ref,
        "provenance_ref": provenance_ref,
        "ocr_provenance_schema_version": OCR_PROVENANCE_SCHEMA_VERSION,
        "tokens": wire_tokens,
    }
    key = secrets.token_bytes(32)
    provisional = StructuredShadowBuild(
        payload, source_kind, tuple(local_tokens), _fingerprint(observation), key, ""
    )
    return StructuredShadowBuild(
        payload,
        source_kind,
        tuple(local_tokens),
        provisional.source_fingerprint,
        key,
        _seal(provisional),
    )


def prepare_structured_shadow_from_level2(
    observation: OcrObservation,
    level2_evaluation: Level2ShadowEvaluation,
    *,
    source_kind: Literal["synthetic_fixture", "real_medical"],
) -> Level2StructuredPreparation:
    """Bind preparation to an existing Level 2 evaluation without its candidate ID."""
    if type(level2_evaluation) is not Level2ShadowEvaluation:
        raise StructuredShadowRejected("invalid_level2_binding")
    if level2_evaluation.evaluation_failed:
        raise StructuredShadowRejected("invalid_level2_binding")
    return Level2StructuredPreparation(
        build_structured_shadow_payload(observation, source_kind=source_kind),
        level2_evaluation.payment_role_evidence_completeness,
    )


def _validate_build(build: StructuredShadowBuild, observation: OcrObservation) -> bytes:
    if type(build) is not StructuredShadowBuild or type(observation) is not OcrObservation:
        raise StructuredShadowRejected("invalid_binding")
    if not hmac.compare_digest(build.source_fingerprint, _fingerprint(observation)):
        raise StructuredShadowRejected("invalid_binding")
    expected = StructuredShadowBuild(
        build.payload,
        build.source_kind,
        build.tokens,
        build.source_fingerprint,
        build.integrity_key,
        "",
    )
    if not hmac.compare_digest(build.integrity_seal, _seal(expected)):
        raise StructuredShadowRejected("invalid_binding")
    payload = build.payload
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "synthetic_document_id",
        "unit_ref",
        "provenance_ref",
        "ocr_provenance_schema_version",
        "tokens",
    }:
        raise StructuredShadowRejected("invalid_payload")
    if (
        payload["schema_version"] != REQUEST_SCHEMA_VERSION
        or payload["ocr_provenance_schema_version"] != OCR_PROVENANCE_SCHEMA_VERSION
        or not _ID.fullmatch(payload["synthetic_document_id"])
        or not _ID.fullmatch(payload["unit_ref"])
        or not _ID.fullmatch(payload["provenance_ref"])
        or type(payload["tokens"]) is not list
        or len(payload["tokens"]) != len(build.tokens)
    ):
        raise StructuredShadowRejected("invalid_payload")
    allowed_token = {
        "token_id", "unit_ref", "anonymous_text", "normalized_geometry", "confidence_bucket"
    }
    for wire_token, local_token in zip(payload["tokens"], build.tokens):
        if (
            type(wire_token) is not dict
            or set(wire_token) != allowed_token
            or wire_token["token_id"] != local_token.token_id
            or wire_token["unit_ref"] != payload["unit_ref"]
            or not _ID.fullmatch(wire_token["token_id"])
            or set(wire_token["anonymous_text"].split()) - _PLACEHOLDERS
            or wire_token["confidence_bucket"] not in {"high", "medium", "low"}
            or type(wire_token["normalized_geometry"]) is not dict
            or set(wire_token["normalized_geometry"]) != {"x", "y", "width", "height"}
            or any(type(value) is not int or not 0 <= value <= 10000
                   for value in wire_token["normalized_geometry"].values())
        ):
            raise StructuredShadowRejected("invalid_payload")
    return _canonical(payload)


class StructuredOutboundGate:
    def validate_for_transport(
        self,
        build: StructuredShadowBuild,
        observation: OcrObservation,
        policy: StructuredShadowPolicy,
    ) -> ValidatedSyntheticRequest:
        if type(policy) is not StructuredShadowPolicy:
            raise StructuredShadowRejected("disabled")
        if not policy.transport_enabled or policy.kill_switch_engaged:
            raise StructuredShadowRejected("disabled")
        if type(build) is not StructuredShadowBuild or build.source_kind != "synthetic_fixture":
            raise StructuredShadowRejected("real_medical_outbound_rejected")
        return ValidatedSyntheticRequest(_validate_build(build, observation), _CAPABILITY)


def _strict_object(pairs: list[tuple[object, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError()
        result[key] = value
    return result


def _extract_structured_gemini_text(raw: bytes) -> bytes:
    """Extract one text part and discard a bounded provider thought signature.

    Gemini 3.1 may attach ``thoughtSignature`` to the same text part even when
    thoughts are not requested. It is provider metadata, never evidence or a
    follow-up input. No other part field or additional part is accepted.
    """
    if type(raw) is not bytes:
        raise ValueError()
    parsed = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_strict_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    if type(parsed) is not dict:
        raise ValueError()
    candidates = parsed.get("candidates")
    if type(candidates) is not list or len(candidates) != 1 or type(candidates[0]) is not dict:
        raise ValueError()
    content = candidates[0].get("content")
    if type(content) is not dict:
        raise ValueError()
    parts = content.get("parts")
    if type(parts) is not list or len(parts) != 1 or type(parts[0]) is not dict:
        raise ValueError()
    part = parts[0]
    if set(part) not in ({"text"}, {"text", "thoughtSignature"}):
        raise ValueError()
    text = part.get("text")
    if type(text) is not str or not text or len(text.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise ValueError()
    if "thoughtSignature" in part:
        signature = part["thoughtSignature"]
        if type(signature) is not str or not signature or len(signature) > 16384:
            raise ValueError()
    return text.encode("utf-8")


def _vertically_overlaps(left: _LocalTokenEvidence, right: _LocalTokenEvidence) -> bool:
    _, ly, _, lh = left.bbox
    _, ry, _, rh = right.bbox
    return max(ly, ry) <= min(ly + lh, ry + rh)


def _has_existing_strong_relation(
    label: _LocalTokenEvidence,
    amount: _LocalTokenEvidence,
    observation: OcrObservation,
) -> bool:
    """Use only the existing Level 2 STRONG relation classifier in shadow binding."""
    if label.unit_ref != amount.unit_ref or label.page != amount.page:
        return False
    regions = {region.ordinal: region for region in observation.regions}
    source_label = regions.get(label.ordinal)
    source_amount = regions.get(amount.ordinal)
    if source_label is None or source_amount is None:
        return False
    return classify_structural_relation(source_label, source_amount).state == "STRONG"


def _response_bind(
    build: StructuredShadowBuild,
    observation: OcrObservation,
    response_bytes: bytes,
) -> StructuredShadowResult:
    if type(response_bytes) is not bytes or not 0 < len(response_bytes) <= _MAX_RESPONSE_BYTES:
        raise StructuredShadowRejected("malformed_response")
    try:
        parsed = json.loads(
            response_bytes.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except Exception:
        raise StructuredShadowRejected("malformed_response") from None
    fields = set(_RESPONSE_JSON_SCHEMA["required"])
    if type(parsed) is not dict or set(parsed) != fields:
        raise StructuredShadowRejected("malformed_response")
    payload = build.payload
    if (
        parsed["schema_version"] != RESPONSE_SCHEMA_VERSION
        or parsed["request_schema_version"] != REQUEST_SCHEMA_VERSION
        or parsed["document_id"] != payload["synthetic_document_id"]
        or parsed["unit_ref"] != payload["unit_ref"]
        or parsed["provenance_ref"] != payload["provenance_ref"]
        or parsed["document_type"] not in {"medical_receipt", "not_medical", "unknown"}
        or parsed["decision"] not in {"select", "abstain", "unresolved"}
        or parsed["reason_code"] not in _REASON_CODES
        or type(parsed["payment_label_token_ids"]) is not list
        or len(parsed["payment_label_token_ids"]) > 4
        or len(set(parsed["payment_label_token_ids"])) != len(parsed["payment_label_token_ids"])
    ):
        raise StructuredShadowRejected("binding_rejected")
    if parsed["decision"] != "select":
        if parsed["payment_amount_token_id"] is not None or parsed["payment_label_token_ids"]:
            raise StructuredShadowRejected("binding_rejected")
        return StructuredShadowResult("needs_review", parsed["reason_code"], parsed["decision"])
    if (
        parsed["document_type"] != "medical_receipt"
        or parsed["reason_code"] != "selected_bound_evidence"
        or type(parsed["payment_amount_token_id"]) is not str
        or not parsed["payment_label_token_ids"]
        or not all(type(value) is str for value in parsed["payment_label_token_ids"])
    ):
        raise StructuredShadowRejected("binding_rejected")
    by_id = {token.token_id: token for token in build.tokens}
    amount = by_id.get(parsed["payment_amount_token_id"])
    labels = [by_id.get(token_id) for token_id in parsed["payment_label_token_ids"]]
    if (
        amount is None
        or amount.numeric_amount is None
        or amount.scope == "excluded"
        or any(label is None or not label.exact_strong_label for label in labels)
        or any(label.unit_ref != amount.unit_ref or label.page != amount.page for label in labels)
        or not any(
            label.token_id == amount.token_id
            or _has_existing_strong_relation(label, amount, observation)
            for label in labels
        )
    ):
        raise StructuredShadowRejected("binding_rejected")
    strong_labels = tuple(token for token in build.tokens if token.exact_strong_label)
    viable = {
        token.token_id
        for token in build.tokens
        if token.numeric_amount is not None
        and token.scope != "excluded"
        and any(label.token_id == token.token_id
                or _has_existing_strong_relation(label, token, observation)
                for label in strong_labels)
    }
    if viable != {amount.token_id}:
        raise StructuredShadowRejected("ambiguous_evidence")
    return StructuredShadowResult(
        "needs_review", "accepted_shadow_candidate", "select", amount.numeric_amount
    )


def run_structured_shadow(
    build: StructuredShadowBuild,
    observation: OcrObservation,
    transport: StructuredShadowTransport,
    policy: StructuredShadowPolicy = StructuredShadowPolicy(),
) -> StructuredShadowResult:
    """One-shot orchestration. Every path remains review-only and write-disabled."""
    try:
        request = StructuredOutboundGate().validate_for_transport(build, observation, policy)
    except StructuredShadowRejected as error:
        return StructuredShadowResult("needs_review", str(error))
    try:
        response = transport.send(request)
    except StructuredShadowRejected as error:
        return StructuredShadowResult("needs_review", str(error))
    except Exception:
        return StructuredShadowResult("needs_review", "transport_error")
    try:
        return _response_bind(build, observation, response)
    except StructuredShadowRejected as error:
        return StructuredShadowResult("needs_review", str(error))
    except Exception:
        return StructuredShadowResult("needs_review", "binding_rejected")


def handoff_to_existing_review_first(
    result: StructuredShadowResult, item: MedicalReviewItem
) -> ExistingReviewFirstHandoff:
    """Attach to existing review state while deliberately discarding any AI value."""
    if (
        type(result) is not StructuredShadowResult
        or type(item) is not MedicalReviewItem
        or result.status != "needs_review"
        or result.production_authorized is not False
        or result.write_authorized is not False
        or item.review_status != "pending"
    ):
        raise StructuredShadowRejected("invalid_review_handoff")
    return ExistingReviewFirstHandoff(item.review_item_id, item.receipt_unit_ref)


def evaluate_real_medical_offline(
    build: StructuredShadowBuild, observation: OcrObservation
) -> dict[str, int]:
    """Value-free counters for M1-M8-style local evaluation; performs no I/O."""
    payload_ok = 0
    outbound_rejected = 0
    evidence_bindable = 0
    try:
        _validate_build(build, observation)
        payload_ok = 1
        labels = tuple(token for token in build.tokens if token.exact_strong_label)
        viable = {
            token.token_id
            for token in build.tokens
            if token.numeric_amount is not None
            and token.scope != "excluded"
            and any(label.token_id == token.token_id
                    or _has_existing_strong_relation(label, token, observation)
                    for label in labels)
        }
        evidence_bindable = int(len(viable) == 1 and bool(labels))
    except Exception:
        pass
    try:
        StructuredOutboundGate().validate_for_transport(
            build,
            observation,
            StructuredShadowPolicy(transport_enabled=True, kill_switch_engaged=False),
        )
    except StructuredShadowRejected:
        outbound_rejected = 1
    return {
        "anonymous_payload_generated": payload_ok,
        "real_medical_outbound_rejected": outbound_rejected,
        "evidence_binding_possible": evidence_bindable,
        "review_first_required": 1,
        "production_authorized": 0,
        "write_authorized": 0,
    }
