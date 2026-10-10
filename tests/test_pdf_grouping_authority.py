"""Synthetic local observations + fake durable Drive + management-only UI."""
from copy import deepcopy
from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from app.drive_run_state import StateError
from app import pdf_grouping_authority as authority, pdf_grouping_ui as ui
from app import pdf_grouping_review as worker
from app import receipt_pdf_grouping as local, receipt_pdf_units as pdf
from test_receipt_pdf_units import local_ocr, synthetic_pdf, pipeline

BINDING = pdf._digest(['private-folder', 'dedicated-file', 'management-sheet'])
NOW = '2026-10-01T09:00:00+00:00'


class MemoryTransport:
    def __init__(self):
        self.payload = authority.encoded(authority.empty_state(BINDING))
        self.tag, self.writes = 0, 0
        self.fail_write = self.fail_read = self.bad_readback = False
        self.lock = threading.Lock()

    def read_versioned(self):
        if self.fail_read:
            raise StateError('state_drive_read_failed')
        with self.lock:
            return self.payload, '"memory-'+str(self.tag)+'"'

    def replace_versioned(self, expected, tag, proposed):
        with self.lock:
            if expected != self.payload or tag != '"memory-'+str(self.tag)+'"':
                raise StateError('state_changed_since_read')
            if self.fail_write:
                raise StateError('state_drive_write_unknown')
            self.payload, self.tag = proposed, self.tag + 1
            self.writes += 1
            if self.bad_readback:
                self.payload = b'{}'


def context(kinds=('normal', 'normal'), *, transport=None):
    transport = transport or MemoryTransport()
    live = SimpleNamespace(observations=pdf.observe_pdf(synthetic_pdf(kinds), 'drive-source-id'))
    store = authority.DriveGroupingStore(transport, BINDING)
    svc = authority.DurablePdfGrouping(store, lambda sid, previous: live.observations, clock=lambda: NOW)
    return svc, live, transport


def request(view, *, operation='confirm', partition=None, number=1):
    proposal = view['proposal']
    return {'request_id': str(UUID(int=number)), 'source_file_id': proposal['source_file_id'],
            'source_content_hash': proposal['source_content_hash'], 'proposal_digest': proposal['proposal_digest'],
            'grouping_revision': proposal['grouping_version'], 'operation': operation, 'partition': partition}


class MemorySheet:
    def __init__(self, view, operation='確定', target=''):
        self.snapshot = ui.project(view)
        self.snapshot[7:9] = [operation, target]
        self.rows, self.finished, self.fail_publish, self.fail_finish = [], [], False, False

    def request(self, request_id):
        return 2, deepcopy(self.snapshot)

    def publish(self, view):
        if self.fail_publish:
            raise RuntimeError('private sheet failure')
        self.rows.append(ui.project(view))

    def finish(self, request_id, snapshot, result, now):
        if self.fail_finish:
            raise RuntimeError('private sheet finish failure')
        self.finished.append((request_id, result))

    def mark_unverified(self, snapshot, result):
        self.rows.append(['authority未確認', result])


