"""Synthetic only. No production proof, credentials, network, or Google state."""
import base64
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
import yaml

from app import bank_attempt_recovery as r
from app.drive_run_state import StateBinding, StateError, envelope
from app.production_ledger import initial_ledger, _encode
from app.bank_pdf_pipeline import NormalizedBankTransaction


class Transport:
    def __init__(self, name, payload, events):
        self.name, self.payload, self.events = name, payload, events
        self.fail = ''
        self.before_write = None

    def read(self):
        self.events.append(('read', self.name))
        return self.payload

    def write(self, payload):
        self.events.append(('write', self.name))
        if self.before_write:
            self.before_write()
        if self.fail == 'before':
            raise ConnectionError('synthetic-private-body')
        self.payload = payload
        if self.fail == 'after':
            raise ConnectionError('synthetic-private-body')


@pytest.fixture
def rig(monkeypatch):
    events = []
    bindings = {s: StateBinding(s, 'synthetic-sheet', 'synthetic-folder', s + '-synthetic') for s in r.SOURCES}
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE recurring_checkpoint(singleton INTEGER, successful_window_end TEXT, run_id TEXT)')
    checkpoint = [[1, '2026-10-15T09:08:44+09:00', 'synthetic-prior']]
    con.executemany('INSERT INTO recurring_checkpoint VALUES (?,?,?)', checkpoint)
    con.execute('CREATE TABLE recurring_runs(run_id TEXT, recorded_at TEXT, status TEXT, window_start TEXT, window_end TEXT, summary_json TEXT)')
    for i in range(32):
        con.execute('INSERT INTO recurring_runs VALUES (?,?,?,?,?,?)', (str(i), 'synthetic', 'noop', '', '', '{"status":"noop"}'))
    con.commit()
    native = con.serialize()
    con.close()
    files = {'bank-recurring.sqlite3': {'sha256': r.sha(native), 'data': base64.b64encode(native).decode()}}
    before_bank = envelope(bindings[r.SOURCES[0]], files, generation=53, phase='pending', attempt='a' * 32)
    parent = json.loads(initial_ledger(bindings[r.SOURCES[1]]))
    parent['generation'] = 1300
    parent['sources']['bank'].update(phase='pending', attempt='b' * 32, error='source_execution_failed')
    before_parent = _encode(parent)
    after_bank = envelope(bindings[r.SOURCES[0]], files, generation=54)
    parent['generation'] = 1301
    parent['sources']['bank'].update(phase='ready', attempt='', error='')
    after_parent = _encode(parent)
    ids = [f'synthetic-id-{i:03}' for i in range(497)]
    # Duplicates0..47 present, D48/49 absent. New493..496 absent.
    present = ids[:48] + ids[50:378]
    raw = dict(zip(r.TABLES, [[['id']] + [[i] + [''] * 11 for i in present], [['id']], [['id']], [['id']]], strict=True))
    record = dict(kind='bank40961220838_state_release_only', run_id=r.FAILED_RUN, run_attempt=1,
        failed_main_sha=r.FAILED_HEAD, financial_apply_authorized=False,
        financial_requests='UNKNOWN', pdf_requests='UNKNOWN', transaction_ids=ids,
        duplicate_ids=ids[:50], new4_ids=ids[-4:], unimported6_ids=ids[48:50] + ids[-4:],
        history_count=32, checkpoint=checkpoint, sqlite_sha256=r.sha(native),
        raw_financial_sha256=r.sha(r.encode(raw)), states={}, pdfs=[{} for _ in range(5)], inbox_folder='synthetic-inbox')
    before = dict(zip(r.SOURCES, [before_bank, before_parent]))
    after = dict(zip(r.SOURCES, [after_bank, after_parent]))
    for s, gen, attempt in [(r.SOURCES[0], 53, 'a' * 32), (r.SOURCES[1], 1300, 'b' * 32)]:
        b = bindings[s]
        record['states'][s] = dict(source=s, spreadsheet_id=b.spreadsheet_id, file_id=b.file_id,
            folder_id=b.folder_id, binding_reference=b.reference, generation_before=gen,
            generation_after=gen + 1, attempt=attempt, before_sha256=r.sha(before[s]), after_sha256=r.sha(after[s]))
    def pin():
        monkeypatch.setattr(r, 'RECORD_SHA256', r.sha(r.encode(record)))
    pin()
    transports = {s: Transport(s, before[s], events) for s in r.SOURCES}
    report = {}
    def guard():
        events.append(('guard', 'exclusive'))
    def observation():
        events.append(('observe', 'all497_pdf5'))
        r.verify_financial(raw, record)
    def independent(source):
        events.append(('independent', source))
        return transports[source].payload
    def run(dry_run=False, **kwargs):
        return r.recover(record, transports, bindings, kwargs.get('guard', guard),
            kwargs.get('observation', observation), kwargs.get('independent', independent),
            dry_run=dry_run, report=report)
    return SimpleNamespace(**locals())


