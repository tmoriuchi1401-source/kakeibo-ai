from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import load, encoded
from app.unified_receipt_review import digest
from sheet_handoff_fixtures import Rig


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_fresh_sheet_import_explicit_confirmation_and_saved_result_stop_at_finance(kind, monkeypatch, tmp_path):
    r = Rig(kind, monkeypatch, tmp_path)
    r.set('memo', '合成Sheetsの途中入力を引き継ぐ')
    proof = r.proof()
    assert proof['review']['fields']['memo'] == '合成Sheetsの途中入力を引き継ぐ'
    assert r.journal.read_versioned()[0]['revision'] == 1  # prepare did not write
    assert r.send(proof)['status'] == 'DRAFT_IMPORTED'
    current = r.rebuild()
    assert current.inspect(proof['request_id'], r.gate)['reviewed_input'] == proof['review']
    assert r.c.readers.draft.read_versioned()[0] != b''
    confirmation = current.prepare(r.cid, 'confirm_handoff', r.gate)
    with pytest.raises(StateError, match='explicit_confirmation'):
        r.send(confirmation, False, current)
    result = r.send(confirmation, True, current)
    assert result['status'] == 'FINANCIAL_BOUNDARY_STOPPED'
    assert result['reviewed_input'] == proof['review']
    assert result['native_authority'] is False and result['accounting_permission'] is False
    assert result['writer_dispatches'] == 0
    fresh = r.rebuild(); calls = deepcopy(r.calls); saved = r.journal.read_versioned()
    assert fresh.inspect(confirmation['request_id'], r.gate) == result
    assert r.calls == calls and r.journal.read_versioned() == saved  # GET result has no capture/CAS
    r.sources[r.c.source_id]['source_version'] = 'replacement-after-result'
    assert fresh.inspect(confirmation['request_id'], r.gate) == result
    with pytest.raises(StateError): r.send(confirmation, True, fresh)
    with pytest.raises(StateError, match='duplicate_confirmation'):
        fresh.prepare(r.cid, 'confirm_handoff', r.gate)
    assert r.financial() == r.before_financial


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('attack', ['memo', 'date', 'amount', 'id', 'kind', 'formula', 'sort', 'insert',
    'delete', 'duplicate', 'note', 'protection', 'revision', 'hash', 'source_id', 'native_version', 'master', 'draft'])
def test_stale_or_foreign_snapshot_never_adopted(kind, attack, monkeypatch, tmp_path):
    r = Rig(kind, monkeypatch, tmp_path); proof = r.proof(); before = r.journal.read_versioned()
    cells = r.cells(); rows = r.raw['sheets'][0]['data'][0]['rowData']
    if attack in {'memo', 'date', 'amount'}: r.set(attack, {'memo':'other edit','date':'2027-01-01','amount':999}[attack])
    elif attack == 'id': cells[0] = {'userEnteredValue': {'stringValue': 'SYN-G102'}}
    elif attack == 'kind': cells[1] = {'userEnteredValue': {'stringValue': 'PDF'}}
    elif attack == 'formula': cells[12]['userEnteredValue'] = {'formulaValue': '=1'}
    elif attack == 'sort': rows[1:] = rows[1:][::-1]
    elif attack == 'insert': rows.insert(2, {'values': []})
    elif attack == 'delete': rows.pop(2)
    elif attack == 'duplicate': rows[2] = deepcopy(rows[1])
    elif attack == 'note': cells[12]['note'] = 'foreign identity is not authority'
    elif attack == 'protection': r.raw['sheets'][0]['protectedRanges'] = []
    elif attack in {'revision', 'hash', 'source_id'}:
        s = r.sources[r.c.source_id]
        s[{'revision':'source_version','hash':'sha256','source_id':'source_id'}[attack]] = 'changed'
    else:
        port = {'native_version':r.c.readers.source,'master':r.c.readers.categories,'draft':r.c.readers.draft}[attack]
        v = load(port.read_versioned()[0])
        if attack == 'native_version': v['source']['native_source_version'] = 'different-native-version'
        elif attack == 'master': v['revision'] = 'new-master'
        else: v['revision'] += 1; v['value']['fields']['memo'] = 'competing draft'; v['input_digest'] = digest(v['value'])
        port.replace_versioned(*port.read_versioned(), encoded(v))
    preserved = deepcopy(r.raw)
    with pytest.raises((StateError, AssertionError)): r.send(proof)
    assert r.journal.read_versioned() == before
    assert r.financial()[:2] == r.before_financial[:2]
    # Preserve the untrusted Sheet itself; no regeneration/repair performed.
    assert r.raw == preserved


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_equal_read_gate_stops_observed_concurrency(kind, monkeypatch, tmp_path):
    r = Rig(kind, monkeypatch, tmp_path); count = 0
    def racing():
        nonlocal count
        count += 1
        if count == 2: r.set('memo', 'racing edit')
        return deepcopy(r.raw)
    r.reader.read_raw = racing
    with pytest.raises(StateError, match='concurrent'): r.proof()
    assert r.journal.read_versioned()[0]['revision'] == 1
    assert r.financial() == r.before_financial


