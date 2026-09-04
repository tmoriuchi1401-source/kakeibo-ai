import pytest

from app import payroll_write_application_service as service
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_write_plan_materialization import payroll_write_plan_to_materialization_plan
from app.payroll_writer import PayrollAppendOutcome


def snapshot(*, statements=None):
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns) for key, columns in PAYROLL_SCHEMAS.items()],
        statements=statements or [],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="basic", section="earning", value_type="money",
        )],
    )


def candidate(index=1, **statement_overrides):
    preview = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08", parse_status="success",
        items=[PayrollItem(
            raw_item_name="basic", section="earnings", raw_value="300,000", value=300000,
            standard_item_candidate="basic_pay",
        )],
    )
    value = phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary", source_type="drive",
        source_file_id=f"file-{index}", content_hash=f"hash-{index}",
    )
    for field, field_value in statement_overrides.items():
        setattr(value.statement, field, field_value)
    return value


def plans(*indexes):
    return build_write_plan([candidate(index) for index in indexes], snapshot())


def materialization_plans(payroll_plans):
    return {
        plan.identity.statement_id: payroll_write_plan_to_materialization_plan(plan)
        for plan in payroll_plans if plan.status == "ready"
    }


class OutcomeWriter:
    def __init__(self, *, header="confirmed_success", items="confirmed_success"):
        self.header = header
        self.items = items
        self.calls = []

    def append_header_rows(self, rows):
        self.calls.append("header")
        return PayrollAppendOutcome(
            status=self.header,
            requested_rows=len(rows),
            confirmed_rows=len(rows) if self.header == "confirmed_success" else 0,
            failure_kind=(None if self.header == "confirmed_success" else "header_failure"),
        )

    def append_item_rows(self, rows):
        self.calls.append("items")
        return PayrollAppendOutcome(
            status=self.items,
            requested_rows=len(rows),
            confirmed_rows=len(rows) if self.items == "confirmed_success" else 0,
            failure_kind=(None if self.items == "confirmed_success" else "item_failure"),
        )


def apply(payroll_plans, writer, *, materializations=None, latest=None):
    return service.apply_payroll_write_application(
        payroll_plans,
        materialization_plans(payroll_plans) if materializations is None else materializations,
        writer,
        confirmed=True,
        latest_plans=lambda: payroll_plans if latest is None else latest,
    )


def test_all_applied_returns_unchanged_writer_result_and_in_memory_observations():
    payroll_plans = plans(1, 2)
    writer = OutcomeWriter()

    result = apply(payroll_plans, writer)

    assert result.writer_result.status == "completed"
    assert result.writer_result.applied is True
    assert [item.status for item in result.materialization_results] == ["applied", "applied"]
    assert len(result.audit_records) == 2
    assert result.projection_failures == ()
    assert writer.calls == ["header", "items", "header", "items"]


def test_confirmed_failure_remains_writer_failure_and_is_projected():
    payroll_plans = plans(1)
    writer = OutcomeWriter(header="confirmed_failure")

    result = apply(payroll_plans, writer)

    assert result.writer_result.status == "confirmed_failure"
    assert result.writer_result.results[0].outcome == "confirmed_failure"
    assert result.materialization_results[0].status == "failed"
    assert result.materialization_results[0].external_write is False
    assert len(result.audit_records) == 1
    assert writer.calls == ["header"]


def test_unknown_remains_writer_unknown_and_never_claims_no_external_write():
    payroll_plans = plans(1)
    result = apply(payroll_plans, OutcomeWriter(header="outcome_unknown"))

    assert result.writer_result.status == "header_outcome_unknown"
    assert result.writer_result.results[0].outcome_unknown is True
    materialization = result.materialization_results[0]
    assert materialization.status == "failed"
    assert materialization.external_write is False
    assert materialization.observed_after["outcome_unknown"] is True


def test_non_ready_writer_skip_is_preserved_without_inventing_materialization_intent():
    existing = candidate().statement.model_copy(update={"statement_id": "stored"})
    skipped_plan = build_write_plan([candidate()], snapshot(statements=[existing]))[0]
    writer = OutcomeWriter()

    result = apply([skipped_plan], writer)

    assert result.writer_result.results[0].outcome == "skipped"
    assert result.materialization_results == ()
    assert result.audit_records == ()
    assert result.projection_failures == ()
    assert writer.calls == []


def test_stale_plan_has_no_statement_results_or_invented_observations():
    payroll_plans = plans(1)
    writer = OutcomeWriter()

    result = apply(payroll_plans, writer, latest=plans(2))

    assert result.writer_result.status == "stale_plan"
    assert result.writer_result.results == ()
    assert result.materialization_results == ()
    assert result.audit_records == ()
    assert writer.calls == []


def test_not_attempted_statement_has_no_invented_materialization_or_audit():
    payroll_plans = plans(1, 2)
    writer = OutcomeWriter(header="outcome_unknown")

    result = apply(payroll_plans, writer)

    assert result.writer_result.status == "header_outcome_unknown"
    assert result.writer_result.not_attempted_statement_ids == (
        payroll_plans[1].identity.statement_id,
    )
    assert len(result.materialization_results) == len(result.audit_records) == 1
    assert result.materialization_results[0].observed_after["statement_id"] == (
        payroll_plans[0].identity.statement_id
    )


def test_mismatched_preview_materialization_plan_fails_closed_before_writer_call():
    payroll_plans = plans(1)
    other = plans(2)[0]
    wrong = {payroll_plans[0].identity.statement_id: payroll_write_plan_to_materialization_plan(other)}
    writer = OutcomeWriter()

    with pytest.raises(ValueError, match="materialization_plan_mismatch"):
        apply(payroll_plans, writer, materializations=wrong)

    assert writer.calls == []


@pytest.mark.parametrize("failure_stage", ["materialization", "audit"])
def test_projection_failure_is_reported_separately_from_completed_writer_result(
    monkeypatch, failure_stage,
):
    payroll_plans = plans(1)
    writer = OutcomeWriter()
    if failure_stage == "materialization":
        monkeypatch.setattr(
            service,
            "build_payroll_batch_materialization_results",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("raw provider response")),
        )
    else:
        monkeypatch.setattr(
            service,
            "build_materialization_audit_record",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("raw provider response")),
        )

    result = apply(payroll_plans, writer)

    assert result.writer_result.status == "completed"
    assert result.writer_result.applied is True
    assert result.writer_result.results[0].outcome == "written"
    assert len(result.projection_failures) == 1
    assert "raw provider response" not in repr(result.projection_failures)
    if failure_stage == "materialization":
        assert result.materialization_results == ()
        assert result.audit_records == ()
        assert result.projection_failures[0].stage == "materialization_result"
    else:
        assert len(result.materialization_results) == 1
        assert result.audit_records == ()
        assert result.projection_failures[0].stage == "audit_record"
