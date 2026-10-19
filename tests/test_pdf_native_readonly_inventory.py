"""2D-1 investigation only: no writer body, claim, grant or financial proof.

Reuse the existing normal PDF rig solely to obtain its deterministic synthetic
candidate/manifest. It seeds two MEMORY registry revisions, no authority or
ledger. Freeze that fixture before every observation; never rebuild its Units.
"""
import ast
from copy import copy, deepcopy
import inspect
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.iphone_accounting_ownership import relation
from app.pdf_intake_authority import AuthorityAdapter, DecisionSealer, review_identity
from app.pdf_intake_registry import RegistryStore
from app.pdf_intake_runner import PageIntake, exact_readback, ledger_source_id
from app.receipt_pipeline import ReceiptPipeline
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from accounting_gate_fixtures import reader_for
from staging_shadow.accounting_native_preflight import verify_registered_pdf_partition
from staging_shadow.accounting_recovery_model import SnapshotDB
from test_pdf_intake_authority import page, RAW
from test_pdf_intake_runner import rig
from test_unified_receipt_write import Memory

ROOT = Path(__file__).resolve().parents[1]


def forbidden(*args, **kwargs):
    raise AssertionError('2D1 side effect forbidden')


@pytest.fixture(autouse=True)
def no_effects(monkeypatch):
    from app.sheets import SheetsDB
    from googleapiclient import discovery
    from google.cloud import firestore
    from app.receipt_plan import posting, unit_posting
    monkeypatch.setattr(ReceiptPipeline, '_materialize_result', forbidden)
    monkeypatch.setattr(DecisionSealer, 'seal', forbidden)
    monkeypatch.setattr(posting, 'approval', forbidden)
    monkeypatch.setattr(unit_posting, 'approval', forbidden)
    monkeypatch.setattr(SheetsDB, '__init__', forbidden)
    monkeypatch.setattr(discovery, 'build', forbidden)
    monkeypatch.setattr(firestore, 'Client', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


@pytest.fixture
def frozen(monkeypatch):
    runner, key, uid, db = rig()
    saved = deepcopy(runner.store.load())
    raw, tag = runner.store.io.read_versioned()
    # Existing candidate UID/index/bbox/segmentation stay exactly as initialized.
    monkeypatch.setattr(runner.store, 'save', forbidden)
    monkeypatch.setattr(runner.store.io, 'replace_versioned', forbidden)
    monkeypatch.setattr(db, 'append_raw', forbidden)
    p = saved['pages'][key]['page']
    source = dict(source_id=p['source']['source_file_id'], sha256=p['source']['source_content_hash'],
        source_kind='pdf', page_count=p['source']['page_count'], native_source_version='1',
        drive_revision_id='synthetic-existing-revision', drive_numeric_version='1')
    manifest = saved['pages'][key]['units'][uid]['candidate']['manifest']
    units = [dict(unit_id=u['receipt_unit_id'], receipt_index=u['receipt_index'], members=[dict(
        page=p['page_number'], identity=p['stable_page_identity'],
        box=[u['bbox'][k] for k in ('left', 'top', 'right', 'bottom')])]) for u in manifest['units']]
    reader = reader_for('pdf_manual', source, RAW, units,
        dict(namespace='pdf_intake', scope='unit', unit_id=uid, receipt_index=units[0]['receipt_index'],
             native_page=p, input_snapshot=digest('readonly-fixture-input'),
             authority_revision=p['authority_revision'], ledger_targets=['R-'+ledger_source_id(uid)]))
    h = SimpleNamespace(runner=runner, key=key, uid=uid, db=db, saved=saved, reader=reader)
    yield h
    assert runner.store.io.read_versioned() == (raw, tag)
    assert runner.store.load() == saved and not db.writes
    assert all(not rows for rows in db.data.values())


def test_existing_candidate_has_no_posting_authority_or_native_intent(frozen):
    h = frozen; unit = h.saved['pages'][h.key]['units'][h.uid]
    assert unit['posting_authority'] is unit['intent'] is unit['readback'] is None
    assert h.runner.post_manual(h.key, h.uid, {}, apply=False) == dict(status='posting_authority_required', write=0)
    own = h.reader.reader.observe('pdf_manual', 'u1')
    # Ownership is a fence observation, not native financial authority.
    assert own.unit['unit_id'] == h.uid
    verify_registered_pdf_partition(own, h.saved, h.key, h.uid)
    # The old helper accepts a PAGE-local snapshot even though the source has
    # more pages. This is NOT proof of a complete SOURCE partition.
    assert len(h.saved['pages']) < h.saved['pages'][h.key]['page']['source']['page_count']


def test_normal_dry_run_reaches_materializer_during_planning_but_body_is_blocked(frozen):
    h = frozen
    with pytest.raises(AssertionError, match='side effect forbidden'):
        h.runner.post_normal(h.key, h.uid, apply=False)
    assert h.runner.store.load()['pages'][h.key]['units'][h.uid]['intent'] is None


@pytest.mark.parametrize('kind', ['medical', 'payroll', 'sensitive_unknown'])
def test_pdf_privacy_never_issues_authority_or_sends_ai(kind):
    p = page(classification=kind)
    gate = AuthorityAdapter(lambda *_: deepcopy(p), forbidden, forbidden, forbidden)
    if kind == 'sensitive_unknown': p['clearly_sensitive'] = True
    p['review_identity'] = review_identity(p)
    with pytest.raises(StateError, match='sensitive_page_forbidden'): gate.authorize(p)
    r = PageIntake(SimpleNamespace(load=lambda: dict(pages={'p': dict(page=p, status='observed')})),
        forbidden, object(), object(), forbidden)
    assert r.analyze('p')['status'] in {'medical_manual_pending', 'privacy_held'}


@pytest.mark.parametrize('failure', ['bytes', 'page_revision', 'missing_proof'])
def test_existing_authority_reader_rejects_stale_source_or_missing_proof(failure):
    p = page(classification='unknown'); current = deepcopy(p)
    if failure == 'page_revision':
        current['authority_revision'] += 1
        current['review_identity'] = review_identity(current)
    gate = AuthorityAdapter(lambda *_: current, lambda _: b'changed' if failure == 'bytes' else RAW,
                            lambda *_: None, forbidden)
    expected = dict(bytes='source_stale', page_revision='page_stale', missing_proof='authority_missing')[failure]
    with pytest.raises(StateError, match=expected): gate.authorize(p)


@pytest.mark.parametrize('change', ['drive_revision', 'native_version', 'partition', 'old_pending', 'unmapped_ledger'])
def test_trusted_ownership_stops_on_version_old_pending_or_unmapped_ledger(frozen, change):
    f = frozen.reader; old = f.reader.observe('pdf_manual', 'u1').value()
    if change == 'drive_revision': f.source.frame['drive_revision_id'] = 'changed'
    elif change == 'native_version': f.native.frame['pdf_manual', 'u1']['source']['native_source_version'] = '2'
    elif change == 'partition': f.partition.frame['revision'] += 1
    elif change == 'old_pending': f.pending.frame['records'] = [old]
    else: f.ledger.frame['records'] = [dict(transaction_id='unmapped-ledger-row', ownership=old)]
    with pytest.raises(StateError):
        new = f.reader.observe('pdf_manual', 'u1').value()
        relation(old, new)


def test_same_native_unit_and_whole_source_conflict_without_shared_claim(frozen):
    f = frozen.reader
    unit = f.reader.observe('pdf_manual', 'u1').value()
    whole = deepcopy(unit); whole.update(scope='whole', unit=None)
    assert relation(unit, whole) == relation(whole, unit) == 'conflict'
    assert relation(unit, deepcopy(unit)) == 'conflict'
    # The shared CAS is read only; no claim, native request or permission minted.
    io = Memory(); io.synthetic_memory_only = True
    io.replace_versioned = forbidden
    requests = ConditionalRequests(io, 'readonly-shared', synthetic_accounting=True)
    assert requests.read()[0]['operations'] == {} and io.writes == 0


def test_partition_helper_is_page_local_and_rejects_changed_manifest(frozen):
    h = frozen; own = h.reader.reader.observe('pdf_manual', 'u1')
    changed = deepcopy(h.saved)
    changed['pages'][h.key]['units'][h.uid]['candidate']['manifest']['units'][0]['bbox']['right'] = .9
    with pytest.raises(StateError, match='partition_changed'):
        verify_registered_pdf_partition(own, changed, h.key, h.uid)
    source = inspect.getsource(verify_registered_pdf_partition)
    assert "observation.partition['units'] != units" in source
    assert "registry['pages'][page_key]" in source


@pytest.mark.parametrize('observation', ['before', 'full', 'partial', 'formula'])
def test_legacy_pending_readback_is_observation_not_writer_start_proof(frozen, observation):
    from app.sheets import HEADERS
    h = frozen
    before = [{t: [deepcopy(HEADERS[t])] for t in ('レシート', '支出明細', '取込データ')} for _ in range(2)]
    # Deliberately UNMAPPED legacy ledger evidence. Never invent source/Unit
    # mapping or claim authority from its row values.
    rows = [['legacy-unmapped-A', '2026-01-01', 'synthetic', 'x', 1, '食費', '食料品', '', '', '', '', '', 'active'],
            ['legacy-unmapped-B', '2026-01-01', 'synthetic', 'y', 2, '食費', '食料品', '', '', '', '', '', 'active']]
    expected = dict(operations=[dict(table='支出明細', rows=rows)])
    snapshot = deepcopy(before)
    for part in snapshot:
        part['支出明細'].extend(deepcopy(rows if observation in {'full', 'formula'} else rows[:1] if observation == 'partial' else []))
    if observation == 'formula': snapshot[1]['支出明細'][-1][4] = '=1+1'
    actual = exact_readback(SnapshotDB(snapshot), expected, digest(before))
    assert actual == ('complete' if observation == 'full' else 'not_written' if observation == 'before' else 'partial_or_unknown')
    assert h.saved['pages'][h.key]['units'][h.uid]['intent'] is None
    legacy = deepcopy(h.saved['pages'][h.key])
    legacy['units'][h.uid]['intent'] = dict(status='claimed', plan=expected, before_ledger_digest=digest(before))
    shadow = copy(h.runner); shadow.db = SnapshotDB(snapshot)
    result = shadow.recover(h.key, h.uid, legacy, apply=False)
    assert result['status'] == actual and result['write'] == 0 and result['retry_allowed'] is False
    # Neither COMPLETE nor NOT_EXECUTED is inferred for a financial operation.


def function(path, name, owner=None):
    tree = ast.parse((ROOT/path).read_text('utf-8'))
    if owner:
        tree = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner)
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


