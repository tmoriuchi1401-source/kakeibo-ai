import pytest

from app.payroll_google_sheets_adapter import PayrollGoogleSheetsRecoveryReadAdapter
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_recovery import build_payroll_recovery_preview
from app.payroll_sheets import SHEET_TITLES, PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_ITEM_COLUMNS,
    PAYROLL_SCHEMAS,
    PAYROLL_STATEMENT_COLUMNS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import PayrollPlannedRow, build_write_plan


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeValues:
    def __init__(self, ranges, calls):
        self.ranges = ranges
        self.calls = calls

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        result = self.ranges.get(kwargs["range"], [])
        if isinstance(result, BaseException):
            raise result
        return FakeRequest({"values": result})

    def append(self, **kwargs):
        self.calls.append(("append", kwargs))
        raise AssertionError("recovery read must never write")

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        raise AssertionError("recovery read must never write")


class FakeService:
    def __init__(self, ranges):
        self.calls = []
        self._values = FakeValues(ranges, self.calls)

    def spreadsheets(self):
        return type("Spreadsheets", (), {"values": lambda instance: self._values})()


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


def ranges_for(plan, *, headers=None, items=None):
    return {
        f"'{SHEET_TITLES['payroll_statements']}'!A1:ZZ": [
            list(PAYROLL_STATEMENT_COLUMNS),
            *(list(row.values) for row in (headers or ())),
        ],
        f"'{SHEET_TITLES['payroll_items']}'!A1:ZZ": [
            list(PAYROLL_ITEM_COLUMNS),
            *(list(row.values) for row in (items or ())),
        ],
    }


def preview_for(plan, *, headers=None, items=None):
    service = FakeService(ranges_for(plan, headers=headers, items=items))
    reader = PayrollGoogleSheetsRecoveryReadAdapter("sheet-id", service=service)
    return build_payroll_recovery_preview(plan, reader), service


def test_exact_readback_confirms_idempotent_completion_without_write():
    plan = ready_plan()

    result, service = preview_for(
        plan,
        headers=plan.planned_header_rows,
        items=plan.planned_item_rows,
    )

    assert result.verification == "confirmed"
    assert result.disposition == "no_write_required"
    assert result.provenance == plan.identity
    assert result.assessment.header_matches_expected is True
    assert result.safe_to_automatic_retry is False
    assert result.external_write_authorized is False
    assert [kind for kind, _kwargs in service.calls] == ["get", "get"]
    assert all(kwargs["valueRenderOption"] == "UNFORMATTED_VALUE"
               for _kind, kwargs in service.calls)


def test_repeated_exact_readback_remains_idempotent_without_write():
    plan = ready_plan()
    service = FakeService(ranges_for(
        plan,
        headers=plan.planned_header_rows,
        items=plan.planned_item_rows,
    ))
    reader = PayrollGoogleSheetsRecoveryReadAdapter("sheet-id", service=service)

    first = build_payroll_recovery_preview(plan, reader)
    second = build_payroll_recovery_preview(plan, reader)

    assert first.verification == second.verification == "confirmed"
    assert [kind for kind, _kwargs in service.calls] == ["get", "get", "get", "get"]


def test_absent_header_and_items_require_fresh_preview_not_automatic_write():
    result, service = preview_for(ready_plan())

    assert result.verification == "missing"
    assert result.disposition == "fresh_plan_required"
    assert result.assessment.header_state == "absent"
    assert result.assessment.item_state == "absent"
    assert all(kind == "get" for kind, _kwargs in service.calls)


def test_partial_write_is_mismatch_and_requires_manual_review():
    plan = ready_plan()
    result, _service = preview_for(plan, headers=plan.planned_header_rows)

    assert result.verification == "mismatch"
    assert result.disposition == "manual_review_required"
    assert result.assessment.header_matches_expected is True
    assert result.assessment.item_state == "absent"


def test_header_missing_but_items_present_is_mismatch_not_retriable():
    plan = ready_plan()
    result, _service = preview_for(plan, items=plan.planned_item_rows)

    assert result.verification == "mismatch"
    assert result.disposition == "manual_review_required"
    assert result.assessment.header_state == "absent"
    assert result.assessment.item_state == "complete"


def test_item_value_mismatch_requires_manual_review():
    plan = ready_plan()
    altered = list(plan.planned_item_rows[0].values)
    altered[PAYROLL_ITEM_COLUMNS.index("value")] = 999999
    changed_item = PayrollPlannedRow(
        columns=PAYROLL_ITEM_COLUMNS,
        values=tuple(altered),
    )

    result, _service = preview_for(
        plan,
        headers=plan.planned_header_rows,
        items=(changed_item,),
    )

    assert result.verification == "mismatch"
    assert result.disposition == "manual_review_required"
    assert result.assessment.item_state == "incomplete_or_duplicate"


