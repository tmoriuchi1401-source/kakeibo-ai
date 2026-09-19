import pytest

from app.drive_receipts import (
    is_supported_receipt_mime,
    normalize_folder_id,
    should_archive_result,
)


def test_normalize_drive_folder_id():
    folder_id = "1AbCdEfGhIjKlMnOp"
    assert normalize_folder_id(folder_id) == folder_id
    assert normalize_folder_id(f"https://drive.google.com/drive/folders/{folder_id}") == folder_id
    assert normalize_folder_id(f"https://drive.google.com/drive/u/0/folders/{folder_id}?usp=sharing") == folder_id


def test_reject_invalid_drive_folder_id():
    with pytest.raises(ValueError, match="フォルダIDが不正"):
        normalize_folder_id("not a folder id!")


@pytest.mark.parametrize(
    "result",
    [
        {"status": "imported"},
        {"status": "needs_review"},
        {"status": "skipped", "reason": "already_imported"},
    ],
)
def test_archive_recorded_receipt_results(result):
    assert should_archive_result(result)


@pytest.mark.parametrize(
    "result",
    [
        {"status": "skipped", "reason": "unsupported"},
        {"status": "privacy_blocked"},
        {"status": "error"},
        {},
    ],
)
def test_leave_unrecorded_receipt_results_in_inbox(result):
    assert not should_archive_result(result)


@pytest.mark.parametrize(
    "mime_type",
    ["image/jpeg", "image/heic", "image/png", "application/pdf"],
)
def test_supported_receipt_mime_types(mime_type):
    assert is_supported_receipt_mime(mime_type)


@pytest.mark.parametrize("mime_type", ["text/plain", "application/zip"])
def test_unsupported_receipt_mime_types(mime_type):
    assert not is_supported_receipt_mime(mime_type)


@pytest.mark.parametrize('status',[429,503])
def test_ai_quota_or_unavailability_defers_remaining_without_archive(monkeypatch,status):
    from unittest.mock import Mock
    from google.genai.errors import APIError
    from app import drive_receipts as m
    service=Mock();service.files().list().execute.return_value={'files':[
        {'id':str(i),'name':'synthetic','mimeType':'image/png','parents':['synthetic-inbox']} for i in range(3)]}
    monkeypatch.setattr(m,'drive_service',lambda:service)
    monkeypatch.setattr(m,'download_drive_file',Mock(return_value=b'synthetic'))
    pipeline=Mock();pipeline.process_bytes.side_effect=APIError(status,{'error':{'message':'synthetic'}})
    results=m.process_inbox('synthetic-inbox',pipeline,'synthetic-archive')
    assert len(results)==3 and all(r['status']=='deferred' for _,r in results)
    assert pipeline.process_bytes.call_count==1 and m.download_drive_file.call_count==1
    service.files().update.assert_not_called()
    assert not any(should_archive_result(r) for _,r in results)


def test_sheet_write_failure_is_not_treated_as_deferred_ai(monkeypatch):
    from unittest.mock import Mock
    from googleapiclient.errors import HttpError
    from httplib2 import Response
    from app import drive_receipts as m
    service=Mock();service.files().list().execute.return_value={'files':[
        {'id':'one','name':'synthetic','mimeType':'image/png','parents':['synthetic-inbox']}]}
    monkeypatch.setattr(m,'drive_service',lambda:service)
    monkeypatch.setattr(m,'download_drive_file',lambda *a:b'synthetic')
    pipeline=Mock();pipeline.process_bytes.side_effect=HttpError(Response({'status':429}),b'synthetic')
    with pytest.raises(HttpError):m.process_inbox('synthetic-inbox',pipeline,'synthetic-archive')
    service.files().update.assert_not_called()
