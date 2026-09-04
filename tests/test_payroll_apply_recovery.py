import socket

import pytest

from app.payroll_apply_recovery import apply_payroll_write_with_recovery
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_writer import PayrollAppendOutcome


class FakeAppendWriter:
    def __init__(self, *, header_error=None, item_error=None):
        self.header_error = header_error
        self.item_error = item_error
        self.calls = []

    def append_header_rows(self, rows):
        self.calls.append(("header", rows))
        if self.header_error is not None:
            raise self.header_error
        return PayrollAppendOutcome(
            status="confirmed_success",
            requested_rows=len(rows),
            confirmed_rows=len(rows),
        )

    def append_item_rows(self, rows):
        self.calls.append(("items", rows))
        if self.item_error is not None:
            raise self.item_error
        return PayrollAppendOutcome(
            status="confirmed_success",
            requested_rows=len(rows),
            confirmed_rows=len(rows),
        )


class FakeRecoveryReader:
    def __init__(self, *, headers=(), items=(), error=None):
        self.headers = headers
        self.items = items
        self.error = error
        self.calls = []

    def read_header_rows(self, statement_id):
        self.calls.append(("header", statement_id))
        if self.error is not None:
            raise self.error
        return self.headers

    def read_item_rows(self, statement_id):
        self.calls.append(("items", statement_id))
        if self.error is not None:
            raise self.error
        return self.items


def snapshot():
    return PayrollSheetsSnapshot(
        schemas=[
            validate_sheet_schema(key, columns)
            for key, columns in PAYROLL_SCHEMAS.items()
        ],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay",
            standard_name="基本給",
            section="earning",
            value_type="money",
        )],
    )


def ready_plan(index=1):
    preview = PayrollPreview(
        file_type="pdf",
        extraction_method="pdf_text",
        pay_period="2026-08",
        parse_status="success",
        items=[PayrollItem(
            raw_item_name="基本給",
            section="earnings",
            raw_value="300,000",
            value=300000,
            standard_item_candidate="basic_pay",
        )],
    )
    candidate = phase_a_to_storage_candidate(
        preview,
        employer_id="employer-1",
        statement_type="salary",
        source_type="drive",
        source_file_id=f"file-{index}",
        content_hash=f"hash-{index}",
    )
    return build_write_plan([candidate], snapshot())[0]


def run(plans, writer, reader, *, latest=None, confirmed=True):
    return apply_payroll_write_with_recovery(
        plans,
        writer,
        reader,
        confirmed=confirmed,
        latest_plans=lambda: plans if latest is None else latest,
    )


def test_write_success_is_confirmed_only_after_exact_readback():
    plan = ready_plan()
    writer = FakeAppendWriter()
    reader = FakeRecoveryReader(
        headers=plan.planned_header_rows,
        items=plan.planned_item_rows,
    )

    result = run([plan], writer, reader)

    assert result.status == "confirmed"
    statement = result.statements[0]
    assert statement.status == "confirmed"
    assert statement.writer_outcome == "written"
    assert statement.recovery.verification == "confirmed"
    assert [stage for stage, _rows in writer.calls] == ["header", "items"]
    assert len(reader.calls) == 2
    assert result.automatic_retry_performed is False


def test_write_success_with_mismatch_is_uncertain_and_not_retried():
    plan = ready_plan()
    changed_item = plan.planned_item_rows[0].model_copy(update={
        "values": (*plan.planned_item_rows[0].values[:-1], 99),
    })
    writer = FakeAppendWriter()
    reader = FakeRecoveryReader(
        headers=plan.planned_header_rows,
        items=(changed_item,),
    )

    result = run([plan], writer, reader)

    assert result.status == "uncertain_requires_readback_or_review"
    assert result.statements[0].recovery.verification == "mismatch"
    assert len(writer.calls) == 2


def test_readback_failure_is_uncertain_and_never_retries_write():
    plan = ready_plan()
    writer = FakeAppendWriter()
    reader = FakeRecoveryReader(error=OSError("read reset"))

    result = run([plan], writer, reader)

    assert result.status == "uncertain_requires_readback_or_review"
    assert result.statements[0].recovery.verification == "read_failed"
    assert len(writer.calls) == 2
    assert reader.calls == [("header", plan.identity.statement_id)]