@pytest.mark.parametrize('path,name,owner,writer', [
    ('app/pdf_intake_runner.py', '_post', 'PageIntake', '_materialize_result'),
    ('app/unified_receipt_native.py', 'execute', 'PDFManualUnitPort', 'post_manual'),
    ('app/unified_receipt_native.py', 'execute', 'GroupedUnitPort', '_materialize_result'),
    ('app/receipt_plan/posting.py', 'execute', None, '_materialize_result'),
    ('app/receipt_plan/unit_posting.py', 'execute', None, '_materialize_result')])
def test_native_writer_boundaries_remain_inspection_only(path, name, owner, writer):
    node = function(path, name, owner)
    attrs = [n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    if writer: assert writer in attrs
    assert not any('writer_started' in n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str))


def test_manual_request_and_intent_are_distinct_and_not_consumed_as_a_grant():
    manual = ast.unparse(function('app/pdf_intake_runner.py', 'post_manual'))
    post = ast.unparse(function('app/pdf_intake_runner.py', '_post'))
    assert "verified['request_id']" in manual and "journal['requests'].get(rid)" in manual
    assert "'request_id': str(uuid4())" in post
    assert "'posting_binding': authorized" in post and "'before_ledger_digest'" in post
    assert 'writer_started' not in post and 'authority_consume' not in post


