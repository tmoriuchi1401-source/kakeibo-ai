"""Synthetic-only tests for the structured medical AI shadow boundary."""
from __future__ import annotations

from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

import app.medical_ai_structured_shadow as shadow
from app.medical_ai_structured_shadow import (
    FakeStructuredShadowTransport,
    GeminiStructuredSyntheticTransport,
    StructuredOutboundGate,
    StructuredShadowPolicy,
    StructuredShadowRejected,
    build_structured_shadow_payload,
    evaluate_real_medical_offline,
    handoff_to_existing_review_first,
    prepare_structured_shadow_from_level2,
    run_structured_shadow,
)
from app.medical_gemini_shadow import HttpResponse
from app.medical_ocr_observation_shadow import ReceiptImage, make_observation
from app.medical_payment_level2_shadow import evaluate_level2_payment_shadow
from app.medical_receipt_privacy import build_receipt_privacy_preview
from app.medical_review_workflow_shadow import MedicalReviewItem, ReviewProvenance


def observation(*, competitor=False, negative=False, pii=False):
    rows = [
        {"text": "領収金額", "polygon": [(10, 10), (90, 10), (90, 20), (10, 20)],
         "confidence": .96, "detection_confidence": .98},
        {"text": "1,200円", "polygon": [(100, 11), (160, 11), (160, 21), (100, 21)],
         "confidence": .94, "detection_confidence": .97},
    ]
    if competitor:
        rows.append({"text": "2,400円", "polygon": [(170, 12), (230, 12), (230, 22), (170, 22)],
                     "confidence": .93, "detection_confidence": .96})
    if negative:
        rows.append({"text": "小計 9,999円", "polygon": [(100, 13), (190, 13), (190, 23), (100, 23)],
                     "confidence": .95, "detection_confidence": .96})
    if pii:
        rows.extend([
            {"text": "山田太郎", "polygon": [(10, 40), (80, 40), (80, 50), (10, 50)],
             "confidence": .92, "detection_confidence": .95},
            {"text": "患者ID AB-123456", "polygon": [(10, 55), (120, 55), (120, 65), (10, 65)],
             "confidence": .91, "detection_confidence": .94},
            {"text": "東京都千代田区1-2-3", "polygon": [(10, 70), (160, 70), (160, 80), (10, 80)],
             "confidence": .90, "detection_confidence": .93},
        ])
    image = ReceiptImage("synthetic-unit-only", 1, b"synthetic-image-only")
    return make_observation(image, "rapidocr-shadow", ("a" * 64,), 300, 200, rows)


def response(build, *, amount_id=None, label_ids=None, decision="select",
             reason="selected_bound_evidence", **changes):
    value = {
        "schema_version": shadow.RESPONSE_SCHEMA_VERSION,
        "request_schema_version": shadow.REQUEST_SCHEMA_VERSION,
        "document_id": build.payload["synthetic_document_id"],
        "unit_ref": build.payload["unit_ref"],
        "provenance_ref": build.payload["provenance_ref"],
        "document_type": "medical_receipt",
        "payment_amount_token_id": amount_id,
        "payment_label_token_ids": label_ids or [],
        "decision": decision,
        "reason_code": reason,
    }
    value.update(changes)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def ids(build):
    return build.tokens[1].token_id, [build.tokens[0].token_id]


def enabled():
    return StructuredShadowPolicy(transport_enabled=True, kill_switch_engaged=False)