def writes(s):
    return [v for k, v in s.events if k == 'write']


def test_default_dry_run_has_no_writes(rig):
    s = rig
    result = r.recover(s.record, s.transports, s.bindings, s.guard, s.observation, s.independent)
    assert result['outcome'] == 'dry_run' and not writes(s)
    assert all(s.transports[k].payload == s.before[k] for k in r.SOURCES)


def test_exact_order_and_no_outside_changes(rig):
    s = rig
    assert s.run()['outcome'] == 'released_stop'
    assert writes(s) == list(r.SOURCES)
    bank_write = s.events.index(('write', r.SOURCES[0]))
    independent = s.events.index(('independent', r.SOURCES[0]), bank_write)
    parent_write = s.events.index(('write', r.SOURCES[1]))
    assert bank_write < independent < parent_write
    assert ('independent', r.SOURCES[1]) in s.events[parent_write:]
    assert all(s.transports[k].payload == s.after[k] for k in r.SOURCES)
    assert s.report['financial_write_calls'] == s.report['pdf_changes'] == 0


@pytest.mark.parametrize('phase,expected', [('initial', []), ('bank_before', []),
    ('bank_after', [r.SOURCES[0]]), ('parent_before', [r.SOURCES[0]])])
def test_guard_stop_at_each_boundary(rig, phase, expected):
    s, n = rig, 0
    stop = {'initial': 1, 'bank_before': 2, 'bank_after': 3, 'parent_before': 4}[phase]
    def guard():
        nonlocal n
        n += 1
        if n == stop:
            raise StateError('synthetic_external_writer_or_expired_freeze')
        s.guard()
    with pytest.raises(StateError):
        s.run(guard=guard)
    assert writes(s) == expected
    assert s.transports[r.SOURCES[1]].payload == s.before[r.SOURCES[1]]


@pytest.mark.parametrize('source,when,count', [(r.SOURCES[0], 'before', 1), (r.SOURCES[0], 'after', 1),
                                             (r.SOURCES[1], 'before', 2), (r.SOURCES[1], 'after', 2)])
def test_unknown_response_stops_no_retry_or_rollback(rig, source, when, count):
    s = rig
    s.transports[source].fail = when
    with pytest.raises(ConnectionError):
        s.run()
    assert len(writes(s)) == count
    assert s.report['automatic_retry'] is False and s.report['rollback'] is False
    if source == r.SOURCES[0]:
        assert s.transports[r.SOURCES[1]].payload == s.before[r.SOURCES[1]]


def test_partial_repeat_cannot_continue_parent(rig):
    s = rig
    s.transports[r.SOURCES[0]].fail = 'after'
    with pytest.raises(ConnectionError): s.run()
    s.transports[r.SOURCES[0]].fail = ''
    s.events.clear()
    assert s.run(dry_run=True)['bank_ready'] is True
    with pytest.raises(StateError): s.run()
    assert not writes(s)


def test_duplicate_complete_is_no_write(rig):
    s = rig
    s.run()
    s.events.clear()
    assert s.run()['outcome'] == 'already_released_no_write'
    assert not writes(s)


