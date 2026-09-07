"""Synthetic-only tests for the anonymous, no-transport medical shadow boundary."""
import json
from pathlib import Path

import pytest
import app.medical_gemini_shadow as shadow

from app.medical_gemini_shadow import (
    EnvironmentMedicalShadowApiKeyProvider, FakeAnonymousShadowTransport,
    FinalInboundGate, FinalOutboundGate, HttpResponse,
    GeminiFreeTierShadowPolicy, InboundRejected, LocalAmountMap,
    MedicalAnonymousShadowTransportPolicy, MedicalGeminiShadowTransport,
    OutboundRejected, TransportResponse,
    ValidatedAnonymousBytes, build_anonymous_shadow_payload, prepare_anonymous_shadow,
    receive_synthetic_shadow_response, rehydrate_anonymous_response,
    run_fake_shadow_transport, run_real_shadow_transport,
)
from app.medical_ocr_observation_shadow import ReceiptImage, make_observation


@pytest.fixture(autouse=True)
def isolated_shadow(monkeypatch):
    # Every case gets a fresh PROCESS ledger; tests within a case must not reset
    # it unless comparing independent failure fixtures.
    monkeypatch.setattr(shadow, "_REPLAY_REGISTRY", shadow._ReplayRegistry())
    monkeypatch.setattr(shadow, "_isolated_http_opener",
                        lambda: pytest.fail("real HTTP opener forbidden"))
    import os
    original = os._Environ.__getitem__
    def no_real_key(self, key):
        if "API_KEY" in str(key).upper():
            pytest.fail("real API key access forbidden")
        return original(self, key)
    monkeypatch.setattr(os._Environ, "__getitem__", no_real_key)


def observation(rows, *, page=1):
    image = ReceiptImage("synthetic_local_unit", page, b"synthetic-only-image")
    return make_observation(image, "synthetic", ("a" * 64,), 100, 100, rows)


def row(text, *, x=1, confidence=.95):
    return {"text": text, "confidence": confidence, "detection_confidence": .8,
            "polygon": [[x, 1], [x + 20, 1], [x + 20, 10], [x, 10]]}


def safe_build():
    return build_anonymous_shadow_payload(observation([row("領収金額 4321円")]))


def large_amount_build():
    return build_anonymous_shadow_payload(observation([row("領収金額 38430円")]))


def response(build, *, decision="select", amount_id="first_candidate", confidence="high", **extra):
    if amount_id == "first_candidate":
        amount_id = build.amount_map.ids[0]
    value = {"schema_version": "medical-anonymous-shadow-response-v2",
             "unit_ref": build.payload["unit_ref"], "decision": decision,
             "amount_id": amount_id, "confidence": confidence}
    value.update(extra)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_safe_payload_has_only_allowlisted_semantics_and_local_amount_mapping():
    build = safe_build()
    payload = build.payload
    assert set(payload) == {"schema_version", "unit_ref", "candidates"}
    assert payload["schema_version"] == "medical-anonymous-shadow-v2"
    candidate = payload["candidates"][0]
    assert set(candidate) == {"candidate_id"}
    assert build.amount_map.resolve(candidate["candidate_id"]) == 4321
    wire = GeminiFreeTierShadowPolicy(enabled=True).prepare(build, observation([row("領収金額 4321円")]))
    assert b"4321" not in wire and "領収金額".encode("unicode_escape") not in wire
    assert json.loads(wire)["unit_ref"] == payload["unit_ref"]