def test_anonymous_payload_has_closed_token_schema_and_no_source_identity():
    observed = observation(pii=True)
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    payload = build.payload
    assert set(payload) == {
        "schema_version", "synthetic_document_id", "unit_ref", "provenance_ref",
        "ocr_provenance_schema_version", "tokens",
    }
    assert payload["schema_version"] == shadow.REQUEST_SCHEMA_VERSION
    assert all(set(token) == {
        "token_id", "unit_ref", "anonymous_text", "normalized_geometry", "confidence_bucket"
    } for token in payload["tokens"])
    wire = json.dumps(payload, ensure_ascii=False)
    for private in ("山田太郎", "AB-123456", "東京都", "synthetic-unit-only", "1,200円"):
        assert private not in wire
    for forbidden in ("filename", "path", "drive_id", "raw_image", "source_identity"):
        assert forbidden not in wire.casefold()
    assert payload["tokens"][0]["anonymous_text"] == "<PAYMENT_LABEL>"
    assert payload["tokens"][1]["anonymous_text"] == "<NUMERIC>"
    assert all(0 <= value <= 10000 for token in payload["tokens"]
               for value in token["normalized_geometry"].values())


def test_pii_like_nonsemantic_tokens_are_fully_redacted():
    build = build_structured_shadow_payload(observation(pii=True), source_kind="synthetic_fixture")
    assert [token["anonymous_text"] for token in build.payload["tokens"][2:]] == [
        "<REDACTED>", "<NUMERIC>", "<NUMERIC>"
    ]


def test_default_policy_and_kill_switch_block_before_transport():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    transport = FakeStructuredShadowTransport(response(build, amount_id=ids(build)[0], label_ids=ids(build)[1]))
    assert run_structured_shadow(build, observed, transport).reason_code == "disabled"
    assert run_structured_shadow(
        build, observed, transport,
        StructuredShadowPolicy(transport_enabled=True, kill_switch_engaged=True),
    ).reason_code == "disabled"
    assert transport.invocations == 0


def test_real_medical_payload_can_only_be_evaluated_offline():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="real_medical")
    transport = FakeStructuredShadowTransport(b"should-not-be-used")
    result = run_structured_shadow(build, observed, transport, enabled())
    assert result.reason_code == "real_medical_outbound_rejected"
    assert transport.invocations == 0
    assert evaluate_real_medical_offline(build, observed) == {
        "anonymous_payload_generated": 1,
        "real_medical_outbound_rejected": 1,
        "evidence_binding_possible": 1,
        "review_first_required": 1,
        "production_authorized": 0,
        "write_authorized": 0,
    }


def test_existing_level2_is_an_explicit_value_free_preparation_boundary():
    observed = observation()
    level2 = evaluate_level2_payment_shadow(observed)
    preparation = prepare_structured_shadow_from_level2(
        observed, level2, source_kind="real_medical"
    )
    assert preparation.level2_evidence_state == level2.payment_role_evidence_completeness
    assert preparation.build.source_kind == "real_medical"
    assert preparation.production_authorized is False
    assert not hasattr(preparation, "amount")


def test_ai_result_can_only_link_to_an_existing_pending_review_item_without_value():
    provenance = ReviewProvenance(parser_version="parser-v1", policy_version="policy-v1")
    item = MedicalReviewItem(
        review_item_id="a" * 64,
        receipt_unit_ref="b" * 64,
        reason_code="structural_relationship_unresolved",
        parser_status="complete",
        materialization_status="stable",
        candidate_count=0,
        ambiguity_category="geometry_ambiguity",
        provenance=provenance,
        ux_requirement="payment_candidate_position",
    )
    result = shadow.StructuredShadowResult(
        "needs_review", "accepted_shadow_candidate", "select", 1200
    )
    handoff = handoff_to_existing_review_first(result, item)
    assert handoff.review_item_id == item.review_item_id
    assert handoff.receipt_unit_ref == item.receipt_unit_ref
    assert handoff.status == "pending_existing_review"
    assert handoff.ai_value_forwarded is False
    assert handoff.production_authorized is handoff.write_authorized is False
    assert not hasattr(handoff, "amount")


def test_valid_selection_is_locally_bound_but_never_authoritative():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    amount_id, label_ids = ids(build)
    transport = FakeStructuredShadowTransport(response(build, amount_id=amount_id, label_ids=label_ids))
    result = run_structured_shadow(build, observed, transport, enabled())
    assert result.status == "needs_review"
    assert result.reason_code == "accepted_shadow_candidate"
    assert result.decision == "select"
    assert result.candidate_amount == 1200
    assert result.production_authorized is result.write_authorized is False
    assert transport.invocations == 1


