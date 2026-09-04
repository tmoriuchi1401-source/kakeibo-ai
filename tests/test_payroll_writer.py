import pytest

from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    PayrollStatementRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_writer import (
    PayrollAppendOutcome,
    PayrollWriterContractError,
    apply_payroll_write_plans,
    preview_payroll_write,
)


def snapshot(*, statements=None):
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns)
                 for key, columns in PAYROLL_SCHEMAS.items()],
        statements=statements or [],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        )],
    )


def candidate(index=1, **statement_overrides):
    preview = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        parse_status="success", items=[PayrollItem(
            raw_item_name="基本給", section="earnings", raw_value="300,000",
            value=300000, standard_item_candidate="basic_pay",
        )],
    )
    result = phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id=f"file-{index}",
        content_hash=f"hash-{index}",
    )
    for key, value in statement_overrides.items():
        setattr(result.statement, key, value)
    return result


class FakeAppendAdapter:
    def __init__(self, fail_stage=None):
        self.fail_stage = fail_stage
        self.calls = []

    def append_header_rows(self, rows):
        self.calls.append(("header", rows))
        if self.fail_stage == "header":
            raise RuntimeError("header append failed")
        return PayrollAppendOutcome(
            status="confirmed_success", requested_rows=len(rows),
            confirmed_rows=len(rows),
        )

    def append_item_rows(self, rows):
        self.calls.append(("items", rows))
        if self.fail_stage == "items":
            raise RuntimeError("item append failed")
        return PayrollAppendOutcome(
            status="confirmed_success", requested_rows=len(rows),
            confirmed_rows=len(rows),
        )


def apply(plans, adapter, latest=None, confirmed=True):
    return apply_payroll_write_plans(
        plans, adapter, confirmed=confirmed,
        latest_plans=lambda: plans if latest is None else latest,
    )


def test_ready_plan_writes_only_authoritative_rows_without_modification():
    plan = build_write_plan([candidate()], snapshot())[0]
    adapter = FakeAppendAdapter()

    result = apply([plan], adapter)

    assert result.status == "completed"
    assert result.applied is True
    assert adapter.calls == [
        ("header", plan.planned_header_rows),
        ("items", plan.planned_item_rows),
    ]
    assert adapter.calls[0][1][0] is plan.planned_header_rows[0]
    assert adapter.calls[1][1][0] is plan.planned_item_rows[0]


@pytest.mark.parametrize("kind", ["blocked", "skipped_duplicate"])
def test_non_ready_plan_performs_zero_writes(kind):
    if kind == "blocked":
        plan = build_write_plan([candidate(needs_review=True)], snapshot())[0]
    else:
        existing = candidate().statement.model_copy(update={"statement_id": "stored"})
        plan = build_write_plan([candidate()], snapshot(statements=[existing]))[0]
    adapter = FakeAppendAdapter()

    result = apply([plan], adapter)

    assert result.status == "completed"
    assert result.results[0].outcome == "skipped"
    assert adapter.calls == []


@pytest.mark.parametrize(
    "updates",
    [
        {"item_action": "none"},
        {"planned_item_rows": ()},
        {"eligibility": "ineligible"},
    ],
)
def test_malformed_ready_plan_fails_before_any_write(updates):
    plan = build_write_plan([candidate()], snapshot())[0]
    malformed = plan.model_copy(update=updates)
    adapter = FakeAppendAdapter()

    with pytest.raises(PayrollWriterContractError):
        apply([plan, malformed], adapter)

    assert adapter.calls == []


def test_header_or_item_identity_mismatch_fails_closed():
    plan = build_write_plan([candidate()], snapshot())[0]
    header = plan.planned_header_rows[0]
    changed_header = header.model_copy(update={
        "values": tuple(
            "different" if column == "source_file_id" else value
            for column, value in zip(header.columns, header.values)
        ),
    })
    item = plan.planned_item_rows[0]
    changed_item = item.model_copy(update={
        "values": tuple(
            "different" if column == "statement_id" else value
            for column, value in zip(item.columns, item.values)
        ),
    })
    adapter = FakeAppendAdapter()

    for malformed in (
        plan.model_copy(update={"planned_header_rows": (changed_header,)}),
        plan.model_copy(update={"planned_item_rows": (changed_item,)}),
    ):
        with pytest.raises(PayrollWriterContractError):
            apply([malformed], adapter)

    assert adapter.calls == []


def test_multiple_plans_preserve_plan_and_header_item_order():
    plans = build_write_plan([candidate(1), candidate(2)], snapshot())
    adapter = FakeAppendAdapter()

    apply(plans, adapter)

    assert [(stage, rows[0].as_dict()["statement_id"])
            for stage, rows in adapter.calls] == [
        ("header", plans[0].identity.statement_id),
        ("items", plans[0].identity.statement_id),
        ("header", plans[1].identity.statement_id),
        ("items", plans[1].identity.statement_id),
    ]


