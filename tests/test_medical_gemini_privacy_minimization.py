"""v2 leakage probes: entirely artificial observations, no source receipts."""
from dataclasses import replace
from itertools import permutations
import json

import pytest
import app.medical_gemini_shadow as s
from test_medical_gemini_anonymous_shadow import (
    isolated_shadow, observation, row, safe_build, response,
)


def seeded_build(monkeypatch, source):
    # Fixed local entropy makes non-interference byte equality testable; the
    # production implementation always uses secrets, never this test generator.
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    monkeypatch.setattr(s, "_opaque_id", lambda prefix: prefix + next(letters) * 32)
    class ReverseRandom:
        def shuffle(self, values):
            values.reverse()
    monkeypatch.setattr(s.secrets, "SystemRandom", ReverseRandom)
    return s.build_anonymous_shadow_payload(source)


def wire(build, source):
    return s._validated_request_for_transport(build, source)._for_transport()


def test_privacy_exact_closed_schema_has_no_geometry_text_amount_or_metadata():
    source = observation([row("領収金額 4321円"), row("小計 7654円", x=45)])
    build = s.build_anonymous_shadow_payload(source)
    data = json.loads(wire(build, source))
    assert set(data) == {"schema_version", "unit_ref", "candidates"}
    assert all(set(item) == {"candidate_id"} for item in data["candidates"])
    encoded = json.dumps(data)
    for forbidden in ("geometry", "width", "height", "area", "ordinal", "confidence",
                      "regions", "relations", "context", "competing", "unresolved",
                      "4321", "7654", "metadata", "filename", "path", "drive_id", "page"):
        assert forbidden not in encoded
    assert "領収金額" not in encoded and "小計" not in encoded
    assert len(data["candidates"]) == 1


@pytest.mark.parametrize("extra", [
    row("小計 7654円", x=45),              # irrelevant excluded amount
    row("保険者支払額 7654円", x=45),
    row("小計", x=45),                     # excluded anchor
    row("支払額", x=45),                   # payment anchor
    row("ARTIFICIAL_UNRELATED_LABEL", x=45),
    row("7654", x=45),                     # unassigned unrelated numeric
    row("小計 7654円", x=45, confidence=None),
])
def test_privacy_irrelevant_regions_do_not_change_outbound(monkeypatch, extra):
    base = observation([row("領収金額 4321円")])
    extended = observation([extra, row("領収金額 4321円")])
    a = seeded_build(monkeypatch, base)
    b = seeded_build(monkeypatch, extended)
    assert wire(a, base) == wire(b, extended)
    assert a._source_fingerprint != b._source_fingerprint  # local source binding survives


def test_privacy_hidden_anchor_no_longer_changes_competing_bit(monkeypatch):
    base = observation([row("支払額", x=1), row("支払額 4321円", x=25)])
    extended = observation([row("支払額", x=1), row("支払額 4321円", x=25),
                            row("UNRELATED", x=45)])
    a, b = seeded_build(monkeypatch, base), seeded_build(monkeypatch, extended)
    assert wire(a, base) == wire(b, extended)
    assert "competing_structure" not in a.payload


def test_privacy_geometry_page_confidence_and_source_identity_are_local(monkeypatch):
    a_source = observation([row("領収金額 4321円", x=1, confidence=.91)], page=1)
    b_source = replace(observation([row("領収金額 4321円", x=45, confidence=.99)], page=9),
                       unit_id="different-artificial-unit", engine="different-artificial-engine")
    a = seeded_build(monkeypatch, a_source)
    b = seeded_build(monkeypatch, b_source)
    assert wire(a, a_source) == wire(b, b_source)


def test_privacy_input_permutations_do_not_drive_ids_order_or_local_mapping(monkeypatch):
    rows = [row("領収金額 4321円"), row("支払額 7654円", x=25), row("小計", x=45)]
    seen = []
    for ordered in permutations(rows):
        source = observation(list(ordered))
        build = seeded_build(monkeypatch, source)
        seen.append((wire(build, source), build.amount_map._values))
    assert all(item == seen[0] for item in seen)
    # The deterministic TEST shuffle reverses local numeric sorting. Neither
    # candidate IDs nor their order encode the source ordinal.
    assert tuple(value for _, value in seen[0][1]) == (7654, 4321)


