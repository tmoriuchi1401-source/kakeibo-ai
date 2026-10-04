"""Synthetic Drive v2 only; no credentials or live side effects."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app import conditional_drive_state_v2 as v2
from app.drive_run_state import StateError
from test_pdf_grouping_transport_ui import DriveRequest
from test_pdf_grouping_authority import BINDING, context, request
from test_receipt_pdf_units import local_ocr
from app import pdf_grouping_authority as authority


class FakeV2:
    _rootDesc = {'version': 'v2'}

    def __init__(self):
        self.payload, self.revision = b'original-state', 1
        self.tag_override = 'automatic'
        self.header_mismatch = self.media_race = self.update_race = False
        self.unknown = self.ignore_write = False
        self.status = None
        self.metadata_patch = {}
        self.updates, self.reads = [], 0

    def files(self):
        return self

    @property
    def tag(self):
        return f'"opaque-{self.revision}"'

    def get(self, **kwargs):
        assert kwargs == {'fileId': 'dedicated-file', 'supportsAllDrives': True,
            'fields': 'id,etag,parents(id),labels(trashed),mimeType,editable'}

        def get(headers):
            tag = self.tag if self.tag_override == 'automatic' else self.tag_override
            return {'id': 'dedicated-file', 'parents': [{'id': 'private-folder'}],
                'labels': {'trashed': False}, 'mimeType': 'application/json',
                'editable': True, 'etag': tag, **self.metadata_patch}, {
                    'etag': '"different"' if self.header_mismatch else tag}
        return DriveRequest(get)

    def get_media(self, **kwargs):
        assert kwargs == {'fileId': 'dedicated-file', 'supportsAllDrives': True}

        def media(headers):
            self.reads += 1
            if self.media_race:
                self.revision += 1
            return self.payload, {}
        return DriveRequest(media)

    def update(self, **kwargs):
        assert set(kwargs) == {'fileId', 'supportsAllDrives', 'media_body', 'fields'}
        assert kwargs['fileId'] == 'dedicated-file' and kwargs['fields'] == 'id,etag'

        def update(headers):
            assert set(headers) == {'If-Match'} and v2.strong_etag(headers['If-Match'])
            self.updates.append(deepcopy(headers))
            if self.update_race:
                self.revision += 1
            status = self.status or (412 if headers['If-Match'] != self.tag else None)
            if status:
                raise HttpError(httplib2.Response({'status': str(status)}), b'{}')
            if not self.ignore_write:
                upload = kwargs['media_body']
                self.payload = upload.getbytes(0, upload.size())
                self.revision += 1
            if self.unknown:
                raise ConnectionError('synthetic delivery failure')
            return {'id': 'dedicated-file', 'etag': self.tag}, {}
        return DriveRequest(update)


def adapter(drive):
    return v2.ConditionalDriveStateTransportV2(drive,
        SimpleNamespace(folder_id='private-folder', file_id='dedicated-file'))


def test_current_update_strong_tag_new_tag_exact_readback():
    drive = FakeV2()
    transport = adapter(drive)
    old, tag = transport.read_versioned()
    transport.replace_versioned(old, tag, b'new-synthetic-state')
    payload, new_tag = transport.read_versioned()
    assert payload == b'new-synthetic-state' and new_tag != tag
    assert drive.updates == [{'If-Match': tag}]
    assert not hasattr(transport, 'write')


@pytest.mark.parametrize('tag', [None, '', '*', 'W/"weak"', 'unquoted', '""',
    '"x"\r\nX-Header: value', '"has space"', '"embedded"quote"', '"\x7f"'])
def test_weak_missing_invalid_or_wildcard_tags_never_allow_update(tag):
    drive = FakeV2()
    drive.tag_override = tag
    with pytest.raises(StateError, match='conditional_write_unavailable'):
        adapter(drive).read_versioned()
    with pytest.raises(StateError, match='conditional_write_unavailable'):
        adapter(drive).replace_versioned(b'original-state', tag, b'new')
    assert drive.updates == [] and drive.reads == 0


def test_header_and_file_etag_disagreement_fails_closed():
    drive = FakeV2()
    drive.header_mismatch = True
    with pytest.raises(StateError, match='conditional_write_unavailable'):
        adapter(drive).read_versioned()
    assert drive.updates == []


@pytest.mark.parametrize('patch', [{'id': 'other'}, {'parents': [{'id': 'other'}]},
    {'mimeType': 'application/pdf'}, {'labels': {'trashed': True}}])
def test_pinned_identity_and_json_type_required(patch):
    drive = FakeV2()
    drive.metadata_patch = patch
    with pytest.raises(StateError, match='target_mismatch'):
        adapter(drive).read_versioned()
    assert drive.updates == [] and drive.reads == 0


def test_no_edit_permission_blocks_write():
    drive = FakeV2()
    drive.metadata_patch = {'editable': False}
    transport = adapter(drive)
    before, tag = transport.read_versioned()
    with pytest.raises(StateError, match='not_editable'):
        transport.replace_versioned(before, tag, b'new')
    assert drive.updates == []


def test_changed_during_download_rejected():
    drive = FakeV2()
    drive.media_race = True
    with pytest.raises(StateError, match='changed_during_read'):
        adapter(drive).read_versioned()
    assert drive.updates == []


def test_client_stale_tag_or_bytes_blocks_before_update():
    drive = FakeV2()
    transport = adapter(drive)
    before, tag = transport.read_versioned()
    drive.revision += 1
    with pytest.raises(StateError, match='changed_since_read'):
        transport.replace_versioned(before, tag, b'new')
    with pytest.raises(StateError, match='changed_since_read'):
        transport.replace_versioned(b'other', drive.tag, b'new')
    assert drive.updates == []


def test_server_stale_412_no_retry_or_readback_acknowledgement():
    drive = FakeV2()
    transport = adapter(drive)
    before, tag = transport.read_versioned()
    drive.update_race = True
    with pytest.raises(StateError, match='changed_since_read'):
        transport.replace_versioned(before, tag, before)
    assert drive.updates == [{'If-Match': tag}]
    assert drive.payload == before and drive.reads == 2


@pytest.mark.parametrize('status', [400, 403, 409, 429, 500, 503])
def test_other_http_errors_are_not_conflict_success_and_never_retry(status):
    drive = FakeV2()
    transport = adapter(drive)
    before, tag = transport.read_versioned()
    drive.status = status
    with pytest.raises(StateError, match='write_unknown'):
        transport.replace_versioned(before, tag, b'new')
    assert drive.updates == [{'If-Match': tag}] and drive.payload == before


@pytest.mark.parametrize('saved', [True, False])
def test_ambiguous_delivery_exact_readback_only_no_second_write(saved):
    drive = FakeV2()
    transport = adapter(drive)
    before, tag = transport.read_versioned()
    drive.unknown, drive.ignore_write = True, not saved
    if saved:
        transport.replace_versioned(before, tag, b'new')
        assert drive.payload == b'new'
    else:
        with pytest.raises(StateError, match='write_unknown'):
            transport.replace_versioned(before, tag, b'new')
    assert len(drive.updates) == 1


def test_success_response_without_exact_content_is_rejected():
    drive = FakeV2()
    transport = adapter(drive)
    before, tag = transport.read_versioned()
    drive.ignore_write = True
    with pytest.raises(StateError, match='readback_mismatch'):
        transport.replace_versioned(before, tag, b'new')
    assert len(drive.updates) == 1


def test_v3_resource_refused_and_factory_reuses_existing_scopes(monkeypatch):
    with pytest.raises(StateError, match='api_mismatch'):
        adapter(SimpleNamespace(_rootDesc={'version': 'v3'}))
    existing_credentials = Mock(return_value=object())
    build = Mock()
    monkeypatch.setattr('app.google_clients.credentials', existing_credentials)
    monkeypatch.setattr('googleapiclient.discovery.build', build)
    v2.conditional_drive_state_service_v2()
    existing_credentials.assert_called_once_with()
    build.assert_called_once_with('drive', 'v2', credentials=existing_credentials.return_value,
                                 cache_discovery=False)


def test_only_grouping_state_uses_v2_acl_and_source_clients_remain_v3(monkeypatch):
    import json
    from app import pdf_grouping_review as worker
    from app import google_clients, settings, private_state_bindings
    from app.receipt_pdf_units import _digest
    info = {'private_key': 'synthetic-only', 'client_email': 'sa@example.test'}
    cfg = SimpleNamespace(spreadsheet_id='management-sheet', receipt_drive_folder_id='receipt-inbox',
                          validate=Mock())
    monkeypatch.setattr(settings, 'Settings', lambda: cfg)
    monkeypatch.setattr(settings, 'service_account_source', lambda: (None, info))
    monkeypatch.setattr(private_state_bindings, 'unwrap',
        lambda label, wrapped, key: 'private-folder' if label == 'KAKEIBO_STATE_FOLDER_ID' else 'dedicated-file')
    acl, source, sheet = Mock(), Mock(), Mock()
    acl.files.return_value.get.return_value.execute.return_value = {
        'owners': [{'emailAddress': 'owner@example.test'}], 'permissions': [
            {'type': 'user', 'role': 'owner', 'emailAddress': 'owner@example.test'},
            {'type': 'user', 'role': 'writer', 'emailAddress': 'sa@example.test'}]}
    monkeypatch.setattr(google_clients, 'drive_service', lambda: acl)
    monkeypatch.setattr(google_clients, 'read_only_drive_service', lambda: source)
    monkeypatch.setattr(google_clients, 'sheets_service', lambda: sheet)
    drive = FakeV2()
    drive.payload = authority.encoded(authority.empty_state(BINDING))
    monkeypatch.setattr(worker, 'conditional_drive_state_service_v2', lambda: drive)
    env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': 'tmoriuchi1401-source/kakeibo-ai', 'GITHUB_REF': 'refs/heads/main',
        'GITHUB_SHA': 'reviewed', 'KAKEIBO_VALIDATED_MAIN_SHA': 'reviewed',
        'PDF_GROUPING_REVIEW_ENABLED': 'true',
        'GITHUB_WORKFLOW_REF': 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/pdf-grouping-review.yml@refs/heads/main',
        'PDF_GROUPING_BINDING': json.dumps({'folder': 'wrapped', 'file': 'wrapped',
                                          'owner_digest': _digest('owner@example.test')})}
    grouping, projection, reader = worker.open_context(env)
    assert grouping.store.transport.service is drive
    assert projection.service is sheet and reader.service is source
    assert acl.files.return_value.get.call_count == 4
    acl.files.return_value.update.assert_not_called()
    source.files.assert_not_called()
    sheet.spreadsheets.assert_not_called()
    assert drive.updates == []


@pytest.mark.parametrize('kinds', [('normal', 'normal'), ('normal', 'medical')])
def test_grouping_confirmation_v2_and_replay_no_forbidden_calls(local_ocr, monkeypatch, kinds):
    forbidden = Mock(side_effect=AssertionError('forbidden boundary'))
    monkeypatch.setattr('app.gemini_ai.GeminiAI.__init__', forbidden)
    monkeypatch.setattr('app.receipt_pipeline.ReceiptPipeline.process_bytes', forbidden)
    monkeypatch.setattr('app.google_clients.drive_service', forbidden)
    monkeypatch.setattr('app.google_clients.sheets_service', forbidden)
    monkeypatch.setattr('app.settings.Settings.medical_review_handoff', forbidden)
    drive = FakeV2()
    drive.payload = authority.encoded(authority.empty_state(BINDING))
    service, _, _ = context(kinds, transport=adapter(drive))
    view = service.display('drive-source-id')
    assert not service.confirmed_units('drive-source-id')
    result = service.review(request(view))
    assert result['status'] == 'grouping_confirmed'
    for flag in ('accounting_allowed', 'medical_handoff_allowed', 'archive_allowed'):
        assert result['confirmation'][flag] is False
    before = len(drive.updates)
    assert service.review(request(view))['confirmation'] == result['confirmation']
    assert len(drive.updates) == before
    if 'medical' in kinds:
        assert 'medical_pending' in result['confirmation']['unit_statuses']
    forbidden.assert_not_called()
