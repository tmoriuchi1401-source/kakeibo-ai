"""Design review evidence only. No native transition, authority or writer."""
from copy import deepcopy
from pathlib import Path
import socket
import subprocess

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation import ReceiptConfirmation
from app.receipt_confirmation_contract import validate_request, planned_after
from iphone_draft_request_design_examples import Example, SCHEMA, rehash_review

ROOT = Path(__file__).resolve().parents[1]
PARENT = '66a259b7e77d62a153cc918518e09aff8d48ffb5'


@pytest.fixture(autouse=True)
def no_effects(monkeypatch):
    from app.receipt_pipeline import ReceiptPipeline
    from app.unified_receipt_write import ConditionalRequests
    from app.receipt_confirmation_contract import IsolatedConfirmationContract, IsolatedReimportStore
    from app.pdf_intake_authority import DecisionSealer, AuthorityAdapter
    from app.sheets import SheetsDB
    from google.cloud import firestore
    from googleapiclient import discovery
    def denied(*args, **kwargs):
        pytest.fail('design_only_no_effects')
    for obj, names in [(ReceiptConfirmation, ['_write_accounting_plan']),
        (ReceiptPipeline, ['_materialize_result']), (ConditionalRequests, ['replace', 'transition']),
        (IsolatedReimportStore, ['_commit']), (IsolatedConfirmationContract, ['accept_fixture', 'consume', 'start', 'complete_fixture', 'dispatch']),
        (DecisionSealer, ['seal']), (AuthorityAdapter, ['authorize']),
        (SheetsDB, ['__init__']), (discovery, ['build']), (firestore, ['Client']),
        (socket, ['create_connection'])]:
        for name in names:
            monkeypatch.setattr(obj, name, denied)
    monkeypatch.setattr('app.receipt_confirmation.now_jst_string', lambda: '2026-10-19 00:00:00')


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_corrected_snapshot_and_native_plan_without_baseline_write(kind, monkeypatch):
    f = Example(kind, monkeypatch); before = f.raw_state()
    review = f.derive_example()
    assert review['inputs']['schema'] == SCHEMA
    assert review['inputs']['baseline']['item']['inputs'] != review['inputs']['proposed']['confirmed']
    assert f.compare_with_current_readers(review)['accounting_permission'] is False
    rows = dict((t, []) for t in ['レシート', '支出明細', '取込データ'])
    for table, row in review['plan']: rows[table].append(row)
    assert rows['レシート'][0][1:5] == [f.fields['date'], f.fields['merchant'], f.fields['amount'], f.fields['payment']]
    assert rows['取込データ'][0][4:8] == [f.fields['date'], f.fields['merchant'], f.fields['amount'], f.fields['payment']]
    if kind == 'normal':
        assert [r[0] for r in rows['支出明細']] == [f.rid+'-01', f.rid+'-02']
        assert [r[3:7] for r in rows['支出明細']] == [
            ['Synthetic corrected 1', 80, '日用品', '消耗品'], ['Synthetic corrected 2', 40, '食費', '食品']]
        assert rows['支出明細'][0][11] == 'preserve-label'
        assert f.service._parsed(f.service.items[f.key]).merchant == 'Synthetic shop'
        old_items = f.baseline['records'][f.sid]['parsed']['items']
        for proposed, old in zip(review['inputs']['proposed']['parsed']['items'], old_items):
            assert {k: v for k, v in proposed.items() if k not in ('name', 'amount', 'major_category', 'minor_category')} == {
                k: v for k, v in old.items() if k not in ('name', 'amount', 'major_category', 'minor_category')}
    else:
        assert len(rows['支出明細']) == 1 and rows['支出明細'][0][4:7] == [135, '医療・保険', '病院']
        assert review['inputs']['draft']['value']['provenance'] == 'human-entered'
    expected = planned_after(review['before'], review['plan'])  # PURE expected projection, not a ledger write.
    assert next(r for r in expected[1]['支出明細'] if r[0] == 'unrelated-id')[4] == '=1+2'
    assert f.raw_state() == before and f.financial_calls == 0
    assert review['accounting_permission'] is review['qualified_request'] is False
    with pytest.raises(StateError): validate_request(review)  # Review example is never a qualified native request.


@pytest.mark.parametrize('attack', ['reorder', 'duplicate', 'add', 'remove', 'id', 'index', 'state', 'category', 'sum', 'broadcast', 'bool'])
def test_general_existing_item_identity_and_native_validation(attack, monkeypatch):
    f = Example('normal', monkeypatch); before = f.raw_state()
    if attack == 'reorder': f.items.reverse()
    elif attack == 'duplicate': f.items[1] = deepcopy(f.items[0])
    elif attack == 'add': f.items.append(deepcopy(f.items[-1]))
    elif attack == 'remove': f.items.pop()
    elif attack == 'id': f.items[0]['expense_id'] = 'unrelated-id'
    elif attack == 'index': f.items[0]['baseline_index'] = True
    elif attack == 'state': f.items[0]['state'] = 'held'
    elif attack == 'category': f.items[0]['category'] = 'unknown｜category'
    elif attack == 'sum': f.items[0]['amount'] += 1
    elif attack == 'broadcast': f.fields['category'] = '食費｜食品'
    else: f.fields['amount'] = True
    with pytest.raises((AssertionError, ValueError, StateError)): f.derive_example()
    assert f.raw_state() == before and f.financial_calls == 0


