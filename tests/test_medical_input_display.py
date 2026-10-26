"""Only synthetic snapshots. Compare decisions/writes with the old display."""
from copy import deepcopy
from types import MethodType
import time

import pytest

from registration_fixtures import Rig
from sheets_only_fixtures import no_clients, evaluate
from staging_shadow.medical_input_display import partial_reason, PREFIX, STOPPED
from staging_shadow.sheets_only_review import SheetsOnlyReview
from staging_shadow.live_synthetic_sheets import LiveSyntheticBridge
from test_live_synthetic_sheets import provision, row, poll, accept, apply
from types import SimpleNamespace

CID = 'SYN-M101'


@pytest.mark.parametrize('changes,error,names', [
    ({'amount': ''}, 'draft_v2_integer', ['実支払額']),
    ({'date': '', 'merchant': '', 'amount': '', 'category': ''}, 'draft_v2_text',
     ['日付', '施設名', '実支払額', 'カテゴリ']),
    ({'category': ' '}, 'draft_v2_complete_manual_only', ['カテゴリ']),
    ({'payment': '', 'memo': ''}, 'draft_v2_receipt_validator', []),
    ({'amount': -1}, 'draft_v2_integer', []),
])
def test_rejected_snapshot_to_labels_contains_no_medical_values(changes, error, names):
    fields = dict(date='2026-10-01', merchant='SYNTHETIC-PRIVATE-NAME', amount=135,
                  category='SYNTHETIC-CATEGORY', payment='', memo='SYNTHETIC-PRIVATE-MEMO')
    fields.update(changes)
    before = deepcopy(fields)
    text = partial_reason(error, fields)
    assert fields == before
    assert ('未入力：' in text) == bool(names)
    for name in names:
        assert name in text
    for value in ['SYNTHETIC-PRIVATE-NAME', 'SYNTHETIC-PRIVATE-MEMO', '2026-10-01', '135']:
        assert value not in text


@pytest.mark.parametrize('changes', [
    {'amount': ''}, {'date': '', 'merchant': '', 'amount': '', 'category': ''},
    {'category': ''}, {'payment': '', 'memo': ''}, {'date': 'invalid-date'},
])
def test_actual_validator_decision_receipts_and_draft_writes_equal_old_display(monkeypatch, tmp_path, changes):
    no_clients(monkeypatch)
    clock = int(time.time())
    monkeypatch.setattr(time, 'time', lambda: clock)
    (tmp_path/'old').mkdir()
    (tmp_path/'new').mkdir()
    old = Rig(monkeypatch, tmp_path/'old')
    new = Rig(monkeypatch, tmp_path/'new')
    # Exactly the base finishing/partial-message methods used by #159.
    old.worker._finish = MethodType(SheetsOnlyReview._finish, old.worker)
    old.worker._partial_reason = MethodType(SheetsOnlyReview._partial_reason, old.worker)
    for r in (old, new):
        for key, value in changes.items():
            r.set(CID, key, value)
    a, b = old.register(CID), new.register(CID)
    assert a == b
    assert old.draft_writes() == new.draft_writes()
    assert old.sheet.core() == new.sheet.core()
    ar, br = old.worker._state()[1]['requests'], new.worker._state()[1]['requests']
    assert ar.keys() == br.keys()
    for rid in ar:
        assert {k:v for k,v in ar[rid].items() if k != 'reason'} == {k:v for k,v in br[rid].items() if k != 'reason'}
    text = evaluate(new.sheet, CID)
    if changes == {'amount': ''}:
        assert '入力不足・未入力：実支払額' in text and '登録 2' in text
    if changes == {'payment': '', 'memo': ''}:
        assert b['status'] == 'VALIDATED_UNPOSTED' and '未入力：' not in text
    if br[b['request_id']]['reason'].startswith(PREFIX):
        assert STOPPED in text
    old.assert_safe(); new.assert_safe()


def live(r):
    raw, sources, profile, acl = provision(r)
    return SimpleNamespace(r=r, raw=raw, baseline=deepcopy(raw), sources=sources,
                           profile=profile, acl=acl,
                           b=LiveSyntheticBridge(r.worker, raw, sources, profile, acl, enabled=True))


def deliver(x, result):
    for p in result['pending']:
        apply(x, x.b.ready(p['id'], x.raw, x.sources, x.profile, x.acl))
    assert not poll(x)['pending']