@pytest.mark.parametrize("bad", [b"not-json", b"{}", b'{"schema_version":NaN}'])
def test_malformed_response_rejects_without_authority(bad):
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    result = run_structured_shadow(build, observed, FakeStructuredShadowTransport(bad), enabled())
    assert result.status == "needs_review"
    assert result.reason_code == "malformed_response"
    assert result.candidate_amount is None
    assert result.production_authorized is result.write_authorized is False


def test_duplicate_response_key_rejected():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    valid = response(build, decision="abstain", reason="insufficient_evidence")
    bad = valid[:-1] + b',"decision":"select"}'
    result = run_structured_shadow(build, observed, FakeStructuredShadowTransport(bad), enabled())
    assert result.reason_code == "malformed_response"


def test_hallucinated_amount_or_label_token_is_rejected():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    amount_id, label_ids = ids(build)
    for bad in (
        response(build, amount_id="token_" + "Z" * 32, label_ids=label_ids),
        response(build, amount_id=amount_id, label_ids=["token_" + "Y" * 32]),
    ):
        result = run_structured_shadow(
            build, observed, FakeStructuredShadowTransport(bad), enabled()
        )
        assert result.reason_code == "binding_rejected"
        assert result.candidate_amount is None


@pytest.mark.parametrize("field", ["document_id", "unit_ref", "provenance_ref", "request_schema_version"])
def test_document_unit_provenance_and_schema_binding(field):
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    amount_id, label_ids = ids(build)
    bad_value = shadow.REQUEST_SCHEMA_VERSION + "-old" if field == "request_schema_version" else "doc_" + "Q" * 32
    result = run_structured_shadow(
        build, observed,
        FakeStructuredShadowTransport(
            response(build, amount_id=amount_id, label_ids=label_ids, **{field: bad_value})
        ), enabled(),
    )
    assert result.reason_code == "binding_rejected"


def test_ambiguous_competitor_selection_rejected():
    observed = observation(competitor=True)
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    result = run_structured_shadow(
        build, observed,
        FakeStructuredShadowTransport(
            response(build, amount_id=build.tokens[1].token_id,
                     label_ids=[build.tokens[0].token_id])
        ), enabled(),
    )
    assert result.reason_code == "ambiguous_evidence"
    assert result.candidate_amount is None


def test_negative_context_numeric_never_becomes_competitor_or_selection():
    observed = observation(negative=True)
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    assert build.payload["tokens"][2]["anonymous_text"] == "<NEGATIVE_CONTEXT> <NUMERIC>"
    good = run_structured_shadow(
        build, observed,
        FakeStructuredShadowTransport(
            response(build, amount_id=build.tokens[1].token_id,
                     label_ids=[build.tokens[0].token_id])
        ), enabled(),
    )
    assert good.reason_code == "accepted_shadow_candidate"
    bad = run_structured_shadow(
        build_structured_shadow_payload(observed, source_kind="synthetic_fixture"),
        observed,
        FakeStructuredShadowTransport(b"{}"), enabled(),
    )
    # A direct gate/binder check below uses the matching build and negative ID.
    second = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    selected_negative = run_structured_shadow(
        second, observed,
        FakeStructuredShadowTransport(
            response(second, amount_id=second.tokens[2].token_id,
                     label_ids=[second.tokens[0].token_id])
        ), enabled(),
    )
    assert bad.reason_code == "malformed_response"
    assert selected_negative.reason_code == "binding_rejected"


def test_changed_observation_and_mutated_payload_fail_before_transport():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    changed = observation(pii=True)
    transport = FakeStructuredShadowTransport(b"{}")
    assert run_structured_shadow(build, changed, transport, enabled()).reason_code == "invalid_binding"
    mutated_payload = dict(build.payload)
    mutated_payload["tokens"] = list(build.payload["tokens"]) + [{"raw_ocr_text": "forbidden"}]
    mutated = replace(build, payload=mutated_payload)
    assert run_structured_shadow(mutated, observed, transport, enabled()).reason_code == "invalid_binding"
    assert transport.invocations == 0