def test_proposal_projection_contains_no_accounting_or_raw_ocr(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    assert view['status'] == 'grouping_required'
    assert view['authority_scope'] == []
    row = ui.project(view)
    assert row[1] == '未確定' and row[2] == 2
    assert row[3] == 'Group 1: p1\nGroup 2: p2'
    assert row[4] == 'Group 1: 一般候補\nGroup 2: 一般候補'
    assert row[6] == 'https://drive.google.com/file/d/drive-source-id/view'
    assert not svc.confirmed_units('drive-source-id')
    assert b'PRIVATE_ATTACHMENT_99' not in transport.payload
    assert b'PRIVATE_MEDICAL_42' not in transport.payload


def test_normal_confirm_durable_between_independent_workers_and_readback(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    result = svc.review(request(view))
    assert result['status'] == 'grouping_confirmed'
    confirmation = result['confirmation']
    assert confirmation['confirmed_at'] == NOW
    assert confirmation['authority_scope'] == ['grouping_confirmed', 'rendered_payload_only']
    assert all(confirmation[k] is False for k in ('accounting_allowed', 'medical_handoff_allowed', 'archive_allowed'))
    value = json.loads(transport.payload)
    assert value['records'][pdf._digest('drive-source-id')]['confirmation'] == confirmation
    next_worker = authority.DurablePdfGrouping(authority.DriveGroupingStore(transport, BINDING),
        lambda sid, previous: live.observations, clock=lambda: '2026-10-02T00:00:00+00:00')
    assert next_worker.display('drive-source-id')['confirmation'] == confirmation
    assert next_worker.confirmed_units('drive-source-id') == svc.confirmed_units('drive-source-id')


def test_synthetic_projection_has_no_broken_drive_link_and_keeps_identity_checks(local_ocr):
    svc, live, _ = context()
    source = 'synthetic-pdf-grouping-live-v1'
    live.observations = replace(live.observations, source_file_id=source,
        pages=tuple(replace(p, source_file_id=source) for p in live.observations.pages))
    view = svc.display(source)
    row = ui.project(view)
    assert row[1] == 'テストデータ・未確定'
    assert row[6] == 'テストデータ（原本ファイルなし）'
    assert not any(str(cell).startswith('https://drive.google.com/') for cell in row)
    row[7] = '確定'
    captured = ui.captured_request(row, str(UUID(int=1)), view['proposal'])
    assert captured['source_file_id'] == source
    row[6] = 'https://drive.google.com/file/d/' + source + '/view'
    with pytest.raises(StateError, match='stale_proposal'):
        ui.captured_request(row, str(UUID(int=1)), view['proposal'])


def test_edit_new_proposal_confirm_and_unchanged_edit_no_revision(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    svc.review(request(view))
    old = svc.confirmed_units('drive-source-id')
    edit = svc.review(request(view, operation='edit', partition=[[1, 2]], number=2))
    assert edit['proposal']['grouping_version'] == 2 and edit['confirmation'] is None
    svc.review(request(edit, number=3))
    new = svc.confirmed_units('drive-source-id')
    assert len(new) == 1 and new[0].unit_id not in {u.unit_id for u in old}
    unchanged = svc.review(request(edit, operation='edit', partition=[[1, 2]], number=4))
    assert unchanged['result'] == 'unchanged_partition'
    assert unchanged['proposal']['grouping_version'] == 2
    assert svc.confirmed_units('drive-source-id') == new


@pytest.mark.parametrize('operation', ['reject', 'hold'])
def test_reject_and_hold_revoke_authority(local_ocr, operation):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    svc.review(request(view))
    result = svc.review(request(view, operation=operation, number=2))
    assert result['status'] == ('rejected' if operation == 'reject' else 'held')
    assert not svc.confirmed_units('drive-source-id')
    assert result['confirmation'] is None


@pytest.mark.parametrize('change', ['source_hash', 'member_hash', 'count', 'classification', 'extraction'])
def test_original_changes_invalidate_confirmation_and_stale_request(local_ocr, change):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    svc.review(request(view))
    if change == 'source_hash':
        live.observations = pdf.observe_pdf(synthetic_pdf(['normal', 'normal'], metadata='CHANGED'), 'drive-source-id')
    elif change == 'count':
        live.observations = replace(live.observations, pages=live.observations.pages +
            (replace(live.observations.pages[1], page_number=3),))
    else:
        changes = {'page_hash': 'f' * 64} if change == 'member_hash' else {'classification': 'medical'} if change == 'classification' else {'extraction_status': 'ocr_failed'}
        live.observations = replace(live.observations, pages=(replace(live.observations.pages[0], **changes), live.observations.pages[1]))
    assert not svc.confirmed_units('drive-source-id')
    assert svc.review(request(view, number=2))['result'] == 'stale_proposal'
    assert not svc.display('drive-source-id')['confirmation']


@pytest.mark.parametrize('field', ['source_content_hash', 'proposal_digest', 'grouping_revision'])
def test_stale_context_fields_rejected(local_ocr, field):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    req = request(view)
    req[field] = 99 if field == 'grouping_revision' else 'f' * 64
    assert svc.review(req)['result'] == 'stale_proposal'
    assert svc.confirmed_units('drive-source-id') == ()


def test_confirmation_replay_is_idempotent_and_keeps_timestamp_identity(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    first = svc.review(request(view))
    units = svc.confirmed_units('drive-source-id')
    writes = transport.writes
    assert svc.review(request(view)) == first
    assert transport.writes == writes
    svc.clock = lambda: '2026-10-02T00:00:00+00:00'
    second = svc.review(request(view, number=2))
    assert second['confirmation'] == first['confirmation']
    assert svc.confirmed_units('drive-source-id') == units


def test_old_confirm_request_replay_after_hold_cannot_reactivate_authority(local_ocr):
    svc, live, remote = context()
    view = svc.display('drive-source-id')
    original = request(view)
    svc.review(original)
    svc.review(request(view, operation='hold', number=2))
    assert svc.review(original)['result'] == 'stale_proposal'
    assert not svc.confirmed_units('drive-source-id')
    assert svc.display('drive-source-id')['status'] == 'held'


def test_failed_proposer_persists_grouping_required_without_confirmation(local_ocr):
    svc, live, remote = context()
    svc.proposer = Mock()
    svc.proposer.propose.side_effect = RuntimeError('PRIVATE_FAILURE')
    view = svc.display('drive-source-id')
    assert view['confirmation'] is None and view['proposal'] is None
    assert view['status'] == 'grouping_required' and view['authority_scope'] == []
    assert svc.confirmed_units('drive-source-id') == ()
    assert b'PRIVATE_FAILURE' not in remote.payload


def test_two_stale_workers_cannot_overwrite_and_queued_old_edit_rejected(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    left, right = authority.DriveGroupingStore(transport, BINDING), authority.DriveGroupingStore(transport, BINDING)
    a, b = left.load(), right.load()
    a['generation'] += 1
    left.save(a)
    b['generation'] += 2
    with pytest.raises(StateError, match='stale_proposal'):
        right.save(b)
    edited = svc.review(request(view, operation='edit', partition=[[1, 2]], number=2))
    assert svc.review(request(view, number=3))['result'] == 'stale_proposal'
    assert svc.display('drive-source-id')['proposal'] == edited['proposal']


@pytest.mark.parametrize('corruption', ['missing', 'json', 'schema', 'binding', 'member', 'digest', 'revision', 'flag', 'extra'])
def test_bad_drive_state_fail_closed_never_uses_cache(local_ocr, corruption):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    svc.review(request(view))
    if corruption == 'missing':
        transport.fail_read = True
    elif corruption == 'json':
        transport.payload = b'broken'
    else:
        value = json.loads(transport.payload)
        record = next(iter(value['records'].values()))
        if corruption == 'schema': value['schema'] = 9
        elif corruption == 'binding': value['binding'] = 'other'
        elif corruption == 'member': record['confirmation']['pages'][0]['page_hash'] = 'f' * 64
        elif corruption == 'digest': record['confirmation']['proposal_digest'] = 'f' * 64
        elif corruption == 'revision': record['confirmation']['grouping_revision'] += 1
        elif corruption == 'flag': record['confirmation']['accounting_allowed'] = 0
        else: record['confirmation']['raw_ocr'] = 'PRIVATE'
        transport.payload = authority.encoded(value)
    assert svc.display('drive-source-id')['status'] == 'grouping_required'
    assert svc.confirmed_units('drive-source-id') == ()


def test_local_only_confirmation_is_not_actions_authority(tmp_path, local_ocr, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    svc, live, transport = context()
    offline = local.PdfGroupingService(local.GroupingStore(tmp_path / 'local'),
                                      observation_store=pdf.PdfUnitManifestStore(tmp_path / 'observations'))
    proposal = offline.prepare(live.observations)['proposal']
    offline.review(live.observations, proposal['proposal_digest'], action='confirm')
    assert offline.units(live.observations)
    assert svc.confirmed_units('drive-source-id') == ()
    transport.fail_read = True
    assert svc.confirmed_units('drive-source-id') == ()
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    with pytest.raises(local.GroupingError, match='not_actions_authority'):
        offline.units(live.observations)


@pytest.mark.parametrize('operation,target', [('確定', ''), ('結合', '1+2')])
@pytest.mark.parametrize('failure', ['publish', 'finish'])
def test_projection_failure_replay_does_not_duplicate_authority_or_revision(local_ocr, operation, target, failure):
    svc, live, transport = context()
    sheet = MemorySheet(svc.display('drive-source-id'), operation, target)
    setattr(sheet, 'fail_' + failure, True)
    with pytest.raises(RuntimeError):
        ui.process_request(svc, sheet, str(UUID(int=1)))
    saved = transport.payload
    units = svc.confirmed_units('drive-source-id')
    setattr(sheet, 'fail_' + failure, False)
    result = ui.process_request(svc, sheet, str(UUID(int=1)))
    assert result['result'] == ('confirmed' if operation == '確定' else 'proposal_updated')
    assert transport.payload == saved and svc.confirmed_units('drive-source-id') == units


def test_drive_save_failure_never_displays_confirmed(local_ocr):
    svc, live, transport = context()
    sheet = MemorySheet(svc.display('drive-source-id'))
    transport.fail_write = True
    with pytest.raises(StateError, match='write_unknown'):
        ui.process_request(svc, sheet, str(UUID(int=1)))
    assert not sheet.rows and not sheet.finished
    assert not svc.confirmed_units('drive-source-id')


def test_save_readback_failure_blocks_success(local_ocr):
    svc, live, transport = context()
    sheet = MemorySheet(svc.display('drive-source-id'))
    transport.bad_readback = True
    with pytest.raises(StateError, match='readback_mismatch'):
        ui.process_request(svc, sheet, str(UUID(int=1)))
    assert not sheet.rows and not sheet.finished
    assert svc.confirmed_units('drive-source-id') == ()


@pytest.mark.parametrize('kinds', [('normal', 'normal'), ('normal', 'medical'), ('medical', 'normal')])
def test_confirmed_normal_and_medical_cannot_call_production_boundaries(tmp_path, local_ocr, monkeypatch, kinds):
    from app import gemini_ai, google_clients, cli
    gemini, medical, ledger, move = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(gemini_ai.GeminiAI, 'analyze_receipt', gemini)
    monkeypatch.setattr(cli, 'make_receipt_pipeline', medical)
    monkeypatch.setattr(cli, 'make', ledger)
    monkeypatch.setattr(google_clients, 'drive_service', move)
    svc, live, transport = context(kinds)
    sheet = MemorySheet(svc.display('drive-source-id'))
    result = ui.process_request(svc, sheet, str(UUID(int=1)))
    assert result['result'] == 'confirmed'
    assert all(result[k] == 0 for k in ('gemini_calls', 'medical_calls', 'accounting_writes', 'archive_moves'))
    for boundary in (gemini, medical, ledger, move): boundary.assert_not_called()
    if 'medical' in kinds:
        assert 'medical_pending' in svc.display('drive-source-id')['confirmation']['unit_statuses']
    assert b'PRIVATE_MEDICAL_42' not in transport.payload


def test_audit_minimal_without_actor_or_ocr_and_stale_ui_audited(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    sheet = MemorySheet(view)
    svc.review(request(view, operation='edit', partition=[[1, 2]], number=2))
    assert ui.process_request(svc, sheet, str(UUID(int=3)))['result'] == 'stale_proposal'
    events = json.loads(transport.payload)['audit']
    assert events[-1]['result'] == 'stale_proposal'
    assert events[-1]['timestamp'] == NOW
    assert events[-1]['before_revision'] == events[-1]['after_revision'] == 2
    assert not any('actor' in e or 'raw_ocr' in e for e in events)


def test_ui_partition_edit_controls_require_human_reconfirmation(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    sheet = MemorySheet(view, '結合', '1+2')
    assert ui.process_request(svc, sheet, str(UUID(int=1)))['result'] == 'proposal_updated'
    merged = svc.display('drive-source-id')
    assert not svc.confirmed_units('drive-source-id')
    split = MemorySheet(merged, '分割', '1')
    assert ui.process_request(svc, split, str(UUID(int=2)))['result'] == 'proposal_updated'
    new = svc.display('drive-source-id')
    assert [g['page_numbers'] for g in new['proposal']['groups']] == [[1], [2]]
    assert ui.process_request(svc, MemorySheet(new), str(UUID(int=3)))['result'] == 'confirmed'


def test_worker_denies_scheduled_unvalidated_context_and_ai_credentials():
    env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
           'GITHUB_REPOSITORY': 'tmoriuchi1401-source/kakeibo-ai', 'GITHUB_REF': 'refs/heads/main',
           'GITHUB_SHA': 'reviewed', 'KAKEIBO_VALIDATED_MAIN_SHA': 'reviewed', 'PDF_GROUPING_REVIEW_ENABLED': 'true',
           'GITHUB_WORKFLOW_REF': 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/pdf-grouping-review.yml@refs/heads/main'}
    worker.require_manual_context(env)
    for patch in ({'GITHUB_EVENT_NAME': 'schedule'}, {'GITHUB_SHA': 'other'}, {'GEMINI_API_KEY': 'fake'},
                  {'PDF_GROUPING_REVIEW_ENABLED': 'false'}, {'GITHUB_REF': 'refs/heads/codex/pdf-page-privacy'}):
        with pytest.raises(StateError): worker.require_manual_context({**env, **patch})
