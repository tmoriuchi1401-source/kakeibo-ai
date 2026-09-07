"""Actual urllib control flow with mocked HTTPSConnection; zero socket I/O."""
import copy
from dataclasses import replace
from email.message import Message
import http.client
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import app.medical_gemini_shadow as s
from test_medical_gemini_anonymous_shadow import (
    isolated_shadow, safe_build, row, observation, response, gemini_envelope,
    real_transport, enabled_transport_policy, SyntheticKeyProvider,
)

OPENER_FACTORY = s._isolated_http_opener


class WireResponse:
    def __init__(self, status=200, content_type="application/json", body=b"{}"):
        self.status = self.code = status
        self.reason = "synthetic"
        self.headers = Message()
        if content_type is not None:
            self.headers.add_header("Content-Type", content_type)
        self.body = body
        self.read_limits = []
        self.closed = False

    def info(self):
        return self.headers

    def read(self, limit=None):
        assert type(limit) is int, "unbounded read"
        self.read_limits.append(limit)
        return self.body[:limit]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def install_wire(monkeypatch, wire_response, failure=None):
    attempts = []
    class Connection:
        _http_vsn = 11
        sock = None
        def __init__(self, host, **kwargs):
            self.host = host
        def set_debuglevel(self, level):
            assert level == 0
        def request(self, method, url, body=None, headers=None, **kwargs):
            attempts.append((self.host, method, url, headers))
            if failure:
                raise failure
        def getresponse(self):
            return wire_response
        def close(self):
            pass
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(s, "_isolated_http_opener", OPENER_FACTORY)
    return attempts


def http_request(build=None):
    build = build or safe_build()
    source = observation([row("領収金額 4321円")])
    validated = s._validated_request_for_transport(build, source)
    transport = s.MedicalGeminiShadowTransport(s.UrllibHttpExecutor(), SyntheticKeyProvider())
    return transport.build_request(validated, transport.acquire_api_key())


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_transport_redirects_never_follow_forward_key_or_issue_second_attempt(monkeypatch, status):
    wire = WireResponse(status, body=b"SYNTHETIC_ERROR_BODY")
    wire.headers.add_header("Location", "https://synthetic-other.invalid/private")
    attempts = install_wire(monkeypatch, wire)
    build = safe_build()
    transport = s.MedicalGeminiShadowTransport(s.UrllibHttpExecutor(), SyntheticKeyProvider())
    result = s.run_real_shadow_transport(build, observation([row("領収金額 4321円")]),
                                         transport, enabled_transport_policy())
    assert result.reason_code == "redirect_rejected" and result.local_result is None
    assert len(attempts) == 1
    assert attempts[0][0] == "generativelanguage.googleapis.com"
    assert attempts[0][1] == "POST"
    assert wire.read_limits == [] and wire.closed
    assert not any("synthetic-other" in a[0] for a in attempts)


def test_transport_redirect_request_hook_itself_refuses_new_request():
    from urllib.request import Request
    from urllib.error import HTTPError
    with pytest.raises(HTTPError):
        s._RejectRedirectHandler().redirect_request(
            Request("https://synthetic.invalid/", method="POST"), None, 302,
            "synthetic", {}, "https://synthetic-other.invalid/")


@pytest.mark.parametrize("content_type", [
    None, "text/plain", "application/json; charset=shift_jis",
    "application/json\r\nInjected: synthetic",
])
def test_transport_invalid_mime_precedes_body_read_and_envelope_parse(monkeypatch, content_type):
    wire = WireResponse(content_type=content_type, body=b"\xff" * 100)
    attempts = install_wire(monkeypatch, wire)
    monkeypatch.setattr(s, "_extract_gemini_response_text", lambda *a: pytest.fail("parsed early"))
    result = s.UrllibHttpExecutor().execute(http_request())
    assert result.failure == "invalid_content_type"
    assert len(attempts) == 1 and not wire.read_limits and wire.closed


def test_transport_duplicate_content_type_headers_rejected_before_read(monkeypatch):
    wire = WireResponse()
    wire.headers.add_header("Content-Type", "application/json")
    install_wire(monkeypatch, wire)
    assert s.UrllibHttpExecutor().execute(http_request()).failure == "invalid_content_type"
    assert not wire.read_limits


def test_transport_compressed_envelope_rejected_without_decompression_or_read(monkeypatch):
    wire = WireResponse()
    wire.headers.add_header("Content-Encoding", "gzip")
    install_wire(monkeypatch, wire)
    assert s.UrllibHttpExecutor().execute(http_request()).failure == "malformed_response"
    assert not wire.read_limits