def test_gate_rejects_raw_text_forbidden_fields_unknowns_and_final_byte_leakage():
    payload = safe_build().payload
    payload["raw_ocr_text"] = "SYNTHETIC_PRIVATE"
    with pytest.raises(OutboundRejected): FinalOutboundGate().serialize(payload, ("SYNTHETIC_PRIVATE",))
    payload = safe_build().payload
    payload["candidates"][0]["geometry"] = {"x": .1}
    with pytest.raises(OutboundRejected): FinalOutboundGate().serialize(payload)
    payload = safe_build().payload
    with pytest.raises(OutboundRejected, match="private_literal_detected"):
        FinalOutboundGate().serialize(payload, (payload["unit_ref"],))
    payload = safe_build().payload
    payload["candidates"][0]["candidate_id"] = "candidate_short"
    with pytest.raises(OutboundRejected, match="invalid_candidate_id"):
        FinalOutboundGate().serialize(payload)
    payload = safe_build().payload
    payload["candidates"][0]["confidence"] = "high"
    with pytest.raises(OutboundRejected):
        FinalOutboundGate().serialize(payload)

@pytest.mark.parametrize("text,reason", [
    ("支払 4321円", "ambiguous_semantic_context"),
    ("4321円", "candidate_count_out_of_bounds"),
    ("領収金額 4321円 7654円", "ambiguous_numeric_context"),
    ("領収金額 4.321円", "malformed_numeric"),
])
def test_ambiguous_or_malformed_local_semantics_fail_closed(text, reason):
    with pytest.raises(OutboundRejected, match=reason):
        build_anonymous_shadow_payload(observation([row(text)]))


def test_incomplete_observation_and_page_mixing_are_not_payload_inputs():
    incomplete = observation([row("領収金額 4321円", confidence=None)])
    with pytest.raises(OutboundRejected, match="candidate_quality_insufficient"):
        build_anonymous_shadow_payload(incomplete)
    first = safe_build()
    second = build_anonymous_shadow_payload(observation([row("領収金額 7654円")], page=2))
    assert first.payload["unit_ref"] != second.payload["unit_ref"]
    assert first.amount_map.resolve(first.amount_map.ids[0]) == 4321
    assert second.amount_map.resolve(second.amount_map.ids[0]) == 7654


def test_preparation_returns_data_free_needs_review_and_binds_the_source_observation():
    withheld = prepare_anonymous_shadow(observation([row("4321円")]))
    assert withheld.status == "needs_review"
    assert withheld.build is None
    assert "4321" not in repr(withheld)
    build = safe_build()
    with pytest.raises(OutboundRejected, match="invalid_shadow_handoff"):
        GeminiFreeTierShadowPolicy(enabled=True).prepare(
            build, observation([row("領収金額 7654円")]))
    build.payload["unit_ref"] = "unit_abcdefghijklmnopqrstuvwxyz"
    with pytest.raises(OutboundRejected, match="invalid_shadow_handoff"):
        GeminiFreeTierShadowPolicy(enabled=True).prepare(
            build, observation([row("領収金額 4321円")]))


def test_free_tier_is_only_mode_and_disabled_stop_switch_fails_closed():
    build = safe_build()
    source = observation([row("領収金額 4321円")])
    with pytest.raises(OutboundRejected, match="free_tier_route_disabled"):
        GeminiFreeTierShadowPolicy().prepare(build, source)
    with pytest.raises(OutboundRejected):
        GeminiFreeTierShadowPolicy(enabled=True, tier="paid").prepare(build, source)  # type: ignore[arg-type]


