import socket

import pytest
import requests
from googleapiclient.errors import HttpError

from app.payroll_google_sheets_adapter import (
    PayrollGoogleSheetsAppendAdapter,
    PayrollRequestNotSentError,
)
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_sheets import PayrollSheetsSnapshot, validate_sheet_schema
from app.payroll_single_attempt_executor import (
    PayrollGoogleApiSingleAttemptExecutor,
    PayrollHttpRequest,
    PayrollHttpResponse,
    RequestsPayrollSingleSendTransport,
    payroll_sheets_request_service,
)
from app.payroll_storage import (
    PAYROLL_SCHEMAS,
    PayrollStandardItemRecord,
    phase_a_to_storage_candidate,
)
from app.payroll_storage_preview import build_write_plan
from app.payroll_writer import apply_payroll_write_plans


class FakeCredentials:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def before_request(self, auth_request, method, url, headers):
        self.calls.append((auth_request, method, url))
        if self.error is not None:
            raise self.error
        headers["authorization"] = "Bearer test-token"


class CountingTransport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def send(self, request):
        self.calls.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def response(status=200, content=b'{"ok": true}', reason="OK"):
    return PayrollHttpResponse(
        status=status,
        reason=reason,
        headers={"content-type": "application/json"},
        content=content,
    )


def append_request(*, spreadsheet_id="sheet-id", body=None):
    return payroll_sheets_request_service().spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range="payroll_statements!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body or {"values": [["value"]]},
    )


def executor_with(result, *, credentials=None):
    transport = CountingTransport(result)
    credentials = credentials or FakeCredentials()
    executor = PayrollGoogleApiSingleAttemptExecutor(
        credentials,
        transport=transport,
        auth_request=object(),
    )
    return executor, transport, credentials


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


def test_success_dispatches_transport_exactly_once_and_authenticates_before_send():
    target, transport, credentials = executor_with(response())
    request = append_request()

    result = target.execute_once(request)

    assert result == {"ok": True}
    assert len(transport.calls) == 1
    sent = transport.calls[0]
    assert sent.method == "POST"
    assert sent.url == request.uri
    assert sent.headers["authorization"] == "Bearer test-token"
    assert len(credentials.calls) == 1


def test_confirmed_http_failure_dispatches_transport_exactly_once():
    target, transport, _credentials = executor_with(
        response(status=400, content=b'{"error":{"message":"bad request"}}'),
    )

    with pytest.raises(HttpError) as caught:
        target.execute_once(append_request())

    assert caught.value.status_code == 400
    assert len(transport.calls) == 1


def test_401_response_does_not_refresh_or_replay_write():
    target, transport, credentials = executor_with(response(
        status=401,
        reason="Unauthorized",
        content=b'{"error":{"message":"expired"}}',
    ))

    with pytest.raises(HttpError) as caught:
        target.execute_once(append_request())

    assert caught.value.status_code == 401
    assert len(credentials.calls) == 1
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "error",
    [socket.timeout("timed out"), ConnectionResetError("reset"), OSError("broken")],
)
def test_transport_exception_is_never_retried(error):
    target, transport, _credentials = executor_with(error)

    with pytest.raises(type(error)):
        target.execute_once(append_request())

    assert len(transport.calls) == 1


def test_malformed_response_is_not_retried():
    target, transport, _credentials = executor_with(response(content=b"not-json"))

    assert target.execute_once(append_request()) == "not-json"

    assert len(transport.calls) == 1


def test_invalid_transport_result_after_dispatch_is_not_retried():
    target, transport, _credentials = executor_with(object())

    with pytest.raises(TypeError, match="invalid response"):
        target.execute_once(append_request())

    assert len(transport.calls) == 1


def test_executor_does_not_call_request_execute_and_sends_at_most_once():
    target, transport, _credentials = executor_with(response())
    request = append_request()
    execute_calls = []

    def forbidden_execute(*args, **kwargs):
        execute_calls.append((args, kwargs))
        raise AssertionError("HttpRequest.execute must not be used")

    request.execute = forbidden_execute

    assert target.execute_once(request) == {"ok": True}
    assert execute_calls == []
    assert len(transport.calls) == 1


def test_credential_failure_is_known_to_precede_write_dispatch():
    credentials = FakeCredentials(error=RuntimeError("refresh failed"))
    target, transport, _credentials = executor_with(
        response(), credentials=credentials,
    )

    with pytest.raises(PayrollRequestNotSentError):
        target.execute_once(append_request())

    assert len(credentials.calls) == 1
    assert transport.calls == []