def test_parent_response_lost_then_repeat_does_not_rewrite_either_file(rig):
    s = rig
    s.transports[r.SOURCES[1]].fail = 'after'
    with pytest.raises(ConnectionError): s.run()
    s.transports[r.SOURCES[1]].fail = ''
    s.events.clear()
    assert s.run()['outcome'] == 'already_released_no_write'
    assert not writes(s)


def test_parent_ready_while_native_pending_is_rejected(rig):
    s = rig
    s.transports[r.SOURCES[1]].payload = s.after[r.SOURCES[1]]
    with pytest.raises(StateError): s.run()
    assert not writes(s)


@pytest.mark.parametrize('field,value', [('generation', 55), ('attempt', 'c' * 32)])
@pytest.mark.parametrize('source', r.SOURCES)
def test_wrong_generation_or_attempt_stops_before_writes(rig, field, value, source):
    s = rig
    payload = json.loads(s.transports[source].payload)
    if source == r.SOURCES[0] or field == 'generation': payload[field] = value
    else: payload['sources']['bank'][field] = value
    s.transports[source].payload = r.encode(payload)
    with pytest.raises(StateError): s.run()
    assert not writes(s)


@pytest.mark.parametrize('field,value', [('run_id', 1), ('run_attempt', 2), ('financial_requests', 'NO'), ('pdf_requests', 'NO')])
def test_wrong_fixed_record_even_when_rehashed_is_refused(rig, field, value):
    s = rig
    s.record[field] = value
    s.pin()
    with pytest.raises(StateError): s.run()
    assert not writes(s)


def test_unpinned_record_and_changed_financial_row_fail(rig):
    s = rig
    s.record['checkpoint'][0][2] = 'tampered'
    with pytest.raises(StateError): s.run()
    assert not writes(s)


@pytest.mark.parametrize('table', r.TABLES)
def test_target_or_other_financial_rows_changed_fail(rig, table):
    s = rig
    s.raw[table].append(['unrelated-new-row'])
    with pytest.raises(StateError): s.run()
    assert not writes(s)


def test_independent_bank_get_failure_prevents_parent(rig):
    s = rig
    def independent(source):
        if source == r.SOURCES[0]: raise ConnectionError('read-only GET failed')
        return s.independent(source)
    with pytest.raises(ConnectionError): s.run(independent=independent)
    assert writes(s) == [r.SOURCES[0]]


def test_other_parent_field_changed_after_bank_prevents_parent(rig):
    s = rig
    def independent(source):
        if source == r.SOURCES[1]: return b'changed-parent'
        return s.independent(source)
    with pytest.raises(StateError): s.run(independent=independent)
    assert writes(s) == [r.SOURCES[0]]


def test_cooperating_writer_seen_by_guard_is_excluded(rig):
    s, n = rig, 0
    def guard():
        nonlocal n
        n += 1
        if n == 2: raise StateError('other_writer_entry_not_disabled')
    with pytest.raises(StateError): s.run(guard=guard)
    assert not writes(s)


def test_noncooperating_writer_race_is_not_fixed_by_reread(rig):
    # Deliberately demonstrates the residual CAS limitation, not a success claim.
    s = rig
    intrusions = []
    def race():
        intrusions.append('outside-lock-write-after-last-GET')
        s.transports[r.SOURCES[0]].payload = b'outside-writer-state'
    s.transports[r.SOURCES[0]].before_write = race
    s.run()
    assert intrusions and s.transports[r.SOURCES[0]].payload == s.after[r.SOURCES[0]]
    # GET alone is insufficient; nonexclusive approval must accept this risk.


def context():
    owner = r.REPOSITORY.split('/')[0]
    return dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY=r.REPOSITORY,
        GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/main',
        GITHUB_ACTOR=owner, GITHUB_TRIGGERING_ACTOR=owner, GITHUB_RUN_ATTEMPT='1',
        GITHUB_WORKFLOW_REF=r.REPOSITORY + '/' + r.WORKFLOW + '@refs/heads/main',
        GITHUB_SHA='c' * 40, GITHUB_WORKFLOW_SHA='c' * 40, EXPECTED_SHA='c' * 40,
        KAKEIBO_VALIDATED_MAIN_SHA='c' * 40, KAKEIBO_SCHEDULE_ENABLED='false',
        KAKEIBO_LEGACY_DISABLED='true', CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED='false',
        RECOVERY_MODE='release', RECOVERY_CONFIRM='RELEASE_40961220838', RECOVERY_OPERATION='d' * 32,
        GITHUB_RUN_ID='123', GH_TOKEN='synthetic-token')


