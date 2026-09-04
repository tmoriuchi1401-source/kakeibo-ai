import socket

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.payroll_google_sheets_adapter import (
    PayrollGoogleSheetsAppendAdapter,
    inspect_payroll_recovery,
)
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, SHEET_TITLES, validate_sheet_schema
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_writer import apply_payroll_write_plans, preview_payroll_write


def snapshot():
    return PayrollSheetsSnapshot(
        schemas=[validate_sheet_schema(key, columns)
                 for key, columns in PAYROLL_SCHEMAS.items()],
        standard_items=[PayrollStandardItemRecord(
            standard_item_id="basic_pay", standard_name="基本給",
            section="earning", value_type="money",
        )],
    )


def ready_plan(index=1):
    preview = PayrollPreview(
        file_type="pdf", extraction_method="pdf_text", pay_period="2026-08",
        parse_status="success", items=[PayrollItem(
            raw_item_name="基本給", section="earnings", raw_value="300,000",
            value=300000, standard_item_candidate="basic_pay",
        )],
    )
    candidate = phase_a_to_storage_candidate(
        preview, employer_id="employer-1", statement_type="salary",
        source_type="drive", source_file_id=f"file-{index}",
        content_hash=f"hash-{index}",
    )
    return build_write_plan([candidate], snapshot())[0]


def success_response(sheet_key, rows=1):
    return {
        "spreadsheetId": "sheet-id",
        "updates": {
            "updatedRows": rows,
            "updatedRange": f"{SHEET_TITLES[sheet_key]}!A2:Z2",
        },
    }


class FakeRequest:
    def __init__(self, *, response=None, error=None):
        self.response = response
        self.error = error
        self.execute_calls = []

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeSingleAttemptExecutor:
    def __init__(self):
        self.calls = []

    def execute_once(self, request):
        self.calls.append(request)
        return request.execute(num_retries=0)


class FakeValues:
    def __init__(self, requests):
        self.requests = list(requests)
        self.append_calls = []

    def append(self, **kwargs):
        self.append_calls.append(kwargs)
        request = self.requests.pop(0)
        if isinstance(request, Exception):
            raise request
        return request


class FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class FakeService:
    def __init__(self, requests):
        self.values = FakeValues(requests)

    def spreadsheets(self):
        return FakeSpreadsheets(self.values)


def http_error(status):
    return HttpError(httplib2.Response({"status": str(status)}), b"error")


def adapter(requests):
    service = FakeService(requests)
    executor = FakeSingleAttemptExecutor()
    return PayrollGoogleSheetsAppendAdapter(
        "sheet-id", service=service, executor=executor,
    ), service, executor


def test_adapter_requires_explicit_single_attempt_executor():
    with pytest.raises(ValueError, match="single-attempt executor"):
        PayrollGoogleSheetsAppendAdapter(
            "sheet-id", service=FakeService([]), executor=None,
        )


def test_adapter_rejects_schema_mismatch_before_request_construction():
    plan = ready_plan()
    malformed = plan.planned_header_rows[0].model_copy(
        update={"columns": ("unexpected",)},
    )
    target, service, executor = adapter([])

    outcome = target.append_header_rows((malformed,))

    assert outcome.status == "confirmed_failure"
    assert outcome.failure_kind == "local_contract_rejection"
    assert service.values.append_calls == []
    assert executor.calls == []


@pytest.mark.parametrize(
    ("method", "kind", "sheet_key"),
    [
        ("append_header_rows", "header", "payroll_statements"),
        ("append_item_rows", "items", "payroll_items"),
    ],
)
def test_success_maps_response_and_preserves_request_values(method, kind, sheet_key):
    plan = ready_plan()
    rows = (plan.planned_header_rows if kind == "header"
            else plan.planned_item_rows)
    request = FakeRequest(response=success_response(sheet_key, len(rows)))
    target, service, executor = adapter([request])

    outcome = getattr(target, method)(rows)

    assert outcome.status == "confirmed_success"
    assert outcome.confirmed_rows == len(rows)
    assert request.execute_calls == [{"num_retries": 0}]
    assert executor.calls == [request]
    call = service.values.append_calls[0]
    assert call == {
        "spreadsheetId": "sheet-id",
        "range": f"'{SHEET_TITLES[sheet_key]}'!A:A",
        "valueInputOption": "RAW",
        "insertDataOption": "INSERT_ROWS",
        "body": {"values": [list(row.values) for row in rows]},
    }
    assert tuple(call["body"]["values"][0]) == rows[0].values


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 413, 414, 415, 422])
def test_known_rejection_is_confirmed_failure_without_retry(status):
    request = FakeRequest(error=http_error(status))
    target, _service, executor = adapter([request])

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "confirmed_failure"
    assert outcome.http_status == status
    assert outcome.confirmed_rows == 0
    assert request.execute_calls == [{"num_retries": 0}]
    assert executor.calls == [request]


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 503, 504])
def test_ambiguous_http_failure_is_unknown_without_retry(status):
    request = FakeRequest(error=http_error(status))
    target, _service, executor = adapter([request])

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "outcome_unknown"
    assert outcome.http_status == status
    assert request.execute_calls == [{"num_retries": 0}]
    assert executor.calls == [request]


