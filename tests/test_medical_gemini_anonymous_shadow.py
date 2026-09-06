"""Synthetic-only tests for the anonymous, no-transport medical shadow boundary."""
import json
from pathlib import Path

import pytest

from app.medical_gemini_shadow import (
    FinalOutboundGate, GeminiFreeTierShadowPolicy, OutboundRejected,
    build_anonymous_shadow_payload, prepare_anonymous_shadow,
)
from app.medical_ocr_observation_shadow import ReceiptImage, make_observation


def observation(rows, *, page=1):
    image = ReceiptImage("synthetic_local_unit", page, b"synthetic-only-image")
    return make_observation(image, "synthetic", ("a" * 64,), 100, 100, rows)


def row(text, *, x=1, confidence=.95):
    return {"text": text, "confidence": confidence, "detection_confidence": .8,
            "polygon": [[x, 1], [x + 20, 1], [x + 20, 10], [x, 10]]}


def safe_build():
    return build_anonymous_shadow_payload(observation([row("領収金額 4321円")]))


def test_safe_payload_has_only_allowlisted_semantics_and_local_amount_mapping():
    build = safe_build()
    payload = build.payload
    assert set(payload) == {"schema_version", "unit_ref", "mode", "state", "structure_state", "competing_structure", "regions", "relations"}
    assert payload["structure_state"] == "unresolved"
    numeric = payload["regions"][0]
    assert numeric["amount_id"] == "amount_A"
    assert build.amount_map.resolve("amount_A") == 4321
    wire = GeminiFreeTierShadowPolicy(enabled=True).prepare(build, observation([row("領収金額 4321円")]))
    assert b"4321" not in wire and "領収金額".encode("unicode_escape") not in wire
    assert json.loads(wire)["unit_ref"] == payload["unit_ref"]


def test_gate_rejects_raw_text_forbidden_fields_unknowns_and_final_byte_leakage():
    payload = safe_build().payload
    payload["raw_ocr_text"] = "SYNTHETIC_PRIVATE"
    with pytest.raises(OutboundRejected): FinalOutboundGate().serialize(payload, ("SYNTHETIC_PRIVATE",))
    payload = safe_build().payload
    payload["regions"][0]["geometry"]["metadata"] = "SYNTHETIC_PRIVATE"
    with pytest.raises(OutboundRejected): FinalOutboundGate().serialize(payload)
    payload = safe_build().payload
    payload["unit_ref"] = "unit_SYNTHETIC_PRIVATE_abcdefghijklmnop"
    with pytest.raises(OutboundRejected): FinalOutboundGate().serialize(payload, ("SYNTHETIC_PRIVATE",))
    payload = safe_build().payload
    payload["regions"][0]["amount_id"] = "amount_SYNTHETIC_PRIVATE"
    with pytest.raises(OutboundRejected, match="invalid_amount_id"):
        FinalOutboundGate().serialize(payload)
    payload = safe_build().payload
    payload["regions"][0]["confidence"] = 1
    with pytest.raises(OutboundRejected, match="invalid_semantic_category"):
        FinalOutboundGate().serialize(payload)


@pytest.mark.parametrize("text,reason", [
    ("支払 4321円", "ambiguous_semantic_context"),
    ("4321円", "ambiguous_numeric_context"),
    ("領収金額 4321円 7654円", "ambiguous_numeric_context"),
    ("領収金額 4.321円", "malformed_numeric"),
])
def test_ambiguous_or_malformed_local_semantics_fail_closed(text, reason):
    with pytest.raises(OutboundRejected, match=reason):
        build_anonymous_shadow_payload(observation([row(text)]))


def test_incomplete_observation_and_page_mixing_are_not_payload_inputs():
    incomplete = observation([row("領収金額 4321円", confidence=None)])
    with pytest.raises(OutboundRejected, match="observation_incomplete"):
        build_anonymous_shadow_payload(incomplete)
    first = safe_build()
    second = build_anonymous_shadow_payload(observation([row("小計 7654円")], page=2))
    assert first.payload["unit_ref"] != second.payload["unit_ref"]
    assert first.amount_map.resolve("amount_A") == 4321
    assert second.amount_map.resolve("amount_A") == 7654


def test_preparation_returns_data_free_needs_review_and_binds_the_source_observation():
    withheld = prepare_anonymous_shadow(observation([row("4321円")]))
    assert withheld.status == "needs_review"
    assert withheld.build is None
    assert "4321" not in repr(withheld)
    build = safe_build()
    with pytest.raises(OutboundRejected, match="shadow_source_mismatch"):
        GeminiFreeTierShadowPolicy(enabled=True).prepare(
            build, observation([row("領収金額 7654円")]))
    build.payload["unit_ref"] = "unit_abcdefghijklmnopqrstuvwxyz"
    with pytest.raises(OutboundRejected, match="shadow_unit_mismatch"):
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
