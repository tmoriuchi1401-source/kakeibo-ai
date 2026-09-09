"""Synthetic proof for optional post-WritePlan ownership attestation."""
from dataclasses import replace

import pytest

from app.payroll_diagnostic_evidence import observe_tokens
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_ocr import PositionedText
from app.payroll_ownership_provenance import (
    PayrollOwnershipPlanBinding,
    PayrollStorageAuthorityEvidence,
    analyze_successful_claim_authority,
    attest_payroll_write_plan_ownership,
    capture_candidate_enumeration,
    evaluate_adoption_candidate,
    reconstruct_consumption,
)
from app.payroll_parser import parse_positioned_items
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_write_plan_materialization import (
    payroll_write_plan_to_materialization_plan,
)
from app.payroll_ownership_integration import (
    PayrollOwnershipAttestationRequest,
    evaluate_payroll_ownership_attestation_integration,
)
from app.payroll_writer import preview_payroll_write


KEY = b"synthetic-attestation-key-00000000"


def ownership_candidate():
    tokens = (
        PositionedText("基本給", 1, 0, 20, 30, 10, 100),
        PositionedText("1,234", 1, 40, 20, 30, 10, 100),
    )
    snapshot = observe_tokens(
        tokens, local_key=KEY, parser_mode="ocr",
        snapshot_context=("attestation-proof", "ocr"),
    )
    provenance = reconstruct_consumption(snapshot)
    ledger = capture_candidate_enumeration(snapshot)
    authority = analyze_successful_claim_authority(snapshot, provenance, ledger)
    claim = authority.claims[0]
    evaluated = evaluate_adoption_candidate(
        snapshot, provenance, ledger, claim,
        employer_scope="employer-1", expected_employer_scope="employer-1",
        source_replay_closed=True,
        storage_evidence=PayrollStorageAuthorityEvidence(
            "basic_pay", False, True, None,
        ),
    )
    assert evaluated.accepted
    return tokens, evaluated.candidate


def sheets_snapshot():
    return PayrollSheetsSnapshot(
        schemas=[
            validate_sheet_schema(key, columns)
            for key, columns in PAYROLL_SCHEMAS.items()
        ],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        )],
    )


def storage_and_plan(*, unknown=False):
    preview = PayrollPreview(
        file_type="image", extraction_method="ocr", pay_period="2026-08",
        parse_status="success", items=[PayrollItem(
            raw_item_name="独自手当" if unknown else "基本給",
            section="earnings", raw_value="1,234", value=1234,
            standard_item_candidate=None if unknown else "basic_pay",
            needs_review=False,
        )],
    )
    storage = phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id="file-1", content_hash="hash-1",
    )
    return storage, build_write_plan([storage], sheets_snapshot())[0]


def binding(candidate, plan, **overrides):
    values = dict(
        portable_claim_id=candidate.portable_claim_id,
        snapshot_id=candidate.snapshot_id,
        parser_mode=candidate.parser_mode,
        employer_scope=candidate.employer_scope,
        source_file_id=plan.identity.source_file_id,
        content_hash=plan.identity.content_hash,
        authoritative_standard_item_id=candidate.authoritative_standard_item_id,
        enumerated_candidate_id=candidate.enumerated_candidate_id,
        authoritative_value=1234,
        source_alignment_closed=True,
    )
    values.update(overrides)
    return PayrollOwnershipPlanBinding(**values)


def changed_item_plan(plan, field, value):
    row = plan.planned_item_rows[0]
    values = row.as_dict()
    values[field] = value
    changed = row.model_copy(update={
        "values": tuple(values[column] for column in row.columns),
    })
    return plan.model_copy(update={"planned_item_rows": (changed,)})


def test_authoritative_candidate_attests_after_ready_write_plan_idempotently():
    _tokens, candidate = ownership_candidate()
    _storage, plan = storage_and_plan()
    proof = binding(candidate, plan)
    first = attest_payroll_write_plan_ownership(plan, candidate, proof, local_key=KEY)
    second = attest_payroll_write_plan_ownership(plan, candidate, proof, local_key=KEY)
    assert first.accepted
    assert first.attestation == second.attestation
    assert first.attestation.authoritative_standard_item_id == "basic_pay"
    assert not hasattr(first.attestation, "write")


