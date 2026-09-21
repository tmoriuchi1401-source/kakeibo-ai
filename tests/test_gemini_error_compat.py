from unittest.mock import Mock

import httpx
import pytest
from google.genai.errors import APIError as LegacyAPIError
from googleapiclient.errors import HttpError
from httplib2 import Response

try:
    from google.genai._gaos.lib import compat_errors as sdk
except ModuleNotFoundError:
    from google.genai import _interactions as sdk

from app import drive_receipts, production_source
from app.gemini_errors import gemini_api_status, is_gemini_api_error


def status_error(status):
    response = httpx.Response(status, request=httpx.Request('POST', 'https://example.invalid'))
    return sdk.APIStatusError('private receipt', response=response, body={'private': 'receipt'})


@pytest.mark.parametrize('status,expected', [
    (400, 'gemini_api_or_model_rejected'), (401, 'gemini_auth_rejected'),
    (403, 'gemini_auth_rejected'), (404, 'gemini_api_or_model_rejected'),
    (429, 'gemini_quota_rejected'), (503, 'gemini_request_failed_unknown'),
])
def test_actual_interactions_status_errors_reach_fixed_diagnostics(status, expected):
    error = status_error(status)
    assert not isinstance(error, LegacyAPIError)  # the old guard misses these
    assert is_gemini_api_error(error)
    assert gemini_api_status(error) == status
    assert production_source.source_error_code(error) == expected


@pytest.mark.parametrize('kind', ['timeout', 'connection', 'response'])
def test_actual_interactions_transport_and_validation_errors_are_sanitized(kind):
    request = httpx.Request('POST', 'https://example.invalid')
    if kind == 'timeout':error = sdk.APITimeoutError(request=request)
    elif kind == 'connection':error = sdk.APIConnectionError(request=request, message='private receipt')
    else:error = sdk.APIResponseValidationError(httpx.Response(200, request=request), {'private': 'receipt'})
    assert gemini_api_status(error) is None
    expected = 'gemini_result_invalid' if kind == 'response' else 'gemini_transport_unknown'
    assert production_source.source_error_code(error) == expected


@pytest.mark.parametrize('family', ['legacy', 'interactions'])
@pytest.mark.parametrize('status', [429, 503])
def test_transient_extraction_failure_preserves_completed_receipt_and_defers_remaining(monkeypatch, family, status):
    error = (LegacyAPIError(status, {'error': {'message': 'private receipt'}})
             if family == 'legacy' else status_error(status))
    service = Mock()
    service.files().list().execute.return_value = {'files': [
        {'id': str(i), 'name': 'private filename', 'mimeType': 'image/png',
         'parents': ['synthetic-inbox']} for i in range(3)]}
    monkeypatch.setattr(drive_receipts, 'drive_service', lambda: service)
    monkeypatch.setattr(drive_receipts, 'download_drive_file', lambda *a: b'synthetic')
    pipeline = Mock()
    pipeline.process_bytes.side_effect = [{'status': 'imported'}, error]
    results = drive_receipts.process_inbox('synthetic-inbox', pipeline, 'synthetic-archive')
    assert [result['status'] for _, result in results] == ['imported', 'deferred', 'deferred']
    assert pipeline.process_bytes.call_count == 2
    assert service.files().update.call_count == 1
    assert service.files().update.call_args.kwargs['fileId'] == '0'


@pytest.mark.parametrize('error', [status_error(403), status_error(500),
                                 HttpError(Response({'status': 429}), b'private receipt')])
def test_nontransient_or_accounting_errors_never_enter_extraction_deferral(monkeypatch, error):
    service = Mock()
    service.files().list().execute.return_value = {'files': [
        {'id': 'one', 'name': 'private filename', 'mimeType': 'image/png', 'parents': ['synthetic-inbox']}]}
    monkeypatch.setattr(drive_receipts, 'drive_service', lambda: service)
    monkeypatch.setattr(drive_receipts, 'download_drive_file', lambda *a: b'synthetic')
    pipeline = Mock(); pipeline.process_bytes.side_effect = error
    with pytest.raises(type(error)):
        drive_receipts.process_inbox('synthetic-inbox', pipeline, 'synthetic-archive')
    assert pipeline.process_bytes.call_count == 1
    service.files().update.assert_not_called()


def test_matching_status_attribute_on_unrelated_error_is_not_authority_to_defer():
    error = RuntimeError('private receipt'); error.status_code = 429; error.code = 503
    assert not is_gemini_api_error(error)
    assert gemini_api_status(error) is None
