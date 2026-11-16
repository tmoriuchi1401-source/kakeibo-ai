"""Real recovery/journal/reader/cache code, synthetic GET/CAS ports only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
import socket
from threading import Barrier, RLock
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app.receipt_private_adapter import FIELDS
from app.receipt_sheet_worker import ExistingReceiptDriveIO
from test_receipt_private_adapter import encode
from app.receipt_sheet_validation import cached_result
from app.receipt_terminal_recovery import AUTH_SCHEMA, DurableRecoveryAuthority, TerminalRecovery
from test_receipt_sheet_connection import connected, accept
from test_receipt_sheet_validation import RID

SECRET = 'synthetic-private-credential-body-etag-must-not-be-logged'
APPROVAL = 'synthetic-recovery-approval'


@pytest.fixture(autouse=True)
def no_live_or_financial_io(monkeypatch):
    calls = []
    def deny(*args, **kwargs):
        calls.append(True)
        raise AssertionError('offline_recovery_only')
    for method in ('connect', 'connect_ex', 'sendto'):
        monkeypatch.setattr(socket.socket, method, deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, '__init__', deny)
    yield
    assert calls == []


class AuthorityMemory:
    """Protected durable CAS stand-in shared across authority process instances."""
    def __init__(self):
        self.value = dict(schema=AUTH_SCHEMA, generation=0, grants={})
        self.generation = self.attempts = self.writes = 0
        self.lock = RLock()
        self.faults, self.on_write = {}, None
        self.fail_read = False
        self.barrier = None
        self.preflights = 0

    def preflight(self):
        self.preflights += 1

    def read_versioned(self):
        with self.lock:
            if self.fail_read:
                self.fail_read = False
                raise TimeoutError(SECRET)
            return deepcopy(self.value), f'"authority-{self.generation}"'

    def replace_versioned(self, value, tag):
        if self.barrier:
            self.barrier.wait(timeout=5)
        with self.lock:
            self.attempts += 1
            fault = self.faults.get(self.attempts)
            if fault == 'before':
                raise TimeoutError(SECRET)
            if tag != f'"authority-{self.generation}"':
                raise StateError('HTTP_412')
            self.value = deepcopy(value)
            self.generation += 1
            self.writes += 1
            if self.on_write:
                self.on_write(self.attempts)
            if fault == 'after':
                raise TimeoutError(SECRET)
            if fault == 'readback':
                self.fail_read = True


class Controls:
    def __init__(self):
        self.gets = 0
        self.value = dict(
            original_run=dict(id='900001', request_id=RID, sha='a'*40, status='completed',
                              attempt=1, stop_stage='claim_readback_failed'),
            execution_sha='b'*40, validation_enabled=False, competing_runs=0,
            competing_recoveries=0, writers_quiescent=True, cache_read_complete=True)

    def read(self):
        self.gets += 1
        return deepcopy(self.value)


def failed_claim():
    """Reproduce the fifth failure shape, not the actual fifth case or UUID.

    CAS succeeds; only JSON version differs in outer readback metadata.
    The old adapter remains permanently write-stopped after GET reconciliation.
    """
    c, w, p, io = connected()
    accept(c, w)
    replace = io.replace_versioned
    changed = [0]
    def version_race(fid, value):
        if fid == 'synthetic-private':
            changed[0] += 1
            if changed[0] == 2:
                value['version'] = str(int(value['version']) + 1)
    def claim(before, tag, after):
        replace(before, tag, after)
        io.meta_change = version_race
    io.replace_versioned = claim
    output = c.run(RID)
    io.replace_versioned = replace
    io.meta_change = None
    assert output['record']['state'] == '結果不明・照合待ち'
    assert io.attempts == io.writes == 3 and p.append_calls == 0 and c.private._stopped
    return c, w, p, io


def setup(approve=True):
    c, w, p, io = failed_claim()
    controls, store = Controls(), AuthorityMemory()
    authority = DurableRecoveryAuthority(store, preflight=store.preflight, clock=lambda: 100)
    recovery = TerminalRecovery(c, authority, read_controls=controls.read)
    proposal = recovery.inspect(RID, generation=3)
    if approve:
        # Trusted issuer fixture seeds a protected store; the application has
        # NO grant issuance API and never derives this history from empty rows.
        store.value['grants'][APPROVAL] = dict(scope=deepcopy(proposal.scope), expires=200,
            consumed=False, token=None,
            append_history=dict(key=proposal.entry['key'], complete=True, attempts=0))
    return c, w, p, io, controls, store, authority, recovery, proposal


def replay(parts):
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    restarted = DurableRecoveryAuthority(store, preflight=store.preflight, clock=lambda: 100)
    return TerminalRecovery(c, restarted, read_controls=controls.read).recover(APPROVAL)


def assert_stopped(parts, outcome, generation=3, appends=0):
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    assert outcome.code == 'receipt_recovery_stopped' and not outcome.cache_verified
    assert json.loads(io.raw)['journal']['generation'] == generation
    assert p.append_calls == appends and c.private._stopped
    assert json.loads(io.raw)['journal']['requests'][RID]['snapshot'] == proposal.scope['snapshot']


def test_inspection_is_independent_get_only_and_normal_worker_still_rejects_replay(caplog):
    parts = setup(approve=False)
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    assert 'receipt_private_metadata_json_version_changed' in caplog.messages
    before = (io.raw, io.attempts, p.append_calls, deepcopy(p.rows))
    assert recovery.inspect(RID, generation=3) == proposal
    assert before == (io.raw, io.attempts, p.append_calls, p.rows)
    assert controls.gets > 1 and store.attempts == 0
    with pytest.raises(StateError, match='replay_rejected'):
        c.run(RID)
    assert_stopped(parts, recovery.recover(APPROVAL))
    assert io.attempts == 3 and store.attempts == 0


def test_success_reuses_checks_terminal_cas_and_both_acks_once(caplog):
    parts = setup()
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    before_rows = deepcopy(p.rows)
    caplog.clear()
    caplog.set_level(logging.WARNING)
    outcome = recovery.recover(APPROVAL)
    assert outcome.code == 'receipt_recovery_complete' and outcome.terminal_verified and outcome.cache_verified
    assert io.attempts == io.writes == 4 and p.append_calls == 1 and p.rows == before_rows
    assert store.attempts == store.writes == 2
    grant = store.value['grants'][APPROVAL]
    assert grant['consumed'] and grant['append_history']['attempts'] == 1
    final = c.journal.query(RID)
    assert final['state'] == proposal.scope['to_state'] and final['result'] == proposal.scope['result']
    assert final['result']['posting_authority'] is False and final['result']['financial_write_count'] == 0
    assert cached_result(p.read_results(), final) == proposal.entry
    assert 'receipt_private_adapter_readback_ok' in caplog.messages
    assert 'receipt_private_ack_readback_ok' in caplog.messages
    assert 'receipt_worker_claim_begin' not in caplog.messages
    assert_stopped(parts, replay(parts), generation=4, appends=1)
    assert io.attempts == 4 and p.append_calls == 1


@pytest.mark.parametrize('field,value', [
    ('validation_enabled',True), ('competing_runs',1), ('competing_recoveries',1),
    ('writers_quiescent',False), ('cache_read_complete',False),
])
def test_control_gets_must_prove_exclusion_and_complete_cache(field, value):
    parts = setup()
    parts[4].value[field] = value
    assert_stopped(parts, parts[7].recover(APPROVAL))
    assert parts[3].attempts == 3 and parts[5].attempts == 0


@pytest.mark.parametrize('field,value', [('status','in_progress'), ('attempt',2), ('stop_stage','unknown'), ('request_id','wrong')])
def test_original_run_evidence_is_bound_not_inferred_from_time(field, value):
    parts = setup()
    parts[4].value['original_run'][field] = value
    assert_stopped(parts, parts[7].recover(APPROVAL))
    assert parts[3].attempts == 3


@pytest.mark.parametrize('damage', ['generation','snapshot','etag','version','acl','body','cache','scope','expiry','history','prior_append','duplicate_grant'])
def test_stale_or_reusable_authority_and_checkpoint_mismatches_never_write(damage):
    parts = setup()
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    grant = store.value['grants'][APPROVAL]
    if damage == 'generation': grant['scope']['generation'] = 4
    elif damage == 'snapshot': grant['scope']['snapshot']['digest'] = 'f'*64
    elif damage == 'etag': grant['scope']['etag'] = '"' + SECRET + '"'
    elif damage == 'version':
        io.meta_change = lambda fid,m: m.update(version='99') if fid == 'synthetic-private' else None
    elif damage == 'acl': io.meta['synthetic-private']['permissions'][1]['role'] = 'reader'
    elif damage == 'body': io.raw += b'\n'
    elif damage == 'cache': p.results.append(deepcopy(proposal.entry))
    elif damage == 'scope': grant['scope']['to_state'] = '受付済み'
    elif damage == 'expiry': grant['expires'] = 99
    elif damage == 'history': grant['append_history']['complete'] = False
    elif damage == 'prior_append': grant['append_history']['attempts'] = 1
    else: store.value['grants']['another-approval'] = deepcopy(grant)
    assert_stopped(parts, recovery.recover(APPROVAL))
    assert io.attempts == 3 and store.attempts == 0


@pytest.mark.parametrize('stage', ['source','plan','recheck','after_consume'])
def test_validation_uncertainty_never_becomes_a_success_or_hold_write(monkeypatch, stage):
    parts = setup()
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    reader = c.reader
    calls = [0]
    def interrupted(case_id):
        calls[0] += 1
        if stage == 'source' or stage == 'recheck' and calls[0] == 2:
            raise TimeoutError(SECRET)
        return reader(case_id)
    c.reader = interrupted
    if stage == 'plan':
        from app import receipt_sheet_validation as validation
        def fail(*args, **kwargs): raise RuntimeError(SECRET)
        monkeypatch.setattr(validation, 'registration_plan', fail)
    if stage == 'after_consume':
        store.on_write = lambda attempt: setattr(w, 'raw', b'synthetic source changed')
    assert_stopped(parts, recovery.recover(APPROVAL))
    assert io.attempts == 3
    if stage == 'after_consume':
        assert store.value['grants'][APPROVAL]['consumed']
        assert_stopped(parts, replay(parts))


@pytest.mark.parametrize('fault,generation', [('before',3),('after',4),('adapter',4),('core',4),('after_ack',4)])
def test_terminal_cas_and_ack_faults_stop_without_cache_or_permission_reuse(fault, generation):
    parts = setup()
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    replace, read = io.replace_versioned, io.read_versioned
    post_reads = [0]
    def terminal(before, tag, after):
        if fault in ('before','after'): io.fault = fault
        replace(before, tag, after)
        if fault == 'adapter': io.fault = 'readback'
    def observed_read():
        if io.writes == 4:
            post_reads[0] += 1
            if fault == 'core' and post_reads[0] == 2 or fault == 'after_ack' and post_reads[0] == 3:
                raise TimeoutError(SECRET)
        return read()
    io.replace_versioned, io.read_versioned = terminal, observed_read
    assert_stopped(parts, recovery.recover(APPROVAL), generation=generation)
    assert io.attempts == 4 and io.writes == generation and store.writes == 1
    io.fault = None
    assert_stopped(parts, replay(parts), generation=generation)
    assert io.attempts == 4 and p.append_calls == 0


@pytest.mark.parametrize('operation', [1,2], ids=['consume','append-intent'])
@pytest.mark.parametrize('fault', ['before','after','readback'])
def test_authority_cas_ambiguity_never_advances_or_retries(operation, fault):
    parts = setup()
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    store.faults[operation] = fault
    assert_stopped(parts, recovery.recover(APPROVAL), generation=3 if operation == 1 else 4)
    assert authority._stopped and store.attempts == operation and p.append_calls == 0
    before = (io.attempts, store.attempts)
    assert_stopped(parts, recovery.recover(APPROVAL), generation=3 if operation == 1 else 4)
    assert before == (io.attempts,store.attempts)
    if store.value['grants'][APPROVAL]['consumed']:
        assert_stopped(parts, replay(parts), generation=3 if operation == 1 else 4)
        assert before == (io.attempts,store.attempts)


@pytest.mark.parametrize('committed', [False,True])
def test_cache_unknown_never_reappends_even_when_zero_rows(committed):
    parts = setup()
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    if committed:
        p.lose_response = True
    else:
        def undelivered(entry):
            p.append_calls += 1
            raise TimeoutError(SECRET)
        p.append_result = undelivered
    outcome = recovery.recover(APPROVAL)
    assert_stopped(parts, outcome, generation=4, appends=1)
    assert outcome.terminal_verified and store.value['grants'][APPROVAL]['append_history']['attempts'] == 1
    if committed:
        assert cached_result(p.read_results(),c.journal.query(RID)) == proposal.entry
    else:
        assert p.read_results() == []
    assert_stopped(parts, replay(parts), generation=4, appends=1)
    assert io.attempts == 4 and p.append_calls == 1


def test_double_recovery_consumes_one_durable_grant_across_instances():
    parts = setup()
    c, w, p, io, controls, store, authority, recovery, proposal = parts
    # Synchronize both consumption CAS requests; durable If-Match picks one.
    store.barrier = Barrier(2)
    # Append-intent must not wait at the consume barrier.
    original = store.replace_versioned
    def synchronized(value, tag):
        if value['generation'] == 2:
            store.barrier = None
        return original(value,tag)
    store.replace_versioned = synchronized
    other = TerminalRecovery(c,DurableRecoveryAuthority(store,preflight=store.preflight,clock=lambda:100),read_controls=controls.read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda r:r.recover(APPROVAL),[recovery,other]))
    assert sorted(r.code for r in results) == ['receipt_recovery_complete','receipt_recovery_stopped']
    assert io.attempts == io.writes == 4 and p.append_calls == 1
    assert store.writes == 2  # Consumption once, append intent once; no grant reset.


def test_foreign_update_after_authority_consumption_blocks_terminal_cas():
    parts = setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    store.on_write = lambda attempt: setattr(io,'generation',io.generation+1)
    assert_stopped(parts,recovery.recover(APPROVAL))
    assert io.attempts == 3 and store.writes == 1
    assert_stopped(parts,replay(parts))


def test_only_fixed_codes_no_private_values_or_exception_text(caplog):
    parts=setup()
    parts[5].faults[1]='after'
    caplog.clear();caplog.set_level(logging.WARNING)
    assert_stopped(parts,parts[7].recover(APPROVAL))
    records=[r for r in caplog.records if r.name=='app.receipt_terminal_recovery']
    allowed={'receipt_recovery_source_check_begin','receipt_recovery_plan_begin',
             'receipt_recovery_source_recheck_begin','receipt_recovery_complete','receipt_recovery_stopped'}
    assert records and all(r.msg in allowed and not r.args and not r.exc_info for r in records)
    assert SECRET not in caplog.text and RID not in caplog.text
    assert parts[-1].scope['body_sha256'] not in caplog.text


@pytest.mark.parametrize('fault', [None,'put_before','put_after','http_412','metadata_race','core_ack'])
def test_existing_http_transport_retains_if_match_and_no_retry_or_reclaim(fault, caplog):
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    trace=[]
    post=[0]
    class Session:
        def request(self,method,url,**kwargs):
            assert kwargs['allow_redirects'] is False and 0 < kwargs['timeout'] <= 20
            trace.append((method,url,deepcopy(kwargs.get('params')),deepcopy(kwargs.get('headers'))))
            if method=='PUT':
                assert url=='https://www.googleapis.com/upload/drive/v2/files/synthetic-private'
                assert kwargs['params']=={'uploadType':'media','fields':'id,etag'}
                assert kwargs['headers']=={'If-Match':'"3"','Content-Type':'application/json'}
                assert kwargs['data']==proposal.terminal_raw
                if fault=='http_412':
                    return SimpleNamespace(status_code=412,content=SECRET.encode())
                io.fault={'put_before':'before','put_after':'after'}.get(fault)
                io.replace_versioned(io.raw,kwargs['headers']['If-Match'],kwargs['data'])
                return SimpleNamespace(status_code=200,content=b'{}')
            assert method=='GET' and url.startswith('https://www.googleapis.com/drive/v2/files/')
            fid=url.rsplit('/',1)[1]
            if io.writes==4:
                post[0]+=1
                if fault=='core_ack' and post[0]==10:
                    raise TimeoutError(SECRET)
            params=kwargs['params']
            if params=={'alt':'media'}:
                raw,_=io.read_versioned()
                return SimpleNamespace(status_code=200,content=raw)
            value=io.metadata(fid,params['fields'])
            if fault=='metadata_race' and post[0]==9:
                value['version']=str(int(value['version'])+1)
            fields=params['fields']
            if fields=='id,owners(emailAddress),permissions(type,role,emailAddress,deleted)':
                value={k:value[k] for k in ('id','owners','permissions')}
            elif fields=='id,etag,parents(id),labels(trashed),mimeType':
                value={k:value[k] for k in ('id','etag','parents','labels','mimeType')}
            return SimpleNamespace(status_code=200,content=encode(value),json=lambda:deepcopy(value))
    c.private.io=ExistingReceiptDriveIO(dict(private_id=c.private.fid,folder_id=c.private.folder,
        owner_email=c.private.owner,service_email=c.private.service),Session())
    caplog.clear();caplog.set_level(logging.WARNING)
    outcome=recovery.recover(APPROVAL)
    assert sum(method=='PUT' for method,*_ in trace)==1
    assert io.attempts==(3 if fault=='http_412' else 4)
    assert io.writes==(3 if fault in ('put_before','http_412') else 4)
    if fault is None:
        assert outcome.code=='receipt_recovery_complete' and p.append_calls==1
    else:
        assert_stopped(parts,outcome,generation=3 if fault in ('put_before','http_412') else 4)
        assert not outcome.terminal_verified
    if fault=='metadata_race':
        assert 'receipt_private_metadata_json_version_changed' in caplog.messages
    if fault=='core_ack':
        assert 'receipt_private_ack_readback_failed' in caplog.messages
    before=len(trace)
    assert replay(parts).code=='receipt_recovery_stopped'
    assert len(trace)==before  # Used grant is rejected before receipt GET/PUT.


@pytest.mark.parametrize('state', ['保留','修正が必要'])
def test_successful_revalidation_can_finalize_a_non_success_result(monkeypatch,state):
    from app import receipt_sheet_validation as validation
    def plan(*args):
        return dict(state=state,issues=['synthetic_validation_issue'],snapshot_digest='f'*64,
                    posting_authority=False,financial_write_count=0)
    monkeypatch.setattr(validation,'registration_plan',plan)
    parts=setup()
    assert parts[-1].scope['to_state']==state
    assert parts[7].recover(APPROVAL).code=='receipt_recovery_complete'
    assert parts[0].journal.query(RID)['state']==state
    assert parts[2].append_calls==1 and parts[3].writes==4


@pytest.mark.parametrize('boundary', ['after_terminal_ack','after_append_intent'])
def test_process_exit_after_terminal_commit_never_restores_cache_capability(monkeypatch,boundary):
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    def crash(*args): raise SystemExit('synthetic process death')
    if boundary=='after_terminal_ack':
        monkeypatch.setattr(authority,'reserve_append',crash)
    else:
        monkeypatch.setattr(p,'append_result',crash)
    with pytest.raises(SystemExit):
        recovery.recover(APPROVAL)
    assert io.attempts==io.writes==4 and p.append_calls==0
    grant=store.value['grants'][APPROVAL]
    assert grant['consumed'] and grant['append_history']['attempts']==(0 if boundary=='after_terminal_ack' else 1)
    assert_stopped(parts,replay(parts),generation=4)
    assert io.attempts==4 and p.append_calls==0


@pytest.mark.parametrize('change', ['cache','control','journal'])
def test_changes_during_append_reservation_stop_before_post(change):
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    def changed(attempt):
        if attempt!=2: return
        if change=='cache': p.results.append(deepcopy(proposal.entry))
        elif change=='control': controls.value['writers_quiescent']=False
        else: io.generation+=1
    store.on_write=changed
    assert_stopped(parts,recovery.recover(APPROVAL),generation=4)
    assert io.attempts==io.writes==4 and p.append_calls==0
    assert store.value['grants'][APPROVAL]['append_history']['attempts']==1


def test_consumed_authority_cannot_change_target_or_gain_financial_scope():
    parts=setup()
    grant=parts[5].value['grants'][APPROVAL]
    grant['scope']['result']['posting_authority']=True
    grant['scope']['result']['financial_write_count']=1
    assert_stopped(parts,parts[7].recover(APPROVAL))
    assert parts[3].attempts==3 and parts[5].attempts==0


def test_snapshot_change_from_reader_is_rejected_before_authority_or_terminal_write():
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    original=c.reader
    def mismatch(case_id):
        value=original(case_id)
        value['request_snapshot']['operations'].append({'synthetic':'not-captured'})
        return value
    c.reader=mismatch
    assert_stopped(parts,recovery.recover(APPROVAL))
    assert io.attempts==3 and store.attempts==0


def test_competing_update_at_cas_is_412_without_any_retry():
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    original=io.replace_versioned
    def race(before,tag,after):
        io.generation+=1
        original(before,tag,after)
    io.replace_versioned=race
    assert_stopped(parts,recovery.recover(APPROVAL))
    assert io.attempts==4 and io.writes==3 and p.append_calls==0
    assert_stopped(parts,replay(parts))
    assert io.attempts==4


def test_expiry_after_consumption_does_not_authorize_late_terminal_cas():
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    store.on_write=lambda attempt:setattr(authority,'clock',lambda:201)
    assert_stopped(parts,recovery.recover(APPROVAL))
    assert io.attempts==3 and store.writes==1
    assert store.value['grants'][APPROVAL]['consumed']


def test_expiry_during_append_reservation_never_sends_the_append():
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    def expired(attempt):
        if attempt==2: authority.clock=lambda:201
    store.on_write=expired
    assert_stopped(parts,recovery.recover(APPROVAL),generation=4)
    assert io.attempts==io.writes==4 and p.append_calls==0
    assert store.value['grants'][APPROVAL]['append_history']['attempts']==1


def test_full_checkpoint_still_rejects_metadata_drift_on_repeated_get():
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    reads=[0]
    def drift(fid,value):
        if fid=='synthetic-private':
            reads[0]+=1
            if reads[0]>2:
                value['version']=str(int(value['version'])+1)
    io.meta_change=drift
    assert_stopped(parts,recovery.recover(APPROVAL))
    assert io.attempts==3 and store.attempts==0


def test_authority_store_acl_preflight_failure_cannot_consume_a_grant_or_write(caplog):
    parts=setup()
    def denied(): raise StateError(SECRET)
    parts[6].preflight=denied
    caplog.clear()
    assert_stopped(parts,parts[7].recover(APPROVAL))
    assert parts[3].attempts==3 and parts[5].attempts==0 and SECRET not in caplog.text


@pytest.mark.parametrize('version', ['4','3'],ids=['unchanged','decreased'])
def test_post_cas_version_must_increase_even_when_existing_ack_succeeds(version):
    parts=setup()
    c,w,p,io,controls,store,authority,recovery,proposal=parts
    original=io.replace_versioned
    def terminal(before,tag,after):
        original(before,tag,after)
        io.meta_change=lambda fid,m:m.update(version=version) if fid=='synthetic-private' else None
    io.replace_versioned=terminal
    outcome=recovery.recover(APPROVAL)
    assert_stopped(parts,outcome,generation=4)
    assert not outcome.terminal_verified and io.attempts==io.writes==4 and p.append_calls==0
    assert_stopped(parts,replay(parts),generation=4)