def test_unknown_write_can_converge_to_confirmed_after_exact_readback():
    plan = ready_plan()
    writer = FakeAppendWriter(header_error=socket.timeout("timed out"))
    reader = FakeRecoveryReader(
        headers=plan.planned_header_rows,
        items=plan.planned_item_rows,
    )

    result = run([plan], writer, reader)

    assert result.status == "confirmed"
    assert result.statements[0].writer_outcome == "header_outcome_unknown"
    assert result.statements[0].status == "confirmed"
    assert [stage for stage, _rows in writer.calls] == ["header"]
    assert len(reader.calls) == 2


def test_unknown_write_with_missing_readback_requires_fresh_plan_not_retry():
    plan = ready_plan()
    writer = FakeAppendWriter(header_error=ConnectionResetError("reset"))
    reader = FakeRecoveryReader()

    result = run([plan], writer, reader)

    assert result.status == "safe_to_create_fresh_plan"
    assert result.statements[0].recovery.verification == "missing"
    assert [stage for stage, _rows in writer.calls] == ["header"]
    assert len(reader.calls) == 2


def test_partial_write_is_uncertain_even_when_header_readback_matches():
    plan = ready_plan()
    writer = FakeAppendWriter(item_error=OSError("item connection reset"))
    reader = FakeRecoveryReader(headers=plan.planned_header_rows)

    result = run([plan], writer, reader)

    assert result.status == "uncertain_requires_readback_or_review"
    assert result.statements[0].writer_outcome == "partial_failure"
    assert result.statements[0].recovery.verification == "mismatch"
    assert [stage for stage, _rows in writer.calls] == ["header", "items"]
    assert len(reader.calls) == 2


def test_stale_preflight_with_preexisting_exact_state_performs_zero_writes():
    plan = ready_plan()
    stale = plan.model_copy(update={
        "eligibility": "ineligible",
        "status": "skipped_duplicate",
        "reason": "exact_duplicate",
        "reasons": ("exact_duplicate",),
        "header_action": "none",
        "item_action": "none",
        "planned_header_rows": (),
        "planned_item_rows": (),
        "duplicate": plan.duplicate.model_copy(update={
            "status": "exact_duplicate",
            "reason": "exact_duplicate",
            "matched_statement_id": plan.identity.statement_id,
        }),
    })
    writer = FakeAppendWriter()
    reader = FakeRecoveryReader(
        headers=plan.planned_header_rows,
        items=plan.planned_item_rows,
    )

    result = run([plan], writer, reader, latest=[stale])

    assert result.write_result.status == "stale_plan"
    assert result.status == "no_write_required"
    assert result.statements[0].status == "no_write_required"
    assert writer.calls == []
    assert len(reader.calls) == 2


def test_stale_preflight_with_ambiguous_state_stops_without_write():
    plan = ready_plan()
    stale = plan.model_copy(update={
        "eligibility": "ineligible",
        "status": "skipped_duplicate",
        "reason": "exact_duplicate",
        "reasons": ("exact_duplicate",),
        "header_action": "none",
        "item_action": "none",
        "planned_header_rows": (),
        "planned_item_rows": (),
        "duplicate": plan.duplicate.model_copy(update={
            "status": "exact_duplicate",
            "reason": "exact_duplicate",
        }),
    })
    writer = FakeAppendWriter()
    reader = FakeRecoveryReader(
        headers=(plan.planned_header_rows[0], plan.planned_header_rows[0]),
        items=plan.planned_item_rows,
    )

    result = run([plan], writer, reader, latest=[stale])

    assert result.status == "uncertain_requires_readback_or_review"
    assert result.statements[0].recovery.verification == "ambiguous"
    assert writer.calls == []


def test_apply_guard_stops_writer_and_reader_before_any_operation():
    plan = ready_plan()
    writer = FakeAppendWriter()
    reader = FakeRecoveryReader()

    with pytest.raises(RuntimeError, match="--apply"):
        run([plan], writer, reader, confirmed=False)

    assert writer.calls == []
    assert reader.calls == []


def test_single_invocation_calls_writer_once_and_never_continues_after_failure():
    plans = [ready_plan(1), ready_plan(2)]
    writer = FakeAppendWriter(header_error=socket.timeout("timed out"))
    reader = FakeRecoveryReader(
        headers=plans[0].planned_header_rows,
        items=plans[0].planned_item_rows,
    )

    result = run(plans, writer, reader)

    assert result.status == "uncertain_requires_readback_or_review"
    assert [stage for stage, _rows in writer.calls] == ["header"]
    assert [entry.status for entry in result.statements] == [
        "confirmed", "not_attempted",
    ]
    assert len(reader.calls) == 2
