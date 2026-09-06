"""Anonymous, shadow-only medical payloads; this module never sends a request.

The input observation is private local OCR evidence.  The only value that may
cross the prospective Gemini boundary is the JSON returned by
``FinalOutboundGate.serialize``.  That gate deliberately accepts an exact,
closed schema rather than arbitrary mappings, prompts, metadata, or OCR DTOs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import secrets
import unicodedata
from typing import Literal, Protocol

from .medical_layout_shadow import PageFrame, observe_layout
from .medical_ocr_observation_shadow import OcrObservation
from .medical_payment_evidence import _NUMERIC_RUN, _scope
from .medical_receipt_privacy import _StructuredOcrToken, _compact_ocr_token, _payment_labels_on_line, _structured_amount


_VERSION = "medical-anonymous-shadow-v1"
_RESPONSE_VERSION = "medical-anonymous-shadow-response-v1"
_UNIT_REF = re.compile(r"unit_[A-Za-z0-9_-]{24,64}\Z")
_REGION_ID = re.compile(r"region_[A-Z]+\Z")
_AMOUNT_ID = re.compile(r"amount_[A-Z]+\Z")
_MAX_REGIONS = 128
_MAX_RESPONSE_BYTES = 4096
_NUMERIC_SURFACE = re.compile(r"(?<![A-Za-z0-9])[0-9０-９][0-9０-９,，.．\s]*")
_TRANSPORT_CAPABILITY = object()
_FREE_TIER_MODEL = "gemini-3.1-flash-lite"
_JSON_CONTENT_TYPES = frozenset({"application/json", "application/json; charset=utf-8"})


class OutboundRejected(ValueError):
    """A fixed, data-free failure for unsafe local-to-provider handoff."""


class InboundRejected(ValueError):
    """A fixed, data-free failure for unsafe provider-response handoff."""


class TransportResponseRejected(ValueError):
    """A fixed, data-free failure before semantic inbound parsing."""


class ValidatedAnonymousBytes:
    """Opaque canonical bytes issued only by the final outbound gate.

    This is a normal-code boundary, not cryptographic capability security: Python
    code in the same process can introspect private names.  The transport accepts
    this exact type only, so raw observations and mappings have no supported path.
    """

    __slots__ = ("__bytes",)

    def __init__(self, value: bytes, token: object) -> None:
        if token is not _TRANSPORT_CAPABILITY or type(value) is not bytes:
            raise TypeError("validated_anonymous_bytes_required")
        self.__bytes = value

    def __repr__(self) -> str:
        return "ValidatedAnonymousBytes()"

    def _for_transport(self) -> bytes:
        return self.__bytes


class ValidatedTransportResponse:
    """Opaque response bytes that passed transport-format and privacy checks."""

    __slots__ = ("__bytes",)

    def __init__(self, value: bytes, token: object) -> None:
        if token is not _TRANSPORT_CAPABILITY or type(value) is not bytes:
            raise TypeError("validated_transport_response_required")
        self.__bytes = value

    def __repr__(self) -> str:
        return "ValidatedTransportResponse()"

    def _for_inbound_gate(self) -> bytes:
        return self.__bytes


@dataclass(frozen=True)
class TransportResponse:
    """Minimal synthetic transport result; body stays hidden from repr."""

    status: Literal["ok", "timeout", "quota", "authentication", "unavailable", "transport_error"]
    content_type: str | None = None
    body: bytes | None = field(default=None, repr=False)


class AnonymousShadowTransport(Protocol):
    """The future sender's only supported input is validated anonymous bytes."""

    def send(self, request: ValidatedAnonymousBytes) -> TransportResponse: ...


@dataclass
class FakeAnonymousShadowTransport:
    """Synthetic test double. It has no network, key, model, or HTTP behavior."""

    response: TransportResponse | None = None
    failure: Exception | None = field(default=None, repr=False)
    invocations: int = 0
    received: list[ValidatedAnonymousBytes] = field(default_factory=list, repr=False)

    def send(self, request: ValidatedAnonymousBytes) -> TransportResponse:
        if type(request) is not ValidatedAnonymousBytes:
            raise TypeError("validated_anonymous_bytes_required")
        self.invocations += 1
        self.received.append(request)
        if self.failure is not None:
            raise self.failure
        if type(self.response) is not TransportResponse:
            raise RuntimeError("synthetic_transport_response_missing")
        return self.response