@pytest.fixture
def control_rig(tmp_path):
    env = context()
    att = dict(at_epoch=1000, record_sha256=r.RECORD_SHA256, main_sha=env['GITHUB_SHA'],
        operation_id=env['RECOVERY_OPERATION'], mode='release', release_authorized=True,
        external_writers_frozen=True, no_dispatch_until_explicit_resume=True,
        configuration_unchanged=True, writers={'KnownWriterCount': 0, 'Tasks': [{'State': 'Disabled'}]})
    workflows = tmp_path / '.github/workflows'
    workflows.mkdir(parents=True)
    (workflows / 'other.yml').write_text('secrets.GOOGLE_SERVICE_ACCOUNT_JSON', encoding='utf-8')
    owner = r.REPOSITORY.split('/')[0]
    data = {
        'branches/main': {'commit': {'sha': env['GITHUB_SHA']}},
        'actions/variables?per_page=100': {'total_count': 4, 'variables': [
            {'name': k, 'value': env[k]} for k in ['KAKEIBO_VALIDATED_MAIN_SHA', 'KAKEIBO_SCHEDULE_ENABLED',
                'KAKEIBO_LEGACY_DISABLED', 'CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED']]},
        'actions/runs/123': dict(path=r.WORKFLOW, head_sha=env['GITHUB_SHA'], run_attempt=1,
            status='in_progress', event='workflow_dispatch', actor={'login': owner}, triggering_actor={'login': owner}),
        'actions/runs/' + str(r.FAILED_RUN): dict(head_sha=r.FAILED_HEAD, run_attempt=1, status='completed', conclusion='failure'),
        'actions/workflows?per_page=100': {'total_count': 1, 'workflows': [{'path': '.github/workflows/other.yml', 'state': 'disabled_manually'}]},
        'actions/workflows/' + r.WORKFLOW.rsplit('/', 1)[1] + '/runs?event=workflow_dispatch&per_page=100': {
            'total_count': 1, 'workflow_runs': [{'id': 123, 'display_title': 'bank40961220838-' + env['RECOVERY_OPERATION']}]}}
    for status in ('in_progress', 'queued', 'waiting', 'pending', 'requested'):
        data['actions/runs?status=' + status + '&per_page=100'] = {'total_count': 0, 'workflow_runs': []}
    def run():
        return r.controls(env, att, tmp_path, get=lambda path: deepcopy(data[path]),
            head=lambda *a, **k: SimpleNamespace(status_code=503), now=1100)
    return SimpleNamespace(**locals())


def test_exclusive_controls(control_rig):
    control_rig.run()


@pytest.fixture
def nonexclusive_rig(control_rig):
    s = control_rig
    s.env['RECOVERY_EXECUTION_POLICY'] = 'nonexclusive'
    for key in ('external_writers_frozen', 'no_dispatch_until_explicit_resume', 'writers'):
        del s.att[key]
    s.att.update(execution_policy='nonexclusive', nonexclusive_authorized=True,
        residual_risk=r.NONEXCLUSIVE_RISK, target_run_id=r.FAILED_RUN, target_run_attempt=1,
        normal_apply_resume_authorized=False)
    s.data['actions/workflows?per_page=100']['workflows'][0]['state'] = 'active'
    return s


def test_nonexclusive_explicit_approval_without_false_freeze(nonexclusive_rig):
    nonexclusive_rig.run()


@pytest.mark.parametrize('change', ['approval', 'risk', 'target_run', 'target_attempt', 'policy',
    'false_freeze', 'normal_apply', 'schedule', 'category', 'legacy', 'competing_run', 'failed_attempt'])