def test_production_path_has_no_shadow_payload_caller_or_gemini_transport():
    root = Path(__file__).resolve().parents[1] / "app"
    for name in ("receipt_pipeline.py", "receipt_privacy_gate.py", "gemini_ai.py", "receipt_text_extraction.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "medical_gemini_shadow" not in text


def test_valid_synthetic_response_is_bound_and_rehydrated_only_locally(monkeypatch):
    build = safe_build()
    calls = []
    original = LocalAmountMap.resolve

    def resolve(self, amount_id):
        calls.append(amount_id)
        return original(self, amount_id)

    monkeypatch.setattr(LocalAmountMap, "resolve", resolve)
    result = receive_synthetic_shadow_response(build, response(build))
    assert result.status == "needs_review"
    assert result.reason_code == "accepted_shadow_response"
    assert result.local_result is not None
    assert result.local_result.decision == "select"
    assert result.local_result.amount == 4321
    assert calls == [build.amount_map.ids[0]]
    assert "4321" not in repr(result)


def test_rejected_responses_do_not_rehydrate_or_expose_private_literals(monkeypatch):
    build = safe_build()
    calls = []
    monkeypatch.setattr(LocalAmountMap, "resolve", lambda *args: calls.append(args))
    raw = response(build, explanation="領収金額 4321円")
    with pytest.raises(InboundRejected) as raised:
        FinalInboundGate().accept(build, raw)
    assert "4321" not in str(raised.value)
    assert "領収金額" not in str(raised.value)
    result = receive_synthetic_shadow_response(build, raw)
    assert result.status == "needs_review" and result.local_result is None
    assert "4321" not in repr(result)
    assert calls == []


def test_inbound_gate_rejects_unknown_text_and_real_amount_echoes():
    build = safe_build()
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, explanation="arbitrary free form"))
    with pytest.raises(InboundRejected) as raised:
        FinalInboundGate().accept(build, response(build, amount_id="4321"))
    assert "4321" not in str(raised.value)


@pytest.mark.parametrize("surface", [
    "38430", "38,430", "38.430", "３８４３０", "3 8 4 3 0", "\\u0033\\u0038\\u0034\\u0033\\u0030",
])
def test_final_gates_reject_normalized_real_amount_surfaces(surface):
    build = large_amount_build()
    raw = response(build, amount_id=surface)
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, raw)
    payload = build.payload
    # v2 random IDs contain no digits; numeric surface scanner remains active.
    with pytest.raises(OutboundRejected, match="private_numeric_literal_detected"):
        shadow._reject_numeric_amount_surface(surface, (38430,), OutboundRejected,
                                              "private_numeric_literal_detected")


@pytest.mark.parametrize("extra", [
    {"metadata": "synthetic"}, {"filename": "synthetic.png"},
    {"path": "C:/synthetic"}, {"drive_id": "synthetic-drive"},
    {"page": 1}, {"reasoning": "free form"},
])
def test_inbound_gate_rejects_metadata_and_source_reference_fields(extra):
    build = safe_build()
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, **extra))


def test_inbound_gate_rejects_unknown_ids_and_response_for_another_request():
    build = safe_build()
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, amount_id="amount_B"))
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, unit_ref="unit_abcdefghijklmnopqrstuvwxyz"))
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, unit_ref="unit_short"))
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, decision="explanation"))
    other = safe_build()
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(other))


def test_inbound_gate_rejects_duplicate_conflicting_invalid_and_oversized_responses():
    build = safe_build()
    duplicate = (
        '{"schema_version":"medical-anonymous-shadow-response-v2","unit_ref":"'
        + build.payload["unit_ref"]
        + '","decision":"select","amount_id":"amount_A","amount_id":"amount_A","confidence":"high"}'
    ).encode("ascii")
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, duplicate)
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, decision="select", amount_id=None, confidence=None))
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, decision="select", confidence=1))
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, response(build, amount_id={"id": "amount_A"}))
    with pytest.raises(InboundRejected):
        FinalInboundGate().accept(build, b"{" + b" " * 4096 + b"}")


def test_empty_and_synthetic_transport_or_parser_failures_are_data_free_review():
    build = safe_build()
    assert receive_synthetic_shadow_response(build, b"").reason_code == "malformed_response"
    for failure, reason in (("timeout", "synthetic_timeout"),
                            ("quota_failure", "synthetic_quota_failure"),
                            ("api_failure", "synthetic_api_failure"),
                            ("parser_failure", "synthetic_parser_failure")):
        result = receive_synthetic_shadow_response(build, synthetic_failure=failure)
        assert result.status == "needs_review"
        assert result.reason_code == reason