def test_live_medical_display_preserves_inputs_and_freezes_received_results(monkeypatch, tmp_path):
    no_clients(monkeypatch); r = Rig(monkeypatch, tmp_path); x = live(r)
    row(x, CID)[8].pop('userEnteredValue', None)
    before = deepcopy(x.raw)
    result = accept(x, CID)
    assert result['results'][-1]['status'] == 'HELD'
    deliver(x, result)
    formula = row(x, CID)[2]['userEnteredValue']['formulaValue']
    assert '未入力：実支払額' in formula and STOPPED in formula
    for a, b in zip(before['sheets'][0]['data'][0]['rowData'], x.raw['sheets'][0]['data'][0]['rowData']):
        assert a['values'][:1]+a['values'][3:] == b['values'][:1]+b['values'][3:]
    saved = deepcopy(r.worker._state()[1]); writes = r.draft_writes()
    r.worker = r.rebuild()
    x.b = LiveSyntheticBridge(r.worker, x.baseline, x.sources, x.profile, x.acl, enabled=True)
    assert not poll(x)['pending']
    assert saved == r.worker._state()[1] and writes == r.draft_writes()
    row(x, CID)[8]['userEnteredValue'] = {'numberValue': 135}
    row(x, CID)[1]['userEnteredValue'] = {'stringValue': '登録 2'}
    deliver(x, poll(x)); r.old.h.now += 3
    result = poll(x)
    assert result['results'][-1]['status'] == 'VALIDATED_UNPOSTED'
    deliver(x, result)
    assert '登録検証済み・未記帳' in row(x, CID)[2]['userEnteredValue']['formulaValue']
    r.assert_safe()


def test_live_received_case_does_not_suggest_another_registration(monkeypatch, tmp_path):
    no_clients(monkeypatch); r = Rig(monkeypatch, tmp_path); x = live(r)
    deliver(x, accept(x, CID))
    row(x, CID)[1]['userEnteredValue'] = {'stringValue': '登録 2'}
    result = poll(x)
    assert result['results'][-1]['status'] == 'HELD'
    text = result['pending'][0]['requests'][0]['updateCells']['rows'][0]['values'][0]['userEnteredValue']['formulaValue']
    assert '受付済み・未記帳' in text and '再登録せず' in text
    assert '登録 3' not in text and '必須項目を入力後' not in text
    r.assert_safe()


@pytest.mark.parametrize('scenario', ['conflict', 'unknown', 'permission'])
def test_stop_states_never_suggest_registration(monkeypatch, tmp_path, scenario):
    no_clients(monkeypatch); r = Rig(monkeypatch, tmp_path)
    if scenario == 'conflict':
        result = r.register(CID, 2)
        assert result['status'] == 'CONFLICT'
    elif scenario == 'unknown':
        def lost(*a, **k): raise TimeoutError('synthetic response loss')
        monkeypatch.setattr(r.old.old.native[CID].readers.draft, 'replace_versioned', lost)
        r.set(CID, 'amount', 136)
        assert r.register(CID)['status'] == 'UNKNOWN'
    else:
        r.policy(lambda v:v.update(permissions_verified=False))
        assert r.register(CID)['status'] == 'PERMISSION_UNVERIFIED'
    text = evaluate(r.sheet, CID)
    assert '登録 2' not in text and '次の登録' not in text and '未入力：' not in text
    r.assert_safe()


def test_legacy_medical_receipt_is_not_republished_for_new_wording(monkeypatch, tmp_path):
    no_clients(monkeypatch); r = Rig(monkeypatch, tmp_path); x = live(r)
    monkeypatch.setattr(r.worker, '_finish', MethodType(SheetsOnlyReview._finish, r.worker))
    monkeypatch.setattr(r.worker, '_partial_reason', MethodType(SheetsOnlyReview._partial_reason, r.worker))
    row(x, CID)[8].pop('userEnteredValue', None)
    deliver(x, accept(x, CID))
    before = deepcopy(x.raw); receipts = deepcopy(r.worker._state()[1]); writes = r.draft_writes()
    r.worker = r.rebuild()
    x.b = LiveSyntheticBridge(r.worker, x.baseline, x.sources, x.profile, x.acl, enabled=True)
    assert not poll(x)['pending']
    assert x.raw == before and r.worker._state()[1] == receipts and r.draft_writes() == writes
    r.assert_safe()


def test_stale_accepted_number_projects_conflict_without_resend_advice(monkeypatch, tmp_path):
    no_clients(monkeypatch); r = Rig(monkeypatch, tmp_path)
    r.set(CID, 'amount', '')
    original_memo = r.sheet.row(CID)[14]['userEnteredValue'].get('stringValue', '')
    assert r.register(CID)['status'] == 'HELD'
    r.set(CID, 'memo', 'synthetic second request')
    assert r.register(CID, 2)['status'] == 'HELD'
    receipts = deepcopy(r.worker._state()[1])
    r.set(CID, 'memo', original_memo); r.request(CID, '登録 1')
    assert r.worker.poll()[-1]['status'] == 'CONFLICT'
    text = evaluate(r.sheet, CID)
    assert '再登録せず' in text and '登録 3' not in text and '次の登録' not in text
    options = r.sheet.row(CID)[1]['dataValidation']['condition']['values']
    assert not any(v['userEnteredValue'] == '登録 3' for v in options)
    assert r.worker._state()[1] == receipts
    r.assert_safe()
