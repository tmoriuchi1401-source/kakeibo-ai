from dataclasses import replace

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_review_authority import (
    apply_payroll_review_assertion,
    capture_payroll_review_evidence,
    create_payroll_review_assertion,
    preview_payroll_review_assertion,
)
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan


KEY = b"synthetic-payroll-review-key-0001"


def snapshot():
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns)
                 for key, columns in PAYROLL_SCHEMAS.items()],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        )],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1", employer_label="Example Employer",
        )],
    )


def pending_candidate(*, raw_value="300,000"):
    preview = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        parse_status="success", items=[PayrollItem(
            raw_item_name="確認対象", section="earning", raw_value=raw_value,
            value=300000 if raw_value is not None else None,
            needs_review=True, review_reason_code="ambiguous_ownership",
        )],
    )
    return phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id="file-1", content_hash="hash-1",
    )


def assertion(candidate, sheets, **overrides):
    evidence = capture_payroll_review_evidence(
        candidate, sheets, 0, local_key=KEY,
    )
    values = dict(
        decision="confirm_existing_value", operator_id="reviewer-1",
        standard_item_id="basic_pay", reviewed_value=300000,
    )
    values.update(overrides)
    return evidence, create_payroll_review_assertion(
        evidence, local_key=KEY, **values,
    )


def test_preview_is_read_only_and_confirmed_apply_can_make_plan_ready():
    sheets = snapshot()
    candidate = pending_candidate()
    evidence, decision = assertion(candidate, sheets)

    preview = preview_payroll_review_assertion(
        candidate, sheets, evidence, decision, local_key=KEY,
    )
    unconfirmed = apply_payroll_review_assertion(
        candidate, sheets, evidence, decision, local_key=KEY,
    )

    assert preview.accepted
    assert not unconfirmed.applied
    assert candidate.items[0].needs_review
    assert build_write_plan([candidate], sheets)[0].status == "blocked"

    applied = apply_payroll_review_assertion(
        candidate, sheets, evidence, decision, local_key=KEY, confirmed=True,
    )
    assert applied.applied
    assert not applied.candidate.items[0].needs_review
    assert applied.candidate.items[0].review_status == "corrected"
    assert applied.candidate.items[0].value == 300000
    assert build_write_plan([applied.candidate], sheets)[0].status == "ready"
    assert candidate.items[0].needs_review


def test_exclusion_requires_explicit_value_free_decision_and_confirmation():
    sheets = snapshot()
    candidate = pending_candidate(raw_value=None)
    evidence, decision = assertion(
        candidate, sheets, decision="exclude_non_item",
        standard_item_id=None, reviewed_value=None,
    )

    applied = apply_payroll_review_assertion(
        candidate, sheets, evidence, decision, local_key=KEY, confirmed=True,
    )

    assert applied.applied
    assert applied.candidate.items[0].review_status == "confirmed"
    assert applied.candidate.items[0].value is None
    assert build_write_plan([applied.candidate], sheets)[0].reason == "no_planned_items"


def test_stale_tampered_replayed_and_nonexact_assertions_fail_closed():
    sheets = snapshot()
    candidate = pending_candidate()
    evidence, decision = assertion(candidate, sheets)

    tampered = replace(decision, reviewed_value=1)
    assert preview_payroll_review_assertion(
        candidate, sheets, evidence, tampered, local_key=KEY,
    ).reason_code == "assertion_signature_invalid"

    stale_candidate = candidate.model_copy(deep=True)
    stale_candidate.statement.content_hash = "changed"
    assert preview_payroll_review_assertion(
        stale_candidate, sheets, evidence, decision, local_key=KEY,
    ).reason_code == "review_evidence_stale"

    wrong_value_evidence, wrong_value = assertion(
        candidate, sheets, reviewed_value=1,
    )
    assert preview_payroll_review_assertion(
        candidate, sheets, wrong_value_evidence, wrong_value, local_key=KEY,
    ).reason_code == "reviewed_value_not_exact_raw_value"

    applied = apply_payroll_review_assertion(
        candidate, sheets, evidence, decision, local_key=KEY, confirmed=True,
    )
    assert preview_payroll_review_assertion(
        applied.candidate, sheets, evidence, decision, local_key=KEY,
    ).reason_code == "review_evidence_not_current"


def test_schema_parser_source_and_business_scope_are_evidence_bound():
    sheets = snapshot()
    candidate = pending_candidate()
    evidence, decision = assertion(candidate, sheets)

    changed_schema = snapshot()
    changed_schema.standard_items[0].section = "reference"
    assert preview_payroll_review_assertion(
        candidate, changed_schema, evidence, decision, local_key=KEY,
    ).reason_code == "review_evidence_stale"

    for field, value, reason in (
        ("parser_version", "changed", "review_evidence_stale"),
        ("source_file_id", "other", "review_evidence_stale"),
        ("source_type", "local", "review_evidence_stale"),
        ("employer_id", "other", "review_evidence_not_current"),
        ("statement_type", "bonus", "review_evidence_stale"),
        ("pay_period", "2026-09", "review_evidence_stale"),
    ):
        changed = candidate.model_copy(deep=True)
        setattr(changed.statement, field, value)
        assert preview_payroll_review_assertion(
            changed, sheets, evidence, decision, local_key=KEY,
        ).reason_code == reason

    changed_item = candidate.model_copy(deep=True)
    changed_item.items[0].raw_value = "1"
    assert preview_payroll_review_assertion(
        changed_item, sheets, evidence, decision, local_key=KEY,
    ).reason_code == "review_evidence_stale"