def test_rehydration_rejects_unvalidated_or_wrong_binding_without_map_access(monkeypatch):
    build = safe_build()
    other = safe_build()
    accepted = FinalInboundGate().accept(build, response(build))
    calls = []
    monkeypatch.setattr(LocalAmountMap, "resolve", lambda *args: calls.append(args))
    with pytest.raises(InboundRejected):
        rehydrate_anonymous_response(other, accepted)
    assert calls == []


def enabled_transport_policy():
    return MedicalAnonymousShadowTransportPolicy.from_setting("true")


class SyntheticKeyProvider:
    def __init__(self, value="synthetic-shadow-key"):
        self.value = value
        self.calls = 0

    def get(self):
        self.calls += 1
        return self.value


class FakeHttpExecutor:
    def __init__(self, response=None, failure=None):
        self.response = response
        self.failure = failure
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        return self.response


def gemini_envelope(text):
    return json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]},
                      separators=(",", ":")).encode("utf-8")


def real_transport(response=None, key="synthetic-shadow-key", failure=None):
    executor = FakeHttpExecutor(response, failure)
    provider = SyntheticKeyProvider(key)
    return MedicalGeminiShadowTransport(executor, provider), executor, provider


def test_transport_kill_switch_is_default_false_unset_and_malformed_without_invocation():
    build = safe_build()
    source = observation([row("領収金額 4321円")])
    for policy in (MedicalAnonymousShadowTransportPolicy(),
                   MedicalAnonymousShadowTransportPolicy.from_setting(None),
                   MedicalAnonymousShadowTransportPolicy.from_setting("TRUE"),
                   MedicalAnonymousShadowTransportPolicy.from_setting(True)):
        fake = FakeAnonymousShadowTransport(TransportResponse("ok", "application/json", response(build)))
        result = run_fake_shadow_transport(build, source, fake, policy)
        assert result.reason_code == "disabled"
        assert result.local_result is None
        assert fake.invocations == 0
        assert "4321" not in repr(result)


def test_transport_policy_is_dedicated_and_fixed_to_no_feature_free_tier_route():
    policy = enabled_transport_policy()
    assert policy.transport_enabled
    assert policy.model == "gemini-3.1-flash-lite"
    assert not any((policy.allow_tools, policy.allow_grounding, policy.allow_caching,
                    policy.allow_batch, policy.allow_priority, policy.allow_retry,
                    policy.allow_fallback))
    assert not MedicalAnonymousShadowTransportPolicy(enabled=True, model="gemini-latest").transport_enabled
    assert not MedicalAnonymousShadowTransportPolicy(enabled=True, allow_retry=True).transport_enabled


def test_enabled_fake_transport_receives_only_validated_bytes_and_one_invocation():
    build = safe_build()
    fake = FakeAnonymousShadowTransport(TransportResponse("ok", "application/json", response(build)))
    result = run_fake_shadow_transport(
        build, observation([row("領収金額 4321円")]), fake, enabled_transport_policy())
    assert result.reason_code == "accepted_shadow_response"
    assert fake.invocations == 1
    assert len(fake.received) == 1 and type(fake.received[0]) is ValidatedAnonymousBytes
    assert "4321" not in repr(fake.received[0])
    with pytest.raises(TypeError):
        fake.send(build)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        fake.send(observation([row("領収金額 4321円")]))  # type: ignore[arg-type]


@pytest.mark.parametrize("status,reason", [
    ("timeout", "timeout"), ("quota", "quota"), ("authentication", "authentication"),
    ("unavailable", "unavailable"), ("transport_error", "transport_error"),
])
def test_transport_failures_are_data_free_and_never_retry(status, reason):
    build = safe_build()
    fake = FakeAnonymousShadowTransport(TransportResponse(status))
    result = run_fake_shadow_transport(
        build, observation([row("領収金額 4321円")]), fake, enabled_transport_policy())
    assert result.reason_code == reason
    assert result.local_result is None and fake.invocations == 1
    assert "4321" not in repr(result)


