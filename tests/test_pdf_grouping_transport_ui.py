from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.drive_run_state import DriveStateTransport, StateError
from app.receipt_pdf_units import _digest
from app import pdf_grouping_authority as authority, pdf_grouping_ui as ui, pdf_grouping_review as worker
from test_pdf_grouping_authority import context, request, MemorySheet, BINDING
from test_receipt_pdf_units import local_ocr, synthetic_pdf


class DriveRequest:
    def __init__(self, operation):
        self.operation, self.callbacks, self.headers = operation, [], {}

    def add_response_callback(self, callback):
        self.callbacks.append(callback)

    def execute(self, num_retries=0):
        assert num_retries == 0
        value, headers = self.operation(self.headers)
        for callback in self.callbacks: callback(headers)
        return value


class FakeDrive:
    def __init__(self):
        self.payload, self.version = b'original-state', 1
        self.etag = True
        self.weak = self.race = self.unknown = self.ignore_write = self.media_race = False
        self.updates, self.reads = [], 0

    def files(self): return self

    def get(self, **kwargs):
        def get(headers):
            return {'id': 'dedicated-file', 'parents': ['private-folder'], 'trashed': False,
                    'mimeType': 'application/json', 'version': str(self.version), 'capabilities': {'canEdit': True}}, (
                    {'etag': ('W/' if self.weak else '') + '"v' + str(self.version) + '"'} if self.etag else {})
        return DriveRequest(get)

    def get_media(self, **kwargs):
        def media(headers):
            if self.media_race: self.version += 1
            self.reads += 1
            return self.payload, {}
        return DriveRequest(media)

    def update(self, **kwargs):
        assert set(kwargs) == {'fileId', 'supportsAllDrives', 'media_body', 'fields'}
        assert kwargs['fileId'] == 'dedicated-file'
        def update(headers):
            self.updates.append(deepcopy(headers))
            if self.race: self.version += 1
            if headers.get('If-Match') != '"v' + str(self.version) + '"':
                raise HttpError(httplib2.Response({'status': '412'}), b'{"error":{"message":"private"}}')
            if not self.ignore_write:
                upload = kwargs['media_body']
                self.payload = upload.getbytes(0, upload.size())
                self.version += 1
            if self.unknown: raise ConnectionError('PRIVATE_DELIVERY')
            return {'id': 'dedicated-file'}, {}
        return DriveRequest(update)


def transport(drive):
    return DriveStateTransport(drive, worker.GroupingBinding('private-folder', 'dedicated-file'))


def test_real_transport_if_match_exact_readback_no_move_or_create():
    drive = FakeDrive()
    adapter = transport(drive)
    before, tag = adapter.read_versioned()
    adapter.replace_versioned(before, tag, b'new-private-state')
    assert drive.payload == b'new-private-state'
    assert drive.updates == [{'If-Match': '"v1"'}]


@pytest.mark.parametrize('mode', ['missing', 'weak'])
def test_unavailable_strong_etag_blocks_before_any_update(mode):
    drive = FakeDrive()
    drive.etag, drive.weak = mode != 'missing', mode == 'weak'
    with pytest.raises(StateError, match='conditional_write_unavailable'):
        transport(drive).read_versioned()
    assert not drive.updates


def test_server_conflict_in_read_write_gap_is_rejected_not_overwritten():
    drive = FakeDrive()
    adapter = transport(drive)
    before, tag = adapter.read_versioned()
    drive.race = True
    with pytest.raises(StateError, match='changed_since_read'):
        adapter.replace_versioned(before, tag, b'must-not-overwrite')
    assert drive.payload == before
    assert len(drive.updates) == 1


def test_read_changed_during_media_download_fails_closed():
    drive = FakeDrive()
    drive.media_race = True
    with pytest.raises(StateError, match='changed_during_read'):
        transport(drive).read_versioned()
    assert not drive.updates


def test_unknown_write_is_acknowledged_only_by_exact_readback_never_retried():
    drive = FakeDrive()
    adapter = transport(drive)
    before, tag = adapter.read_versioned()
    drive.unknown = True
    adapter.replace_versioned(before, tag, b'new')
    assert drive.payload == b'new' and len(drive.updates) == 1
    drive.ignore_write = True
    before, tag = adapter.read_versioned()
    with pytest.raises(StateError, match='write_unknown'):
        adapter.replace_versioned(before, tag, b'not-saved')
    assert len(drive.updates) == 2


