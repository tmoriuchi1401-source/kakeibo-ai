"""Minimal v2 medical shadow boundary; live I/O requires explicit injection.

The input observation is private local OCR evidence.  The only value that may
cross the prospective Gemini boundary is the JSON returned by
``FinalOutboundGate.serialize``.  That gate deliberately accepts an exact,
closed schema rather than arbitrary mappings, prompts, metadata, or OCR DTOs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import secrets
import socket
import string
import threading
import unicodedata
from typing import Callable, Literal, Protocol
from urllib.error import HTTPError
from urllib.request import (
    Request as UrlRequest, OpenerDirector, HTTPSHandler, HTTPRedirectHandler,
    HTTPDefaultErrorHandler, HTTPErrorProcessor,
)

from .medical_ocr_observation_shadow import OcrObservation
from .medical_payment_evidence import _NUMERIC_RUN, _scope
from .medical_receipt_privacy import _compact_ocr_token, _payment_labels_on_line, _structured_amount


_VERSION = "medical-anonymous-shadow-v2"
_RESPONSE_VERSION = "medical-anonymous-shadow-response-v2"
_UNIT_REF = re.compile(r"unit_[A-Za-z]{32}\Z")
_MAX_CANDIDATES = 4
_AMOUNT_ID = re.compile(r"candidate_[A-Za-z]{32}\Z")
_MAX_REGIONS = 128
_MAX_RESPONSE_BYTES = 4096
_MAX_ENVELOPE_BYTES = 32768
_MAX_USED_REQUESTS = 4096
_NUMERIC_SURFACE = re.compile(r"(?<![A-Za-z0-9])[0-9０-９][0-9０-９,，.．\s]*")
_TRANSPORT_CAPABILITY = object()
_FREE_TIER_MODEL = "gemini-3.1-flash-lite"
_HTTP_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_GEMINI_GENERATE_CONTENT_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.1-flash-lite:generateContent"
)
_HTTP_TIMEOUT_SECONDS = 10
_STATIC_SHADOW_INSTRUCTION = (
    "Return only one JSON object matching the supplied response schema. "
    "Use only the anonymous JSON evidence in the adjacent part. "
    "Candidate IDs carry no rank, confidence, geometry, or comparative evidence. "
    "If there is exactly one candidate, select it or abstain. If there are multiple "
    "candidates, return abstain or unresolved with null amount_id and confidence. "
    "For select, put the candidate_id in amount_id. Echo the unit_ref. "
    "Do not include explanation, markdown, reasoning, metadata, or extra fields."
)
_SHADOW_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "unit_ref", "decision", "amount_id", "confidence"],
    "properties": {
        "schema_version": {"type": "string", "enum": [_RESPONSE_VERSION]},
        "unit_ref": {"type": "string"},
        "decision": {"type": "string", "enum": ["select", "abstain", "unresolved"]},
        "amount_id": {"type": ["string", "null"]},
        "confidence": {"type": ["string", "null"]},
    },
}


class OutboundRejected(ValueError):
    """A fixed, data-free failure for unsafe local-to-provider handoff."""


class InboundRejected(ValueError):
    """A fixed, data-free failure for unsafe provider-response handoff."""


class TransportResponseRejected(ValueError):
    """A fixed, data-free failure before semantic inbound parsing."""


class _ApiKeyHandle:
    """Private transport-only secret holder; it deliberately has no useful repr."""

    __slots__ = ("__value",)

    def __init__(self, value: str, token: object) -> None:
        if token is not _TRANSPORT_CAPABILITY or type(value) is not str or not value:
            raise TypeError("api_key_required")
        self.__value = value

    def __repr__(self) -> str:
        return "ApiKeyHandle()"

    def _for_header(self) -> str:
        return self.__value


class _SingleUse:
    """Shared by copied wrappers; claiming is atomic and is never undone."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used = False

    def claim(self) -> bool:
        with self._lock:
            if self._used:
                return False
            self._used = True
            return True

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        raise TypeError("capability_not_serializable")

    def __repr__(self) -> str:
        return "SingleUse()"