@dataclass(frozen=True)
class LocalAmountMap:
    """Local-only ID -> amount correspondence.  It has no wire representation."""

    _values: tuple[tuple[str, int], ...] = field(repr=False)

    def resolve(self, amount_id: str) -> int:
        for key, value in self._values:
            if key == amount_id:
                return value
        raise KeyError("unknown local amount id")

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self._values)


@dataclass(frozen=True)
class AnonymousShadowBuild:
    """Payload plus private correspondence; never serialize this wrapper."""

    payload: dict = field(repr=False)
    amount_map: LocalAmountMap = field(repr=False)
    _source_fingerprint: str = field(repr=False)
    _unit_ref: str = field(repr=False)
    _response_private_literals: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class ShadowPreparation:
    """Data-free local result; a withheld unit remains needs_review."""

    status: Literal["shadow_ready", "needs_review"]
    reason_code: str
    build: AnonymousShadowBuild | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AcceptedAnonymousResponse:
    """Validated provider semantics only; no raw response text is retained."""

    decision: Literal["select", "abstain", "unresolved"]
    amount_id: str | None
    confidence: Literal["high", "medium", "low"] | None
    _binding: str = field(repr=False)


@dataclass(frozen=True)
class LocalResponseResult:
    """Local-only rehydration result; it never grants business confirmation."""

    status: Literal["needs_review"]
    decision: Literal["select", "abstain", "unresolved"]
    amount: int | None = field(repr=False)


@dataclass(frozen=True)
class InboundShadowResult:
    """Data-free public shadow result, including synthetic transport failures."""

    status: Literal["needs_review"]
    reason_code: Literal[
        "accepted_shadow_response", "malformed_response", "synthetic_timeout",
        "synthetic_quota_failure", "synthetic_api_failure", "synthetic_parser_failure",
        "disabled", "timeout", "quota", "authentication", "unavailable",
        "transport_error", "invalid_content_type", "response_too_large", "invalid_utf8",
        "privacy_rejected", "binding_rejected", "validation_rejected",
    ]
    local_result: LocalResponseResult | None = field(default=None, repr=False)


def _alpha(index: int) -> str:
    """A, B, ..., Z, AA: opaque labels, never derived from an amount."""
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _confidence(value: float | None) -> Literal["high", "medium", "low"]:
    if value is None or value < 0.7:
        return "low"
    if value < 0.9:
        return "medium"
    return "high"


def _geometry(observation: OcrObservation, ordinal: int) -> dict[str, float]:
    box = observation.regions[ordinal].bbox
    if box is None:
        raise OutboundRejected("unsafe_observation")
    x, y, width, height = box
    result = {"x": x / observation.width, "y": y / observation.height,
              "width": width / observation.width, "height": height / observation.height}
    if not all(type(v) is float and math.isfinite(v) and 0 <= v <= 1 for v in result.values()):
        raise OutboundRejected("unsafe_geometry")
    return result


def _local_scope(text: str) -> str:
    compact = _compact_ocr_token(unicodedata.normalize("NFKC", text))
    return _scope(compact, bool(_payment_labels_on_line(text)))