@pytest.mark.parametrize(
    "error",
    [socket.timeout("timed out"), ConnectionResetError("reset"), OSError("transport")],
)
def test_transport_interruption_is_unknown_and_never_retried(error):
    request = FakeRequest(error=error)
    target, _service, executor = adapter([request])

    outcome = target.append_item_rows(ready_plan().planned_item_rows)

    assert outcome.status == "outcome_unknown"
    assert outcome.failure_kind == "transport_outcome_unknown"
    assert request.execute_calls == [{"num_retries": 0}]
    assert executor.calls == [request]


def test_request_construction_failure_is_confirmed_before_dispatch():
    target, service, executor = adapter([ValueError("cannot construct")])

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "confirmed_failure"
    assert outcome.failure_kind == "request_construction_failed"
    assert len(service.values.append_calls) == 1
    assert executor.calls == []


def test_incomplete_success_response_is_unknown_not_retried():
    request = FakeRequest(response={
        "spreadsheetId": "sheet-id",
        "updates": {"updatedRows": 0, "updatedRange": "other!A2"},
    })
    target, _service, executor = adapter([request])

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "outcome_unknown"
    assert outcome.failure_kind == "response_confirmation_mismatch"
    assert request.execute_calls == [{"num_retries": 0}]
    assert executor.calls == [request]


def test_core_stops_items_after_confirmed_or_ambiguous_header_failure():
    plan = ready_plan()
    for status in (400, 500):
        request = FakeRequest(error=http_error(status))
        target, service, _executor = adapter([request])
        result = apply_payroll_write_plans(
            [plan], target, confirmed=True, latest_plans=lambda: [plan],
        )

        expected = "confirmed_failure" if status == 400 else "header_outcome_unknown"
        assert result.status == expected
        assert len(service.values.append_calls) == 1


def test_core_stops_next_plan_after_confirmed_or_ambiguous_item_failure():
    plans = [ready_plan(1), ready_plan(2)]
    for status in (400, 500):
        header = FakeRequest(response=success_response("payroll_statements"))
        items = FakeRequest(error=http_error(status))
        target, service, _executor = adapter([header, items])

        result = apply_payroll_write_plans(
            plans, target, confirmed=True, latest_plans=lambda: plans,
        )

        assert result.status == "partial_failure"
        assert result.results[0].outcome_unknown is (status == 500)
        assert result.not_attempted_statement_ids == (plans[1].identity.statement_id,)
        assert len(service.values.append_calls) == 2


class FakeRecoveryReader:
    def __init__(self, headers=(), items=()):
        self.headers = headers
        self.items = items
        self.calls = []

    def read_header_rows(self, statement_id):
        self.calls.append(("headers", statement_id))
        return self.headers

    def read_item_rows(self, statement_id):
        self.calls.append(("items", statement_id))
        return self.items


def test_recovery_readback_separates_header_identity_from_item_completeness():
    plan = ready_plan()
    reader = FakeRecoveryReader(headers=plan.planned_header_rows, items=())

    assessment = inspect_payroll_recovery(plan, reader)

    assert assessment.header_state == "identity_confirmed"
    assert assessment.item_state == "absent"
    assert assessment.safe_to_automatic_retry is False
    assert reader.calls == [
        ("headers", plan.identity.statement_id),
        ("items", plan.identity.statement_id),
    ]


def test_recovery_detects_complete_items_but_never_authorizes_retry():
    plan = ready_plan()
    reader = FakeRecoveryReader(
        headers=plan.planned_header_rows, items=plan.planned_item_rows,
    )

    assessment = inspect_payroll_recovery(plan, reader)

    assert assessment.header_state == "identity_confirmed"
    assert assessment.item_state == "complete"
    assert assessment.safe_to_automatic_retry is False


def test_recovery_reports_identity_conflict_and_incomplete_items():
    plan = ready_plan()
    header = plan.planned_header_rows[0]
    header_values = list(header.values)
    header_values[header.columns.index("content_hash")] = "different-hash"
    conflicting_header = header.model_copy(update={"values": tuple(header_values)})
    item = plan.planned_item_rows[0]
    item_values = list(item.values)
    item_values[item.columns.index("raw_item_name")] = "different-item"
    partial_item = item.model_copy(update={"values": tuple(item_values)})
    reader = FakeRecoveryReader(
        headers=(conflicting_header,), items=(partial_item,),
    )

    assessment = inspect_payroll_recovery(plan, reader)

    assert assessment.header_state == "conflict_or_duplicate"
    assert assessment.matching_header_count == 0
    assert assessment.item_state == "incomplete_or_duplicate"
    assert assessment.safe_to_automatic_retry is False


def test_preview_has_no_path_to_google_adapter():
    plan = ready_plan()
    request = FakeRequest(response=success_response("payroll_statements"))
    _target, service, executor = adapter([request])

    preview = preview_payroll_write([plan])

    assert preview.ready_count == 1
    assert service.values.append_calls == []
    assert request.execute_calls == []
    assert executor.calls == []