class _ReplayRegistry:
    """Process-local, bounded, non-evicting replay ledger. Full means fail closed."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._units: set[str] = set()
        self._sources: set[str] = set()

    def claim(self, unit: str, source: str | None) -> bool:
        with self._lock:
            if (unit in self._units or (source is not None and source in self._sources)
                    or len(self._units) >= _MAX_USED_REQUESTS):
                return False
            self._units.add(unit)
            if source is not None:
                self._sources.add(source)
            return True


_REPLAY_REGISTRY = _ReplayRegistry()


class ValidatedAnonymousBytes:
    """Gate-issued v2 bytes with shared, local-only send/HTTP capabilities.

    This prevents accidental reuse, not hostile introspection in the same Python
    process. Copies share guards. Rebuilding the same source is also refused by
    the process-local replay ledger after its first attempted send.
    """
    __slots__ = ("__bytes", "__source", "__send_use", "__http_use")

    def __init__(self, value: bytes, token: object, *, source: str | None = None,
                 send_use: _SingleUse | None = None, http_use: _SingleUse | None = None) -> None:
        if token is not _TRANSPORT_CAPABILITY or type(value) is not bytes:
            raise TypeError("validated_anonymous_bytes_required")
        self.__bytes = value
        self.__source = source
        self.__send_use = send_use if send_use is not None else _SingleUse()
        self.__http_use = http_use if http_use is not None else _SingleUse()

    def __repr__(self) -> str:
        return "ValidatedAnonymousBytes()"

    def __reduce__(self):
        raise TypeError("capability_not_serializable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def _for_transport(self) -> bytes:
        return self.__bytes

    def _http_guard(self) -> _SingleUse:
        return self.__http_use

    def _claim_send(self) -> bool:
        payload = _strict_json_loads(self.__bytes)
        return (self.__send_use.claim()
                and _REPLAY_REGISTRY.claim(payload["unit_ref"], self.__source))

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

    status: Literal[
        "ok", "timeout", "quota", "authentication", "unavailable", "transport_error",
        "malformed_response", "invalid_content_type", "response_too_large",
        "invalid_utf8", "redirect_rejected", "request_reused", "validation_rejected",
    ]
    content_type: str | None = field(default=None, repr=False)
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
        if not request._claim_send():
            return TransportResponse("request_reused")
        self.invocations += 1
        self.received.append(request)
        if self.failure is not None:
            raise self.failure
        if type(self.response) is not TransportResponse:
            raise RuntimeError("synthetic_transport_response_missing")
        return self.response


@dataclass(frozen=True)
class HttpRequest:
    """Minimal REST request; URL, headers, and body never appear in repr."""

    method: Literal["POST"]
    url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)
    timeout_seconds: int
    _attempt: _SingleUse = field(default_factory=_SingleUse, repr=False, compare=False)


@dataclass(frozen=True)
class HttpResponse:
    """Minimal executor output; no response headers beyond Content-Type survive."""

    status_code: int
    content_type: str | None = field(default=None, repr=False)
    body: bytes = field(default=b"", repr=False)
    failure: str | None = field(default=None, repr=False)


class HttpExecutor(Protocol):
    """Explicitly injected execution boundary. Tests use a fake, never sockets."""

    def execute(self, request: HttpRequest) -> HttpResponse: ...


class ApiKeyProvider(Protocol):
    """Transport-only source for a secret; builders and gates never receive it."""

    def get(self) -> str | None: ...


@dataclass(frozen=True)
class EnvironmentMedicalShadowApiKeyProvider:
    """Lazy environment boundary. Nothing reads it until transport preflight."""

    getenv: Callable[[str], str | None] = field(default=os.getenv, repr=False)
    variable_name: str = field(default="MEDICAL_GEMINI_SHADOW_API_KEY", init=False, repr=False)

    def get(self) -> str | None:
        value = self.getenv(self.variable_name)
        return value if type(value) is str and value.strip() else None


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Never constructs a redirected request or passes credentials to a new URL."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect_rejected", {}, None)

    def http_error_302(self, req, fp, code, msg, headers):
        # Do not read Location, resolve a URL, drain a body, or invoke parent.open.
        raise HTTPError(req.full_url, code, "redirect_rejected", {}, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _isolated_http_opener():
    # Explicit handlers: no global opener, proxy, auth retry, cookies or HTTP
    # downgrade. HTTPSHandler performs one HTTP request; redirects are rejected.
    opener = OpenerDirector()
    for handler in (HTTPSHandler(debuglevel=0), _RejectRedirectHandler(),
                    HTTPDefaultErrorHandler(), HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


class UrllibHttpExecutor:
    """At most one HTTP attempt per request capability, with bounded reads.

    A failed attempt consumes the capability. No redirect, proxy/auth handler,
    retry or fallback can initiate another HTTP request. DNS/TCP address probes
    are not additional HTTP requests. Only explicit callers create this executor.
    """
    def execute(self, request: HttpRequest) -> HttpResponse:
        if (type(request) is not HttpRequest or request.method != "POST"
                or request.url != _GEMINI_GENERATE_CONTENT_URL
                or request.timeout_seconds != _HTTP_TIMEOUT_SECONDS):
            return HttpResponse(0, failure="transport_error")
        if not request._attempt.claim():
            return HttpResponse(0, failure="request_reused")
        try:
            wire_request = UrlRequest(request.url, data=request.body, method="POST",
                                      headers=dict(request.headers))
            with _isolated_http_opener().open(wire_request, timeout=request.timeout_seconds) as handle:
                status = handle.status
                if type(status) is not int or not 200 <= status < 300:
                    return HttpResponse(status if type(status) is int else 0)
                content_types = handle.headers.get_all("Content-Type", [])
                if (len(content_types) != 1
                        or not _is_allowed_json_content_type(content_types[0])):
                    return HttpResponse(status, failure="invalid_content_type")
                # No compression decoder (nor decompression bomb) on this route.
                encodings = handle.headers.get_all("Content-Encoding", [])
                if encodings and encodings != ["identity"]:
                    return HttpResponse(status, failure="malformed_response")
                body = handle.read(_MAX_ENVELOPE_BYTES + 1)
                if type(body) is not bytes or len(body) > _MAX_ENVELOPE_BYTES:
                    return HttpResponse(status, failure="response_too_large")
                return HttpResponse(status, "application/json", body)
        except HTTPError as error:
            status = error.code
            try:
                error.close()  # Never read/log the error body or its raw headers.
            except Exception:
                pass
            return HttpResponse(status if type(status) is int else 0)
        except (TimeoutError, socket.timeout):
            return HttpResponse(0, failure="timeout")
        except Exception:
            return HttpResponse(0, failure="transport_error")

def _strict_json_loads(raw: bytes) -> object:
    """Decode provider wrapper strictly without retaining it outside transport."""
    if type(raw) is not bytes:
        raise ValueError()
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_response_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _is_allowed_json_content_type(value: object) -> bool:
    """Accept only semantic ``application/json`` with an optional UTF-8 charset.

    HTTP media types and parameter names are case-insensitive.  This deliberately
    does not use prefix matching or accept arbitrary extension parameters: every
    present parameter must be the sole ``charset`` parameter and must normalize
    to UTF-8.  Invalid syntax, duplicate parameters, quoted escapes, and header
    control characters therefore fail closed without exposing the input value.
    """
    if type(value) is not str or "\r" in value or "\n" in value:
        return False
    if len(value) > 1024 or not value.isascii():
        return False
    parts = value.split(";")
    media_type = parts[0].strip(" \t")
    if media_type.casefold() != "application/json":
        return False
    seen: set[str] = set()
    for raw_parameter in parts[1:]:
        parameter = raw_parameter.strip(" \t")
        if not parameter or parameter.count("=") != 1:
            return False
        raw_name, raw_value = parameter.split("=", 1)
        name = raw_name.strip(" \t").casefold()
        parameter_value = raw_value.strip(" \t")
        if not _HTTP_TOKEN.fullmatch(raw_name.strip(" \t")) or not parameter_value:
            return False
        if parameter_value.startswith('"') or parameter_value.endswith('"'):
            if (len(parameter_value) < 2 or not parameter_value.startswith('"')
                    or not parameter_value.endswith('"')):
                return False
            parameter_value = parameter_value[1:-1]
            if "\\" in parameter_value or '"' in parameter_value:
                return False
        elif not _HTTP_TOKEN.fullmatch(parameter_value):
            return False
        if name in seen or name != "charset" or parameter_value.casefold() != "utf-8":
            return False
        seen.add(name)
    return True


def _extract_gemini_response_text(raw: bytes) -> bytes:
    """Extract only the structured text candidate; discard all provider metadata."""
    try:
        parsed = _strict_json_loads(raw)
        if type(parsed) is not dict:
            raise ValueError()
        candidates = parsed.get("candidates")
        if type(candidates) is not list or len(candidates) != 1 or type(candidates[0]) is not dict:
            raise ValueError()
        content = candidates[0].get("content")
        if type(content) is not dict:
            raise ValueError()
        parts = content.get("parts")
        if (type(parts) is not list or len(parts) != 1 or type(parts[0]) is not dict
                or set(parts[0]) != {"text"}):
            raise ValueError()
        text = parts[0].get("text")
        if type(text) is not str:
            raise ValueError()
        return text.encode("utf-8")
    except Exception as error:
        raise ValueError("invalid_provider_response") from None


class MedicalGeminiShadowTransport:
    """Explicit-injection REST transport; it is not wired to any production path."""

    def __init__(self, executor: HttpExecutor, key_provider: ApiKeyProvider) -> None:
        self._executor = executor
        self._key_provider = key_provider

    def acquire_api_key(self) -> _ApiKeyHandle | None:
        try:
            value = self._key_provider.get()
        except Exception:
            return None
        if type(value) is not str or not value.strip():
            return None
        return _ApiKeyHandle(value, _TRANSPORT_CAPABILITY)

    def build_request(self, request: ValidatedAnonymousBytes, key: _ApiKeyHandle) -> HttpRequest:
        if type(request) is not ValidatedAnonymousBytes or type(key) is not _ApiKeyHandle:
            raise TypeError("validated_anonymous_bytes_and_api_key_required")
        wire = request._for_transport()
        try:
            payload = _strict_json_loads(wire)
            FinalOutboundGate()._validate(payload)
            if FinalOutboundGate().serialize(payload) != wire:
                raise ValueError()
        except Exception as error:
            raise OutboundRejected("invalid_validated_anonymous_bytes") from None
        # Only constant protocol fields surround the prevalidated anonymous bytes.
        body = json.dumps({
            "contents": [{"role": "user", "parts": [
                {"text": _STATIC_SHADOW_INSTRUCTION}, {"text": wire.decode("ascii")},
            ]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": _SHADOW_RESPONSE_SCHEMA,
                "candidateCount": 1,
                "temperature": 0,
            },
        }, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        return HttpRequest(
            "POST", _GEMINI_GENERATE_CONTENT_URL,
            (("Content-Type", "application/json"), ("x-goog-api-key", key._for_header())),
            body, _HTTP_TIMEOUT_SECONDS, request._http_guard(),
        )

    def send(self, request: ValidatedAnonymousBytes, key: _ApiKeyHandle) -> TransportResponse:
        if type(request) is not ValidatedAnonymousBytes or type(key) is not _ApiKeyHandle:
            return TransportResponse("validation_rejected")
        try:
            http_request = self.build_request(request, key)
            if not request._claim_send():
                return TransportResponse("request_reused")
            http_response = self._executor.execute(http_request)
        except (TimeoutError, socket.timeout):
            return TransportResponse("timeout")
        except Exception:
            return TransportResponse("transport_error")
        if type(http_response) is not HttpResponse or type(http_response.status_code) is not int:
            return TransportResponse("transport_error")
        status = http_response.status_code
        if 300 <= status < 400:
            return TransportResponse("redirect_rejected")
        if status in {401, 403}:
            return TransportResponse("authentication")
        if status == 404:
            return TransportResponse("unavailable")
        if status == 429:
            return TransportResponse("quota")
        if http_response.failure is not None:
            allowed = {"invalid_content_type", "response_too_large", "timeout",
                       "transport_error", "request_reused", "malformed_response"}
            return TransportResponse(http_response.failure if http_response.failure in allowed
                                     else "transport_error")
        if not 200 <= status < 300:
            return TransportResponse("unavailable" if status >= 500 else "transport_error")
        # Recheck injected executors before any envelope semantic parsing.
        if not _is_allowed_json_content_type(http_response.content_type):
            return TransportResponse("invalid_content_type")
        if type(http_response.body) is not bytes or len(http_response.body) > _MAX_ENVELOPE_BYTES:
            return TransportResponse("response_too_large")
        try:
            http_response.body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return TransportResponse("invalid_utf8")
        try:
            candidate = _extract_gemini_response_text(http_response.body)
        except Exception:
            return TransportResponse("malformed_response")
        if not 0 < len(candidate) <= _MAX_RESPONSE_BYTES:
            return TransportResponse("response_too_large")
        return TransportResponse("ok", "application/json", candidate)

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
    _integrity: str = field(repr=False)
    _send_use: _SingleUse = field(default_factory=_SingleUse, repr=False, compare=False)
    _http_use: _SingleUse = field(default_factory=_SingleUse, repr=False, compare=False)


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
        "redirect_rejected", "request_reused",
        "privacy_rejected", "binding_rejected", "validation_rejected",
    ]
    local_result: LocalResponseResult | None = field(default=None, repr=False)


def _opaque_id(prefix: str) -> str:
    # Letter-only CSPRNG IDs avoid accidental concrete amount digit surfaces.
    return prefix + "".join(secrets.choice(string.ascii_letters) for _ in range(32))

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


def _build_integrity(payload: dict, amount_map: LocalAmountMap, fingerprint: str,
                     literals: tuple[str, ...]) -> str:
    # Seals mutable payload, private correspondence AND literal scan context.
    raw = json.dumps([payload, amount_map._values, fingerprint, literals],
                     ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def build_anonymous_shadow_payload(observation: OcrObservation) -> AnonymousShadowBuild:
    """Build v2 from explicit payment candidates; every other field stays local.

    No layout calculation: excluded/unrelated/anchor regions cannot influence the
    wire representation. Potential-payment numeric ambiguity is withheld locally.
    A candidate must have complete, high-confidence, single-amount evidence.
    Nothing here resolves a production payment or authorizes communication.
    """
    if (type(observation) is not OcrObservation or observation.issues
            or type(observation.page) is not int or observation.page < 1
            or not observation.regions or len(observation.regions) > _MAX_REGIONS):
        raise OutboundRejected("observation_incomplete")
    values = []
    for region in observation.regions:
        scope = _local_scope(region.text)
        runs = tuple(_NUMERIC_RUN.finditer(unicodedata.normalize("NFKC", region.text)))
        if scope == "possible_payment_region" and runs:
            raise OutboundRejected("ambiguous_semantic_context")
        # Anchors and excluded/unassigned regions never contribute evidence.
        if scope != "payment_region" or not runs:
            continue
        if (region.issues or region.confidence is None or region.confidence < .9
                or region.bbox is None):
            raise OutboundRejected("candidate_quality_insufficient")
        if len(runs) != 1:
            raise OutboundRejected("ambiguous_numeric_context")
        amount = _structured_amount(runs[0].group().strip())
        if amount is None:
            raise OutboundRejected("malformed_numeric")
        values.append(amount)
    if not 1 <= len(values) <= _MAX_CANDIDATES:
        raise OutboundRejected("candidate_count_out_of_bounds")
    # Canonicalize only locally, then CSPRNG shuffle. No scan-order mapping is
    # exported. No duplicate-value flag or value-based deduplication is exported.
    values.sort()
    secrets.SystemRandom().shuffle(values)
    ids = [_opaque_id("candidate_") for _ in values]
    if len(set(ids)) != len(ids):
        raise OutboundRejected("random_id_collision")
    unit_ref = _opaque_id("unit_")
    payload = {"schema_version": _VERSION, "unit_ref": unit_ref,
               "candidates": [{"candidate_id": identifier} for identifier in ids]}
    amount_map = LocalAmountMap(tuple(zip(ids, values)))
    literals = _private_literals(observation, amount_map)
    fingerprint = _observation_fingerprint(observation)
    FinalOutboundGate().serialize(payload, literals,
                                  private_amounts=tuple(values))
    return AnonymousShadowBuild(payload, amount_map, fingerprint, unit_ref, literals,
                                _build_integrity(payload, amount_map, fingerprint, literals))

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
        _source: str | None = None, _send_use: _SingleUse | None = None,
        _http_use: _SingleUse | None = None,
    ) -> ValidatedAnonymousBytes:
        """Issue the only supported request wrapper for a future transport."""
        return ValidatedAnonymousBytes(
            self.serialize(payload, private_literals, private_amounts=private_amounts),
            _TRANSPORT_CAPABILITY, source=_source, send_use=_send_use, http_use=_http_use,
        )

    def _validate(self, payload: object) -> None:
        if type(payload) is not dict or set(payload) != {"schema_version", "unit_ref", "candidates"}:
            raise OutboundRejected("forbidden_or_unknown_field")
        if (type(payload["schema_version"]) is not str or payload["schema_version"] != _VERSION
                or type(payload["unit_ref"]) is not str or not _UNIT_REF.fullmatch(payload["unit_ref"])):
            raise OutboundRejected("invalid_payload_header")
        candidates = payload["candidates"]
        if type(candidates) is not list or not 1 <= len(candidates) <= _MAX_CANDIDATES:
            raise OutboundRejected("candidate_count_out_of_bounds")
        seen = set()
        for candidate in candidates:
            if type(candidate) is not dict or set(candidate) != {"candidate_id"}:
                raise OutboundRejected("forbidden_or_unknown_field")
            identifier = candidate["candidate_id"]
            if type(identifier) is not str or not _AMOUNT_ID.fullmatch(identifier) or identifier in seen:
                raise OutboundRejected("invalid_candidate_id")
            seen.add(identifier)

@dataclass(frozen=True)
class GeminiFreeTierShadowPolicy:
    """A stop switch with no paid mode and no transport implementation."""

    enabled: bool = False
    tier: Literal["free"] = "free"

    def prepare(self, build: AnonymousShadowBuild, observation: OcrObservation) -> bytes:
        if type(self.enabled) is not bool or self.tier != "free" or not self.enabled:
            raise OutboundRejected("free_tier_route_disabled")
        return _validated_request_for_transport(build, observation)._for_transport()

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
        return cls(enabled=type(value) is str and value == "true")

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
    try:
        _response_binding(build)
    except Exception:
        raise OutboundRejected("invalid_shadow_handoff") from None
    return FinalOutboundGate().validate_for_transport(
        build.payload, _private_literals(observation, build.amount_map),
        private_amounts=tuple(value for _, value in build.amount_map._values),
        _source=build._source_fingerprint, _send_use=build._send_use, _http_use=build._http_use,
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
        wire_ids = tuple(candidate["candidate_id"] for candidate in build.payload["candidates"])
        if wire_ids != build.amount_map.ids:
            raise InboundRejected("invalid_request_binding")
        if (type(build._integrity) is not str or not secrets.compare_digest(
                build._integrity, _build_integrity(build.payload, build.amount_map,
                                                   build._source_fingerprint,
                                                   build._response_private_literals))):
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
            raise InboundRejected("malformed_response") from None
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
            if len(build.amount_map.ids) != 1:
                raise InboundRejected("insufficient_selection_evidence")
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
            if not _is_allowed_json_content_type(response.content_type):
                raise TransportResponseRejected("invalid_content_type")
            if type(response.body) is not bytes:
                raise TransportResponseRejected("malformed_response")
            if not 0 < len(response.body) <= _MAX_RESPONSE_BYTES:
                raise TransportResponseRejected("response_too_large")
            try:
                text = response.body.decode("utf-8")
            except UnicodeDecodeError as error:
                raise TransportResponseRejected("invalid_utf8") from None
            try:
                _reject_response_literal(text, build._response_private_literals)
                _reject_numeric_amount_surface(
                    text, tuple(value for _, value in build.amount_map._values), InboundRejected,
                    "private_response_numeric_literal",
                )
            except InboundRejected as error:
                raise TransportResponseRejected("privacy_rejected") from None
            try:
                # json.loads consumes one complete JSON document; fencing, prefixes,
                # suffixes, and trailing garbage fail here. Duplicate keys fail too.
                json.loads(text, object_pairs_hook=_strict_response_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except InboundRejected as error:
                raise TransportResponseRejected("malformed_response") from None
            except Exception as error:
                raise TransportResponseRejected("malformed_response") from None
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
            "redirect_rejected": "redirect_rejected", "request_reused": "request_reused",
            "invalid_content_type": "invalid_content_type", "response_too_large": "response_too_large",
            "invalid_utf8": "invalid_utf8",
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


def run_real_shadow_transport(
    build: AnonymousShadowBuild,
    observation: OcrObservation,
    transport: MedicalGeminiShadowTransport,
    policy: MedicalAnonymousShadowTransportPolicy = MedicalAnonymousShadowTransportPolicy(),
) -> InboundShadowResult:
    """One-shot explicit-injection REST orchestration; it has no default caller."""
    # 1/2: dedicated default-false switch and exact no-feature policy first.
    if type(policy) is not MedicalAnonymousShadowTransportPolicy or not policy.transport_enabled:
        return InboundShadowResult("needs_review", "disabled")
    if type(transport) is not MedicalGeminiShadowTransport:
        return InboundShadowResult("needs_review", "validation_rejected")
    # 3: no key means no outbound preparation, request construction, or executor.
    key = transport.acquire_api_key()
    if key is None:
        return InboundShadowResult("needs_review", "authentication")
    # 4: create opaque bytes only after policy and key availability pass.
    try:
        request = _validated_request_for_transport(build, observation)
    except Exception:
        return InboundShadowResult("needs_review", "validation_rejected")
    # 5/6: one fixed request and at most one executor call; no retry/fallback.
    response = transport.send(request, key)
    if type(response) is not TransportResponse:
        return InboundShadowResult("needs_review", "transport_error")
    if response.status != "ok":
        failures = {
            "timeout": "timeout", "quota": "quota", "authentication": "authentication",
            "unavailable": "unavailable", "transport_error": "transport_error",
            "redirect_rejected": "redirect_rejected", "request_reused": "request_reused",
            "invalid_content_type": "invalid_content_type", "response_too_large": "response_too_large",
            "invalid_utf8": "invalid_utf8",
            "malformed_response": "malformed_response",
        }
        return InboundShadowResult("needs_review", failures.get(response.status, "transport_error"))
    # 7: reuse independent format/privacy and semantic/binding boundaries.
    try:
        checked = TransportResponseGate().validate(build, response)
        accepted = FinalInboundGate().accept_transport_validated(build, checked)
        return InboundShadowResult("needs_review", "accepted_shadow_response",
                                   rehydrate_anonymous_response(build, accepted))
    except TransportResponseRejected as error:
        return InboundShadowResult("needs_review", str(error))
    except InboundRejected as error:
        return InboundShadowResult(
            "needs_review", "binding_rejected" if "binding" in str(error) else "validation_rejected")
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
        if len(build.amount_map.ids) != 1:
            raise InboundRejected("insufficient_selection_evidence")
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