@pytest.mark.parametrize("change, reason", [
    ({"snapshot_id": "other"}, "ownership_candidate_scope_mismatch"),
    ({"employer_scope": "other"}, "ownership_candidate_scope_mismatch"),
    ({"parser_mode": "pdf"}, "ownership_candidate_scope_mismatch"),
    ({"enumerated_candidate_id": "other"}, "ownership_candidate_scope_mismatch"),
    ({"binding_version": "old"}, "plan_binding_version_stale"),
    ({"review_authority_contaminated": True}, "review_authority_contamination"),
    ({"source_alignment_closed": False}, "source_alignment_unclosed"),
    ({"source_file_id": "other"}, "write_plan_source_or_employer_mismatch"),
    ({"content_hash": "other"}, "write_plan_source_or_employer_mismatch"),
])
def test_attestation_rejects_scope_version_and_review_contamination(change, reason):
    _tokens, candidate = ownership_candidate()
    _storage, plan = storage_and_plan()
    result = attest_payroll_write_plan_ownership(
        plan, candidate, binding(candidate, plan, **change), local_key=KEY,
    )
    assert not result.accepted
    assert result.reason_code == reason


@pytest.mark.parametrize("field, value, reason", [
    ("standard_item_id", "other", "write_plan_field_relation_mismatch"),
    ("value", 9999, "write_plan_value_mismatch"),
    ("needs_review", True, "write_plan_review_contamination"),
])
def test_attestation_rejects_write_plan_field_value_and_review_mismatch(
    field, value, reason,
):
    _tokens, candidate = ownership_candidate()
    _storage, plan = storage_and_plan()
    changed = changed_item_plan(plan, field, value)
    result = attest_payroll_write_plan_ownership(
        changed, candidate, binding(candidate, plan), local_key=KEY,
    )
    assert not result.accepted
    assert result.reason_code == reason


def test_fallback_unknown_with_value_and_missing_candidate_are_withheld():
    _tokens, candidate = ownership_candidate()
    _storage, unknown_plan = storage_and_plan(unknown=True)
    assert unknown_plan.status == "blocked"
    result = attest_payroll_write_plan_ownership(
        unknown_plan, candidate, binding(candidate, unknown_plan), local_key=KEY,
    )
    assert not result.accepted
    assert result.reason_code == "write_plan_not_authoritative_ready"
    missing = attest_payroll_write_plan_ownership(
        unknown_plan, None, binding(candidate, unknown_plan), local_key=KEY,
    )
    assert not missing.accepted
    assert missing.reason_code == "ownership_adoption_candidate_required"
    assert preview_payroll_write([unknown_plan]).blocked_count == 1


def test_stale_candidate_contract_and_invalid_commitment_key_fail_closed():
    _tokens, candidate = ownership_candidate()
    _storage, plan = storage_and_plan()
    stale = replace(candidate, adoption_contract_version="old")
    assert attest_payroll_write_plan_ownership(
        plan, stale, binding(candidate, plan), local_key=KEY,
    ).reason_code == "adoption_contract_version_stale"
    assert attest_payroll_write_plan_ownership(
        plan, candidate, binding(candidate, plan), local_key=b"short",
    ).reason_code == "attestation_key_invalid"
    tampered = replace(candidate, portable_claim_id="f" * 64)
    assert attest_payroll_write_plan_ownership(
        plan, tampered, binding(tampered, plan), local_key=KEY,
    ).reason_code == "ownership_candidate_identity_invalid"


def test_attestation_is_a_sidecar_and_changes_no_production_visible_value():
    tokens, candidate = ownership_candidate()
    storage, plan = storage_and_plan()
    parser_before = tuple(parse_positioned_items(tokens, ocr=True))
    storage_before = storage.model_dump(mode="json")
    plan_before = plan.model_dump(mode="json")
    writer_before = preview_payroll_write([plan]).model_dump(mode="json")
    materialization_before = payroll_write_plan_to_materialization_plan(plan)

    result = attest_payroll_write_plan_ownership(
        plan, candidate, binding(candidate, plan), local_key=KEY,
    )
    assert result.accepted
    assert tuple(parse_positioned_items(tokens, ocr=True)) == parser_before
    assert storage.model_dump(mode="json") == storage_before
    assert plan.model_dump(mode="json") == plan_before
    assert preview_payroll_write([plan]).model_dump(mode="json") == writer_before
    assert payroll_write_plan_to_materialization_plan(plan) == materialization_before


