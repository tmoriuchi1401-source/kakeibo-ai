from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import uuid

import pytest

from app.iphone_readonly_host import PREFIX, SavedDraftRepository, make_isolated_readonly_application
from app.iphone_sheet_reader import CASE_IDS
from app.unified_receipt_review import digest
from app.unified_receipt_shadow import ShadowConfig
from iphone_persistent_fixtures import provision, repository
from test_iphone_persistent_drafts import edited
from unified_shadow_fixtures import HostRig, ORIGIN, DOMAIN


PATHS = [''] + ['/cases/'+c for c in sorted(CASE_IDS)]


def rig(monkeypatch, tmp_path):
    r, doc, key, bundle, batch, local = provision(tmp_path)
    h = HostRig(monkeypatch, enabled=False)
    path = tmp_path/'display.json'; path.write_text(json.dumps(bundle), encoding='utf8')
    view = SavedDraftRepository(r, path, digest(bundle), r.scope['owner'])
    h.app = make_isolated_readonly_application(h.web.make_application, h.configured, view,
        config=ShadowConfig(True, 'isolated_synthetic'), cookies={'login':h.web.LOGIN_COOKIE, 'ticket':h.web.COOKIE})
    h.app.testing = True; h.client = h.app.test_client()
    return h, r, doc, key, batch, view, local


def test_eight_cases_coobserved_versions_items_and_get_zero_writes(monkeypatch, tmp_path):
    h, r, doc, key, batch, view, local = rig(monkeypatch, tmp_path)
    assert h.login().status_code == 303
    before = deepcopy(h.db.data), doc.payload, doc.writes, local.path.read_bytes()
    for _ in range(2):
        for path in PATHS: assert h.get(PREFIX+path).status_code == 200
    cards = {c['case_id']:c for c in view.snapshots()}
    assert len(cards) == 8
    assert cards['SYN-G103']['state'] == cards['SYN-H301']['state'] == 'held'
    assert cards['SYN-H301']['disposition'] == 'LEGACY_UNMAPPED_HOLD'
    assert cards['SYN-P201']['purpose'] == 'STRUCTURE_REVIEW'
    assert cards['SYN-M101']['display']['manual_only']
    body = h.get(PREFIX+'/cases/SYN-G101').get_data(as_text=True)
    assert '旧値' in body and '提案値' in body and '未入力' in body and '原本 p1' in body
    assert all(c['operation_targets'] == [] and not c['display']['authorized'] for c in cards.values())
    assert before == (h.db.data, doc.payload, doc.writes, local.path.read_bytes())
    assert h.f.native_calls == h.f.accounting_appends == h.f.ai_sends == h.f.source_moves == 0
    for path in PATHS: assert h.client.post(PREFIX+path, base_url=ORIGIN).status_code == 405


@pytest.mark.parametrize('attack', ['unauthenticated', 'expired', 'owner', 'origin', 'cross_site', 'session'])
def test_persistent_view_reuses_host_auth_owner_ttl_and_origin(monkeypatch, tmp_path, attack):
    h, r, doc, _, _, view, _ = rig(monkeypatch, tmp_path)
    if attack != 'unauthenticated': assert h.login().status_code == 303
    if attack == 'expired': h.now += 600
    if attack == 'owner': view.owner = 'f'*64
    if attack == 'session': h.client.set_cookie(h.web.COOKIE, 'invalid', domain=DOMAIN)
    headers = {'Origin':'https://foreign.test'} if attack == 'origin' else {'Sec-Fetch-Site':'cross-site'} if attack == 'cross_site' else {}
    before = doc.payload, doc.writes
    for path in PATHS: assert h.client.get(PREFIX+path, base_url=ORIGIN, headers=headers).status_code in {403, 409, 410}
    assert before == (doc.payload, doc.writes)


def test_other_instance_update_stops_saved_snapshot_display_and_keeps_old_inputs(monkeypatch, tmp_path):
    h, r, doc, key, batch, view, _ = rig(monkeypatch, tmp_path)
    assert h.login().status_code == 303
    other = repository(doc, key, batch); _, tag = other.read_versioned()
    other.save(edited(batch), tag, str(uuid.uuid4()))
    before = doc.payload, doc.writes, deepcopy(h.db.data)
    for path in PATHS: assert h.get(PREFIX+path).status_code == 409
    assert before == (doc.payload, doc.writes, h.db.data)
    assert r._state()[0]['history'][0]['batch']['snapshot_digest'] == batch.snapshot_digest


@pytest.mark.parametrize('attack', ['observation', 'item_snapshot', 'item_marker', 'purpose'])
def test_pinned_saved_evidence_mismatch_fails_closed(monkeypatch, tmp_path, attack):
    h, r, doc, _, _, view, _ = rig(monkeypatch, tmp_path)
    assert h.login().status_code == 303
    bundle = json.loads(view.path.read_text()); m = bundle['mappings']['SYN-G101']
    if attack == 'observation': m['source_observation']['drive_revision_id'] = 'wrong'
    elif attack == 'item_snapshot': m['item_draft']['native_snapshot']['items'][0]['amount'] += 1
    elif attack == 'item_marker':
        m['item_draft']['entries'][0]['marker'] = 'item:wrong'
        m['item_draft_digest'] = digest(m['item_draft'])
    else: m['source_observation']['purpose'] = 'STRUCTURE_REVIEW'
    view.path.write_text(json.dumps(bundle)); view.bundle_digest = digest(bundle)
    for path in PATHS: assert h.get(PREFIX+path).status_code == 409
    assert doc.writes == 0


def test_read_write_race_never_displays_mixed_snapshot(monkeypatch, tmp_path):
    h, r, doc, key, batch, view, _ = rig(monkeypatch, tmp_path)
    assert h.login().status_code == 303
    original = r.read_versioned; calls = 0
    def race():
        nonlocal calls
        value = original(); calls += 1
        if calls == 1:
            repository(doc, key, batch).save(edited(batch), value[1], str(uuid.uuid4()))
        return value
    monkeypatch.setattr(r, 'read_versioned', race)
    assert h.get(PREFIX).status_code == 409
    assert doc.writes == 1