@pytest.mark.parametrize('cid', ['SYN-G103', 'SYN-H301', 'SYN-P201', 'SYN-P202', 'SYN-G102', 'SYN-M102'])
def test_unmapped_pdf_and_held_cases_cannot_enter(cid, monkeypatch, tmp_path):
    r = Rig('normal', monkeypatch, tmp_path)
    with pytest.raises(StateError, match='held_or_unmapped'):
        r.adapter.prepare(cid, 'import_draft', r.gate)
    assert r.journal.read_versioned()[0]['revision'] == 1


def test_receipt_category_never_broadcast_to_multiple_items(monkeypatch, tmp_path):
    r = Rig('normal', monkeypatch, tmp_path)
    old = r.ui._draft(r.cid)[0]['value']['items']
    r.imported()
    assert r.ui._draft(r.cid)[0]['value']['items'] == old
    r.set('category', '食費｜食品')
    with pytest.raises(StateError, match='receipt_category_requires_item_review'): r.proof()
    assert r.ui._draft(r.cid)[0]['value']['items'] == old


def test_partial_manual_input_can_save_but_cannot_confirm(monkeypatch, tmp_path):
    r = Rig('medical', monkeypatch, tmp_path); r.set('amount', ''); r.set('merchant', '手入力途中')
    p = r.imported()
    assert r.adapter.inspect(p['request_id'], r.gate)['reviewed_input']['fields']['amount'] == ''
    with pytest.raises(StateError): r.proof('confirm_handoff')
    r.set('amount', r.h.f.fields['amount'])
    r.imported()
    assert r.send(r.proof('confirm_handoff'), True)['status'] == 'FINANCIAL_BOUNDARY_STOPPED'
    assert r.financial() == r.before_financial


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('count', [2, 16])
@pytest.mark.parametrize('action', ['import_draft', 'confirm_handoff'])
def test_shared_journal_cas_serializes_independent_instances(kind, count, action, monkeypatch, tmp_path):
    r = Rig(kind, monkeypatch, tmp_path)
    if action == 'confirm_handoff': r.imported()
    p = r.proof(action)
    draft_writes = r.c.readers.draft.writes
    instances = [r.rebuild() for _ in range(count)]
    def send(adapter):
        try: return r.send(p, action == 'confirm_handoff', adapter)
        except StateError: return None
    with ThreadPoolExecutor(count) as pool: results = list(pool.map(send, instances))
    assert sum(x is not None for x in results) == 1
    assert r.c.readers.draft.writes - draft_writes == (1 if action == 'import_draft' else 0)
    state, ledger, _ = r.adapter._state()
    assert len(ledger['requests']) == (2 if action == 'confirm_handoff' else 1)
    assert r.financial() == r.before_financial


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('point', ['intent_before', 'intent_after', 'draft_before', 'draft_after', 'result_before', 'result_after'])
def test_unknown_outcomes_lookup_only_without_retries(kind, point, monkeypatch, tmp_path):
    r = Rig(kind, monkeypatch, tmp_path); p = r.proof(); calls = []
    journal_replace = r.io.replace_versioned; draft_replace = r.c.readers.draft.replace_versioned
    def journal_fault(*args):
        calls.append('journal')
        trigger = len([c for c in calls if c == 'journal']) == (2 if point.startswith('result') else 1)
        if trigger and point in {'intent_before', 'result_before'}: raise OSError('synthetic lost request')
        result = journal_replace(*args)
        if trigger and point in {'intent_after', 'result_after'}: raise OSError('synthetic lost response')
        return result
    def draft_fault(*args):
        calls.append('draft')
        if point == 'draft_before': raise OSError('synthetic lost request')
        result = draft_replace(*args)
        if point == 'draft_after': raise OSError('synthetic lost response')
        return result
    monkeypatch.setattr(r.io, 'replace_versioned', journal_fault)
    monkeypatch.setattr(r.c.readers.draft, 'replace_versioned', draft_fault)
    with pytest.raises((OSError, StateError)): r.send(p)
    frozen = deepcopy(calls)
    with pytest.raises(StateError): r.send(p)
    with pytest.raises(StateError): r.proof()
    assert calls == frozen
    other = r.rebuild()
    result = other.inspect(p['request_id'], r.gate)
    assert result['status'] == ('DRAFT_IMPORTED' if point == 'result_after' else 'UNKNOWN')
    assert calls == frozen and r.financial() == r.before_financial
    if point not in {'intent_before'}:
        with pytest.raises(StateError): r.send(p, adapter=other)
        assert calls == frozen