def test_transport_exception_is_redacted_and_never_retried():
    build = safe_build()
    fake = FakeAnonymousShadowTransport(failure=RuntimeError("領収金額 4321円 C:/private/receipt.pdf"))
    result = run_fake_shadow_transport(
        build, observation([row("領収金額 4321円")]), fake, enabled_transport_policy())
    assert result.reason_code == "transport_error" and fake.invocations == 1
    assert "4321" not in repr(result)
    assert "receipt" not in repr(result)


@pytest.mark.parametrize("content_type,body,reason", [
    ("text/html", b"<html>private</html>", "invalid_content_type"),
    (None, b"{}", "invalid_content_type"),
    ("application/json; charset=latin-1", b"{}", "invalid_content_type"),
    ("application/json", b"{" + b" " * 4096 + b"}", "response_too_large"),
    ("application/json", b'\xff', "invalid_utf8"),
    ("application/json", b"```json\n{}\n```", "malformed_response"),
    ("application/json", b"explanation {}", "malformed_response"),
    ("application/json", b"{} trailing", "malformed_response"),
])
def test_transport_response_format_boundary_fails_closed(content_type, body, reason):
    build = safe_build()
    fake = FakeAnonymousShadowTransport(TransportResponse("ok", content_type, body))
    result = run_fake_shadow_transport(
        build, observation([row("領収金額 4321円")]), fake, enabled_transport_policy())
    assert result.reason_code == reason and result.local_result is None
    assert fake.invocations == 1


@pytest.mark.parametrize("content_type", [
    "application/json",
    "application/json; charset=utf-8",
    "application/json; charset=UTF-8",
    "application/json;charset=utf-8",
    'Application/JSON; Charset="utf-8"',
])
def test_transport_content_type_semantic_json_utf8_variants_are_accepted(content_type):
    build = safe_build()
    fake = FakeAnonymousShadowTransport(TransportResponse("ok", content_type, response(build)))
    result = run_fake_shadow_transport(
        build, observation([row("領収金額 4321円")]), fake, enabled_transport_policy())
    assert result.reason_code == "accepted_shadow_response"
    assert result.local_result is not None and fake.invocations == 1


@pytest.mark.parametrize("content_type", [
    "text/plain",
    "text/json",
    "application/xml",
    "application/json; charset=shift_jis",
    "application/json; charset=iso-8859-1",
    "application/jsonx",
    "application/json; charset=utf-8evil",
    "application/json; charset=utf-8; charset=UTF-8",
    "application/json; boundary=untrusted",
    "application/json; charset=utf-8; boundary=untrusted",
    "application/json; charset",
    "application/json; charset=\"utf-8\";",
    "application/json\nX-Injected: value",
])
def test_transport_content_type_malformed_or_unallowlisted_variants_fail_closed(content_type):
    build = safe_build()
    # Invalid bytes demonstrate that MIME rejection happens before UTF-8 parsing.
    fake = FakeAnonymousShadowTransport(TransportResponse("ok", content_type, b"\xff"))
    result = run_fake_shadow_transport(
        build, observation([row("領収金額 4321円")]), fake, enabled_transport_policy())
    assert result.reason_code == "invalid_content_type"
    assert result.local_result is None and fake.invocations == 1


def test_transport_response_privacy_duplicate_binding_and_rehydration_boundaries(monkeypatch):
    build = safe_build()
    source = observation([row("領収金額 4321円")])
    calls = []
    monkeypatch.setattr(LocalAmountMap, "resolve", lambda *args: calls.append(args))
    duplicate = (
        b'{"schema_version":"medical-anonymous-shadow-response-v2","unit_ref":"'
        + build.payload["unit_ref"].encode("ascii")
        + b'","decision":"select","amount_id":"amount_A","amount_id":"amount_A","confidence":"high"}'
    )
    cases = [
        response(build, explanation="領収金額 4321円"),
        response(build, amount_id="4321"),
        response(build, unit_ref="unit_abcdefghijklmnopqrstuvwxyz"),
        duplicate,
    ]
    for body in cases:
        shadow._REPLAY_REGISTRY = shadow._ReplayRegistry()
        build = safe_build()
        fake = FakeAnonymousShadowTransport(TransportResponse("ok", "application/json", body))
        result = run_fake_shadow_transport(build, source, fake, enabled_transport_policy())
        assert result.reason_code in {"privacy_rejected", "binding_rejected", "validation_rejected",
                                      "malformed_response"}
        assert result.local_result is None and fake.invocations == 1
    assert calls == []


