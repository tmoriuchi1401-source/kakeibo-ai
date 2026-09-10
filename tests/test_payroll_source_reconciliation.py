from dataclasses import replace

import pytest

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_source_reconciliation import (
    RECONCILIATION_VERSION,
    apply_payroll_source_reconciliation,
    create_payroll_source_reconciliation_decision,
    deserialize_payroll_source_reconciliation_decision,
    preview_payroll_source_reconciliation,
    serialize_payroll_source_reconciliation_decision,
)
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollEmployerRecord,
    PayrollStandardItemRecord,
    PayrollStatementRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_writer import apply_payroll_write_plans, preview_payroll_write


KEY = b"payroll-reconciliation-test-key-01"


def target(**updates):
    values = {
        "statement_id": "stored-source-3",
        "employer_id": "employer-1",
        "statement_type": "salary",
        "pay_period": "2026-08",
        "gross_pay": 740669,
        "total_deductions": 172381,
        "net_pay": 568288,
        "parse_status": "success",
        "needs_review": False,
        "source_type": "drive",
        "source_file_id": "source-3",
        "content_hash": "3" * 64,
    }
    values.update(updates)
    return PayrollStatementRecord(**values)


def alternate(**updates):
    preview = PayrollPreview(
        file_type="image",
        extraction_method="ocr",
        pay_period="2026-08",
        gross_pay=740669,
        total_deductions=172381,
        net_pay=568288,
        parse_status="success",
        items=[PayrollItem(
            raw_item_name="非課税合計",
            section="summary",
            raw_value="38,430",
            value=38430,
            standard_item_candidate="non_taxable_total",
            needs_review=True,
            review_reason_code="low_confidence",
        )],
    )
    result = phase_a_to_storage_candidate(
        preview,
        employer_id="employer-1",
        statement_type="salary",
        source_type="drive",
        source_file_id="source-4",
        content_hash="4" * 64,
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="non_taxable_total",
            standard_name="非課税合計",
            section="reference",
            value_type="money",
        )],
    )
    for key, value in updates.items():
        setattr(result.statement, key, value)
    return result


def snapshot(stored=None):
    return PayrollSheetsSnapshot(
        schemas=[
            validate_sheet_schema(key, columns)
            for key, columns in PAYROLL_SCHEMAS.items()
        ],
        statements=[stored or target()],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="non_taxable_total",
            standard_name="非課税合計",
            section="reference",
            value_type="money",
        )],
        employers=[PayrollEmployerRecord(
            employer_id="employer-1",
            employer_label="Employer",
        )],
    )


def decision(stored=None, source=None, **updates):
    value = create_payroll_source_reconciliation_decision(
        target=stored or target(),
        alternate=source or alternate(),
        operator_id="operator-1",
        created_at_utc="2026-09-11T00:00:00+00:00",
        local_key=KEY,
    )
    return replace(value, **updates) if updates else value


def test_operator_decision_reconciles_to_zero_row_duplicate_without_mutation():
    source = alternate()
    sheets = snapshot()
    source_before = source.model_dump(mode="json")
    sheets_before = sheets.model_dump(mode="json")
    original = build_write_plan([source], sheets)[0]

    result = apply_payroll_source_reconciliation(
        source, sheets, decision(source=source), local_key=KEY, confirmed=True,
    )

    assert original.status == "blocked"
    assert original.reason == "revision_conflict"
    assert original.duplicate.reason == "statement_key"
    assert result.applied
    assert result.preview.classification == "alternate_source"
    assert result.plan.status == "skipped_duplicate"
    assert result.plan.reason == "operator_reconciled_alternate_source"
    assert result.plan.duplicate.status == "duplicate"
    assert result.plan.duplicate.matched_statement_id == "stored-source-3"
    assert result.plan.planned_header_rows == ()
    assert result.plan.planned_item_rows == ()
    assert source.model_dump(mode="json") == source_before
    assert sheets.model_dump(mode="json") == sheets_before
    assert source.items[0].needs_review


def test_statement_key_alone_and_unconfirmed_decision_remain_blocked():
    source = alternate()
    sheets = snapshot()
    missing = apply_payroll_source_reconciliation(
        source, sheets, None, local_key=KEY, confirmed=True,
    )
    unconfirmed = apply_payroll_source_reconciliation(
        source, sheets, decision(source=source), local_key=KEY,
    )

    assert not missing.applied
    assert missing.preview.reason_code == "reconciliation_decision_required"
    assert missing.plan.status == "blocked"
    assert not unconfirmed.applied
    assert unconfirmed.preview.accepted
    assert unconfirmed.plan.status == "blocked"


def test_fresh_process_serialization_round_trip_replays_same_zero_row_plan():
    source = alternate()
    sheets = snapshot()
    payload = serialize_payroll_source_reconciliation_decision(
        decision(source=source),
    )

    loaded = deserialize_payroll_source_reconciliation_decision(
        payload, local_key=KEY,
    )
    result = apply_payroll_source_reconciliation(
        source.model_copy(deep=True),
        sheets.model_copy(deep=True),
        loaded,
        local_key=KEY,
        confirmed=True,
    )

    assert result.applied
    assert result.plan.status == "skipped_duplicate"
    assert preview_payroll_write([result.plan]).model_dump() == {
        "plan_count": 1,
        "ready_count": 0,
        "blocked_count": 0,
        "skipped_duplicate_count": 1,
        "header_rows": 0,
        "item_rows": 0,
    }


