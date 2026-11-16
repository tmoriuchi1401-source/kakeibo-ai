"""Synthetic split responsibility: checked observations, no continuous ACL claim."""
from copy import deepcopy
import json

import pytest

from app.drive_run_state import StateError
from app.receipt_sheets_owner_check import check_owner_assets, check_owner_after
from app.receipt_sheets_worker import GooglePort, connect
from test_receipt_sheets_acl import MetadataSession
from test_receipt_sheets_source_identity import received, OriginalSession, run_case


@pytest.fixture(autouse=True)
def no_financial_or_legacy_io(monkeypatch):
    from app.sheets import SheetsDB
    from app.receipt_private_adapter import ReceiptPrivateAdapter
    def deny(*a, **kw): raise AssertionError('financial/legacy IO forbidden')
    monkeypatch.setattr(SheetsDB, '__init__', deny)
    monkeypatch.setattr(ReceiptPrivateAdapter, '__init__', deny)


def observations():
    w, c, _, js = received()
    s = MetadataSession(w, c)
    return w, c, js, s.meta[c['spreadsheet_id']], {w.source['source_id']: s.meta[w.source['source_id']]}


def test_owner_preflight_and_postcheck_bind_one_sheet_case_and_png():
    w, c, _, sheet, originals = observations()
    before = check_owner_assets(c, sheet, originals, phase='before')
    after = check_owner_after(c, deepcopy(sheet), deepcopy(originals), before)
    assert before['case_id'] == after['case_id'] == w.case_id
    assert before['source_id'] == after['source_id'] == w.source['source_id']
    assert before['source_sha256'] == after['source_sha256'] == w.source['sha256']
    assert before['continuous_acl_parent_verification'] is after['continuous_acl_parent_verification'] is False
    assert before['posting_authority'] is after['posting_authority'] is False


@pytest.mark.parametrize('field', ['mode', 'synthetic_only', 'ref'])
def test_split_verification_is_unavailable_to_legacy_or_production_bindings(field):
    w, c, _, sheet, originals = observations()
    c[field] = {'mode': 'legacy-journal', 'synthetic_only': False, 'ref': 'main'}[field]
    with pytest.raises(StateError, match='synthetic_trial_required'):
        GooglePort(OriginalSession(w), c)
    with pytest.raises(StateError, match='synthetic_trial_required'):
        with connect(c, {}): raise AssertionError('connected')
    with pytest.raises(StateError, match='synthetic_trial_required'):
        check_owner_assets(c, sheet, originals, phase='before')


@pytest.mark.parametrize('damage', ['sheet_id', 'png_id', 'source_map', 'import_source', 'machine_source', 'case_id'])
def test_owner_rejects_wrong_sheet_or_case_original_correspondence(damage):
    w, c, _, sheet, originals = observations()
    sid = w.source['source_id']
    if damage == 'sheet_id': sheet['id'] = 'another-sheet-id'
    elif damage == 'png_id': originals[sid]['id'] = 'another-original-id'
    elif damage == 'source_map': originals['extra-original'] = originals.pop(sid)
    else:
        item = c['cases'][w.case_id]
        entry = json.loads(item['context_json'])
        if damage == 'import_source': entry['import_row'][3] = 'another-original'
        elif damage == 'machine_source': entry['decoder']['machine'][1] = 'another-original'
        elif damage == 'case_id': entry['import_row'][0] = 'another-case'
        item['context_json'] = json.dumps(entry)
    with pytest.raises(StateError): check_owner_assets(c, sheet, originals, phase='before')


@pytest.mark.parametrize('damage', ['parent', 'extra_user', 'missing_acl'])
def test_owner_fresh_postcheck_rejects_access_drift(damage):
    w, c, _, sheet, originals = observations()
    before = check_owner_assets(c, sheet, originals, phase='before')
    m = originals[w.source['source_id']]
    if damage == 'parent': m['parents'] = ['different-parent']
    elif damage == 'extra_user': m['permissions'].append(dict(type='user',role='reader',emailAddress='other@synthetic.invalid'))
    else: m.pop('permissions')
    with pytest.raises(StateError): check_owner_after(c, sheet, originals, before)


def test_owner_postcheck_rejects_preflight_from_changed_binding():
    _, c, _, sheet, originals = observations()
    before = check_owner_assets(c, sheet, originals, phase='before')
    c['folder_id'] = 'another-parent-id'
    with pytest.raises(StateError, match='preflight_binding_changed'):
        check_owner_after(c, sheet, originals, before)


def test_owner_preflight_pins_identity_while_acceptance_enabled_flag_can_change():
    _, c, _, sheet, originals = observations()
    c['enabled'] = False
    before = check_owner_assets(c, sheet, originals, phase='before')
    c['enabled'] = True
    assert check_owner_after(c, sheet, originals, before)['phase'] == 'after'


def test_worker_accepts_reader_metadata_without_acl_or_parent_and_checks_two_byte_reads():
    w, c, js, _, _ = observations()
    s = OriginalSession(w)
    s.meta.pop('permissions'); s.meta.pop('parents')
    result, p = run_case(w, c, js, s)
    assert result['state'] == '検証済み・未記帳' and result['financial_write_count'] == 0
    assert result['posting_authority'] is False and s.media_reads == 2 and len(p.writes) == 2
    m = GooglePort(s, c).metadata(w.source['source_id'])
    assert set(m) == {'id', 'trashed', 'mimeType', 'owners'}


def test_worker_does_not_request_unavailable_acl_or_parent_fields():
    w, c, _, _, _ = observations()
    s = OriginalSession(w)
    original = s.get
    def get(url, **kw):
        if 'fields' in kw['params']:
            assert kw['params']['fields'] == 'id,trashed,mimeType,owners(emailAddress)'
        return original(url, **kw)
    s.get = get
    GooglePort(s, c).source_check(w.source)
    assert s.media_reads == 1


@pytest.mark.parametrize('damage', ['id', 'mime', 'owner', 'sha256'])
def test_worker_rejects_wrong_content_identity_even_without_acl_parent(damage):
    w, c, js, _, _ = observations()
    s = OriginalSession(w); s.meta.pop('permissions'); s.meta.pop('parents')
    if damage == 'id': s.meta['id'] = 'another-original-id'
    elif damage == 'mime': s.meta['mimeType'] = 'image/jpeg'
    elif damage == 'owner': s.meta['owners'] = [dict(emailAddress='other@synthetic.invalid')]
    else: s.raw += b'changed-content'
    result, p = run_case(w, c, js, s)
    assert result['state'] == '結果不明・照合待ち' and len(p.writes) == 1


def test_context_mismatch_stops_before_source_io():
    w, c, _, _, _ = observations()
    s = OriginalSession(w)
    with pytest.raises(StateError, match='context_changed'):
        GooglePort(s, c).context_check(dict(case_id=w.case_id,context_json='altered snapshot context'))
    assert s.trace == []


def test_change_and_restore_between_owner_observations_cannot_be_detected():
    w, c, js, sheet, originals = observations()
    before = check_owner_assets(c, sheet, originals, phase='before')
    s = OriginalSession(w)
    def transient_change(session):
        old = deepcopy(session.meta)
        session.meta['parents'] = ['transient-parent']
        session.meta['permissions'].append(dict(type='anyone',role='reader'))
        session.meta = old
    result, _ = run_case(w, c, js, s, transient_change)
    after = check_owner_after(c, sheet, originals, before)
    assert result['state'] == '検証済み・未記帳'
    # Matching endpoint observations do NOT prove there was no intervening drift.
    assert after['continuous_acl_parent_verification'] is False