@pytest.mark.parametrize("surface", ["38,430", "38.430", "３８４３０", "3 8 4 3 0",
                                    "\\u0033\\u0038\\u0034\\u0033\\u0030"])
def test_transport_gate_rejects_amount_representation_variants_before_semantics(surface):
    build = large_amount_build()
    fake = FakeAnonymousShadowTransport(
        TransportResponse("ok", "application/json", response(build, amount_id=surface)))
    result = run_fake_shadow_transport(
        build, observation([row("領収金額 38430円")]), fake, enabled_transport_policy())
    assert result.reason_code == "privacy_rejected"
    assert result.local_result is None and fake.invocations == 1


def test_real_transport_kill_switch_precedes_key_request_build_and_network(monkeypatch):
    build = safe_build()
    transport, executor, provider = real_transport(
        HttpResponse(200, "application/json", gemini_envelope(response(build).decode("utf-8"))))
    monkeypatch.setattr(shadow, "_isolated_http_opener", lambda *args, **kwargs: pytest.fail("network attempted"))
    result = run_real_shadow_transport(
        build, observation([row("領収金額 4321円")]), transport,
        MedicalAnonymousShadowTransportPolicy())
    assert result.reason_code == "disabled"
    assert provider.calls == 0 and executor.calls == []


def test_real_transport_missing_key_stops_before_request_and_executor():
    build = safe_build()
    transport, executor, provider = real_transport(key="")
    result = run_real_shadow_transport(
        build, observation([row("領収金額 4321円")]), transport, enabled_transport_policy())
    assert result.reason_code == "authentication"
    assert provider.calls == 1 and executor.calls == []


def test_real_transport_uses_fake_executor_fixed_endpoint_wrapper_and_secret_header_only(monkeypatch):
    build = safe_build()
    key = "synthetic-secret-not-in-body"
    transport, executor, provider = real_transport(
        HttpResponse(200, "application/json", gemini_envelope(response(build).decode("utf-8"))), key)
    monkeypatch.setattr(shadow, "_isolated_http_opener", lambda *args, **kwargs: pytest.fail("network attempted"))
    result = run_real_shadow_transport(
        build, observation([row("領収金額 4321円")]), transport, enabled_transport_policy())
    assert result.reason_code == "accepted_shadow_response"
    assert provider.calls == 1 and len(executor.calls) == 1
    request = executor.calls[0]
    assert request.url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
    assert "?" not in request.url and request.timeout_seconds == 10
    assert dict(request.headers)["x-goog-api-key"] == key
    assert key.encode() not in request.body and key not in repr(request) and key not in repr(result)
    body = json.loads(request.body)
    assert set(body) == {"contents", "generationConfig"}
    assert set(body["generationConfig"]) == {"responseMimeType", "responseJsonSchema", "candidateCount", "temperature"}
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "tools" not in body and "systemInstruction" not in body
    body_text = "".join(part["text"] for part in body["contents"][0]["parts"])
    for forbidden in ("4321", "領収金額", "filename", "Drive", "C:/"):
        assert forbidden not in body_text
    assert "Return only one JSON object" in body_text
    with pytest.raises(TypeError):
        transport.build_request(build, transport.acquire_api_key())  # type: ignore[arg-type]