@pytest.mark.parametrize('attack', ['expired', 'foreign_actor', 'missing_confirm', 'tampered', 'new_sheet_after_import', 'case_id', 'snapshot'])
def test_owner_session_and_explicit_confirmation_are_independent_of_sheet_status(attack, monkeypatch, tmp_path):
    r = Rig('medical', monkeypatch, tmp_path); r.imported(); p = r.proof('confirm_handoff')
    before = r.journal.read_versioned()
    if attack == 'expired': r.adapter.confirmation.clock = lambda: r.ui.clock()+601
    elif attack == 'foreign_actor':
        current = r.gate.current
        r.gate.current = lambda: (*current()[:3], SimpleNamespace(issuer='https://accounts.google.com',subject='other',actor_id='0'*64))
    elif attack == 'tampered': p['proof'] = 'invalid'
    elif attack == 'new_sheet_after_import': r.set('memo', 'changed after review')
    elif attack == 'case_id': p['case_id'] = 'SYN-G101'
    elif attack == 'snapshot': p['sheet_snapshot'] = '0'*64
    with pytest.raises(StateError): r.send(p, attack != 'missing_confirm')
    assert r.journal.read_versioned() == before and r.financial() == r.before_financial


def test_sheet_status_does_not_review_items_or_create_financial_permission(monkeypatch, tmp_path):
    r = Rig('normal', monkeypatch, tmp_path)
    d = r.ui._draft(r.cid)[0]
    for item in d['value']['items']: item['state'] = 'unentered'
    d['input_digest'] = digest(d['value'])
    r.c.readers.draft.replace_versioned(*r.c.readers.draft.read_versioned(), encoded(d))
    r.imported()
    assert all(e['state'] == 'unentered' for e in r.ui._draft(r.cid)[0]['value']['items'])
    with pytest.raises(StateError): r.proof('confirm_handoff')
    assert r.financial() == r.before_financial


@pytest.mark.parametrize('attack', ['bytes', 'numeric_version', 'page_count', 'source_kind'])
def test_independent_original_bytes_and_structure_cannot_be_substituted(attack, monkeypatch, tmp_path):
    r = Rig('medical', monkeypatch, tmp_path); p = r.proof(); before = r.journal.read_versioned()
    if attack == 'bytes': r.c.readers.source_bytes.raw = b'replacement-synthetic-bytes'
    else:
        v = load(r.c.readers.source.read_versioned()[0])
        v['source'][{'numeric_version':'drive_numeric_version','page_count':'page_count','source_kind':'source_kind'}[attack]] = {
            'numeric_version':'2', 'page_count':2, 'source_kind':'pdf'}[attack]
        r.c.readers.source.replace_versioned(*r.c.readers.source.read_versioned(), encoded(v))
    with pytest.raises((StateError, AssertionError)): r.send(p)
    assert r.journal.read_versioned() == before and r.financial() == r.before_financial


def test_handoff_proof_is_not_an_existing_financial_post_proof(monkeypatch, tmp_path):
    r = Rig('medical', monkeypatch, tmp_path); p = r.proof()
    before = r.journal.read_versioned()
    with pytest.raises((StateError, KeyError)):
        r.ui.post(dict(proof=p['proof'], value=p['review'], confirmed=True), r.gate)
    assert r.journal.read_versioned() == before and r.financial() == r.before_financial