def test_privacy_repeated_builds_rotate_ids_but_cardinality_residual_is_explicit():
    source = observation([row("領収金額 4321円"), row("支払額 7654円", x=25)])
    a, b = s.build_anonymous_shadow_payload(source), s.build_anonymous_shadow_payload(source)
    assert a.payload["unit_ref"] != b.payload["unit_ref"]
    assert set(a.amount_map.ids).isdisjoint(b.amount_map.ids)
    assert a.payload["candidates"] != b.payload["candidates"]
    # Once random labels are removed only candidate count remains; this is a
    # minimization assertion, explicitly NOT a proof of unlinkability.
    assert len(a.payload["candidates"]) == len(b.payload["candidates"]) == 2


@pytest.mark.parametrize("count", [0, 5])
def test_privacy_candidate_count_budget_withholds_out_of_bounds(count):
    source = observation([row("支払額 4321円") for _ in range(count)] or [row("小計")])
    assert s.prepare_anonymous_shadow(source).status == "needs_review"


@pytest.mark.parametrize("confidence", [.89, .65, None])
def test_privacy_candidate_quality_is_local_withholding(confidence):
    source = observation([row("支払額 4321円", confidence=confidence)])
    assert s.prepare_anonymous_shadow(source).build is None


@pytest.mark.parametrize("field", [
    "geometry", "width", "height", "area", "ordinal", "confidence", "context",
    "relations", "state", "metadata", "filename", "path", "drive_id",
])
def test_privacy_removed_fields_are_rejected_not_ignored(field):
    build = safe_build()
    build.payload["candidates"][0][field] = "ARTIFICIAL"
    with pytest.raises(s.OutboundRejected):
        s.FinalOutboundGate().serialize(build.payload)


def test_privacy_old_schema_and_old_response_version_cannot_be_used():
    build = safe_build()
    build.payload["schema_version"] = "medical-anonymous-shadow-v1"
    with pytest.raises(s.OutboundRejected):
        s.FinalOutboundGate().validate_for_transport(build.payload)
    build = safe_build()
    with pytest.raises(s.InboundRejected):
        s.FinalInboundGate().accept(build, response(
            build, schema_version="medical-anonymous-shadow-response-v1"))


@pytest.mark.parametrize("mutation", ["payload", "map", "literals"])
def test_privacy_build_integrity_seals_wire_local_map_and_scan_context(mutation):
    source = observation([row("領収金額 4321円")])
    build = s.build_anonymous_shadow_payload(source)
    if mutation == "payload":
        build.payload["candidates"][0]["candidate_id"] = "candidate_" + "Z" * 32
    elif mutation == "map":
        build = replace(build, amount_map=s.LocalAmountMap(((build.amount_map.ids[0], 7654),)))
    else:
        build = replace(build, _response_private_literals=())
    with pytest.raises(s.OutboundRejected):
        s._validated_request_for_transport(build, source)


def test_privacy_multi_candidate_selection_is_withheld_even_for_valid_id(monkeypatch):
    build = s.build_anonymous_shadow_payload(observation(
        [row("領収金額 4321円"), row("支払額 7654円", x=25)]))
    monkeypatch.setattr(s.LocalAmountMap, "resolve", lambda *a: pytest.fail("must not rehydrate"))
    with pytest.raises(s.InboundRejected, match="insufficient_selection_evidence"):
        s.FinalInboundGate().accept(build, response(build))
    for decision in ("abstain", "unresolved"):
        accepted = s.FinalInboundGate().accept(
            build, response(build, decision=decision, amount_id=None, confidence=None))
        result = s.rehydrate_anonymous_response(build, accepted)
        assert result.status == "needs_review" and result.amount is None


def test_privacy_single_candidate_rehydrates_only_matching_bound_response():
    build = safe_build()
    accepted = s.FinalInboundGate().accept(build, response(build))
    assert s.rehydrate_anonymous_response(build, accepted).amount == 4321
    with pytest.raises(s.InboundRejected):
        s.rehydrate_anonymous_response(safe_build(), accepted)