def test_successful_response_with_wrong_readback_is_not_confirmed():
    drive = FakeDrive()
    adapter = transport(drive)
    before, tag = adapter.read_versioned()
    drive.ignore_write = True
    with pytest.raises(StateError, match='readback_mismatch'):
        adapter.replace_versioned(before, tag, b'not-saved')


class SheetRequest:
    def __init__(self, value): self.value = value
    def execute(self, num_retries=0):
        assert num_retries == 0
        return self.value


class FakeSheets:
    def __init__(self):
        self.metadata = [{'properties': {'sheetId': 0, 'title': '支出明細'}}]
        self.value_data, self.writes, self.formats = {}, [], []

    def spreadsheets(self): return self
    def values(self):
        return SimpleNamespace(get=self.values_get, batchUpdate=self.values_write)
    def get(self, **kwargs): return SheetRequest({'sheets': self.metadata})
    def values_get(self, **kwargs):
        assert kwargs['valueRenderOption'] == 'UNFORMATTED_VALUE'
        return SheetRequest({'values': deepcopy(self.value_data.get(kwargs['range'], []))})
    def values_write(self, **kwargs):
        assert kwargs['body']['valueInputOption'] == 'RAW'
        self.writes.append(kwargs['body']['data'])
        return SheetRequest({})
    def batchUpdate(self, **kwargs):
        self.formats.extend(kwargs['body']['requests'])
        return SheetRequest({})


def test_ui_install_touches_only_owned_new_tabs_and_does_not_overwrite_existing_headers():
    native = FakeSheets()
    adapter = ui.GroupingSheet(native, 'management-sheet')
    adapter.install()
    creates = [r['addSheet']['properties'] for r in native.formats if 'addSheet' in r]
    assert {p['title'] for p in creates} == {ui.TITLE, ui.QUEUE}
    assert next(p for p in creates if p['title'] == ui.QUEUE)['hidden'] is True
    assert all(r.get('updateCells', {}).get('range', {}).get('sheetId', ui.SHEET_ID) in {ui.SHEET_ID, ui.QUEUE_ID}
               for r in native.formats)
    native.metadata += [{'properties': {'sheetId': ui.SHEET_ID, 'title': ui.TITLE}},
                        {'properties': {'sheetId': ui.QUEUE_ID, 'title': ui.QUEUE}}]
    native.value_data.update({f"'{ui.TITLE}'!A1:P1": [ui.HEADERS], f"'{ui.QUEUE}'!A1:F1": [ui.QUEUE_HEADERS]})
    native.formats.clear()
    adapter.install()
    assert not any('addSheet' in r or 'updateCells' in r for r in native.formats)


def test_ui_collision_and_changed_headers_fail_before_write():
    native = FakeSheets()
    native.metadata = [{'properties': {'sheetId': ui.SHEET_ID, 'title': 'another-tab'}}]
    with pytest.raises(StateError, match='sheet_collision'): ui.GroupingSheet(native, 'management-sheet').install()
    assert not native.formats
    native.metadata = [{'properties': {'sheetId': ui.SHEET_ID, 'title': ui.TITLE}}]
    native.value_data[f"'{ui.TITLE}'!A1:P1"] = [['changed']]
    with pytest.raises(StateError, match='schema_mismatch'): ui.GroupingSheet(native, 'management-sheet').install()
    assert not native.formats


def test_sheet_projection_preserves_user_intent_and_enforces_range_fence(local_ocr):
    svc, live, _ = context()
    view = svc.display('drive-source-id')
    row = ui.project(view)
    row[7:10] = ['分割', '1', '受付中']
    native = FakeSheets()
    native.value_data[f"'{ui.TITLE}'!A2:P1001"] = [row]
    adapter = ui.GroupingSheet(native, 'management-sheet')
    adapter.publish(view)
    assert native.writes[0][0]['values'][0][7:10] == ['分割', '1', '受付中']
    assert all(entry['range'].startswith(f"'{ui.TITLE}'!") for batch in native.writes for entry in batch)
    with pytest.raises(StateError, match='range_forbidden'):
        adapter._write([{'range': "'支出明細'!A1", 'values': [['forbidden']]}])


def test_drive_unavailable_clears_confirmed_projection_instead_of_reusing_local(local_ocr):
    svc, live, remote = context()
    view = svc.display('drive-source-id')
    svc.review(request(view))
    sheet = MemorySheet(svc.display('drive-source-id'))
    remote.fail_read = True
    result = ui.process_request(svc, sheet, str(UUID(int=2)))
    assert result['result'] == 'state_drive_read_failed'
    assert sheet.rows[0][0] == 'authority未確認'