@pytest.mark.parametrize('case_id', ['P201', 'P202'])
def test_structure_cases_stay_structure_only_without_unit_or_accounting_permission(case_id):
    from test_iphone_review_drafts import row
    from app.iphone_review_drafts import check_row
    r = row(case_id, 'grouping'); before = deepcopy(r)
    check_row(r)
    assert r == before and r['purpose'] == 'STRUCTURE_REVIEW'
    assert r['identity']['receipt_unit_id'] == '' and r['identity']['receipt_index'] == 0


def test_registry_requires_strong_tag_even_for_readonly(frozen, monkeypatch):
    h = frozen; original = h.runner.store.io.read_versioned
    raw, tag = original()
    with monkeypatch.context() as m:
        m.setattr(h.runner.store.io, 'read_versioned', lambda: (raw, 'W/'+tag))
        with pytest.raises(StateError): h.runner.store.load()


@pytest.mark.parametrize('module', ['pdf_grouping_authority', 'pdf_unit_processing'])
def test_pinned_native_inspection_does_not_compose_or_execute_historical_code(module):
    manifest = json.loads((ROOT/'auth-session/baseline.json').read_text('utf-8'))
    sha = manifest['baseline_commit']
    raw = subprocess.check_output(['git', '-C', str(ROOT), 'show', sha+':app/'+module+'.py'])
    tree = ast.parse(raw.decode('utf-8'))
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert '_materialize_result' not in calls
    if module == 'pdf_grouping_authority':
        assert 'confirmed_units' in {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "'accounting_allowed': False" in raw.decode('utf-8')
    else:
        assert "'phase': 'pending'" in raw.decode('utf-8')
        assert 'writer_started' not in raw.decode('utf-8')
        assert 'medical_manual' in raw.decode('utf-8')