def test_transport_oversized_envelope_uses_limit_plus_one_only(monkeypatch):
    wire = WireResponse(body=b"x" * (s._MAX_ENVELOPE_BYTES * 3))
    attempts = install_wire(monkeypatch, wire)
    result = s.UrllibHttpExecutor().execute(http_request())
    assert result.failure == "response_too_large" and result.body == b""
    assert wire.read_limits == [s._MAX_ENVELOPE_BYTES + 1]
    assert len(attempts) == 1 and wire.closed


def test_transport_exact_envelope_limit_is_allowed_before_candidate_checks(monkeypatch):
    body = gemini_envelope("{}")
    body += b" " * (s._MAX_ENVELOPE_BYTES - len(body))
    wire = WireResponse(body=body)
    install_wire(monkeypatch, wire)
    result = s.UrllibHttpExecutor().execute(http_request())
    assert result.failure is None and len(result.body) == s._MAX_ENVELOPE_BYTES
    assert wire.read_limits == [s._MAX_ENVELOPE_BYTES + 1]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_transport_http_error_body_is_never_read_or_retried(monkeypatch, status):
    wire = WireResponse(status, body=b"PRIVATE_SYNTHETIC_ERROR")
    attempts = install_wire(monkeypatch, wire)
    result = s.UrllibHttpExecutor().execute(http_request())
    assert result.status_code == status and result.body == b""
    assert len(attempts) == 1 and not wire.read_limits and wire.closed


@pytest.mark.parametrize("failure", [TimeoutError("PRIVATE"), ConnectionError("PRIVATE")])
def test_transport_network_failure_consumes_attempt_without_retry(monkeypatch, failure):
    attempts = install_wire(monkeypatch, WireResponse(), failure)
    request = http_request()
    executor = s.UrllibHttpExecutor()
    assert executor.execute(request).failure in {"timeout", "transport_error"}
    assert executor.execute(request).failure == "request_reused"
    assert len(attempts) == 1


def test_transport_same_http_capability_copied_or_rebuilt_has_one_attempt(monkeypatch):
    attempts = install_wire(monkeypatch, WireResponse())
    build = safe_build()
    first, second = http_request(build), http_request(build)
    executor = s.UrllibHttpExecutor()
    assert executor.execute(first).failure is None
    for req in (first, second, copy.copy(first), copy.deepcopy(first), replace(first)):
        assert executor.execute(req).failure == "request_reused"
    assert len(attempts) == 1


def test_transport_one_successful_wire_attempt_passes_entire_existing_chain(monkeypatch):
    build = safe_build()
    wire = WireResponse(content_type='Application/JSON; Charset="UTF-8"',
                        body=gemini_envelope(response(build).decode("utf-8")))
    attempts = install_wire(monkeypatch, wire)
    transport = s.MedicalGeminiShadowTransport(s.UrllibHttpExecutor(), SyntheticKeyProvider())
    result = s.run_real_shadow_transport(build, observation([row("領収金額 4321円")]),
                                         transport, enabled_transport_policy())
    assert result.reason_code == "accepted_shadow_response"
    assert result.status == "needs_review" and result.local_result.amount == 4321
    assert len(attempts) == 1 and wire.read_limits == [s._MAX_ENVELOPE_BYTES + 1]


@pytest.mark.parametrize("kind", ["same_wrapper", "new_wrapper", "rebuilt_source", "copied_build"])
def test_transport_repeated_send_or_source_rebuild_is_refused(kind):
    source = observation([row("領収金額 4321円")])
    build = s.build_anonymous_shadow_payload(source)
    transport, executor, provider = real_transport(s.HttpResponse(429))
    key = transport.acquire_api_key()
    first = s._validated_request_for_transport(build, source)
    assert transport.send(first, key).status == "quota"
    if kind == "same_wrapper":
        second = copy.deepcopy(first)
    elif kind == "new_wrapper":
        second = s._validated_request_for_transport(build, source)
    elif kind == "copied_build":
        second = s._validated_request_for_transport(copy.deepcopy(build), source)
    else:
        second = s._validated_request_for_transport(s.build_anonymous_shadow_payload(source), source)
    assert transport.send(second, key).status == "request_reused"
    assert len(executor.calls) == 1