def test_reconciled_plan_invokes_no_writer():
    source = alternate()
    sheets = snapshot()
    reconciled = apply_payroll_source_reconciliation(
        source, sheets, decision(source=source), local_key=KEY, confirmed=True,
    ).plan

    class NoWrite:
        def __init__(self):
            self.calls = []

        def append_header_rows(self, rows):
            self.calls.append(("header", rows))
            raise AssertionError("must not write")

        def append_item_rows(self, rows):
            self.calls.append(("items", rows))
            raise AssertionError("must not write")

    writer = NoWrite()
    result = apply_payroll_write_plans(
        [reconciled], writer, confirmed=True, latest_plans=lambda: [reconciled],
    )

    assert result.status == "completed"
    assert result.results[0].outcome == "skipped"
    assert writer.calls == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing_target", "reconciliation_target_not_unique"),
        ("wrong_target_id", "reconciliation_target_not_unique"),
        ("target_source", "reconciliation_target_source_mismatch"),
        ("target_content", "reconciliation_target_source_mismatch"),
        ("alternate_source", "reconciliation_alternate_source_mismatch"),
        ("alternate_content", "reconciliation_alternate_source_mismatch"),
        ("target_employer", "reconciliation_target_business_mismatch"),
        ("target_type", "reconciliation_target_business_mismatch"),
        ("target_period", "reconciliation_target_business_mismatch"),
        ("target_gross", "reconciliation_target_business_mismatch"),
        ("target_deductions", "reconciliation_target_business_mismatch"),
        ("target_net", "reconciliation_target_business_mismatch"),
        ("alternate_employer", "reconciliation_alternate_business_mismatch"),
        ("alternate_type", "reconciliation_alternate_business_mismatch"),
        ("alternate_period", "reconciliation_alternate_business_mismatch"),
        ("alternate_gross", "reconciliation_alternate_business_mismatch"),
        ("alternate_deductions", "reconciliation_alternate_business_mismatch"),
        ("alternate_net", "reconciliation_alternate_business_mismatch"),
    ],
)
def test_binding_mismatches_fail_closed(change, reason):
    stored = target()
    source = alternate()
    signed = decision(stored=stored, source=source)
    sheets = snapshot(stored)

    if change == "missing_target":
        sheets.statements = []
    elif change == "wrong_target_id":
        sheets.statements[0].statement_id = "other"
    elif change == "target_source":
        sheets.statements[0].source_file_id = "changed"
    elif change == "target_content":
        sheets.statements[0].content_hash = "9" * 64
    elif change == "alternate_source":
        source.statement.source_file_id = "changed"
    elif change == "alternate_content":
        source.statement.content_hash = "9" * 64
    elif change.startswith("target_"):
        field = change.removeprefix("target_")
        field = {
            "employer": "employer_id",
            "type": "statement_type",
            "period": "pay_period",
            "gross": "gross_pay",
            "deductions": "total_deductions",
            "net": "net_pay",
        }[field]
        setattr(sheets.statements[0], field, "changed" if field in {"employer_id", "statement_type", "pay_period"} else 1)
    else:
        field = change.removeprefix("alternate_")
        field = {
            "employer": "employer_id",
            "type": "statement_type",
            "period": "pay_period",
            "gross": "gross_pay",
            "deductions": "total_deductions",
            "net": "net_pay",
        }[field]
        setattr(source.statement, field, "changed" if field in {"employer_id", "statement_type", "pay_period"} else 1)

    result = preview_payroll_source_reconciliation(
        source, sheets, signed, local_key=KEY,
    )
    assert not result.accepted
    assert result.reason_code == reason


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ({"contract_version": "stale"}, "reconciliation_contract_version_stale"),
        ({"decision_revision": 2}, "reconciliation_decision_revision_stale"),
        ({"provenance": "untrusted"}, "reconciliation_provenance_invalid"),
        ({"signature": "0" * 64}, "reconciliation_signature_invalid"),
    ],
)
def test_stale_or_tampered_decision_fails_closed(tamper, reason):
    source = alternate()
    result = preview_payroll_source_reconciliation(
        source, snapshot(), decision(source=source, **tamper), local_key=KEY,
    )
    assert not result.accepted
    assert result.reason_code == reason


def test_serialized_tamper_and_wrong_key_are_rejected():
    payload = serialize_payroll_source_reconciliation_decision(decision())
    values = __import__("json").loads(payload)
    values["gross_pay"] += 1
    tampered = __import__("json").dumps(values)

    with pytest.raises(ValueError, match="signature_invalid"):
        deserialize_payroll_source_reconciliation_decision(
            tampered, local_key=KEY,
        )
    with pytest.raises(ValueError, match="signature_invalid"):
        deserialize_payroll_source_reconciliation_decision(
            payload, local_key=b"different-reconciliation-key-000",
        )


def test_exact_duplicate_does_not_need_or_accept_reconciliation_override():
    source = alternate(
        source_file_id="source-3",
        content_hash="3" * 64,
    )
    original = build_write_plan([source], snapshot())[0]
    assert original.status == "skipped_duplicate"

    signed = create_payroll_source_reconciliation_decision(
        target=target(), alternate=source, operator_id="operator-1",
        created_at_utc="2026-09-11T00:00:00+00:00", local_key=KEY,
    )
    preview = preview_payroll_source_reconciliation(
        source, snapshot(), signed, local_key=KEY,
    )
    assert not preview.accepted
    assert preview.reason_code == "reconciliation_distinct_source_required"


def test_contract_version_is_stable():
    assert RECONCILIATION_VERSION == "payroll-source-reconciliation-v1"