def test_mixed_ready_and_blocked_writes_only_ready_plan():
    plans = build_write_plan(
        [candidate(1), candidate(2, parse_status="partial")], snapshot(),
    )
    adapter = FakeAppendAdapter()

    result = apply(plans, adapter)

    assert [entry.outcome for entry in result.results] == ["written", "skipped"]
    assert [stage for stage, _rows in adapter.calls] == ["header", "items"]


def test_header_failure_has_unknown_outcome_and_stops_remaining_plans():
    plans = build_write_plan([candidate(1), candidate(2)], snapshot())
    adapter = FakeAppendAdapter(fail_stage="header")

    result = apply(plans, adapter)

    assert result.status == "header_outcome_unknown"
    assert result.results[0].failure_stage == "header"
    assert result.results[0].outcome_unknown is True
    assert result.not_attempted_statement_ids == (plans[1].identity.statement_id,)
    assert [stage for stage, _rows in adapter.calls] == ["header"]


def test_item_failure_reports_partial_write_and_never_rolls_back_or_continues():
    plans = build_write_plan([candidate(1), candidate(2)], snapshot())
    adapter = FakeAppendAdapter(fail_stage="items")

    result = apply(plans, adapter)

    assert result.status == "partial_failure"
    attempt = result.results[0]
    assert attempt.header_rows_confirmed == 1
    assert attempt.item_rows_confirmed == 0
    assert attempt.failure_stage == "items"
    assert attempt.outcome_unknown is True
    assert result.not_attempted_statement_ids == (plans[1].identity.statement_id,)
    assert [stage for stage, _rows in adapter.calls] == ["header", "items"]


@pytest.mark.parametrize(
    ("malformed_stage", "expected_status"),
    [("header", "header_outcome_unknown"), ("items", "partial_failure")],
)
def test_malformed_adapter_outcome_is_conservatively_unknown(
    malformed_stage, expected_status,
):
    plan = build_write_plan([candidate()], snapshot())[0]

    class MalformedOutcomeAdapter(FakeAppendAdapter):
        def append_header_rows(self, rows):
            outcome = super().append_header_rows(rows)
            return None if malformed_stage == "header" else outcome

        def append_item_rows(self, rows):
            outcome = super().append_item_rows(rows)
            return None if malformed_stage == "items" else outcome

    adapter = MalformedOutcomeAdapter()

    result = apply([plan], adapter)

    assert result.status == expected_status
    assert result.results[0].outcome_unknown is True
    assert [stage for stage, _rows in adapter.calls] == (
        ["header"] if malformed_stage == "header" else ["header", "items"]
    )


def test_stale_preflight_stops_every_write():
    source = candidate()
    preview_plan = build_write_plan([source], snapshot())[0]
    stored_header = PayrollStatementRecord.model_validate(
        preview_plan.planned_header_rows[0].as_dict(),
    )
    latest_plan = build_write_plan(
        [source], snapshot(statements=[stored_header]),
    )[0]
    adapter = FakeAppendAdapter()

    result = apply([preview_plan], adapter, latest=[latest_plan])

    assert result.status == "stale_plan"
    assert result.not_attempted_statement_ids == (
        preview_plan.identity.statement_id,
    )
    assert adapter.calls == []


def test_retry_requires_replan_and_duplicate_plan_performs_zero_writes():
    source = candidate()
    first_plan = build_write_plan([source], snapshot())[0]
    first_adapter = FakeAppendAdapter(fail_stage="items")
    assert apply([first_plan], first_adapter).status == "partial_failure"

    stored_header = PayrollStatementRecord.model_validate(
        first_plan.planned_header_rows[0].as_dict(),
    )
    retry_plan = build_write_plan([source], snapshot(statements=[stored_header]))[0]
    retry_adapter = FakeAppendAdapter()
    retry_result = apply([retry_plan], retry_adapter)

    assert retry_plan.status == "skipped_duplicate"
    assert retry_result.results[0].outcome == "skipped"
    assert retry_adapter.calls == []


def test_preview_and_missing_apply_confirmation_never_call_adapter():
    plans = build_write_plan([candidate()], snapshot())
    adapter = FakeAppendAdapter()

    preview = preview_payroll_write(plans)
    assert preview.ready_count == 1
    assert preview.header_rows == preview.item_rows == 1
    assert adapter.calls == []

    with pytest.raises(RuntimeError, match="--apply"):
        apply(plans, adapter, confirmed=False)
    assert adapter.calls == []


def test_repeated_ready_statement_id_is_invalid_batch_contract():
    plan = build_write_plan([candidate()], snapshot())[0]
    adapter = FakeAppendAdapter()

    with pytest.raises(PayrollWriterContractError, match="repeated"):
        apply([plan, plan], adapter)

    assert adapter.calls == []