def test_transport_two_standalone_gate_wrappers_for_same_unit_are_not_two_sends():
    build = safe_build()
    a = s.FinalOutboundGate().validate_for_transport(build.payload)
    b = s.FinalOutboundGate().validate_for_transport(build.payload)
    transport, executor, _ = real_transport(s.HttpResponse(429))
    key = transport.acquire_api_key()
    assert transport.send(a, key).status == "quota"
    assert transport.send(b, key).status == "request_reused"
    assert len(executor.calls) == 1


def test_transport_concurrent_send_claim_is_atomic():
    build = safe_build()
    source = observation([row("領収金額 4321円")])
    wrappers = [s._validated_request_for_transport(build, source) for _ in range(8)]
    transport, executor, _ = real_transport(s.HttpResponse(429))
    key = transport.acquire_api_key()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda req: transport.send(req, key).status, wrappers))
    assert results.count("quota") == 1 and results.count("request_reused") == 7
    assert len(executor.calls) == 1


def test_transport_replay_registry_at_capacity_fails_closed_without_eviction(monkeypatch):
    monkeypatch.setattr(s, "_MAX_USED_REQUESTS", 1)
    transport, executor, _ = real_transport(s.HttpResponse(429))
    key = transport.acquire_api_key()
    for index, amount in enumerate((4321, 7654)):
        source = observation([row(f"領収金額 {amount}円")])
        build = s.build_anonymous_shadow_payload(source)
        result = transport.send(s._validated_request_for_transport(build, source), key)
        assert result.status == ("quota" if index == 0 else "request_reused")
    assert len(executor.calls) == 1


@pytest.mark.parametrize("body,reason", [
    (b"\xff", "invalid_utf8"),
    (b"{}" + b" " * s._MAX_ENVELOPE_BYTES, "response_too_large"),
    (b'{"candidates":[],"candidates":[]}', "malformed_response"),
    (b'{"candidates":[]} trailing', "malformed_response"),
    (gemini_envelope("x" * 4097), "response_too_large"),
    (gemini_envelope(""), "response_too_large"),
], ids=["utf8", "envelope_limit", "duplicate", "trailing", "candidate_limit", "empty"])
def test_transport_envelope_order_and_candidate_limits(body, reason):
    build = safe_build()
    transport, executor, _ = real_transport(s.HttpResponse(200, "application/json", body))
    result = s.run_real_shadow_transport(build, observation([row("領収金額 4321円")]),
                                         transport, enabled_transport_policy())
    assert result.reason_code == reason and result.local_result is None
    assert len(executor.calls) == 1


def test_transport_invalid_mime_from_injected_executor_never_parses(monkeypatch):
    monkeypatch.setattr(s, "_extract_gemini_response_text", lambda *a: pytest.fail("parsed"))
    build = safe_build()
    transport, _, _ = real_transport(s.HttpResponse(200, "text/plain", b"\xff"))
    result = s.run_real_shadow_transport(build, observation([row("領収金額 4321円")]),
                                         transport, enabled_transport_policy())
    assert result.reason_code == "invalid_content_type"


def test_transport_repr_and_error_outputs_hide_body_header_key_and_metadata():
    secret, body, header = "SYNTHETIC_SECRET", b"SYNTHETIC_BODY", "SYNTHETIC_HEADER"
    transport, _, _ = real_transport(key=secret)
    key = transport.acquire_api_key()
    build = safe_build()
    validated = s._validated_request_for_transport(build, observation([row("領収金額 4321円")]))
    request = replace(transport.build_request(validated, key), body=body)
    objects = (key, validated, request, s.HttpResponse(200, header, body),
               s.TransportResponse("ok", header, body),
               s.EnvironmentMedicalShadowApiKeyProvider(getenv=lambda _: secret))
    for obj in objects:
        assert all(value not in repr(obj) for value in (secret, body.decode(), header))
    with pytest.raises(TypeError):
        s.EnvironmentMedicalShadowApiKeyProvider(variable_name="GENERIC_API_KEY")


def test_transport_no_optional_feature_policy_and_disabled_route_stays_inert():
    build = safe_build()
    transport, executor, provider = real_transport()
    result = s.run_real_shadow_transport(build, observation([row("領収金額 4321円")]), transport)
    assert result.reason_code == "disabled" and not executor.calls and provider.calls == 0
    for option in ("allow_retry", "allow_fallback", "allow_tools", "allow_grounding",
                   "allow_caching", "allow_batch", "allow_priority"):
        assert not s.MedicalAnonymousShadowTransportPolicy(enabled=True, **{option: True}).transport_enabled