def test_request_construction_service_cannot_execute_directly():
    request = append_request()

    with pytest.raises(PayrollRequestNotSentError, match="direct request"):
        request.execute(num_retries=99)


def test_requests_transport_disables_adapter_retries_and_redirects(monkeypatch):
    transport = RequestsPayrollSingleSendTransport(timeout=(1.0, 2.0))
    low_level_calls = []
    raw_response = requests.Response()
    raw_response.status_code = 307
    raw_response.reason = "Temporary Redirect"
    raw_response.headers = {"location": "https://example.invalid/replayed"}
    raw_response._content = b"redirect"

    def fake_send(prepared, **kwargs):
        low_level_calls.append((prepared, kwargs))
        return raw_response

    monkeypatch.setattr(transport._session, "send", fake_send)
    result = transport.send(PayrollHttpRequest(
        method="POST",
        url="https://sheets.googleapis.com/v4/spreadsheets/id/values/A:append",
        headers={"content-type": "application/json"},
        body=b"{}",
    ))

    assert result.status == 307
    assert len(low_level_calls) == 1
    assert low_level_calls[0][1]["allow_redirects"] is False
    assert low_level_calls[0][1]["timeout"] == (1.0, 2.0)
    for prefix in ("http://", "https://"):
        retries = transport._session.get_adapter(prefix).max_retries
        assert retries.total == 0
        assert retries.read is False


@pytest.mark.parametrize(
    "error",
    [requests.Timeout("timed out"), requests.ConnectionError("reset")],
)
def test_requests_transport_calls_session_send_once_on_failure(monkeypatch, error):
    transport = RequestsPayrollSingleSendTransport()
    low_level_calls = []

    def fake_send(prepared, **kwargs):
        low_level_calls.append((prepared, kwargs))
        raise error

    monkeypatch.setattr(transport._session, "send", fake_send)
    with pytest.raises(type(error)):
        transport.send(PayrollHttpRequest(
            method="POST",
            url="https://sheets.googleapis.com/v4/spreadsheets/id/values/A:append",
            headers={},
            body=b"{}",
        ))

    assert len(low_level_calls) == 1


def adapter_with(result):
    executor, transport, _credentials = executor_with(result)
    adapter = PayrollGoogleSheetsAppendAdapter(
        "sheet-id",
        service=payroll_sheets_request_service(),
        executor=executor,
    )
    return adapter, transport


def test_adapter_maps_timeout_to_unknown_with_one_transport_call():
    target, transport = adapter_with(socket.timeout("timed out"))

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "outcome_unknown"
    assert outcome.failure_kind == "transport_outcome_unknown"
    assert len(transport.calls) == 1


def test_adapter_maps_http_400_to_confirmed_failure_with_one_transport_call():
    target, transport = adapter_with(response(
        status=400,
        reason="Bad Request",
        content=b'{"error":{"message":"bad request"}}',
    ))

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "confirmed_failure"
    assert outcome.failure_kind == "http_request_rejected"
    assert outcome.http_status == 400
    assert len(transport.calls) == 1


def test_adapter_maps_malformed_response_to_unknown_with_one_transport_call():
    target, transport = adapter_with(response(content=b"not-json"))

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "outcome_unknown"
    assert outcome.failure_kind == "response_confirmation_mismatch"
    assert len(transport.calls) == 1


def test_adapter_maps_pre_dispatch_auth_failure_to_confirmed_failure():
    credentials = FakeCredentials(error=RuntimeError("refresh failed"))
    executor, transport, _credentials = executor_with(
        response(), credentials=credentials,
    )
    target = PayrollGoogleSheetsAppendAdapter(
        "sheet-id",
        service=payroll_sheets_request_service(),
        executor=executor,
    )

    outcome = target.append_header_rows(ready_plan().planned_header_rows)

    assert outcome.status == "confirmed_failure"
    assert outcome.failure_kind == "request_not_sent"
    assert transport.calls == []


def test_timeout_stops_batch_before_items_and_next_plan():
    plans = [ready_plan(1), ready_plan(2)]
    target, transport = adapter_with(socket.timeout("timed out"))

    result = apply_payroll_write_plans(
        plans,
        target,
        confirmed=True,
        latest_plans=lambda: plans,
    )

    assert result.status == "header_outcome_unknown"
    assert result.results[0].outcome_unknown is True
    assert result.not_attempted_statement_ids == (plans[1].identity.statement_id,)
    assert len(transport.calls) == 1