@pytest.mark.parametrize('attack', ['date', 'merchant', 'amount', 'category', 'ai', 'ocr', 'items'])
def test_medical_complete_manual_snapshot_only(attack, monkeypatch):
    f = Example('medical', monkeypatch); before = f.raw_state()
    if attack in ('date', 'merchant', 'amount', 'category'): f.fields[attack] = ''
    elif attack in ('ai', 'ocr'): f.provenance = attack+'-candidate'
    else: f.items = [dict(name='automatic-medical-line')]
    with pytest.raises((AssertionError, ValueError, StateError)): f.derive_example()
    assert f.raw_state() == before and f.financial_calls == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
@pytest.mark.parametrize('field', ['draft_revision', 'draft_tag', 'generation', 'category_revision', 'fields', 'source', 'native_tag'])
def test_displayed_snapshot_stale_after_independent_reader_change(kind, field, monkeypatch):
    f = Example(kind, monkeypatch); displayed = f.derive_example()
    if field == 'fields': f.fields['memo'] = 'Synthetic newer saved memo'
    elif field == 'source': f.source['drive_revision_id'] = 'synthetic-next-revision'
    elif field in ('draft_revision', 'generation'): setattr(f, field, getattr(f, field)+1)
    else: setattr(f, field, '"synthetic-next-tag"')
    before = f.raw_state()
    with pytest.raises(AssertionError): f.compare_with_current_readers(displayed)
    assert f.raw_state() == before and f.financial_calls == 0


@pytest.mark.parametrize('field', ['baseline', 'draft', 'source', 'parsed', 'confirmed', 'plan', 'before', 'categories', 'line_binding'])
def test_rehashed_client_edit_still_fails_server_rederivation(field, monkeypatch):
    f = Example('normal', monkeypatch); displayed = f.derive_example(); before = f.raw_state()
    if field == 'baseline': displayed['inputs']['baseline']['item']['reason'] = 'Synthetic forged reason'
    elif field == 'draft': displayed['inputs']['draft']['revision'] += 1
    elif field == 'source': displayed['inputs']['source_binding']['drive_revision_id'] = 'forged'
    elif field == 'parsed': displayed['inputs']['proposed']['parsed']['merchant'] = 'forged'
    elif field == 'confirmed': displayed['inputs']['proposed']['confirmed'][1] = 'forged'
    elif field == 'plan': displayed['plan'][0][1][2] = 'forged'
    elif field == 'before': displayed['before'][1]['支出明細'][-1][4] = '=9+9'
    elif field == 'categories': displayed['inputs']['categories']['revision'] = 'forged'
    else: displayed['inputs']['proposed']['lines'][0]['expense_id'] = 'forged'
    rehash_review(displayed)
    with pytest.raises(AssertionError): f.compare_with_current_readers(displayed)
    assert f.raw_state() == before and f.financial_calls == 0


@pytest.mark.parametrize('attack', ['owner', 'case', 'PDF', 'unit', 'page', 'G103', 'H301', 'source_version', 'old_p1', 'hold'])
def test_source_scope_owner_and_holds_are_not_converted(attack, monkeypatch):
    f = Example('normal', monkeypatch)
    if attack == 'owner': f.draft_owner = '0'*64
    elif attack == 'case': f.draft_case = 'SYN-G102'
    elif attack == 'PDF': f.source['source_kind'] = 'pdf'
    elif attack == 'unit': f.source.update(scope='unit', receipt_unit_id='synthetic-pdf-unit', receipt_index=1)
    elif attack == 'page': f.source['page_identity'] = '0'*64
    elif attack in ('G103', 'H301'): f.case_id = 'SYN-'+attack
    elif attack == 'source_version': f.source['native_source_version'] = '2'
    elif attack == 'old_p1':
        from app.unified_receipt_write import OLD_P1_SOURCE
        f.source['source_id'] = OLD_P1_SOURCE
    else: f.held = True
    before = f.raw_state()
    with pytest.raises(AssertionError): f.derive_example()
    assert f.raw_state() == before and f.financial_calls == 0


@pytest.mark.parametrize('status', ['UNKNOWN', 'qualified', 'pending', 'writer_started', 'complete_fixture', 'applied'])
def test_prior_operation_never_becomes_a_fresh_draft_attempt(status, monkeypatch):
    f = Example('medical', monkeypatch); f.prior_operation = status; before = f.raw_state()
    with pytest.raises(AssertionError): f.derive_example()
    assert f.raw_state() == before and f.financial_calls == 0


@pytest.mark.parametrize('status', ['pending', 'applied'])
def test_legacy_pending_or_applied_fixture_is_not_promoted(status, monkeypatch):
    f = Example('medical', monkeypatch)
    f.baseline['confirmation_items'][f.key]['status'] = status
    f.store.value = deepcopy(f.baseline)
    before = f.raw_state()
    with pytest.raises(AssertionError): f.derive_example()
    assert f.raw_state() == before and f.financial_calls == 0