def test_abstain_has_no_tokens_or_amount():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    result = run_structured_shadow(
        build, observed,
        FakeStructuredShadowTransport(
            response(build, decision="abstain", reason="insufficient_evidence")
        ), enabled(),
    )
    assert result.decision == "abstain"
    assert result.candidate_amount is None
    assert result.production_authorized is result.write_authorized is False


def test_response_cannot_free_generate_an_amount_or_explanation():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    amount_id, label_ids = ids(build)
    base = json.loads(response(build, amount_id=amount_id, label_ids=label_ids))
    for extra in ({"payment_amount": 1200}, {"reason": "synthetic explanation"}):
        result = run_structured_shadow(
            build, observed,
            FakeStructuredShadowTransport(
                json.dumps(base | extra, separators=(",", ":")).encode()
            ), enabled(),
        )
        assert result.reason_code == "malformed_response"
        assert result.candidate_amount is None


def test_shadow_observation_does_not_change_production_preview():
    production_text = "病院 診療\n領収金額"
    before = build_receipt_privacy_preview(production_text, ())
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    amount_id, label_ids = ids(build)
    run_structured_shadow(
        build, observed,
        FakeStructuredShadowTransport(response(build, amount_id=amount_id, label_ids=label_ids)),
        enabled(),
    )
    assert build_receipt_privacy_preview(production_text, ()) == before


def test_gemini_transport_sends_only_validated_synthetic_payload_and_parses_envelope():
    observed = observation(pii=True)
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    amount_id, label_ids = ids(build)
    candidate = response(build, amount_id=amount_id, label_ids=label_ids).decode()
    envelope = json.dumps({"candidates": [{"content": {"parts": [
        {"text": candidate, "thoughtSignature": "synthetic-provider-signature"}
    ]}}]}).encode()

    class Executor:
        request = None

        def execute(self, request):
            self.request = request
            return HttpResponse(200, "application/json; charset=utf-8", envelope)

    class KeyProvider:
        def get(self):
            return "synthetic-test-key"

    executor = Executor()
    transport = GeminiStructuredSyntheticTransport(executor, KeyProvider())
    result = run_structured_shadow(build, observed, transport, enabled())
    assert result.reason_code == "accepted_shadow_candidate"
    body = executor.request.body.decode("ascii")
    for private in ("山田太郎", "AB-123456", "東京都", "1,200円", "synthetic-unit-only"):
        assert private not in body
    assert "synthetic-test-key" not in body
    assert executor.request.url == shadow._GEMINI_GENERATE_CONTENT_URL


def test_gemini_envelope_rejects_unknown_part_metadata():
    candidate = b'{"synthetic":"response"}'.decode()
    raw = json.dumps({"candidates": [{"content": {"parts": [
        {"text": candidate, "unknown": "metadata"}
    ]}}]}).encode()
    with pytest.raises(ValueError):
        shadow._extract_structured_gemini_text(raw)


def test_production_modules_do_not_import_structured_shadow():
    root = Path(__file__).parents[1]
    production = (
        "receipt_pipeline.py", "receipt_privacy_gate.py", "receipt_text_extraction.py",
        "sheets.py", "drive_receipts.py", "cli.py",
    )
    for name in production:
        tree = ast.parse((root / "app" / name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and "medical_ai_structured_shadow" in ast.unparse(node)
            for node in ast.walk(tree)
        )


def test_gate_capability_is_single_use():
    observed = observation()
    build = build_structured_shadow_payload(observed, source_kind="synthetic_fixture")
    request = StructuredOutboundGate().validate_for_transport(build, observed, enabled())
    transport = FakeStructuredShadowTransport(b"{}")
    transport.send(request)
    with pytest.raises(StructuredShadowRejected, match="request_reused"):
        transport.send(request)