def integrated(candidate, plan, **binding_overrides):
    return evaluate_payroll_ownership_attestation_integration(
        [plan],
        [PayrollOwnershipAttestationRequest(
            statement_id=plan.identity.statement_id,
            candidate=candidate,
            binding=binding(candidate, plan, **binding_overrides),
        )],
        enabled=True,
        local_key=KEY,
    ).records[0].evaluation


def test_integration_disabled_preserves_every_production_visible_value():
    tokens, candidate = ownership_candidate()
    storage, plan = storage_and_plan()
    request = PayrollOwnershipAttestationRequest(
        statement_id=plan.identity.statement_id,
        candidate=candidate,
        binding=binding(candidate, plan),
    )
    before = (
        tuple(parse_positioned_items(tokens, ocr=True)),
        storage.model_dump(mode="json"),
        storage.statement.needs_review,
        tuple((item.needs_review, item.review_status) for item in storage.items),
        plan.model_dump(mode="json"),
        preview_payroll_write([plan]).model_dump(mode="json"),
        payroll_write_plan_to_materialization_plan(plan),
    )

    evidence = evaluate_payroll_ownership_attestation_integration(
        [plan], [request], enabled=False, local_key=KEY,
    )

    after = (
        tuple(parse_positioned_items(tokens, ocr=True)),
        storage.model_dump(mode="json"),
        storage.statement.needs_review,
        tuple((item.needs_review, item.review_status) for item in storage.items),
        plan.model_dump(mode="json"),
        preview_payroll_write([plan]).model_dump(mode="json"),
        payroll_write_plan_to_materialization_plan(plan),
    )
    assert evidence.enabled is False
    assert evidence.status == "disabled"
    assert evidence.records == ()
    assert after == before


def test_synthetic_enabled_authoritative_standard_claim_attests_without_writer():
    _tokens, candidate = ownership_candidate()
    _storage, plan = storage_and_plan()

    result = integrated(candidate, plan)

    assert result.accepted
    assert result.reason_code == "ownership_write_plan_attestation_closed"
    assert result.attestation.plan_statement_id == plan.identity.statement_id


@pytest.mark.parametrize("change, reason", [
    ({"employer_scope": "other"}, "ownership_candidate_scope_mismatch"),
    ({"parser_mode": "pdf"}, "ownership_candidate_scope_mismatch"),
    ({"snapshot_id": "other"}, "ownership_candidate_scope_mismatch"),
    ({"enumerated_candidate_id": "hidden"}, "ownership_candidate_scope_mismatch"),
    ({"source_alignment_closed": False}, "source_alignment_unclosed"),
    ({"review_authority_contaminated": True}, "review_authority_contamination"),
])
def test_synthetic_enabled_scope_hidden_stale_and_review_fail_closed(change, reason):
    _tokens, candidate = ownership_candidate()
    _storage, plan = storage_and_plan()

    result = integrated(candidate, plan, **change)

    assert not result.accepted
    assert result.reason_code == reason
    assert result.attestation is None


@pytest.mark.parametrize("field, value, reason", [
    ("standard_item_id", "other", "write_plan_field_relation_mismatch"),
    ("value", 9999, "write_plan_value_mismatch"),
    ("needs_review", True, "write_plan_review_contamination"),
])
def test_synthetic_enabled_plan_field_value_and_review_fail_closed(field, value, reason):
    _tokens, candidate = ownership_candidate()
    _storage, plan = storage_and_plan()
    changed = changed_item_plan(plan, field, value)

    result = integrated(candidate, changed)

    assert not result.accepted
    assert result.reason_code == reason


def test_synthetic_enabled_fallback_unknown_and_stale_evidence_fail_closed():
    _tokens, candidate = ownership_candidate()
    _storage, unknown_plan = storage_and_plan(unknown=True)
    fallback = evaluate_payroll_ownership_attestation_integration(
        [unknown_plan],
        [PayrollOwnershipAttestationRequest(
            statement_id=unknown_plan.identity.statement_id,
            candidate=None,
            binding=binding(candidate, unknown_plan),
        )],
        enabled=True,
        local_key=KEY,
    ).records[0].evaluation
    assert not fallback.accepted
    assert fallback.reason_code == "ownership_adoption_candidate_required"

    _storage, plan = storage_and_plan()
    stale = integrated(replace(candidate, adoption_contract_version="old"), plan)
    assert not stale.accepted
    assert stale.reason_code == "adoption_contract_version_stale"