@pytest.mark.parametrize('schema', ['synthetic-owner-acknowledgements-v1', 'receipt-reimport-v2', 'unknown', ''])
def test_other_schema_is_never_interpreted_as_the_new_review(schema, monkeypatch):
    f = Example('normal', monkeypatch); review = f.derive_example()
    review['inputs']['schema'] = schema
    rehash_review(review)
    with pytest.raises(AssertionError): f.compare_with_current_readers(review)
    assert f.financial_calls == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_existing_native_duplicate_rule_still_holds(kind, monkeypatch):
    f = Example(kind, monkeypatch)
    # Additional invented fixture row, before the measured read-only call.
    f.db.rows['支出明細'].append(['duplicate-other-source', f.fields['date'], 'Synthetic other',
        'Synthetic other', f.fields['amount'], '食費', '食品', '', 'manual', '', '', '', 'active'])
    before = f.raw_state()
    with pytest.raises(ValueError, match='重複'): f.derive_example()
    assert f.raw_state() == before and f.financial_calls == 0


def test_existing_native_extra_expense_row_is_not_silently_deleted(monkeypatch):
    from app.receipt_reimport_production import target_snapshot
    f = Example('normal', monkeypatch)
    extra = deepcopy(f.db.rows['支出明細'][0]); extra[0] = f.rid+'-03'
    f.db.rows['支出明細'].extend([deepcopy(extra), deepcopy(extra)])
    f.db.rows['支出明細'][-1][0] = f.rid+'-04'
    item = f.store.value['confirmation_items'][f.key]
    item['before'] = target_snapshot(f.service.tables(), f.sid)
    f.baseline = deepcopy(f.store.value)
    before = f.raw_state()
    with pytest.raises(ValueError, match='既存明細の削除'): f.derive_example()
    assert f.raw_state() == before and f.financial_calls == 0


def test_runtime_schema_pin_and_old_validators_are_unchanged():
    changes = subprocess.check_output(['git', 'diff', '--name-status', PARENT, '--', 'app', 'auth-session', 'staging_shadow'], cwd=ROOT, text=True).splitlines()
    allowed = {'A\tapp/receipt_confirmation_draft_request.py', 'A\tstaging_shadow/receipt_draft_contract.py',
        'A\tstaging_shadow/synthetic_draft_writer.py',
        'M\tapp/iphone_item_drafts.py', 'M\tapp/iphone_confirmation_host.py',
        'A\tapp/unified_shadow_templates/iphone_one_tab.html', 'A\tapp/unified_shadow_templates/iphone_one_tab.js',
        'A\tapp/unified_shadow_templates/iphone_one_tab_original.html',
        'A\tstaging_shadow/iphone_confirmation_ui.py'}  # User-authorized isolated R1 UI.
    assert set(changes) <= allowed  # Later, separately authorized isolated runtime.
    for path in ('app/receipt_confirmation_contract.py', 'staging_shadow/synthetic_legacy_writer.py'):
        assert subprocess.check_output(['git', 'diff', PARENT, '--', path], cwd=ROOT, text=True) == ''


def test_design_workflow_boundary():
    import yaml
    source = (ROOT/'.github/workflows/iphone-draft-request-design-checks.yml').read_text('utf8')
    w = yaml.load(source, Loader=yaml.BaseLoader)
    assert w['on'] == {'pull_request': {'branches': ['codex/iphone-mvp-r0-r1-synthetic']}}
    assert w['permissions'] == {'contents': 'read'}
    assert w['env'] == {'UNIFIED_RECEIPT_WRITE_ENABLED': 'false'}
    assert 'secrets.' not in source and 'vars.' not in source and 'environment:' not in source
    assert set(w['jobs']) == {'design-readonly'}
    job = w['jobs']['design-readonly']
    assert set(job) == {'runs-on', 'timeout-minutes', 'steps'} and job['runs-on'] == 'ubuntu-24.04'
    assert job['steps'][0] == {'uses': 'actions/checkout@v4', 'with': {
        'ref': '${{ github.event.pull_request.head.sha }}', 'fetch-depth': '0', 'persist-credentials': 'false'}}
    assert job['steps'][1] == {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.12'}}
    assert all(not any(k in s.get('uses', '') for k in ('upload-artifact', 'google-github-actions')) for s in job['steps'])
    assert [s.get('run') for s in job['steps'][2:]] == [
        'pip install -r requirements-test.txt',
        'python -m pytest --collect-only -q tests/test_iphone_draft_request_design.py\npython -m pytest -vv tests/test_iphone_draft_request_design.py\n',
        "python -m pytest -q tests/test_iphone_mvp_r0_adoption.py -k 'not phase131 and not acknowledgement and not ci'",
        "python -m pytest -q tests/test_iphone_review_drafts.py tests/test_pdf_native_readonly_inventory.py tests/test_shared_session_bundle.py tests/test_production_workflows.py -k 'not memory_cas_model'",
        'python -m compileall -q tests/iphone_draft_request_design_examples.py tests/test_iphone_draft_request_design.py\ngit diff --check\n']