def test_latest_import_is_ordered_by_cas_revision_and_old_results_are_retained(monkeypatch, tmp_path):
    r = Rig('medical', monkeypatch, tmp_path); receipts = []
    for n in range(3):
        r.set('memo', '合成の再入力 '+str(n))
        p = r.imported(); receipts.append((p, r.adapter.inspect(p['request_id'], r.gate)))
        r.adapter = r.rebuild()
    p = r.proof('confirm_handoff')
    assert p['review']['fields']['memo'] == '合成の再入力 2'
    assert r.send(p, True)['status'] == 'FINANCIAL_BOUNDARY_STOPPED'
    for old, result in receipts: assert r.adapter.inspect(old['request_id'], r.gate) == result
    assert r.financial() == r.before_financial


@pytest.mark.parametrize('attack', ['default_off', 'foreign_owner', 'foreign_contract', 'live_journal'])
def test_composition_cannot_use_a_foreign_or_live_domain(attack, monkeypatch, tmp_path):
    from staging_shadow.iphone_sheet_handoff import SheetDraftHandoff
    r = Rig('medical', monkeypatch, tmp_path)
    if attack == 'foreign_owner': r.journal.scope['owner'] = '0'*64
    elif attack == 'foreign_contract': r.journal.binding = '0'*64
    elif attack == 'live_journal': r.io.synthetic_memory_only = False
    with pytest.raises(StateError):
        SheetDraftHandoff(r.reader, r.journal, r.ui, enabled=attack!='default_off', isolated_synthetic=True)


def test_ci_is_exactly_isolated():
    from pathlib import Path
    import yaml
    path = Path(__file__).resolve().parents[1]/'.github/workflows/iphone-sheet-handoff-checks.yml'
    w = yaml.load(path.read_text('utf8'), Loader=yaml.BaseLoader)
    assert w == {
        'name': 'iPhone one-tab draft handoff boundary',
        'on': {'pull_request': {'branches': ['codex/iphone-sheets-a-completion']}},
        'permissions': {'contents': 'read'}, 'env': {'UNIFIED_RECEIPT_WRITE_ENABLED': 'false'},
        'concurrency': {'group': 'kakeibo-sheet-handoff-${{ github.ref }}', 'cancel-in-progress': 'true'},
        'jobs': {'synthetic-boundary': {'runs-on': 'ubuntu-24.04', 'timeout-minutes': '20', 'steps': [
            {'uses': 'actions/checkout@v4', 'with': {'ref': '${{ github.event.pull_request.head.sha }}',
                'fetch-depth': '0', 'persist-credentials': 'false'}},
            {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.12'}},
            {'name': 'Install existing synthetic dependencies', 'run': 'pip install -r requirements-test.txt'},
            {'name': 'New handoff and authenticated host evidence', 'run':
                'python -m pytest -q tests/test_iphone_sheet_handoff.py tests/test_iphone_sheet_handoff_host.py --junitxml=.private/handoff-new.xml'},
            {'name': 'Existing reader journal source manual-input and authentication regressions', 'run':
                'python -m pytest -q tests/test_iphone_sheet_reader.py tests/test_iphone_draft_journal.py tests/test_iphone_persistent_drafts.py tests/test_iphone_source_observation.py tests/test_iphone_item_drafts.py tests/test_iphone_review_drafts.py tests/test_iphone_confirmation.py tests/test_production_workflows.py --junitxml=.private/handoff-regression.xml'},
            {'uses': 'actions/upload-artifact@v4', 'if': 'always()', 'with': {
                'name': 'one-tab-handoff-synthetic-junit', 'path': '.private/handoff-*.xml', 'include-hidden-files': 'true'}},
            {'name': 'Compile and whitespace', 'run':
                'python -m compileall -q app/iphone_sheet_handoff_host.py staging_shadow/iphone_sheet_handoff.py tests/sheet_handoff_fixtures.py tests/sheet_handoff_host_fixtures.py tests/test_iphone_sheet_handoff.py tests/test_iphone_sheet_handoff_host.py\ngit diff --check\n'},
        ]}},
    }
