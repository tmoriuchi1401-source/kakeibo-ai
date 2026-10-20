"""R0 stopping evidence, not an R1 bridge or a permission to post.

Exercise existing contracts without changing a validator/schema or producing
financial qualifications. All inputs are the existing invented fixtures.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import validate_candidate, validate_request
from app.unified_receipt_review import digest
from test_synthetic_legacy_writer import harness

ROOT = Path(__file__).resolve().parents[1]
START = 'bcf8f2ce7b791a1a65b8e06d1b3d0e3c83de77dc'


@pytest.fixture(autouse=True)
def no_live_clients_or_financial_bodies(monkeypatch):
    from app.receipt_confirmation import ReceiptConfirmation
    from app.receipt_pipeline import ReceiptPipeline
    from app.pdf_intake_runner import PlanningDB
    from app.sheets import SheetsDB
    from googleapiclient import discovery
    from google.cloud import firestore
    def denied(*args, **kwargs):
        pytest.fail('R0 cannot dispatch a financial writer or construct a live client')
    original = ReceiptPipeline._materialize_result
    def planning_only(service, *args, **kwargs):
        # Existing #131 fixture construction uses the native planner, whose
        # materializer only records operations in this exact PlanningDB class.
        assert type(service.db) is PlanningDB
        return original(service, *args, **kwargs)
    monkeypatch.setattr(ReceiptPipeline, '_materialize_result', planning_only)
    monkeypatch.setattr(ReceiptConfirmation, '_write_accounting_plan', denied)
    monkeypatch.setattr(SheetsDB, '__init__', denied)
    monkeypatch.setattr(discovery, 'build', denied)
    monkeypatch.setattr(firestore, 'Client', denied)
    monkeypatch.setattr('app.receipt_confirmation.now_jst_string', lambda: '2026-10-19 00:00:00')


def persisted(h):
    return [io.read_versioned() for io in (h.native, h.shared.io, h.ledger)]


@pytest.mark.parametrize('field,value', [
    ('date', '2026-01-03'), ('merchant', 'Synthetic edited shop'),
    ('amount', 121), ('category', '医療・保険｜病院'), ('payment', '合成カード')])
def test_legacy_normal_draft_fields_do_not_edit_the_native_parsed_candidate(field, value):
    h = harness('normal')
    before = persisted(h), deepcopy(h.service.store.value), deepcopy(h.db.rows)
    draft = deepcopy(h.case)
    next(f for f in draft.fields if f[0] == field)[2] = value
    baseline = h.port.validate_draft(h.case, h.case)
    edited = h.port.validate_draft(h.case, draft)
    # This legacy adapter validates its STORED candidate; it is not the #131
    # general-item editor. Never label this unchanged plan as the edited plan.
    assert draft.fields != h.case.fields
    assert baseline == edited
    assert h.service._parsed(h.service.items[h.key]).model_dump() == h.c['inputs']['parsed']
    assert before == (persisted(h), h.service.store.value, h.db.rows)
    assert h.adapter.dispatches == h.ledger.writes == h.native.writes == 0


@pytest.mark.parametrize('field,value', [
    ('date', '2026-01-03'), ('merchant', 'Synthetic edited clinic'),
    ('amount', 121), ('memo', 'Synthetic saved manual correction')])
def test_valid_medical_manual_draft_cannot_be_substituted_into_v2_confirmed(field, value):
    h = harness('medical')
    before = persisted(h), deepcopy(h.service.store.value), deepcopy(h.db.rows)
    draft = deepcopy(h.case)
    next(f for f in draft.fields if f[0] == field)[2] = value
    assert h.port.validate_draft(h.case, draft)['status'] == 'validated_not_authorized'
    changed = deepcopy(h.c)
    markers = ('date', 'merchant', 'amount', 'category', 'payment', 'action', 'duplicate_target', 'memo')
    changed['inputs']['confirmed'] = [draft.value(k) for k in markers]
    changed['authority']['input_fingerprint'] = digest(changed['inputs'])
    changed['ownership']['input_snapshot'] = digest(changed['inputs'])
    proof = h.auth.verify_post(None, 'ACCOUNTING_POST', changed['request_id'], digest(changed['inputs']), 0)
    changed['authority']['authenticated_action_digest'] = digest(asdict(proof))
    changed['fingerprint'] = digest({k: v for k, v in changed.items() if k != 'fingerprint'})
    # Native v2 requires old.inputs == confirmed. Updating the outer hash
    # alone cannot turn the separately saved #131 draft into that native input.
    validate_candidate(changed)  # All outer fixture hashes/bindings are valid.
    with pytest.raises(StateError, match='v2_request_invalid'):
        validate_request(changed)
    assert before == (persisted(h), h.service.store.value, h.db.rows)
    assert h.adapter.dispatches == h.ledger.writes == h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_v2_has_no_mutable_legacy_draft_transfer_transition(kind):
    h = harness(kind)
    before = persisted(h)
    value, raw, tag = h.store2.read()
    after = deepcopy(value)
    after['legacy']['confirmation_items'][h.key]['inputs'][7] = 'Synthetic saved draft'
    after['revision'] += 1
    # Probe the guarded entry, not IO.replace_versioned to bypass the guard.
    with pytest.raises(StateError, match='v2_legacy_immutable'):
        h.store2._commit(value, raw, tag, after, value['revision'])
    with pytest.raises(StateError, match='explicit_native_transition_required'):
        h.store2.save(after['legacy'])
    assert persisted(h) == before
    assert not h.store2.uncertain
    assert h.adapter.dispatches == h.ledger.writes == h.native.writes == 0


@pytest.mark.parametrize('kind', ['normal', 'medical'])
def test_rebinding_valid_proposed_native_view_does_not_replace_persisted_old(kind):
    from app.receipt_confirmation import TITLE
    h = harness(kind)
    changed = deepcopy(h.c)
    changed['inputs']['old']['inputs'][7] = 'Synthetic proposed memo'
    changed['inputs']['confirmed'][7] = 'Synthetic proposed memo'
    # Model the proposed native view only. Persisted native/shared/ledger are
    # untouched: an adapter cannot silently replace them with this view.
    h.service.store.value['confirmation_items'][h.key] = deepcopy(changed['inputs']['old'])
    h.db.rows[TITLE][0][14] = 'Synthetic proposed memo'
    next(f for f in h.case.fields if f[0] == 'memo')[2] = 'Synthetic proposed memo'
    changed['authority']['input_fingerprint'] = digest(changed['inputs'])
    proof = h.auth.verify_post(None, 'ACCOUNTING_POST', changed['request_id'], digest(changed['inputs']), 0)
    changed['authority']['authenticated_action_digest'] = digest(asdict(proof))
    h.own.native.frame['legacy_confirmation', 'u1']['input_snapshot'] = digest(changed['inputs'])
    changed['ownership'] = h.own.reader.observe('legacy_confirmation', 'u1').value()
    changed['fingerprint'] = digest({k: v for k, v in changed.items() if k != 'fingerprint'})
    validate_request(changed)
    h.current = dict(source=changed['source'], inputs=changed['inputs'], authority=changed['authority'])
    h.contract._fresh(changed, eligibility=True)  # Real native validation passes.
    before = persisted(h)
    with pytest.raises(StateError, match='v2_legacy_pending_or_applied'):
        h.contract.accept_fixture(changed, None, expected_revision=0)
    assert persisted(h) == before
    assert h.adapter.dispatches == h.ledger.writes == h.native.writes == 0


def test_phase131_general_editor_is_pdf_unit_not_legacy_whole_source():
    from iphone_confirmation_fixtures import contexts
    from app.unified_receipt_native import PDFManualUnitPort, ReceiptConfirmationPort
    n = contexts()['SYN-G101']
    h = harness('normal')
    assert isinstance(n['port'], PDFManualUnitPort)
    assert isinstance(h.port, ReceiptConfirmationPort)
    assert n['context'].case.identity['receipt_unit_id']
    assert h.c['source']['source_kind'] == 'image'
    assert n['context'].case.source_id != h.c['source']['source_id']
    assert n['context'].case.identity['source_content_hash'] != h.c['source']['sha256']
    # Stable display aliases and matching amounts are not a trusted mapping.
    assert n['context'].case.kind != h.case.kind
    assert h.adapter.dispatches == h.ledger.writes == h.native.writes == 0


def test_acknowledgement_stays_nonfinancial_and_separate_from_native(monkeypatch, tmp_path):
    from iphone_confirmation_fixtures import Rig
    r = Rig(monkeypatch, tmp_path)
    assert r.h.login().status_code == 303
    assert r.save('SYN-M101').status_code == 200
    response, _, _ = r.accept('SYN-M101')
    assert response.status_code == 200
    receipt = response.json['result']
    assert receipt['status'] == 'synthetic_owner_acknowledged'
    assert receipt['native_authority'] is receipt['accounting_permission'] is False
    with pytest.raises(StateError):
        validate_request(receipt)


def test_no_runtime_auth_bundle_or_existing_workflow_changed_since_141():
    names = subprocess.check_output(['git', 'diff', '--name-only', START, '--',
        'app', 'auth-session', 'staging_shadow'], cwd=ROOT, text=True).splitlines()
    # The next user-approved isolation adds only these modules. The old code,
    # auth bundle and every pre-existing runtime remain byte-for-byte pinned.
    allowed = {'app/receipt_confirmation_draft_request.py', 'staging_shadow/receipt_draft_contract.py',
        'staging_shadow/mandatory_receipt_gate.py',  # Authorized isolated R2-2 composition.
        'staging_shadow/synthetic_draft_writer.py',
        'app/iphone_item_drafts.py', 'app/iphone_confirmation_host.py',
        'app/unified_shadow_templates/iphone_one_tab.html', 'app/unified_shadow_templates/iphone_one_tab.js',
        'app/unified_shadow_templates/iphone_one_tab_original.html',
        'staging_shadow/iphone_confirmation_ui.py'}  # User-authorized isolated R1 UI.
    assert set(names) <= allowed
    status = subprocess.check_output(['git', 'diff', '--name-status', START, '--',
        'app', 'auth-session', 'staging_shadow'], cwd=ROOT, text=True).splitlines()
    assert all(line.startswith('A\t') or line in {'M\tapp/iphone_item_drafts.py',
        'M\tapp/iphone_confirmation_host.py'} for line in status)
    baseline = json.loads((ROOT / 'docs/iphone-mvp-r0-pr-baseline.json').read_text('utf8'))
    assert baseline['start'] == START
    assert len(baseline['pull_requests']) == 19
    for pr in baseline['pull_requests']:
        if pr['number'] not in (125, 126):
            assert pr['state'] == 'open' and pr['draft'] is True


def test_inventory_covers_exact_main_to_141_difference():
    value = json.loads((ROOT / 'docs/iphone-mvp-r0-delta.json').read_text('utf8'))
    actual = subprocess.check_output(['git', 'diff', '--name-status', value['main'], START], cwd=ROOT, text=True)
    assert [(e['status'], e['path']) for e in value['files']] == [tuple(s.split('\t')) for s in actual.splitlines()]
    assert len(value['files']) == value['inherited_files'] == 136
    for ref, field in [(value['main'], 'main_blob'), (START, 'draft_blob')]:
        tree = subprocess.check_output(['git', 'ls-tree', '-r', ref], cwd=ROOT, text=True)
        blobs = {line.split('\t')[1]: line.split('\t')[0].split()[2] for line in tree.splitlines()}
        assert all(e[field] == blobs.get(e['path']) for e in value['files'])


def test_r0_ci_has_no_production_capabilities():
    import yaml
    path = ROOT / '.github/workflows/iphone-mvp-r0-checks.yml'
    source = path.read_text('utf8')
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert set(workflow['on']) == {'pull_request'}
    assert workflow['on']['pull_request']['branches'] == ['codex/phase5b2b2d1-pdf-native-design']
    assert workflow['permissions'] == {'contents': 'read'}
    assert workflow['env'] == {'UNIFIED_RECEIPT_WRITE_ENABLED': 'false'}
    assert workflow['concurrency'] == {'group': 'kakeibo-iphone-mvp-r0-${{ github.ref }}', 'cancel-in-progress': 'true'}
    assert 'secrets.' not in source and 'vars.' not in source
    assert set(workflow['jobs']) == {'adoption'}
    for job in workflow['jobs'].values():
        assert job['runs-on'] == 'ubuntu-24.04'
        assert set(job) == {'runs-on', 'timeout-minutes', 'steps'}
        assert job['timeout-minutes'] == '30'
        assert job['steps'][0] == {'uses': 'actions/checkout@v4', 'with': {
            'ref': '${{ github.event.pull_request.head.sha }}', 'fetch-depth': '0', 'persist-credentials': 'false'}}
        assert job['steps'][1] == {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.12'}}
        assert len(job['steps']) == 7
        for step in job['steps']:
            assert not any(s in step.get('uses', '') for s in ('upload-artifact', 'google-github-actions', 'cache/save'))
            assert 'continue-on-error' not in step
        assert job['steps'][2] == {'name': 'Install existing test dependencies', 'run': 'pip install -r requirements-test.txt'}
        assert job['steps'][3]['run'] == 'python -m pytest --collect-only -q tests/test_iphone_mvp_r0_adoption.py'
        assert job['steps'][4]['run'] == 'python -m pytest -vv tests/test_iphone_mvp_r0_adoption.py'
        command = job['steps'][5]['run'].split()
        assert command[:4] == ['python', '-m', 'pytest', '-q']
        assert command[4:] and all(p.startswith('tests/test_') and p.endswith('.py') and (ROOT / p).is_file() for p in command[4:])
        assert {'tests/test_synthetic_legacy_writer.py', 'tests/test_receipt_confirmation_contract.py',
                'tests/test_iphone_confirmation.py', 'tests/test_accounting_recovery.py',
                'tests/test_shared_session_bundle.py'} <= set(command[4:])
        assert job['steps'][6]['run'] == 'python -m compileall -q tests/test_iphone_mvp_r0_adoption.py\ngit diff --check\n'
