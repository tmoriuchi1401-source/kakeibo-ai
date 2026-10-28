"""Synthetic only: production data/proofs are never loaded, network is blocked."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import bank_partial_recovery as partial
from app import bank_fixed_backfill as fixed
from app.bank_income import deposit_decisions, INCOME_HEADERS
from app.canonical_import import materialize_import_row
from app.drive_run_state import StateBinding, snapshot, envelope, StateError
from app.production_ledger import initial_ledger, _encode
from app.sheets import HEADERS
from test_bank_fixed_backfill import scenario, hundred_case, permission, RULES, NOW, DB, workspace_temp_directory


class LiteralDB(DB):
    def get_raw(self, rng):
        self.reads.append(('raw', rng))
        name, span = rng.split('!')
        name = name.strip("'")
        rows = deepcopy(self.data[name])
        headers = INCOME_HEADERS if name == '収入明細' else HEADERS[name] if name != '_手入力受付' else ['a'] * 6
        if span in {'A1:L1', 'A1:J1', 'A1:M1'}:
            return [headers]
        return rows if span.startswith('A2:') else [headers] + rows

    def get_raw_ranges(self, ranges):
        before = len(self.reads)
        result = [self.get_raw(rng) for rng in ranges]
        del self.reads[before:]
        self.reads.append(('batch_raw', tuple(ranges)))
        return result

    def get(self, rng):
        rows = self.get_raw(rng)
        name, span = rng.split('!')
        name = name.strip("'")
        if name in fixed.RANGES and span.startswith('A2:'):
            col = {'取込データ': 6, '収入明細': 2, '支出明細': 4}[name]
            for row in rows:
                row[col] = format(row[col], ',')
        return rows


class Transport:
    def __init__(self, source, payload, calls):
        self.source, self.payload, self.calls = source, payload, calls
        self.failure = ''
    def read(self):
        self.calls.append(('read', self.source))
        return self.payload
    def write(self, payload):
        self.calls.append(('write', self.source))
        if self.failure == 'before':
            raise ConnectionError('private-message')
        self.payload = payload
        if self.failure == 'after':
            raise ConnectionError('private-message')


@pytest.fixture
def rig(scenario, monkeypatch):
    s = scenario
    s.db = LiteralDB()
    s.db.data['_手入力受付'] = [['unchanged-request', '', '', '', '', '']]
    hundred_case(s)
    amounts = iter([100, 200, 300, 400, 500, 3169])
    for identity, item in s.plan['items'].items():
        if item['disposition'] == 'income':
            tx = replace(s.txs[identity], signed_amount=next(amounts))
            s.txs[identity] = tx
            item['transaction'] = asdict(tx)
    s.grant = permission(s.plan, s.policy)
    selected = s.plan['batches'][0]
    imports, incomes = {}, {}
    for identity in selected:
        tx = s.txs[identity]
        kind = s.plan['items'][identity]['disposition']
        imports[identity] = materialize_import_row(tx.to_canonical(), imported_at=NOW,
            status='bank_income' if kind == 'income' else 'bank_non_expense')
        if kind == 'income':
            row = deposit_decisions([tx])[0][0].row()
            incomes[row[0]] = row
    incomes = dict(sorted(incomes.items()))
    s.db.data['取込データ'].extend(deepcopy(list(imports.values())))
    bindings = {source: StateBinding(source, s.db.sid, 'folder', source + '-file') for source in partial.SOURCES}
    files = snapshot(s.state.path.parent, bindings['bank_pdf_drive'])
    before = {'bank_pdf_drive': envelope(bindings['bank_pdf_drive'], files, generation=47, phase='pending', attempt='a' * 32)}
    ledger = json.loads(initial_ledger(bindings['production_run']))
    ledger['generation'] = 1293
    ledger['sources']['bank'].update(phase='pending', attempt='b' * 32, error='source_execution_failed')
    before['production_run'] = _encode(ledger)
    ledger['generation'] = 1294
    ledger['sources']['bank'].update(phase='ready', attempt='', error='')
    after = dict(bank_pdf_drive=envelope(bindings['bank_pdf_drive'], files, generation=48), production_run=_encode(ledger))
    record = dict(kind='fixed_bank_partial_100_reconciliation', run_id=partial.FAILED_RUN, run_attempt=1,
        plan_sha256=fixed.digest(s.plan), financial_apply_authorized_by_this_record=False,
        failed_main_sha=partial.FAILED_HEAD, import_origin_run=partial.IMPORT_ORIGIN_RUN,
        origin_record_sha256=partial.ORIGIN_RECORD_SHA256, income_append_request_reached=False,
        read_failure_http_status=429, unresolved_material_write_results=[],
        remote_sqlite_intents_requiring_resolution=[], sqlite_and_all_native_files_required_unchanged=files,
        checkpoint_before_and_required_after=[], states={})
    for source, generation in [('bank_pdf_drive', 47), ('production_run', 1293)]:
        record['states'][source] = dict(spreadsheet_id=s.db.sid, folder_id='folder', file_id=bindings[source].file_id,
            generation_before=generation, generation_required_after=generation + 1,
            observed_raw_sha256=partial.sha(before[source]), expected_after_raw_sha256=partial.sha(after[source]),
            attempt=('a' if source == 'bank_pdf_drive' else 'b') * 32)
    financial = fixed.snapshot(s.db)
    completed = deepcopy(financial)
    completed['収入明細'].update(incomes)
    raw = partial.raw_tables(s.db)
    raw_after = deepcopy(raw)
    raw_after['収入明細'].extend(incomes.values())
    manifest = dict(selected100=selected, imported_at=next(iter(imports.values()))[1],
        imports_sha256=partial.sha(partial.encode(imports)), incomes=incomes,
        financial_before_sha256=partial.sha(partial.encode(financial)),
        financial_after_sha256=partial.sha(partial.encode(completed)),
        raw_tables_before_sha256=partial.sha(partial.encode(raw)), raw_tables_after_sha256=partial.sha(partial.encode(raw_after)))
    calls = []
    transports = {source: Transport(source, value, calls) for source, value in before.items()}
    report = {}
    def seal():
        monkeypatch.setattr(partial, 'RECORD_SHA256', partial.sha(partial.encode(record)))
        monkeypatch.setattr(partial, 'MANIFEST_SHA256', partial.sha(partial.encode(manifest)))
        monkeypatch.setattr(partial, 'PLAN_SHA256', fixed.digest(s.plan))
    seal()
    monkeypatch.setattr(fixed, 'validate_live', lambda *a, **k: (fixed.snapshot(s.db), s.txs))
    def execute(apply=True, batch=0):
        return partial.execute(s.plan, batch, db=s.db, state=s.state, policy=s.policy,
            pdf_bytes=lambda _: b'pdf', rules=RULES, resolver=s.resolver, now=NOW, apply=apply,
            grant=s.grant, context=(record, manifest, lambda: calls.append(('guard',))))
    def release(operation):
        return partial.release(operation, record, manifest, s.plan, transports, bindings,
            lambda: calls.append(('guard',)), lambda: calls.append(('observation',)), report)
    return SimpleNamespace(**locals())


def test_partial100_only_six_income_writes_4669_and_replay_zero_writes(rig):
    r = rig
    before = deepcopy(r.s.db.data)
    preview = r.execute(False)
    assert preview['written'] == 0 and not r.s.db.calls
    result = r.execute()
    assert result['written'] == 6 and result['income_yen'] == 4669
    assert result['imports_skipped'] == 100 and result['deposit_imports_created'] == 0
    assert r.s.db.data['取込データ'] == before['取込データ']
    assert r.s.db.data['取込データ'][:185] == before['取込データ'][:185]
    assert r.s.db.data['支出明細'] == before['支出明細']
    assert r.s.db.data['_手入力受付'] == before['_手入力受付']
    assert len(r.s.db.calls) == 1 and r.s.db.calls[0][1] == '収入明細'
    assert sum(row[2] for row in r.s.db.data['収入明細']) == 4669
    state_before = r.s.state.path.read_bytes()
    assert r.execute()['written'] == 0
    assert r.s.state.path.read_bytes() == state_before and len(r.s.db.calls) == 1
    assert r.s.state.successful_window_end() is None


@pytest.mark.parametrize('column', range(12))
def test_any_import_column_change_refuses_before_income_write(rig, column):
    r = rig
    row = next(row for row in r.s.db.data['取込データ'] if row[0] in r.imports)
    row[column] = row[column] + 1 if column == 6 else str(row[column]) + 'changed'
    with pytest.raises((StateError, ValueError, RuntimeError)):
        r.execute()
    assert not r.s.db.calls


@pytest.mark.parametrize('change', ['missing_import', 'extra_import', 'protected185', 'extra_income', 'expense', 'manual', 'batch'])
def test_invalid_partial_states_or_other_data_changes_refuse(rig, change):
    r = rig
    data = r.s.db.data
    if change == 'missing_import':
        data['取込データ'].pop()
    elif change == 'extra_import':
        row = deepcopy(data['取込データ'][-1]); row[0] = 'outside'; data['取込データ'].append(row)
    elif change == 'protected185':
        data['取込データ'][0][5] = 'changed'
    elif change == 'extra_income':
        data['収入明細'].append(deepcopy(next(iter(r.incomes.values()))))
    elif change == 'expense':
        data['支出明細'].append(['outside', '', '', '', 1] + [''] * 8)
    elif change == 'manual':
        data['_手入力受付'][0][1] = 'changed'
    with pytest.raises((StateError, ValueError, RuntimeError)):
        r.execute(batch=1 if change == 'batch' else 0)
    assert not r.s.db.calls


def test_income_unknown_never_resends_or_records_success(rig):
    r = rig
    r.s.db.fail_income = True
    with pytest.raises(ConnectionError):
        r.execute()
    assert len(r.s.db.calls) == 1
    with pytest.raises(StateError):
        r.execute()
    assert len(r.s.db.calls) == 1


def test_income_readback_mismatch_never_retries(rig, monkeypatch):
    r = rig
    original = r.s.db.append_raw
    def corrupt(sheet, rows):
        original(sheet, rows)
        r.s.db.data[sheet][-1][2] += 1
    monkeypatch.setattr(r.s.db, 'append_raw', corrupt)
    with pytest.raises(RuntimeError):
        r.execute()
    with pytest.raises(StateError):
        r.execute()
    assert len(r.s.db.calls) == 1


def test_no_parent_release_before_bank_and_explicit_stop_after_each(rig):
    r = rig
    with pytest.raises(StateError):
        r.release('release_parent')
    assert not [c for c in r.calls if c[0] == 'write']
    r.release('release_bank')
    assert json.loads(r.transports['bank_pdf_drive'].payload)['generation'] == 48
    assert r.transports['bank_pdf_drive'].payload == r.after['bank_pdf_drive']
    assert r.transports['production_run'].payload == r.before['production_run']
    assert [c[1] for c in r.calls if c[0] == 'write'] == ['bank_pdf_drive']
    r.release('release_parent')
    assert json.loads(r.transports['production_run'].payload)['generation'] == 1294
    assert r.transports['production_run'].payload == r.after['production_run']
    assert r.report['outcome'] == 'readback_verified_stop'
    for operation in ['release_bank', 'release_parent']:
        with pytest.raises(StateError):
            r.release(operation)
    assert len([c for c in r.calls if c[0] == 'write']) == 2


@pytest.mark.parametrize('change', ['old_generation', 'old_run', 'wrong_origin', 'append_unknown', 'unknown_result'])
def test_new_quota_case_rejects_old_or_ambiguous_proof_before_any_release(rig, change):
    r = rig
    if change == 'old_generation': r.record['states']['bank_pdf_drive']['generation_before'] = 45
    if change == 'old_run': r.record['run_id'] = 40590186305
    if change == 'wrong_origin': r.record['origin_record_sha256'] = '0' * 64
    if change == 'append_unknown': r.record['income_append_request_reached'] = None
    if change == 'unknown_result': r.record['unresolved_material_write_results'] = ['unknown']
    r.seal()
    with pytest.raises(StateError): r.release('release_bank')
    assert not [c for c in r.calls if c[0] == 'write'] and not r.s.db.calls


@pytest.mark.parametrize('clears', [False, True])
def test_quota_during_fresh_append_guard_recovers_or_stops_without_write_retry(rig, monkeypatch, clears):
    from test_bank_read_quota import Clock, paced_db, quota
    r = rig
    db = paced_db(Clock())
    original = r.s.db.get
    faults = [0]
    guard_done = [False]
    def guarded_read(rng):
        def execute():
            # Only the final, fresh target duplicate check in FixedWriteDB.
            if rng == '収入明細!A2:J' and guard_done[0]:
                faults[0] += 1
                if not clears or faults[0] <= 2: raise quota()
            return original(rng)
        return db._execute_sheet_read(lambda: SimpleNamespace(execute=execute))
    # Trigger only after the guarded writer's safety checks, not preview reads.
    real_append = fixed.FixedWriteDB.append_raw
    def append(target, sheet, rows):
        original_guard = target.guard
        def guard():
            original_guard()
            guard_done[0] = True
        target.guard = guard
        monkeypatch.setattr(r.s.db, 'get', guarded_read)
        return real_append(target, sheet, rows)
    monkeypatch.setattr(fixed.FixedWriteDB, 'append_raw', append)
    if clears:
        assert r.execute()['written'] == 6
        assert len(r.s.db.calls) == 1 and r.s.db.calls[0][1] == '収入明細'
        assert db.sheet_read_metrics()['retries'] == 2
    else:
        from googleapiclient.errors import HttpError
        with pytest.raises(HttpError): r.execute()
        assert not r.s.db.calls and db.sheet_read_metrics()['retries'] == 4
        monkeypatch.setattr(r.s.db, 'get', original)
        with pytest.raises(StateError): r.execute()
        assert not r.s.db.calls


@pytest.mark.parametrize('source', partial.SOURCES)
def test_both_attempts_checked_before_any_release(rig, source):
    rig.record['states'][source]['attempt'] = 'c' * 32
    rig.seal()
    with pytest.raises(StateError):
        rig.release('release_bank')
    assert not [c for c in rig.calls if c[0] == 'write']


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_ambiguous_bank_release_stops_and_never_releases_parent(rig, failure):
    rig.transports['bank_pdf_drive'].failure = failure
    with pytest.raises(ConnectionError):
        rig.release('release_bank')
    assert rig.report['outcome'] == 'attempted_unknown'
    assert [c[1] for c in rig.calls if c[0] == 'write'] == ['bank_pdf_drive']


def environment():
    return dict(GITHUB_ACTIONS='true', GITHUB_REPOSITORY=partial.REPOSITORY,
        GITHUB_ACTOR='tmoriuchi1401-source', GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',
        GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_RUN_ATTEMPT='1',
        GITHUB_SHA='a' * 40, GITHUB_WORKFLOW_SHA='a' * 40, KAKEIBO_VALIDATED_MAIN_SHA='a' * 40,
        GITHUB_WORKFLOW_REF=partial.REPOSITORY + '/' + partial.WORKFLOW + '@refs/heads/main',
        KAKEIBO_SCHEDULE_ENABLED='false', KAKEIBO_LEGACY_DISABLED='true', CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED='false')


@pytest.mark.parametrize('key,value', [('GITHUB_ACTOR', 'other'), ('GITHUB_TRIGGERING_ACTOR', 'other'),
    ('GITHUB_SHA', 'b' * 40), ('GITHUB_WORKFLOW_SHA', 'b' * 40), ('GITHUB_REF', 'refs/heads/other'),
    ('GITHUB_WORKFLOW_REF', 'other'), ('GITHUB_RUN_ATTEMPT', '2'), ('KAKEIBO_SCHEDULE_ENABLED', 'true')])
def test_boundary_rejects_wrong_owner_sha_entry_and_rerun(key, value):
    env = environment()
    partial.boundary(env, 'a' * 40)
    env[key] = value
    with pytest.raises(StateError):
        partial.boundary(env, 'a' * 40)


def test_new_workflow_is_disabled_manual_common_lock_and_no_financial_operation():
    root = Path(__file__).resolve().parents[1]
    source = (root / partial.WORKFLOW).read_text(encoding='utf-8')
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert set(workflow['on']) == {'workflow_dispatch'}
    assert workflow['concurrency'] == {'group': 'kakeibo-production', 'cancel-in-progress': 'false', 'queue': 'max'}
    assert workflow['on']['workflow_dispatch']['inputs']['operation']['options'] == ['inspect', 'release_bank', 'release_parent']
    assert "vars.KAKEIBO_BANK_PARTIAL_RECOVERY_ENABLED == 'true'" in workflow['jobs']['reconcile']['if']
    assert 'BANK_FIXED_BACKFILL_APPROVAL_JSON' not in source
    with pytest.raises(ValueError, match='disabled'):
        fixed.run_from_environment({'BANK_FIXED_BACKFILL_RECOVERY': 'private'}, mode='apply', scope='drive_bank', bank_apply=True)


def test_new_proof_does_not_accept_old_reconciliation(rig, monkeypatch):
    monkeypatch.setattr(partial, 'RECORD_SHA256', '0' * 64)
    with pytest.raises(StateError):
        rig.execute()
    assert not rig.s.db.calls


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_parent_release_unknown_preserves_recovered_bank_and_stops(rig, failure):
    rig.release('release_bank')
    rig.transports['production_run'].failure = failure
    with pytest.raises(ConnectionError):
        rig.release('release_parent')
    assert rig.transports['bank_pdf_drive'].payload == rig.after['bank_pdf_drive']
    assert [c[1] for c in rig.calls if c[0] == 'write'] == ['bank_pdf_drive', 'production_run']


def test_private_failure_never_logs_exception_or_capsule(monkeypatch, capsys):
    monkeypatch.setattr(partial.subprocess, 'check_output', lambda *a, **k: 'a' * 40)
    monkeypatch.setattr(partial, 'boundary', lambda *a: (_ for _ in ()).throw(ValueError('PRIVATE_MESSAGE')))
    monkeypatch.setenv('RECOVERY_CAPSULE', 'PRIVATE_INPUT')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    with pytest.raises(SystemExit):
        partial.main()
    text = capsys.readouterr().out
    assert 'PRIVATE' not in text and 'Traceback' not in text
    assert json.loads(text)['outcome'] == 'not_attempted'


@pytest.mark.parametrize('change', ['none', 'competing', 'expired', 'configuration', 'writer', 'cloud'])
def test_controls_bind_new_run_and_fresh_configuration(monkeypatch, change):
    import requests
    from datetime import timezone
    env = dict(environment(), GH_TOKEN='synthetic', GITHUB_RUN_ID='123')
    att = dict(at_epoch=datetime.now(timezone.utc).timestamp(), main_sha='a' * 40,
        record_sha256=partial.RECORD_SHA256, configuration_unchanged=True,
        writers=dict(KnownWriterCount=0, Tasks=[dict(State='Disabled')]))
    if change == 'expired': att['at_epoch'] -= 901
    if change == 'configuration': att['configuration_unchanged'] = False
    if change == 'writer': att['writers']['KnownWriterCount'] = 1
    def get(url, **kwargs):
        if url.endswith('branches/main'):
            value = dict(commit=dict(sha='a' * 40))
        elif url.endswith('runs/123'):
            value = dict(path=partial.WORKFLOW, head_sha='a' * 40, event='workflow_dispatch',
                run_attempt=1, status='in_progress', actor=dict(login='tmoriuchi1401-source'),
                triggering_actor=dict(login='tmoriuchi1401-source'))
        elif url.endswith('runs/' + str(partial.FAILED_RUN)):
            value = dict(status='completed', conclusion='failure', run_attempt=1, head_sha=partial.FAILED_HEAD)
        else:
            value = dict(total_count=1 if change == 'competing' else 0,
                         workflow_runs=[dict(id=999)] if change == 'competing' else [])
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)
    monkeypatch.setattr(requests.Session, 'get', lambda self, *a, **k: get(*a, **k))
    monkeypatch.setattr(requests, 'head', lambda *a, **k: SimpleNamespace(status_code=200 if change == 'cloud' else 503))
    if change == 'none':
        partial.controls(env, att)
    else:
        with pytest.raises(StateError): partial.controls(env, att)


def test_encrypted_capsule_verifies_new_pins_signature_and_ciphertext(rig, monkeypatch):
    import base64
    import os
    import zlib
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import app.settings
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.setattr(app.settings, 'service_account_source', lambda: (None, dict(private_key=pem)))
    attestation = dict(synthetic=True)
    signature = key.sign(partial.encode(attestation), padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    value = dict(record=base64.b64encode(partial.encode(rig.record)).decode(), manifest=rig.manifest,
                 attestation=attestation, signature=base64.b64encode(signature).decode())
    aes, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    wrapped = key.public_key().encrypt(aes, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
        algorithm=hashes.SHA256(), label=partial.LABEL))
    data = AESGCM(aes).encrypt(nonce, zlib.compress(partial.encode(value)), partial.LABEL)
    packet = {n: base64.b64encode(b).decode() for n, b in [('key', wrapped), ('nonce', nonce), ('data', data)]}
    text = base64.b64encode(partial.encode(packet)).decode()
    assert partial.decode_evidence(text) == (rig.record, rig.manifest, attestation)
    monkeypatch.setattr(partial, 'RECORD_SHA256', '0' * 64)
    with pytest.raises(StateError): partial.decode_evidence(text)
    rig.seal()
    packet['data'] = packet['data'][:-4] + 'AAAA'
    with pytest.raises(Exception): partial.decode_evidence(base64.b64encode(partial.encode(packet)).decode())


@pytest.mark.parametrize('verified_replay,old_state', [(False, ''), (True, ''), (False, 'bank'), (False, 'parent')])
def test_new_released_states_preview_and_verified_replay_have_no_remote_write(rig, monkeypatch, verified_replay, old_state):
    import gc
    import app.google_clients as clients
    import app.settings as settings
    import app.sheets as sheets
    import app.private_state_bindings as private
    import app.bank_recurring_authority as authority
    import app.bank_meaning_resolver as meaning
    import app.bank_income_recurring as income
    import app.drive_run_state as durable
    r = rig
    # Existing SqliteRecurringRunState keeps cyclic sqlite connection objects;
    # collect those before Windows removes a restored temporary fixture file.
    temporary_directory = fixed.tempfile.TemporaryDirectory
    class CollectedTemporaryDirectory(temporary_directory):
        def __exit__(self, *args):
            gc.collect()
            return super().__exit__(*args)
    monkeypatch.setattr(fixed.tempfile, 'TemporaryDirectory', CollectedTemporaryDirectory)
    r.s.plan['policy_sha256'] = partial.sha(b'{}')
    r.record['plan_sha256'] = fixed.digest(r.s.plan)
    r.s.grant = permission(r.s.plan, r.s.policy)
    r.seal()
    if verified_replay:
        r.execute()
    remote_calls = []
    files = snapshot(r.s.state.path.parent, r.bindings['bank_pdf_drive'])
    parent = json.loads(r.after['production_run'])
    parent['generation'] = 1296 if verified_replay else 1294
    transports = dict(bank_pdf_drive=Transport('bank', envelope(r.bindings['bank_pdf_drive'], files, generation=50 if verified_replay else 48), remote_calls),
                      production_run=Transport('parent', _encode(parent), remote_calls))
    if old_state == 'bank':
        transports['bank_pdf_drive'].payload = envelope(r.bindings['bank_pdf_drive'], files, generation=46)
    if old_state == 'parent':
        parent['generation'] = 1291
        transports['production_run'].payload = _encode(parent)
    env = dict(environment(), GITHUB_WORKFLOW_REF=partial.REPOSITORY + '/' + partial.PRODUCTION + '@refs/heads/main',
        KAKEIBO_PRODUCTION_ENABLED='true', KAKEIBO_BANK_PARTIAL_RECOVERY_ENABLED='true',
        BANK_FIXED_BACKFILL_PLAN='wrapped', BANK_FIXED_BACKFILL_BATCH='0', BANK_FIXED_BACKFILL_RECOVERY='encrypted',
        BANK_PDF_RECURRING_AUTHORITY_JSON='{}', SPREADSHEET_ID=r.s.db.sid, KAKEIBO_STATE_FOLDER_ID='folder',
        BANK_STATE_FILE_ID=r.bindings['bank_pdf_drive'].file_id,
        KAKEIBO_RUN_LEDGER_FILE_ID=r.bindings['production_run'].file_id, CONFIRM='APPLY')
    monkeypatch.setattr(fixed.subprocess if hasattr(fixed, 'subprocess') else partial.subprocess,
                        'check_output', lambda *a, **k: 'a' * 40)
    monkeypatch.setattr(income, 'require_income_actions', lambda *a: None)
    monkeypatch.setattr(partial, 'income_context', lambda *a: (r.record, r.manifest, lambda: None))
    monkeypatch.setattr(clients, 'read_only_drive_service', lambda: object())
    monkeypatch.setattr(clients, 'drive_service', lambda: object())
    monkeypatch.setattr(clients, 'read_only_sheets_service', lambda: object())
    monkeypatch.setattr(clients, 'download_drive_file', lambda *a: json.dumps(r.s.plan).encode())
    monkeypatch.setattr(settings, 'service_account_source', lambda: (None, dict(private_key='synthetic')))
    monkeypatch.setattr(settings, 'Settings', lambda: SimpleNamespace(
        bank_confirmed_internal_transfers=lambda: RULES['confirmed_internal_transfers'],
        bank_confirmed_non_own_classifications=lambda: RULES['confirmed_non_own_classifications']))
    monkeypatch.setattr(private, 'unwrap', lambda *a: 'fixed-plan')
    def production_db(*a, **kwargs):
        assert kwargs['read_pacer'].interval == 1.25
        assert kwargs['read_retry_base'] == 20 and kwargs['read_retry_attempts'] == 5
        assert 0 <= kwargs['read_retry_jitter']() <= 1
        return r.s.db
    monkeypatch.setattr(sheets, 'SheetsDB', production_db)
    monkeypatch.setattr(authority, 'ProtectedBankRecurringAuthorityProvider', lambda *a, **k: SimpleNamespace(load=lambda: r.s.policy))
    monkeypatch.setattr(meaning, 'from_environment', lambda *a, **k: r.s.resolver)
    monkeypatch.setattr(durable, 'DriveStateTransport', lambda service, binding: transports[binding.source])
    monkeypatch.setattr(fixed, 'authorize', lambda *a: r.s.grant)
    if old_state:
        with pytest.raises(ValueError, match='partial_released_states_required'):
            fixed.run_from_environment(env, mode='preview', scope='drive_bank', bank_apply=False)
        assert not r.s.db.calls and not [c for c in remote_calls if c[0] == 'write']
        return
    result = fixed.run_from_environment(env, mode='apply' if verified_replay else 'preview', scope='drive_bank', bank_apply=verified_replay)
    assert result['written'] == 0 and result['imports_skipped'] == 100
    if verified_replay:
        assert result['backfill_verified_replay'] == 1
    assert not [c for c in remote_calls if c[0] == 'write']
    assert len(r.s.db.calls) == (1 if verified_replay else 0)