def test_nonexclusive_rejects_unapproved_or_unsafe_context(nonexclusive_rig, change):
    s = nonexclusive_rig
    if change == 'approval': s.att['nonexclusive_authorized'] = False
    if change == 'risk': s.att['residual_risk'] = ''
    if change == 'target_run': s.att['target_run_id'] += 1
    if change == 'target_attempt': s.att['target_run_attempt'] = 2
    if change == 'policy': s.att['execution_policy'] = 'strict'
    if change == 'false_freeze': s.att['external_writers_frozen'] = True
    if change == 'normal_apply': s.att['normal_apply_resume_authorized'] = True
    if change == 'schedule': s.env['KAKEIBO_SCHEDULE_ENABLED'] = 'true'
    if change == 'category': s.env['CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED'] = 'true'
    if change == 'legacy': s.env['KAKEIBO_LEGACY_DISABLED'] = 'false'
    if change == 'competing_run':
        s.data['actions/runs?status=in_progress&per_page=100'] = {'total_count': 1, 'workflow_runs': [{'id': 999}]}
    if change == 'failed_attempt': s.data['actions/runs/' + str(r.FAILED_RUN)]['run_attempt'] = 2
    with pytest.raises(StateError): s.run()


@pytest.mark.parametrize('failure', ['', 'bank_after', 'parent_after', 'state_mismatch'])
def test_nonexclusive_order_and_failure_boundaries(rig, nonexclusive_rig, failure):
    s = rig
    if failure == 'bank_after': s.transports[r.SOURCES[0]].fail = 'after'
    if failure == 'parent_after': s.transports[r.SOURCES[1]].fail = 'after'
    if failure == 'state_mismatch': s.transports[r.SOURCES[0]].payload = b'changed'
    if failure:
        with pytest.raises((ConnectionError, StateError)): s.run(guard=nonexclusive_rig.run)
        expected = [] if failure == 'state_mismatch' else list(r.SOURCES[:1 if failure == 'bank_after' else 2])
        assert writes(s) == expected
        assert s.report['automatic_retry'] is False and s.report['rollback'] is False
    else:
        assert s.run(guard=nonexclusive_rig.run)['outcome'] == 'released_stop'
        assert writes(s) == list(r.SOURCES)


@pytest.mark.parametrize('change', ['active_workflow', 'queued_run', 'external', 'dispatch', 'expired',
    'wrong_nonce', 'reused_nonce', 'schedule', 'rerun', 'wrong_actor', 'moved_main', 'wrong_failed_attempt'])
def test_controls_refuse_competing_or_unbound_context(control_rig, change):
    s = control_rig
    if change == 'active_workflow': s.data['actions/workflows?per_page=100']['workflows'][0]['state'] = 'active'
    if change == 'queued_run': s.data['actions/runs?status=queued&per_page=100'] = {'total_count': 1, 'workflow_runs': [{'id': 999}]}
    if change == 'external': s.att['external_writers_frozen'] = False
    if change == 'dispatch': s.att['no_dispatch_until_explicit_resume'] = False
    if change == 'expired': s.att['at_epoch'] = 0
    if change == 'wrong_nonce': s.att['operation_id'] = 'e' * 32
    if change == 'reused_nonce':
        key = 'actions/workflows/' + r.WORKFLOW.rsplit('/', 1)[1] + '/runs?event=workflow_dispatch&per_page=100'
        s.data[key]['workflow_runs'] *= 2
        s.data[key]['total_count'] = 2
    if change == 'schedule': s.env['KAKEIBO_SCHEDULE_ENABLED'] = 'true'
    if change == 'rerun': s.env['GITHUB_RUN_ATTEMPT'] = '2'
    if change == 'wrong_actor': s.env['GITHUB_ACTOR'] = 'outsider'
    if change == 'moved_main': s.data['branches/main']['commit']['sha'] = 'f' * 40
    if change == 'wrong_failed_attempt': s.data['actions/runs/' + str(r.FAILED_RUN)]['run_attempt'] = 2
    with pytest.raises(StateError): s.run()