def test_drive_source_rechecked_and_sensitive_provenance_retained(local_ocr):
    content = synthetic_pdf(['normal', 'medical'])
    source = Mock()
    metadata = {'id': 'drive-source-id', 'parents': ['receipt-inbox'], 'mimeType': 'application/pdf', 'version': '1'}
    source.files.return_value.get.return_value.execute.return_value = metadata
    source.files.return_value.get_media.return_value.execute.return_value = content
    reader = worker.DrivePdfReader(source, 'receipt-inbox')
    observed = reader('drive-source-id', None)
    assert observed.pages[1].classification == 'medical'
    assert source.files.return_value.get.call_count == 2
    source.files.return_value.update.assert_not_called()
    source.files.return_value.get.return_value.execute.side_effect = [metadata, {**metadata, 'version': '2'}]
    with pytest.raises(StateError, match='source_changed'): reader('drive-source-id', None)


@pytest.mark.parametrize('change', [{'parents': ['processed']}, {'mimeType': 'image/png'}, {'trashed': True}])
def test_source_outside_pinned_inbox_or_changed_type_is_blocked(change):
    source = Mock()
    source.files.return_value.get.return_value.execute.return_value = {
        'id': 'drive-source-id', 'parents': ['receipt-inbox'], 'mimeType': 'application/pdf', 'version': '1', **change}
    with pytest.raises(StateError): worker.DrivePdfReader(source, 'receipt-inbox')('drive-source-id', None)
    source.files.return_value.get_media.assert_not_called()
    source.files.return_value.update.assert_not_called()


def test_permission_preflight_never_adds_auth_or_permissions_and_blocks_public():
    service = Mock()
    metadata = {'owners': [{'emailAddress': 'owner@example.test'}], 'permissions': [
        {'type': 'user', 'role': 'owner', 'emailAddress': 'owner@example.test'},
        {'type': 'user', 'role': 'writer', 'emailAddress': 'sa@example.test'}]}
    service.files.return_value.get.return_value.execute.return_value = metadata
    worker.preflight_permissions(service, worker.GroupingBinding('folder', 'file'), _digest('owner@example.test'), 'sa@example.test')
    metadata['permissions'].append({'type': 'anyone', 'role': 'reader'})
    with pytest.raises(StateError, match='private_permissions_mismatch'):
        worker.preflight_permissions(service, worker.GroupingBinding('folder', 'file'), _digest('owner@example.test'), 'sa@example.test')
    service.permissions.assert_not_called()
    service.files.return_value.update.assert_not_called()


def test_permission_change_is_rechecked_before_each_state_read_write(local_ocr):
    svc, live, remote = context()
    view = svc.display('drive-source-id')
    check = Mock(side_effect=StateError('grouping_private_permissions_mismatch'))
    svc.store.preflight = check
    assert svc.confirmed_units('drive-source-id') == ()
    with pytest.raises(StateError): svc.review(request(view))
    assert check.call_count == 2


def test_manual_workflow_has_no_schedule_ai_key_or_accounting_route():
    from pathlib import Path
    workflow = Path('.github/workflows/pdf-grouping-review.yml').read_text(encoding='utf-8')
    assert 'workflow_dispatch:' in workflow and 'group: kakeibo-production' in workflow
    assert 'schedule:' not in workflow and 'GEMINI_API_KEY' not in workflow
    assert 'PROCESSED_DRIVE_FOLDER_ID' not in workflow
    assert 'PDF_GROUPING_REVIEW_ENABLED' in workflow and 'KAKEIBO_VALIDATED_MAIN_SHA' in workflow
    assert 'python -m app.pdf_grouping_review' in workflow


def test_owned_ui_result_updates_do_not_touch_accounting_sheets(local_ocr):
    svc, live, remote = context()
    view = svc.display('drive-source-id')
    row = ui.project(view)
    row[7] = '確定'
    id = str(UUID(int=1))
    native = FakeSheets()
    native.value_data.update({f"'{ui.QUEUE}'!A2:F1001": [[id, 'dispatching', json.dumps(row), 'submitted']],
                              f"'{ui.TITLE}'!A2:P1001": [row]})
    adapter = ui.GroupingSheet(native, 'management-sheet')
    adapter.mark_unverified(row, 'grouping_state_unavailable')
    adapter.finish(id, row, 'grouping_required', '2026-10-01T00:00:00+00:00')
    assert all(entry['range'].startswith((f"'{ui.TITLE}'!", f"'{ui.QUEUE}'!"))
               for batch in native.writes for entry in batch)
    assert any(entry['values'] == [['authority未確認・再表示必要']] for batch in native.writes for entry in batch)
