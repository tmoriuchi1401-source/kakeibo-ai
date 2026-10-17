"""Actual pinned Google/session host; synthetic saved files and no network."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from app.drive_run_state import StateError
from app.iphone_draft_journal import LocalJournal
from app.iphone_readonly_host import PREFIX, BLUEPRINT, make_isolated_readonly_application
from app.iphone_sheet_reader import CASE_IDS, validate_snapshot
from app.unified_receipt_review import digest
from app.unified_receipt_shadow import ShadowConfig
from iphone_readonly_fixtures import ReadonlyRig, saved_bundle
from unified_shadow_fixtures import Document, ORIGIN, DOMAIN, inputs

PATHS = [''] + ['/cases/'+c for c in sorted(CASE_IDS)]


@pytest.fixture(autouse=True)
def no_clients(monkeypatch):
    from google.cloud import firestore
    from googleapiclient import discovery
    def denied(*a, **kw): pytest.fail('live_client_forbidden')
    monkeypatch.setattr(firestore, 'Client', denied)
    monkeypatch.setattr(discovery, 'build', denied)


def test_eight_case_list_and_exact_saved_details(monkeypatch, tmp_path):
    r = ReadonlyRig(monkeypatch, tmp_path); h = r.h
    assert h.login().status_code == 303
    listing = h.get(PREFIX); html = listing.get_data(as_text=True)
    assert listing.status_code == 200 and html.count('data-case-id=') == 8
    cards = {c['case_id']:c for c in r.repository.snapshots()}
    for alias, m in r.bundle['mappings'].items():
        response = h.get(PREFIX+'/cases/'+alias); body = response.get_data(as_text=True)
        d = cards[alias]['display']
        assert response.status_code == 200 and alias in body and m['input_digest'] in body
        assert d['native_case_id'] == m['native_case_id'] and not d['authorized']
        assert cards[alias]['operation_targets'] == []
        if alias != 'SYN-H301':
            assert m['case']['identity']['source_file_id'] in body
            assert d['original_url'].endswith('/'+m['case']['identity']['source_file_id']+'/view')
            assert d['native_identity']['page_identities'] == m['canonical']['identity']['page_identities']
        assert '確定・記帳操作はありません' in body
    assert cards['SYN-G103']['display']['held'] is True
    assert cards['SYN-H301']['disposition'] == 'LEGACY_UNMAPPED_HOLD'
    assert cards['SYN-H301']['display']['original_url'] == ''
    assert '完全手入力' in h.get(PREFIX+'/cases/SYN-M101').get_data(as_text=True)
    assert cards['SYN-P201']['purpose'] == 'STRUCTURE_REVIEW'
    assert not cards['SYN-P201']['display']['accounting_allowed']
    for alias in ['SYN-G101','SYN-G102','SYN-G103']:
        assert cards[alias]['display']['items'] == r.bundle['mappings'][alias]['item_snapshot']['items']
        assert len({i['category'] for i in cards[alias]['display']['items']}) == 2


@pytest.mark.parametrize('path', PATHS)
def test_unauthenticated_every_get_rejected_before_repository_read(monkeypatch, tmp_path, path):
    r = ReadonlyRig(monkeypatch, tmp_path)
    monkeypatch.setattr(r.repository, 'snapshots', lambda: pytest.fail('read_before_auth'))
    assert r.h.get(PREFIX+path).status_code == 403


@pytest.mark.parametrize('attack', ['other_google_user','bad_signature','audience','nonce','expired_request',
    'expired_login','logout','ticket','owner','case_owner','origin','null_origin','cross_site'])
def test_existing_authentication_owner_session_ttl_origin_rejected(monkeypatch, tmp_path, attack):
    r = ReadonlyRig(monkeypatch, tmp_path); h = r.h
    if attack in {'other_google_user','bad_signature','audience','nonce'}:
        if attack == 'other_google_user': h.claim_changes = {'sub':'999'}
        elif attack == 'bad_signature': h.public = 'not a cert'
        elif attack == 'audience': h.claim_changes = {'aud':'other.apps.googleusercontent.com'}
        else: h.claim_changes = {'nonce':'wrong'}
        assert h.login().status_code == 403
    else:
        assert h.login().status_code == 303
        if attack == 'expired_request': h.now += 600
        elif attack == 'expired_login':
            # Move clock past the actual persisted shared-login expiry.
            login = h.rt.logins.current(h.client.get_cookie(h.web.LOGIN_COOKIE, domain=DOMAIN).value)
            h.now = max(h.now+600, login['expires'])
        elif attack == 'logout': assert h.post_form('/logout', inputs(h.get('/logout'))).status_code == 200
        elif attack == 'ticket': h.client.set_cookie(h.web.COOKIE, 'tampered', domain=DOMAIN)
        elif attack == 'owner': r.repository.owner = 'a'*64
        elif attack == 'case_owner':
            r.bundle['mappings']['SYN-G101']['owner'] = 'b'*64; r.repin_for_negative_semantic_test()
    headers = {'Origin':'https://other.test'} if attack == 'origin' else (
        {'Origin':'null'} if attack == 'null_origin' else {'Sec-Fetch-Site':'cross-site'} if attack == 'cross_site' else {})
    before = deepcopy(h.db.data), r.journal.path.read_bytes(), r.bundle_path.read_bytes()
    for path in PATHS:
        response = h.client.get(PREFIX+path, base_url=ORIGIN, headers=headers)
        assert response.status_code in {403,409,410}
        assert 'SYN-G101' not in response.get_data(as_text=True)
    assert before == (h.db.data, r.journal.path.read_bytes(), r.bundle_path.read_bytes())


@pytest.mark.parametrize('attack', ['hash','source_id','revision','native_version','page','unit','kind','purpose',
    'native_digest','native_case','input_digest','input_edit','sort','insert','legacy','hold','item','item_identity','mapping_duplicate'])
def test_saved_identity_snapshot_layout_and_purpose_fail_closed(monkeypatch, tmp_path, attack):
    r = ReadonlyRig(monkeypatch, tmp_path); h = r.h; assert h.login().status_code == 303
    m = r.bundle['mappings']['SYN-G101']; rows = r.bundle['raw_snapshot']['sheets'][0]['data'][0]['rowData']
    if attack in {'hash','source_id','revision'}:
        key = {'hash':'sha256','source_id':'source_id','revision':'source_version'}[attack]
        r.bundle['sources'][next(iter(r.bundle['sources']))][key] = 'changed'
    elif attack == 'native_version': m['version_binding']['native_source_version'] = 'changed'
    elif attack == 'page': m['canonical']['identity']['page_identities'] = ['a'*64]
    elif attack == 'unit': m['canonical']['identity']['receipt_unit_id'] = 'foreign'
    elif attack == 'kind': m['canonical']['kind'] = 'medical'
    elif attack == 'purpose': m['purpose'] = 'STRUCTURE_REVIEW'
    elif attack == 'native_digest': m['native_digest'] = 'f'*64
    elif attack == 'native_case': m['case']['fields'][0][2] = 'changed'
    elif attack == 'input_digest': m['input_digest'] = 'f'*64
    elif attack == 'input_edit': rows[1]['values'][12]['userEnteredValue']['stringValue'] = 'stale memo'
    elif attack == 'sort': rows[1:] = list(reversed(rows[1:]))
    elif attack == 'insert': rows.insert(2, {'values':[]})
    elif attack == 'legacy': r.bundle['mappings']['SYN-H301']['case']['identity'] = {'source_file_id':'invented'}
    elif attack == 'hold': r.bundle['mappings']['SYN-G103']['canonical']['hold'] = ''
    elif attack == 'item': m['item_snapshot']['items'][0]['category'] = 'broadcast'
    elif attack == 'item_identity':
        m['item_snapshot']['identity']['receipt_unit_id'] = 'foreign'; m['item_digest'] = digest(m['item_snapshot'])
    else: m['native_case_id'] = r.bundle['mappings']['SYN-G102']['native_case_id']
    r.repin_for_negative_semantic_test()
    before = r.journal.path.read_bytes(), deepcopy(h.db.data)
    for path in ['', '/cases/SYN-G101', '/cases/SYN-H301']:
        response = h.get(PREFIX+path)
        assert response.status_code == 409
        assert 'data-case-id=' not in response.get_data(as_text=True)
    assert before == (r.journal.path.read_bytes(), h.db.data)


def test_later_journal_draft_and_observed_concurrency_are_not_adopted(monkeypatch, tmp_path):
    r = ReadonlyRig(monkeypatch, tmp_path); assert r.h.login().status_code == 303
    raw = deepcopy(r.bundle['raw_snapshot'])
    raw['sheets'][0]['data'][0]['rowData'][1]['values'][12]['userEnteredValue']['stringValue'] = 'later draft'
    later = validate_snapshot(r.bundle['contract'], raw, r.bundle['sources'])
    r.journal.save(later, r.journal.read_versioned()[1], str(uuid.uuid4()))
    assert r.h.get(PREFIX).status_code == 409
    # Also detect a commit observed between the two local journal reads.
    r = ReadonlyRig(monkeypatch, tmp_path/'second')
    assert r.h.login().status_code == 303
    original = r.repository.journal.read_versioned; calls = [0]
    def racing():
        state, tag = original(); calls[0] += 1
        return state, tag if calls[0] == 1 else '"different"'
    monkeypatch.setattr(r.repository.journal, 'read_versioned', racing)
    assert r.h.get(PREFIX).status_code == 409


@pytest.mark.parametrize('path', PATHS + ['/cases/foreign-case'])
def test_get_repetition_all_capabilities_zero_and_private_headers(monkeypatch, tmp_path, path):
    r = ReadonlyRig(monkeypatch, tmp_path); h = r.h; assert h.login().status_code == 303
    before = (deepcopy(h.db.data), h.db.writes, h.f.io.raw, r.journal.path.read_bytes(), r.bundle_path.read_bytes(),
              sorted(p.name for p in tmp_path.iterdir()))
    def forbidden(*a, **kw): pytest.fail('GET_side_effect_or_observation')
    with monkeypatch.context() as spy:
        for obj, method in [(Document,'create'),(Document,'update'), (LocalJournal,'save'),
                (LocalJournal,'initialize'),(LocalJournal,'_replace'), (h.f.port,'observe'),
                (h.f.port,'execute'),(h.f.grouping,'display'),(h.f.grouping,'review'),
                (h.f.io,'replace_versioned')]:
            spy.setattr(obj, method, forbidden)
        from app.iphone_sheet_reader import RawReader
        spy.setattr(RawReader, 'read', forbidden)
        spy.setattr(h.rt.logins, 'touch', forbidden, raising=False)
        for _ in range(3):
            response = h.get(PREFIX+path)
            assert response.status_code == (409 if path.endswith('foreign-case') else 200)
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.headers['Referrer-Policy'] == 'strict-origin'
            assert "default-src 'none'" in response.headers['Content-Security-Policy']
            assert 'Set-Cookie' not in response.headers
    assert before == (h.db.data, h.db.writes, h.f.io.raw, r.journal.path.read_bytes(), r.bundle_path.read_bytes(),
                      sorted(p.name for p in tmp_path.iterdir()))
    assert h.f.native_calls == h.f.accounting_appends == h.f.ai_sends == h.f.source_moves == h.f.io.writes == 0


def test_default_off_isolation_and_no_post_registration(monkeypatch, tmp_path):
    r = ReadonlyRig(monkeypatch, tmp_path, enabled=False); h = r.h
    assert h.get(PREFIX).status_code == 404 and BLUEPRINT not in h.app.blueprints
    app = r.application(); rules = [rule for rule in app.url_map.iter_rules() if rule.rule.startswith(PREFIX)]
    assert len(rules) == 2 and all(rule.methods == {'GET','HEAD','OPTIONS'} for rule in rules)
    assert not any('/isolated-shadow/' in rule.rule for rule in app.url_map.iter_rules())
    for path in PATHS + ['/prepare','/submit','/ACCOUNTING_POST/submit']:
        assert app.test_client().post(PREFIX+path, json={}, base_url=ORIGIN).status_code in {404,405}
    for environment, memory in [('production',True),('isolated_synthetic',False)]:
        h.db.shadow_memory_only = memory
        with pytest.raises(StateError, match='isolation_required'):
            make_isolated_readonly_application(h.web.make_application, h.configured, r.repository,
                config=ShadowConfig(True,environment), cookies={'login':h.web.LOGIN_COOKIE,'ticket':h.web.COOKIE})


def test_host_and_process_restart_keep_exact_saved_inputs_without_writes(monkeypatch, tmp_path):
    r = ReadonlyRig(monkeypatch, tmp_path); h = r.h; assert h.login().status_code == 303
    original = {path:h.get(PREFIX+path).data for path in PATHS}
    before = r.journal.path.read_bytes(), r.bundle_path.read_bytes(), deepcopy(h.db.data)
    r.repository = r.reopen(); restarted = r.application().test_client()
    for cookie_name in [h.web.LOGIN_COOKIE, h.web.COOKIE]:
        restarted.set_cookie(cookie_name, h.client.get_cookie(cookie_name, domain=DOMAIN).value, domain=DOMAIN)
    for path in PATHS: assert restarted.get(PREFIX+path, base_url=ORIGIN).data == original[path]
    # Fresh process reconstructs the same private repository, with no auth
    # credentials or cloud config inherited. Session restart is tested above.
    code = '''
import json,sys
from app.iphone_draft_journal import LocalJournal
from app.iphone_readonly_host import SavedDraftRepository
from app.unified_receipt_review import digest
j=LocalJournal(sys.argv[1],sys.argv[3])
r=SavedDraftRepository(j,sys.argv[2],sys.argv[4],sys.argv[5])
print(digest(r.snapshots()))
'''
    env = {k:v for k,v in os.environ.items() if k in {'SystemRoot','WINDIR','TEMP','TMP'}}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONUTF8='1')
    child = subprocess.run([sys.executable,'-c',code,str(r.journal.path),str(r.bundle_path),
        r.batch.contract_digest,digest(r.bundle),r.repository.owner], capture_output=True, text=True, env=env, timeout=20)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == digest(r.repository.snapshots())
    assert before == (r.journal.path.read_bytes(), r.bundle_path.read_bytes(), h.db.data)


def test_bundle_tampering_and_missing_files_never_repaired(monkeypatch, tmp_path):
    r = ReadonlyRig(monkeypatch, tmp_path); h = r.h; assert h.login().status_code == 303
    r.bundle_path.write_text('{}', encoding='utf-8')
    assert h.get(PREFIX).status_code == 409
    r.bundle_path.unlink()
    assert h.get(PREFIX).status_code == 409 and not r.bundle_path.exists()


def test_saved_input_is_escaped_and_never_executed(monkeypatch, tmp_path):
    r = ReadonlyRig(monkeypatch, tmp_path)
    cells = r.bundle['raw_snapshot']['sheets'][0]['data'][0]['rowData'][1]['values']
    cells[12]['userEnteredValue']['stringValue'] = '<script>unsafe()</script>'
    batch = validate_snapshot(r.bundle['contract'], r.bundle['raw_snapshot'], r.bundle['sources'])
    r.bundle['mappings']['SYN-G101']['input_digest'] = digest(cells)
    r.journal.save(batch, r.journal.read_versioned()[1], str(uuid.uuid4()))
    r.repin_for_negative_semantic_test()
    assert r.h.login().status_code == 303
    response = r.h.get(PREFIX+'/cases/SYN-G101')
    assert response.status_code == 200
    assert '<script>' not in response.get_data(as_text=True)
    assert '&lt;script&gt;unsafe()&lt;/script&gt;' in response.get_data(as_text=True)


@pytest.mark.parametrize('damage', ['missing','json','receipt','uninitialized'])
def test_journal_missing_or_corrupt_get_never_saves_or_repairs(monkeypatch, tmp_path, damage):
    r = ReadonlyRig(monkeypatch, tmp_path); assert r.h.login().status_code == 303
    if damage == 'missing': r.journal.path.unlink()
    elif damage == 'json': r.journal.path.write_text('{broken', encoding='utf-8')
    else:
        state, _ = r.journal.read_versioned()
        if damage == 'receipt': next(iter(state['requests'].values()))['batch_digest'] = 'a'*64
        else: state.update(batch=None, requests={}, revision=0)
        r.journal.path.write_text(json.dumps(state), encoding='utf-8')
    before = r.journal.path.read_bytes() if r.journal.path.exists() else None
    assert r.h.get(PREFIX).status_code == 409
    assert (r.journal.path.read_bytes() if r.journal.path.exists() else None) == before


def test_runtime_cannot_change_to_live_after_registration(monkeypatch, tmp_path):
    r = ReadonlyRig(monkeypatch, tmp_path); assert r.h.login().status_code == 303
    r.h.db.shadow_memory_only = False
    assert r.h.get(PREFIX).status_code == 409


@pytest.mark.parametrize('kind', ['medical','general'])
def test_existing_native_validated_snapshot_saved_then_authenticated_get(monkeypatch, tmp_path, kind):
    """Actual existing medical/item validators run OFFLINE, never inside GET."""
    from app.iphone_native_dry_run import preview_bound_draft
    from app.iphone_sheet_reader import issue_contract, original_formula
    from test_iphone_native_dry_run import medical_fixture, raw_draft
    from test_iphone_review_preview import general_fixture
    from app.iphone_native_dry_run import bind_case
    from app.unified_receipt_eligibility import current_snapshot
    r = ReadonlyRig(monkeypatch, tmp_path)
    if kind == 'medical':
        draft, ctx, native = medical_fixture(); accepted = None
    else:
        c,p,db,runner,record,accepted,canonical = general_fixture()
        draft, versions = raw_draft(canonical,'一般')
        ctx = dict(canonical=canonical,case=c,native_digest=digest(current_snapshot(c)),
                   record=record,accepted_snapshot=accepted,accepted_digest=digest(accepted),
                   categories=db.categories(),version_binding=versions)
        ctx['sheet_binding'] = bind_case(draft['case_id'],canonical,ctx['native_digest'])
    validation = preview_bound_draft(draft,draft['input_snapshot_digest'],ctx)
    assert validation['authorized'] is False and validation['operation_targets'] == []
    alias = draft['case_id']; case = ctx['case']; case.synthetic = True
    contract = r.bundle['contract']; row_number = contract['cases'][alias]['row_index']
    cells = r.bundle['raw_snapshot']['sheets'][0]['data'][0]['rowData'][row_number]['values']
    for col in [4,5,6,7,8,12,15,16]: cells[col]['userEnteredValue'] = deepcopy(draft['cells'][col]['userEnteredValue'])
    cells[2] = {'userEnteredValue': {'formulaValue':original_formula(alias,draft['identity'],draft['kind'])}}
    old_source = contract['cases'][alias]['identity']['source_id']; r.bundle['sources'].pop(old_source)
    r.bundle['sources'][draft['identity']['source_id']] = dict(case_id=alias,**draft['identity'])
    m = r.bundle['mappings'][alias]
    m.update(case=asdict(case),native_case_id=case.case_id,native_digest=ctx['native_digest'],
             canonical=ctx['canonical'],version_binding=asdict(ctx['version_binding']),
             sheet_binding=asdict(ctx['sheet_binding']),input_digest=digest(cells),
             item_snapshot=accepted,item_digest=digest(accepted) if accepted else None)
    r.bundle['contract'] = issue_contract(r.bundle['raw_snapshot'],list(r.bundle['sources'].values()))
    batch = validate_snapshot(r.bundle['contract'],r.bundle['raw_snapshot'],r.bundle['sources'])
    r.journal = LocalJournal(tmp_path/'native-drafts.json',batch.contract_digest)
    _, tag = r.journal.initialize(); r.journal.save(batch,tag,str(uuid.uuid4()))
    r.repository.journal = r.journal; r.repin_for_negative_semantic_test()
    assert r.h.login().status_code == 303
    before = r.journal.path.read_bytes(), deepcopy(r.h.db.data)
    def forbidden(*a,**kw): pytest.fail('GET_revalidated_or_executed_native')
    import app.iphone_native_dry_run as adapter
    monkeypatch.setattr(adapter,'preview_bound_draft',forbidden)
    assert r.h.get(PREFIX+'/cases/'+alias).status_code == 200
    card = next(c for c in r.repository.snapshots() if c['case_id'] == alias)
    assert card['display']['native_case_id'] == case.case_id
    if accepted: assert card['display']['items'] == accepted['items']
    assert before == (r.journal.path.read_bytes(),r.h.db.data)


def test_two_instance_local_disks_cannot_supply_a_shared_cas(tmp_path):
    # Negative deployment evidence: both independent local CAS stores accept
    # the same starting version. This is NOT a distributed claim guarantee.
    _, batch = saved_bundle(); stores = []; tags = []
    for name in ['instance-a','instance-b']:
        root = tmp_path/name; root.mkdir()
        journal = LocalJournal(root/'drafts.json',batch.contract_digest)
        stores.append(journal); tags.append(journal.initialize()[1])
    assert tags[0] == tags[1]
    for journal, tag in zip(stores,tags):
        assert journal.save(batch,tag,str(uuid.uuid4()))['status'] == 'draft_saved'
    assert all(j.read_versioned()[0]['revision'] == 1 for j in stores)
    assert stores[0].read_versioned()[0]['requests'] != stores[1].read_versioned()[0]['requests']


def test_get_never_creates_or_acquires_journal_lock(monkeypatch, tmp_path):
    from app.iphone_draft_journal import exclusive
    r = ReadonlyRig(monkeypatch,tmp_path); assert r.h.login().status_code == 303
    with exclusive(r.journal.lock_path):
        assert r.h.get(PREFIX).status_code == 200
    r.journal.lock_path.unlink()
    assert r.h.get(PREFIX).status_code == 200 and not r.journal.lock_path.exists()