def test_workflow_cannot_trigger_or_dispatch_accounting():
    repo = Path(__file__).resolve().parents[1]
    definition = yaml.load((repo / r.WORKFLOW).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert set(definition['on']) == {'workflow_dispatch'}
    assert definition['concurrency'] == {'group': 'kakeibo-production', 'cancel-in-progress': 'false', 'queue': 'max'}
    assert definition['permissions'] == {'contents': 'read', 'actions': 'read'}
    assert definition['on']['workflow_dispatch']['inputs']['mode']['default'] == 'dry_run'
    steps = definition['jobs']['recover']['steps']
    commands = '\n'.join(step.get('run', '') for step in steps)
    assert 'python -m app.bank_attempt_recovery' in commands
    assert 'production_flow' not in commands and 'dispatches' not in commands and 'gh ' not in commands
    stopped = r.stopped_workflows(repo)
    assert '.github/workflows/kakeibo-production.yml' in stopped
    assert '.github/workflows/manual-entry.yml' in stopped
    assert '.github/workflows/monthly-maintenance.yml' in stopped
    assert r.WORKFLOW not in stopped
    import ast
    tree = ast.parse((repo / 'app/bank_attempt_recovery.py').read_text(encoding='utf-8'))
    names = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    methods = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not {'run_durable_source', 'execute_serial', 'run_bank_pdf_recurring', '_mark_processed', 'move_processed'} & names
    assert not {'append_raw', 'batchUpdate', 'commit', 'begin', 'dispatch'} & methods


@pytest.fixture
def observer_rig(rig, monkeypatch, tmp_path):
    s = rig
    monkeypatch.setattr(r.tempfile, 'tempdir', str(tmp_path))
    ids = s.record['transaction_ids']
    partitions = [ids[:110], ids[110:220], ids[220:330], ids[330:440], ids[440:] + ids[:50]]
    parsed_by_index, metadata, contents = {}, {}, {}
    s.record['pdfs'] = []
    for number, selected in enumerate(partitions):
        fid = f'synthetic-pdf-{number}'
        meta = dict(id=fid, mimeType='application/pdf', parents=['synthetic-inbox'], version='1',
                    appProperties={}, modifiedTime='2026-10-01T00:00:00Z')
        txs = [NormalizedBankTransaction(source='千葉銀行PDF', account_alias='synthetic',
            transaction_date='2026-10-01', description='synthetic', signed_amount=-100,
            source_page=1, source_row=i + 1, source_row_identity=identity,
            source_row_hash=r.sha(identity.encode()), transaction_kind='withdrawal') for i, identity in enumerate(selected)]
        contents[fid] = f'NOT-A-REAL-PDF-{number}'.encode()
        metadata[fid] = meta
        parsed_by_index[number] = SimpleNamespace(transactions=txs, issues=[], balance_consistency_failures=[])
        s.record['pdfs'].append(dict(metadata=deepcopy(meta), sha256=r.sha(contents[fid]), account_alias='synthetic',
            transactions_sha256=r.sha(r.encode([asdict(tx) for tx in txs]))))
    s.pin()
    class Request:
        def __init__(self, value): self.value = value
        def execute(self, **kwargs): return deepcopy(self.value)
    class Drive:
        def files(self): return self
        def list(self, **kwargs): return Request({'files': [{'id': i} for i in metadata]})
        def get(self, fileId, **kwargs): return Request(metadata[fileId])
        def get_media(self, fileId, **kwargs): return Request(contents[fileId])
        def update(self, **kwargs): raise AssertionError('PDF update forbidden')
    class Sheets:
        def spreadsheets(self): return self
        def values(self): return self
        def batchGet(self, **kwargs):
            return Request({'valueRanges': [{'values': s.raw[name]} for name in r.TABLES]})
        def append(self, **kwargs): raise AssertionError('financial append forbidden')
    monkeypatch.setattr('app.bank_pdf_pipeline.BankPdfPipeline.parse',
        lambda self, path, **kwargs: parsed_by_index[int(Path(path).stem)])
    def observe(): r.observe(s.record, Drive(), Sheets())
    return SimpleNamespace(**locals())


def test_pdf5_parser497_and_financial_observer_is_read_only(observer_rig):
    observer_rig.observe()


@pytest.mark.parametrize('mutation', ['metadata', 'content', 'missing_pdf', 'parsed_IDs', 'financial', 'new6_posting'])
def test_observer_rejects_each_persistent_mismatch(observer_rig, mutation):
    o = observer_rig
    if mutation == 'metadata': o.metadata['synthetic-pdf-0']['version'] = '2'
    if mutation == 'content': o.contents['synthetic-pdf-0'] = b'changed'
    if mutation == 'missing_pdf': del o.metadata['synthetic-pdf-0']
    if mutation == 'parsed_IDs': o.parsed_by_index[0].transactions = o.parsed_by_index[0].transactions[:-1]
    if mutation == 'financial': o.s.raw[r.TABLES[0]][1][2] = 'changed'
    if mutation == 'new6_posting':
        identity = o.s.record['unimported6_ids'][0]
        o.s.raw[r.TABLES[1]].append(['I-' + r.sha(identity.encode())[:24]])
    with pytest.raises(StateError): o.s.run(observation=o.observe)
    assert not writes(o.s)


@pytest.mark.parametrize('mutation', ['history_count', 'checkpoint', 'pending_append'])
def test_sqlite_semantic_mismatch_rejected_even_with_matching_bytes_hash(rig, mutation):
    s = rig
    bank = json.loads(s.before[r.SOURCES[0]])
    con = sqlite3.connect(':memory:')
    con.deserialize(base64.b64decode(bank['files']['bank-recurring.sqlite3']['data']))
    if mutation == 'history_count': con.execute('DELETE FROM recurring_runs WHERE run_id="31"')
    if mutation == 'checkpoint': con.execute('UPDATE recurring_checkpoint SET successful_window_end="2026-10-22T08:08:44+09:00"')
    if mutation == 'pending_append':
        con.execute('UPDATE recurring_runs SET summary_json=? WHERE run_id="31"',
                    (json.dumps({'status':'income_append_pending','income_attempt':'synthetic-unknown'}),))
    con.commit()
    native = con.serialize()
    con.close()
    bank['files']['bank-recurring.sqlite3'] = dict(data=base64.b64encode(native).decode(), sha256=r.sha(native))
    s.record['sqlite_sha256'] = r.sha(native)
    with pytest.raises(StateError): r.verify_sqlite(bank, s.record)


def test_capsule_signature_fixed_record_and_ciphertext_binding(rig):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import os
    s = rig
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    att = {'synthetic': True}
    signature = key.sign(r.encode(att), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    value = {'record': base64.b64encode(r.encode(s.record)).decode(), 'attestation':att,
             'signature':base64.b64encode(signature).decode()}
    def seal(value):
        import zlib
        aes, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        result = dict(key=key.public_key().encrypt(aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=r.LABEL)),
                      nonce=nonce, data=AESGCM(aes).encrypt(nonce, zlib.compress(r.encode(value)), r.LABEL))
        return base64.b64encode(r.encode({k:base64.b64encode(v).decode() for k,v in result.items()})).decode()
    assert r.decode_capsule(seal(value), key) == (s.record, att)
    value['attestation'] = {'synthetic':False}
    with pytest.raises(Exception): r.decode_capsule(seal(value), key)
    bad = seal(value)
    outer = json.loads(base64.b64decode(bad))
    ciphertext = bytearray(base64.b64decode(outer['data']))
    ciphertext[0] ^= 1
    outer['data'] = base64.b64encode(ciphertext).decode()
    with pytest.raises(Exception): r.decode_capsule(base64.b64encode(r.encode(outer)).decode(), key)


def test_main_fails_closed_without_printing_private_exception(monkeypatch, capsys):
    def refused(env): raise RuntimeError('synthetic-private-financial-body')
    monkeypatch.setattr(r, 'boundary', refused)
    with pytest.raises(SystemExit): r.main()
    output = capsys.readouterr().out
    assert 'synthetic-private-financial-body' not in output
    report = json.loads(output)
    assert report['success'] is False and report['bank_write_attempts'] == report['parent_write_attempts'] == 0