def _observation_fingerprint(observation: OcrObservation) -> str:
    """Bind private literals to their build without creating a wire identifier."""
    try:
        digest = hashlib.sha256()
        digest.update(observation.image_sha256.encode("ascii"))
        digest.update(json.dumps([
            observation.page, observation.width, observation.height,
            [(region.ordinal, region.text, region.polygon, region.confidence,
              region.detection_confidence, region.issues, region.granularity)
             for region in observation.regions],
            observation.issues,
        ], ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii"))
        return digest.hexdigest()
    except Exception as error:
        raise OutboundRejected("invalid_observation_fingerprint") from error


def _layout(observation: OcrObservation):
    tokens = []
    for region in observation.regions:
        box = region.bbox
        if box is None or region.confidence is None:
            raise OutboundRejected("unsafe_observation")
        x, y, width, height = box
        tokens.append(_StructuredOcrToken(
            # The source PDF ordinal is intentionally not layout data or wire data.
            # This one-unit graph has a local page coordinate of one.
            region.text, 1, x, y, width, height, region.confidence * 100,
            (region.ordinal, 0, 0, 0),
        ))
    layout = observe_layout(tuple(tokens), (PageFrame(1, observation.width, observation.height),),
                            expected_pages=1, observation_complete=observation.complete)
    if "observation_incomplete" in layout.issues:
        raise OutboundRejected("unsafe_layout")
    return layout


def build_anonymous_shadow_payload(observation: OcrObservation) -> AnonymousShadowBuild:
    """Create a safe representation for exactly one image/PDF page.

    This is intentionally more conservative than local evidence collection:
    numeric text without an explicit, allowlisted payment/excluded context,
    possible-payment context, multiple numeric runs, or malformed numbers
    produces no payload.  Nothing in this function authorizes a network call.
    """
    if type(observation) is not OcrObservation or not observation.complete:
        raise OutboundRejected("observation_incomplete")
    if not 1 <= observation.page or not observation.regions or len(observation.regions) > _MAX_REGIONS:
        raise OutboundRejected("invalid_unit")

    layout = _layout(observation)
    included: dict[int, dict] = {}
    values: list[tuple[str, int]] = []
    for region in observation.regions:
        raw_runs = tuple(_NUMERIC_RUN.finditer(unicodedata.normalize("NFKC", region.text)))
        scope = _local_scope(region.text)
        if scope == "possible_payment_region":
            raise OutboundRejected("ambiguous_semantic_context")
        if not raw_runs:
            # Label-only anchors are useful only where their category is explicit.
            if scope in {"payment_region", "excluded"}:
                included[region.ordinal] = {
                    "kind": "context_anchor",
                    "context": "payment" if scope == "payment_region" else "excluded",
                    "amount_id": None,
                }
            continue
        if scope == "unassigned" or len(raw_runs) != 1:
            raise OutboundRejected("ambiguous_numeric_context")
        amount = _structured_amount(raw_runs[0].group().strip())
        if amount is None:
            raise OutboundRejected("malformed_numeric")
        amount_id = f"amount_{_alpha(len(values))}"
        values.append((amount_id, amount))
        included[region.ordinal] = {
            "kind": "numeric_evidence",
            "context": "payment" if scope == "payment_region" else "excluded",
            "amount_id": amount_id,
        }
    if not values:
        raise OutboundRejected("amount_not_observed")

    region_ids = {ordinal: f"region_{_alpha(index)}" for index, ordinal in enumerate(sorted(included))}
    regions = []
    for ordinal in sorted(included):
        item = included[ordinal]
        regions.append({
            "id": region_ids[ordinal], "kind": item["kind"], "context": item["context"],
            "amount_id": item["amount_id"], "geometry": _geometry(observation, ordinal),
            "confidence": _confidence(observation.regions[ordinal].confidence), "status": "observed",
        })
    relations = [
        {"left": region_ids[relation.left], "right": region_ids[relation.right], "kind": relation.axis}
        for relation in layout.relations
        if relation.left in region_ids and relation.right in region_ids
    ]
    competing = any(
        hypothesis.label in region_ids and hypothesis.numeric in region_ids
        and "competing_relationships" in hypothesis.issues
        for hypothesis in layout.hypotheses
    )
    unit_ref = "unit_" + secrets.token_urlsafe(24)
    payload = {
        "schema_version": _VERSION,
        # This random value is generated for this handoff only.  It is not a receipt,
        # filename, page ordinal, path, Drive ID, image digest, or persistent key.
        "unit_ref": unit_ref,
        "mode": "shadow_only",
        "state": "shadow_ready",
        # The local geometry is only an observation.  It never establishes a
        # payment role, so unresolved state is explicit even in a safe payload.
        "structure_state": "unresolved",
        "competing_structure": competing,
        "regions": regions,
        "relations": relations,
    }
    amount_map = LocalAmountMap(tuple(values))
    result = AnonymousShadowBuild(
        payload, amount_map, _observation_fingerprint(observation), unit_ref,
        _private_literals(observation, amount_map),
    )
    # Gate the builder's own output as a regression tripwire before a caller gets it.
    FinalOutboundGate().serialize(
        result.payload, _private_literals(observation, result.amount_map),
        private_amounts=tuple(value for _, value in result.amount_map._values),
    )
    return result


def prepare_anonymous_shadow(observation: OcrObservation) -> ShadowPreparation:
    """Turn every local semanticization failure into data-free needs_review."""
    try:
        return ShadowPreparation("shadow_ready", "ready", build_anonymous_shadow_payload(observation))
    except OutboundRejected as error:
        # OutboundRejected messages are fixed codes defined in this module, never
        # OCR text, filenames, paths, values, or an upstream exception string.
        return ShadowPreparation("needs_review", str(error))


def _private_literals(observation: OcrObservation, amount_map: LocalAmountMap) -> tuple[str, ...]:
    """Private material used only by final-byte defense in depth."""
    values = [region.text for region in observation.regions if region.text.strip()]
    for _, amount in amount_map._values:
        grouped = f"{amount:,}"
        values.extend((str(amount), f"{amount}円", f"¥{amount}", grouped,
                       f"{grouped}円", f"¥{grouped}"))
    return tuple(values)


def _decode_json_unicode_escapes(text: str) -> str:
    """Decode only JSON-style codepoint escapes for a local leakage scan."""
    return re.sub(r"\\+u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)), text)


def _reject_numeric_amount_surface(
    text: str, amounts: tuple[int, ...], error_type: type[ValueError], reason: str,
) -> None:
    """Reject amount digits despite grouping, Unicode digits, whitespace, or escaping."""
    if (type(text) is not str or type(amounts) is not tuple
            or not all(type(amount) is int and amount >= 0 for amount in amounts)):
        raise error_type("invalid_private_scan")
    targets = {str(amount) for amount in amounts}
    normalized = unicodedata.normalize("NFKC", _decode_json_unicode_escapes(text))
    for match in _NUMERIC_SURFACE.finditer(normalized):
        digits = "".join(character for character in match.group() if "0" <= character <= "9")
        if digits in targets:
            raise error_type(reason)


class FinalOutboundGate:
    """Independent exact-schema and final-bytes gate for a future client boundary."""

    def serialize(
        self, payload: object, private_literals: tuple[str, ...] = (), *,
        private_amounts: tuple[int, ...] = (),
    ) -> bytes:
        self._validate(payload)
        try:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        except Exception as error:
            raise OutboundRejected("payload_serialization_rejected") from error
        text = encoded.decode("ascii")
        # OCR text / concrete values are never expected to be wire strings.  This
        # check catches accidental future passthrough before a provider client exists.
        for literal in private_literals:
            if type(literal) is not str:
                raise OutboundRejected("invalid_private_scan")
            escaped = json.dumps(literal, ensure_ascii=True)[1:-1]
            if escaped and escaped in text:
                raise OutboundRejected("private_literal_detected")
        _reject_numeric_amount_surface(text, private_amounts, OutboundRejected,
                                       "private_numeric_literal_detected")
        return encoded

    def validate_for_transport(
        self, payload: object, private_literals: tuple[str, ...] = (), *,
        private_amounts: tuple[int, ...] = (),
    ) -> ValidatedAnonymousBytes:
        """Issue the only supported request wrapper for a future transport."""
        return ValidatedAnonymousBytes(
            self.serialize(payload, private_literals, private_amounts=private_amounts),
            _TRANSPORT_CAPABILITY,
        )

    def _validate(self, payload: object) -> None:
        if type(payload) is not dict or set(payload) != {"schema_version", "unit_ref", "mode", "state", "structure_state", "competing_structure", "regions", "relations"}:
            raise OutboundRejected("forbidden_or_unknown_field")
        if (type(payload["schema_version"]) is not str or type(payload["mode"]) is not str
                or type(payload["state"]) is not str or payload["schema_version"] != _VERSION
                or payload["mode"] != "shadow_only" or payload["state"] != "shadow_ready"
                or type(payload["unit_ref"]) is not str
                or not _UNIT_REF.fullmatch(payload["unit_ref"])):
            raise OutboundRejected("invalid_payload_header")
        if (type(payload["structure_state"]) is not str or payload["structure_state"] != "unresolved"
                or type(payload["competing_structure"]) is not bool):
            raise OutboundRejected("invalid_structure_state")
        regions = payload["regions"]
        relations = payload["relations"]
        if (type(regions) is not list or not 1 <= len(regions) <= _MAX_REGIONS
                or type(relations) is not list or len(relations) > len(regions) * (len(regions) - 1) // 2):
            raise OutboundRejected("invalid_payload_collections")
        ids: set[str] = set()
        amount_ids: set[str] = set()
        for index, region in enumerate(regions):
            if type(region) is not dict or set(region) != {"id", "kind", "context", "amount_id", "geometry", "confidence", "status"}:
                raise OutboundRejected("forbidden_or_unknown_field")
            identifier = region["id"]
            if (type(identifier) is not str or identifier != f"region_{_alpha(index)}"
                    or not _REGION_ID.fullmatch(identifier) or identifier in ids):
                raise OutboundRejected("invalid_region_id")
            ids.add(identifier)
            if (type(region["kind"]) is not str or type(region["context"]) is not str
                    or type(region["confidence"]) is not str or type(region["status"]) is not str
                    or region["kind"] not in {"numeric_evidence", "context_anchor"}
                    or region["context"] not in {"payment", "excluded"}
                    or region["confidence"] not in {"high", "medium", "low"}
                    or region["status"] != "observed"):
                raise OutboundRejected("invalid_semantic_category")
            amount_id = region["amount_id"]
            if region["kind"] == "numeric_evidence":
                if (type(amount_id) is not str or amount_id != f"amount_{_alpha(len(amount_ids))}"
                        or not _AMOUNT_ID.fullmatch(amount_id) or amount_id in amount_ids):
                    raise OutboundRejected("invalid_amount_id")
                amount_ids.add(amount_id)
            elif amount_id is not None:
                raise OutboundRejected("invalid_anchor")
            geometry = region["geometry"]
            if type(geometry) is not dict or set(geometry) != {"x", "y", "width", "height"}:
                raise OutboundRejected("invalid_geometry")
            if not all(type(value) is float and math.isfinite(value) and 0 <= value <= 1 for value in geometry.values()):
                raise OutboundRejected("invalid_geometry")
        seen_relations: set[tuple[str, str, str]] = set()
        for relation in relations:
            if type(relation) is not dict or set(relation) != {"left", "right", "kind"}:
                raise OutboundRejected("forbidden_or_unknown_field")
            if (type(relation["left"]) is not str or type(relation["right"]) is not str
                    or type(relation["kind"]) is not str
                    or relation["left"] not in ids or relation["right"] not in ids
                    or relation["left"] == relation["right"]
                    or relation["kind"] not in {"row", "column", "overlap"}):
                raise OutboundRejected("invalid_relation")
            key = (relation["left"], relation["right"], relation["kind"])
            if key in seen_relations:
                raise OutboundRejected("duplicate_relation")
            seen_relations.add(key)
        if not amount_ids:
            raise OutboundRejected("amount_not_observed")


@dataclass(frozen=True)
class GeminiFreeTierShadowPolicy:
    """A stop switch with no paid mode and no transport implementation."""

    enabled: bool = False
    tier: Literal["free"] = "free"

    def prepare(self, build: AnonymousShadowBuild, observation: OcrObservation) -> bytes:
        if type(self.enabled) is not bool or self.tier != "free" or not self.enabled:
            raise OutboundRejected("free_tier_route_disabled")
        if type(build) is not AnonymousShadowBuild or type(observation) is not OcrObservation:
            raise OutboundRejected("invalid_shadow_handoff")
        if type(build._source_fingerprint) is not str or type(build._unit_ref) is not str:
            raise OutboundRejected("invalid_shadow_handoff")
        if not secrets.compare_digest(build._source_fingerprint, _observation_fingerprint(observation)):
            raise OutboundRejected("shadow_source_mismatch")
        if type(build.payload) is not dict or build.payload.get("unit_ref") != build._unit_ref:
            raise OutboundRejected("shadow_unit_mismatch")
        return FinalOutboundGate().serialize(
            build.payload, _private_literals(observation, build.amount_map),
            private_amounts=tuple(value for _, value in build.amount_map._values),
        )


@dataclass(frozen=True)
class MedicalAnonymousShadowTransportPolicy:
    """Dedicated default-false gate and fixed Free-Tier transport constraints.

    ``from_setting`` deliberately parses an explicit local setting without reading
    environment variables.  A future caller must check this policy before key
    lookup, outbound preparation, or transport invocation.
    """

    enabled: bool = False
    model: str = _FREE_TIER_MODEL
    allow_tools: bool = False
    allow_grounding: bool = False
    allow_caching: bool = False
    allow_batch: bool = False
    allow_priority: bool = False
    allow_retry: bool = False
    allow_fallback: bool = False

    @classmethod
    def from_setting(cls, value: object | None) -> "MedicalAnonymousShadowTransportPolicy":
        # Only this exact opt-in spelling enables the isolated shadow route.
        return cls(enabled=value == "true")

    @property
    def transport_enabled(self) -> bool:
        return (
            type(self.enabled) is bool and self.enabled
            and type(self.model) is str and self.model == _FREE_TIER_MODEL
            and all(type(value) is bool and not value for value in (
                self.allow_tools, self.allow_grounding, self.allow_caching,
                self.allow_batch, self.allow_priority, self.allow_retry,
                self.allow_fallback,
            ))
        )


def _validated_request_for_transport(
    build: AnonymousShadowBuild, observation: OcrObservation,
) -> ValidatedAnonymousBytes:
    """Bind source locally, then wrap bytes issued by the final outbound gate."""
    if type(build) is not AnonymousShadowBuild or type(observation) is not OcrObservation:
        raise OutboundRejected("invalid_shadow_handoff")
    if (type(build._source_fingerprint) is not str or type(build._unit_ref) is not str
            or not secrets.compare_digest(build._source_fingerprint, _observation_fingerprint(observation))
            or type(build.payload) is not dict or build.payload.get("unit_ref") != build._unit_ref):
        raise OutboundRejected("invalid_shadow_handoff")
    return FinalOutboundGate().validate_for_transport(
        build.payload, _private_literals(observation, build.amount_map),
        private_amounts=tuple(value for _, value in build.amount_map._values),
    )


def _response_binding(build: AnonymousShadowBuild) -> str:
    """Local request/response binding; its digest is never put on either wire."""
    try:
        if (type(build) is not AnonymousShadowBuild or type(build.payload) is not dict
                or type(build.amount_map) is not LocalAmountMap
                or type(build._source_fingerprint) is not str or type(build._unit_ref) is not str
                or type(build._response_private_literals) is not tuple
                or not all(type(value) is str for value in build._response_private_literals)):
            raise InboundRejected("invalid_request_binding")
        FinalOutboundGate()._validate(build.payload)
        if build.payload["unit_ref"] != build._unit_ref:
            raise InboundRejected("invalid_request_binding")
        wire_ids = tuple(region["amount_id"] for region in build.payload["regions"]
                         if region["kind"] == "numeric_evidence")
        if wire_ids != build.amount_map.ids:
            raise InboundRejected("invalid_request_binding")
        digest = hashlib.sha256()
        digest.update(build._source_fingerprint.encode("ascii"))
        digest.update(build._unit_ref.encode("ascii"))
        digest.update(json.dumps(build.payload, ensure_ascii=True, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("ascii"))
        return digest.hexdigest()
    except InboundRejected:
        raise
    except Exception as error:
        raise InboundRejected("invalid_request_binding") from error


def _reject_response_literal(text: str, private_literals: tuple[str, ...]) -> None:
    for literal in private_literals:
        # Scan both literal UTF-8 and JSON-escaped form before parsing. The schema
        # has no free-text destination, so this is defense in depth for regressions.
        escaped = json.dumps(literal, ensure_ascii=True)[1:-1]
        if literal and (literal in text or escaped in text):
            raise InboundRejected("private_response_literal")


def _strict_response_object(pairs: list[tuple[object, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise InboundRejected("duplicate_or_invalid_response_key")
        result[key] = value
    return result


class FinalInboundGate:
    """Strict synthetic-response boundary. It performs no network operation."""

    def accept(self, build: AnonymousShadowBuild, response_bytes: object) -> AcceptedAnonymousResponse:
        binding = _response_binding(build)
        if type(response_bytes) is not bytes or not 0 < len(response_bytes) <= _MAX_RESPONSE_BYTES:
            raise InboundRejected("malformed_response")
        try:
            text = response_bytes.decode("utf-8")
            _reject_response_literal(text, build._response_private_literals)
            _reject_numeric_amount_surface(
                text, tuple(value for _, value in build.amount_map._values), InboundRejected,
                "private_response_numeric_literal",
            )
            parsed = json.loads(text, object_pairs_hook=_strict_response_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except InboundRejected:
            raise
        except Exception as error:
            raise InboundRejected("malformed_response") from error
        if type(parsed) is not dict or set(parsed) != {
            "schema_version", "unit_ref", "decision", "amount_id", "confidence",
        }:
            raise InboundRejected("forbidden_or_unknown_response_field")
        if (type(parsed["schema_version"]) is not str or parsed["schema_version"] != _RESPONSE_VERSION
                or type(parsed["unit_ref"]) is not str or parsed["unit_ref"] != build._unit_ref
                or not _UNIT_REF.fullmatch(parsed["unit_ref"])
                or type(parsed["decision"]) is not str
                or parsed["decision"] not in {"select", "abstain", "unresolved"}):
            raise InboundRejected("invalid_response_header_or_binding")
        decision = parsed["decision"]
        amount_id = parsed["amount_id"]
        confidence = parsed["confidence"]
        if decision == "select":
            if (type(amount_id) is not str or not _AMOUNT_ID.fullmatch(amount_id)
                    or amount_id not in build.amount_map.ids
                    or type(confidence) is not str or confidence not in {"high", "medium", "low"}):
                raise InboundRejected("invalid_selection")
        elif amount_id is not None or confidence is not None:
            raise InboundRejected("conflicting_response")
        return AcceptedAnonymousResponse(decision, amount_id, confidence, binding)

    def accept_transport_validated(
        self, build: AnonymousShadowBuild, response: object,
    ) -> AcceptedAnonymousResponse:
        if type(response) is not ValidatedTransportResponse:
            raise InboundRejected("invalid_transport_response")
        return self.accept(build, response._for_inbound_gate())


class TransportResponseGate:
    """Format, privacy, and whole-JSON boundary before semantic response parsing."""

    def validate(self, build: AnonymousShadowBuild, response: object) -> ValidatedTransportResponse:
        try:
            _response_binding(build)
            if type(response) is not TransportResponse or response.status != "ok":
                raise TransportResponseRejected("transport_error")
            if type(response.content_type) is not str or response.content_type not in _JSON_CONTENT_TYPES:
                raise TransportResponseRejected("invalid_content_type")
            if type(response.body) is not bytes:
                raise TransportResponseRejected("malformed_response")
            if not 0 < len(response.body) <= _MAX_RESPONSE_BYTES:
                raise TransportResponseRejected("response_too_large")
            try:
                text = response.body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise TransportResponseRejected("invalid_utf8") from error
            try:
                _reject_response_literal(text, build._response_private_literals)
                _reject_numeric_amount_surface(
                    text, tuple(value for _, value in build.amount_map._values), InboundRejected,
                    "private_response_numeric_literal",
                )
            except InboundRejected as error:
                raise TransportResponseRejected("privacy_rejected") from error
            try:
                # json.loads consumes one complete JSON document; fencing, prefixes,
                # suffixes, and trailing garbage fail here. Duplicate keys fail too.
                json.loads(text, object_pairs_hook=_strict_response_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except InboundRejected as error:
                raise TransportResponseRejected("malformed_response") from error
            except Exception as error:
                raise TransportResponseRejected("malformed_response") from error
            return ValidatedTransportResponse(response.body, _TRANSPORT_CAPABILITY)
        except TransportResponseRejected:
            raise
        except InboundRejected as error:
            raise TransportResponseRejected("binding_rejected") from error
        except Exception as error:
            raise TransportResponseRejected("validation_rejected") from error


def run_fake_shadow_transport(
    build: AnonymousShadowBuild,
    observation: OcrObservation,
    transport: AnonymousShadowTransport,
    policy: MedicalAnonymousShadowTransportPolicy = MedicalAnonymousShadowTransportPolicy(),
) -> InboundShadowResult:
    """Synthetic-only one-shot orchestration; this function never performs I/O.

    It deliberately checks the dedicated kill switch before source binding and
    byte preparation.  It does not know keys, URLs, headers, models beyond policy,
    retry, fallback, or any production component.
    """
    if type(policy) is not MedicalAnonymousShadowTransportPolicy or not policy.transport_enabled:
        return InboundShadowResult("needs_review", "disabled")
    try:
        request = _validated_request_for_transport(build, observation)
    except Exception:
        return InboundShadowResult("needs_review", "validation_rejected")
    try:
        # Exactly one call site and one invocation: no retry or fallback.
        response = transport.send(request)
    except Exception:
        return InboundShadowResult("needs_review", "transport_error")
    if type(response) is not TransportResponse:
        return InboundShadowResult("needs_review", "transport_error")
    if response.status != "ok":
        failures = {
            "timeout": "timeout", "quota": "quota", "authentication": "authentication",
            "unavailable": "unavailable", "transport_error": "transport_error",
        }
        return InboundShadowResult("needs_review", failures.get(response.status, "transport_error"))
    try:
        checked = TransportResponseGate().validate(build, response)
        accepted = FinalInboundGate().accept_transport_validated(build, checked)
        return InboundShadowResult("needs_review", "accepted_shadow_response",
                                   rehydrate_anonymous_response(build, accepted))
    except TransportResponseRejected as error:
        return InboundShadowResult("needs_review", str(error))
    except InboundRejected as error:
        code = "binding_rejected" if "binding" in str(error) else "validation_rejected"
        return InboundShadowResult("needs_review", code)
    except Exception:
        return InboundShadowResult("needs_review", "validation_rejected")


def rehydrate_anonymous_response(
    build: AnonymousShadowBuild, response: AcceptedAnonymousResponse,
) -> LocalResponseResult:
    """Resolve an amount only after a complete, bound inbound validation."""
    binding = _response_binding(build)
    if (type(response) is not AcceptedAnonymousResponse or type(response._binding) is not str
            or not secrets.compare_digest(response._binding, binding)):
        raise InboundRejected("invalid_response_binding")
    if response.decision == "select":
        if (type(response.amount_id) is not str or response.amount_id not in build.amount_map.ids
                or response.confidence not in {"high", "medium", "low"}):
            raise InboundRejected("invalid_selection")
        # This is the only inbound call site of LocalAmountMap.resolve. The output
        # remains needs_review and is not passed to the production resolver.
        return LocalResponseResult("needs_review", response.decision,
                                   build.amount_map.resolve(response.amount_id))
    if response.amount_id is not None or response.confidence is not None:
        raise InboundRejected("conflicting_response")
    return LocalResponseResult("needs_review", response.decision, None)


def receive_synthetic_shadow_response(
    build: AnonymousShadowBuild,
    response_bytes: bytes | None = None,
    *,
    synthetic_failure: Literal["timeout", "quota_failure", "api_failure", "parser_failure"] | None = None,
) -> InboundShadowResult:
    """Synthetic-only convenience boundary; every failure is data-free review."""
    failure_codes = {
        "timeout": "synthetic_timeout",
        "quota_failure": "synthetic_quota_failure",
        "api_failure": "synthetic_api_failure",
        "parser_failure": "synthetic_parser_failure",
    }
    if synthetic_failure is not None:
        return InboundShadowResult("needs_review", failure_codes.get(synthetic_failure, "malformed_response"))
    try:
        accepted = FinalInboundGate().accept(build, response_bytes)
        return InboundShadowResult("needs_review", "accepted_shadow_response",
                                   rehydrate_anonymous_response(build, accepted))
    except Exception:
        # Do not expose parser, provider, OCR, response, or local amount details.
        return InboundShadowResult("needs_review", "malformed_response")