def test_environment_key_provider_is_lazy_and_testable_without_reading_real_environment():
    seen = []
    provider = EnvironmentMedicalShadowApiKeyProvider(getenv=lambda name: seen.append(name) or "synthetic-key")
    assert seen == []
    assert provider.get() == "synthetic-key"
    assert seen == ["MEDICAL_GEMINI_SHADOW_API_KEY"]


@pytest.mark.parametrize("status,reason", [
    (400, "transport_error"), (401, "authentication"), (403, "authentication"),
    (404, "unavailable"), (429, "quota"), (500, "unavailable"),
])
def test_real_transport_maps_http_statuses_once_without_exposing_google_body(status, reason):
    build = safe_build()
    secret_error = b'{"error":{"message":"receipt 4321 C:/private.pdf"}}'
    transport, executor, provider = real_transport(HttpResponse(status, "application/json", secret_error))
    result = run_real_shadow_transport(
        build, observation([row("領収金額 4321円")]), transport, enabled_transport_policy())
    assert result.reason_code == reason
    assert provider.calls == 1 and len(executor.calls) == 1
    assert "4321" not in repr(result) and "private" not in repr(result)


@pytest.mark.parametrize("failure,reason", [
    (TimeoutError("receipt 4321"), "timeout"),
    (ConnectionError("receipt 4321 C:/private.pdf"), "transport_error"),
])
def test_real_transport_exception_mapping_is_data_free_and_not_retried(failure, reason):
    build = safe_build()
    transport, executor, provider = real_transport(failure=failure)
    result = run_real_shadow_transport(
        build, observation([row("領収金額 4321円")]), transport, enabled_transport_policy())
    assert result.reason_code == reason
    assert provider.calls == 1 and len(executor.calls) == 1
    assert "4321" not in repr(result) and "private" not in repr(result)


@pytest.mark.parametrize("content_type,text,reason", [
    ("text/html", "{}", "invalid_content_type"),
    ("application/json", "```json\\n{}\\n```", "malformed_response"),
    ("application/json", "{} trailing", "malformed_response"),
    ("application/json", '{"schema_version":"medical-anonymous-shadow-response-v2",'
     '"schema_version":"medical-anonymous-shadow-response-v2"}', "malformed_response"),
])
def test_real_transport_reuses_response_format_boundary(content_type, text, reason):
    build = safe_build()
    transport, executor, _ = real_transport(HttpResponse(200, content_type, gemini_envelope(text)))
    result = run_real_shadow_transport(
        build, observation([row("領収金額 4321円")]), transport, enabled_transport_policy())
    assert result.reason_code == reason and len(executor.calls) == 1


def test_real_transport_reuses_privacy_binding_schema_and_rehydration_boundaries(monkeypatch):
    build = safe_build()
    original_unit_ref = build.payload["unit_ref"]
    source = observation([row("領収金額 4321円")])
    calls = []
    monkeypatch.setattr(LocalAmountMap, "resolve", lambda *args: calls.append(args))
    for text, reason in (
        (response(build, explanation="領収金額 4321円").decode("utf-8"), "privacy_rejected"),
        (response(build, unit_ref="unit_abcdefghijklmnopqrstuvwxyz").decode("utf-8"), "binding_rejected"),
        (json.dumps({"schema_version": "medical-anonymous-shadow-response-v2",
                     "unit_ref": build.payload["unit_ref"], "decision": "select",
                     "amount_id": "amount_B", "confidence": "high"}), "validation_rejected"),
    ):
        shadow._REPLAY_REGISTRY = shadow._ReplayRegistry()
        new_build = safe_build()
        if reason != "binding_rejected":
            text = text.replace(original_unit_ref, new_build.payload["unit_ref"])
        build = new_build
        transport, executor, _ = real_transport(HttpResponse(200, "application/json", gemini_envelope(text)))
        result = run_real_shadow_transport(build, source, transport, enabled_transport_policy())
        assert result.reason_code == reason and len(executor.calls) == 1
    assert calls == []