def test_full_header_comparison_detects_non_identity_mismatch():
    plan = ready_plan()
    altered = list(plan.planned_header_rows[0].values)
    altered[PAYROLL_STATEMENT_COLUMNS.index("net_pay")] = 999999
    changed_header = PayrollPlannedRow(
        columns=PAYROLL_STATEMENT_COLUMNS,
        values=tuple(altered),
    )

    result, _service = preview_for(
        plan,
        headers=(changed_header,),
        items=plan.planned_item_rows,
    )

    assert result.verification == "mismatch"
    assert result.disposition == "manual_review_required"
    assert result.assessment.header_state == "identity_confirmed"
    assert result.assessment.header_matches_expected is False


def test_duplicate_target_rows_are_ambiguous_not_success():
    plan = ready_plan()
    result, _service = preview_for(
        plan,
        headers=(plan.planned_header_rows[0], plan.planned_header_rows[0]),
        items=plan.planned_item_rows,
    )

    assert result.verification == "ambiguous"
    assert result.disposition == "manual_review_required"
    assert result.assessment.matching_header_count == 2


def test_reader_uses_statement_id_not_a_guessed_row_number():
    plan = ready_plan()
    unrelated_header = list(plan.planned_header_rows[0].values)
    unrelated_header[0] = "other-statement"
    unrelated_item = list(plan.planned_item_rows[0].values)
    unrelated_item[1] = "other-statement"
    result, _service = preview_for(
        plan,
        headers=(
            PayrollPlannedRow(columns=PAYROLL_STATEMENT_COLUMNS,
                              values=tuple(unrelated_header)),
            plan.planned_header_rows[0],
        ),
        items=(
            PayrollPlannedRow(columns=PAYROLL_ITEM_COLUMNS,
                              values=tuple(unrelated_item)),
            *plan.planned_item_rows,
        ),
    )

    assert result.verification == "confirmed"
    assert result.assessment.observed_item_count == len(plan.planned_item_rows)


def test_schema_mismatch_fails_closed_without_write():
    plan = ready_plan()
    ranges = ranges_for(plan)
    header_range = f"'{SHEET_TITLES['payroll_statements']}'!A1:ZZ"
    ranges[header_range][0] = ["wrong_column"]
    service = FakeService(ranges)
    reader = PayrollGoogleSheetsRecoveryReadAdapter("sheet-id", service=service)

    result = build_payroll_recovery_preview(plan, reader)

    assert result.verification == "read_failed"
    assert result.disposition == "manual_review_required"
    assert result.read_error_type == "PayrollRecoveryReadError"
    assert all(kind == "get" for kind, _kwargs in service.calls)


def test_read_transport_failure_is_not_misclassified_as_missing():
    plan = ready_plan()
    ranges = ranges_for(plan)
    header_range = f"'{SHEET_TITLES['payroll_statements']}'!A1:ZZ"
    ranges[header_range] = OSError("connection reset")
    service = FakeService(ranges)
    reader = PayrollGoogleSheetsRecoveryReadAdapter("sheet-id", service=service)

    result = build_payroll_recovery_preview(plan, reader)

    assert result.verification == "read_failed"
    assert result.disposition == "manual_review_required"
    assert result.read_error_type == "PayrollRecoveryReadError"
    assert [kind for kind, _kwargs in service.calls] == ["get"]


def test_reader_rejects_malformed_target_rows_without_write():
    plan = ready_plan()
    ranges = ranges_for(plan)
    item_range = f"'{SHEET_TITLES['payroll_items']}'!A1:ZZ"
    ranges[item_range].append([plan.identity.statement_id] * (len(PAYROLL_ITEM_COLUMNS) + 1))
    service = FakeService(ranges)
    reader = PayrollGoogleSheetsRecoveryReadAdapter("sheet-id", service=service)

    result = build_payroll_recovery_preview(plan, reader)

    assert result.verification == "read_failed"
    assert result.disposition == "manual_review_required"
    assert all(kind == "get" for kind, _kwargs in service.calls)


def test_non_ready_plan_is_not_a_recovery_target():
    plan = ready_plan().model_copy(update={
        "eligibility": "ineligible",
        "status": "blocked",
        "reason": "statement_needs_review",
        "reasons": ("statement_needs_review",),
        "header_action": "none",
        "item_action": "none",
        "planned_header_rows": (),
        "planned_item_rows": (),
    })

    with pytest.raises(ValueError, match="attempted ready"):
        build_payroll_recovery_preview(plan, object())
