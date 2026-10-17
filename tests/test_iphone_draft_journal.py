from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from threading import Barrier
import uuid
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.drive_run_state import StateError
from app.iphone_draft_journal import LocalJournal, exclusive, quarantine, save_current
from app.iphone_sheet_reader import validate_snapshot, regeneration_preview, RawReader
from test_iphone_sheet_reader import setup


def fixture(tmp_path):
    raw, contract, sources = setup(); batch = validate_snapshot(contract, raw, sources)
    journal = LocalJournal(tmp_path/'drafts.json', batch.contract_digest)
    _, tag = journal.initialize()
    return journal, batch, tag


def test_persistent_reload_cas_replay_and_readonly_recovery(tmp_path):
    journal, batch, tag = fixture(tmp_path); rid = str(uuid.uuid4())
    saved = journal.save(batch, tag, rid)
    reloaded = LocalJournal(journal.path, batch.contract_digest)
    state, next_tag = reloaded.read_versioned()
    assert state['revision'] == 1 and next_tag != tag
    assert state['batch'] == asdict(batch)
    assert reloaded.save(batch, tag, rid)['replay'] is True
    before = journal.path.read_bytes()
    assert reloaded.recover(rid, batch, tag)['is_current'] is True
    assert journal.path.read_bytes() == before
    assert not saved['authorized'] and not saved['operation_targets']
    assert regeneration_preview(batch, state['batch'])['drafts'] == batch.drafts


@pytest.mark.parametrize('attack', ['stale','weak','missing','replay_payload','request_id','contract','authorized','digest'])
def test_journal_rejects_stale_foreign_or_ambiguous_inputs_without_rewrite(tmp_path, attack):
    journal, batch, tag = fixture(tmp_path); rid = str(uuid.uuid4()); journal.save(batch, tag, rid)
    before = journal.path.read_bytes(); other = deepcopy(batch)
    current = journal.read_versioned()[1]
    if attack == 'stale': args = (batch, tag, str(uuid.uuid4()))
    elif attack == 'weak': args = (batch, 'W/'+current, str(uuid.uuid4()))
    elif attack == 'missing': args = (batch, None, str(uuid.uuid4()))
    elif attack == 'replay_payload': args = (batch, current, rid)
    elif attack == 'request_id': args = (batch, current, 'not-an-id')
    else:
        draft = next(iter(other.drafts.values()))
        if attack == 'contract': draft['identity']['source_version'] = 'new'
        elif attack == 'authorized': draft['authorized'] = True
        else: draft['input_snapshot_digest'] = 'a'*64
        args = (other, current, str(uuid.uuid4()))
    with pytest.raises(StateError): journal.save(*args)
    assert journal.path.read_bytes() == before


def test_two_concurrent_writers_with_same_cas_commit_only_one(tmp_path):
    journal, batch, tag = fixture(tmp_path); barrier = Barrier(2)
    def write(_):
        barrier.wait()
        try:
            journal.save(batch, tag, str(uuid.uuid4())); return 'saved'
        except StateError as error:
            assert str(error) in {'iphone_journal_busy','iphone_journal_cas_conflict'}
            return 'stopped'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == ['saved','stopped']
    assert journal.read_versioned()[0]['revision'] == 1


@pytest.mark.parametrize('when', ['before_replace','after_replace'])
def test_disconnection_unknown_outcome_requires_readonly_lookup(tmp_path, monkeypatch, when):
    journal, batch, tag = fixture(tmp_path); rid = str(uuid.uuid4()); original = journal._replace
    def interrupted(value):
        if when == 'after_replace': original(value)
        raise StateError('simulated_response_lost')
    monkeypatch.setattr(journal, '_replace', interrupted)
    with pytest.raises(StateError): journal.save(batch, tag, rid)
    before = journal.path.read_bytes()
    if when == 'after_replace':
        assert journal.recover(rid, batch, tag)['status'] == 'draft_save_observed'
    else:
        with pytest.raises(StateError, match='recovery_unresolved'): journal.recover(rid, batch, tag)
    assert journal.path.read_bytes() == before


def test_os_lock_is_not_stolen_or_deleted_and_released_lockfile_is_reusable(tmp_path):
    journal, batch, tag = fixture(tmp_path)
    with exclusive(journal.lock_path):
        with pytest.raises(StateError, match='busy'): journal.save(batch, tag, str(uuid.uuid4()))
    assert journal.lock_path.exists()
    assert journal.save(batch, tag, str(uuid.uuid4()))['status'] == 'draft_saved'


@pytest.mark.parametrize('corruption', ['json','contract','revision','schema','batch'])
def test_corruption_never_initializes_or_repairs_existing_journal(tmp_path, corruption):
    journal, batch, tag = fixture(tmp_path)
    state, _ = journal.read_versioned()
    if corruption == 'json': journal.path.write_text('{interrupted', encoding='utf-8')
    else:
        if corruption == 'contract': state['contract_digest'] = 'foreign'
        elif corruption == 'revision': state['revision'] = 5
        elif corruption == 'schema': state['schema'] = 'old'
        else: state['batch'] = {'foreign':'batch'}
        journal.path.write_text(json.dumps(state), encoding='utf-8')
    before = journal.path.read_bytes()
    with pytest.raises(StateError): journal.read_versioned()
    with pytest.raises(StateError): journal.initialize()
    assert journal.path.read_bytes() == before


def test_quarantine_preserves_untrusted_snapshot_without_adopting_journal(tmp_path):
    journal, batch, tag = fixture(tmp_path); before = journal.path.read_bytes()
    raw, _, _ = setup(); raw['sheets'][0]['data'][0]['rowData'].reverse()
    path = quarantine(tmp_path, raw, 'case_order_or_identity_changed')
    record = json.loads(path.read_text())
    assert record['snapshot'] == raw and record['adopted'] is False
    assert journal.path.read_bytes() == before


def test_stale_sheet_snapshot_never_replaces_saved_draft_even_with_current_cas(tmp_path):
    journal, batch, tag = fixture(tmp_path)
    raw, contract, sources = setup()
    raw['sheets'][0]['data'][0]['rowData'][1]['values'][12]['userEnteredValue'] = {'stringValue':'new unsent memo'}
    reader = RawReader(contract, lambda:deepcopy(raw), lambda:deepcopy(sources))
    before = journal.path.read_bytes()
    with pytest.raises(StateError, match='stale_sheet_snapshot'):
        save_current(reader, journal, batch.snapshot_digest, tag, str(uuid.uuid4()))
    assert journal.path.read_bytes() == before


def test_later_saved_input_does_not_make_old_request_replay_current(tmp_path):
    journal, batch, tag = fixture(tmp_path); rid = str(uuid.uuid4())
    first = journal.save(batch, tag, rid)
    raw, contract, sources = setup()
    raw['sheets'][0]['data'][0]['rowData'][1]['values'][12]['userEnteredValue'] = {'stringValue':'later draft'}
    later = validate_snapshot(contract,raw,sources)
    journal.save(later, first['current_tag'], str(uuid.uuid4()))
    before = journal.path.read_bytes()
    assert journal.save(batch,tag,rid)['is_current'] is False
    assert journal.recover(rid,batch,tag)['is_current'] is False
    assert journal.path.read_bytes() == before


def child_env():
    # No inherited Google/Cloud/application credentials, tokens or host config.
    return {**{k:v for k,v in os.environ.items() if k in {'SystemRoot','WINDIR','TEMP','TMP'}},
            'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONUTF8':'1'}


@pytest.mark.parametrize('after_replace',[False,True])
def test_process_exit_and_restart_recover_exact_request_without_resubmit(tmp_path,after_replace):
    journal,batch,tag = fixture(tmp_path); rid = str(uuid.uuid4())
    args = tmp_path/'child-args.json'
    args.write_text(json.dumps(dict(batch=asdict(batch),tag=tag,rid=rid)),encoding='utf-8')
    code = '''
import json,os,sys
from dataclasses import dataclass
from app.iphone_draft_journal import LocalJournal
from app.iphone_sheet_reader import DraftBatch
v=json.load(open(sys.argv[2])); b=DraftBatch(**v['batch']); j=LocalJournal(sys.argv[1],b.contract_digest)
original=j._replace
def crash(value):
 if sys.argv[3]=='yes':original(value)
 os._exit(74)
j._replace=crash
j.save(b,v['tag'],v['rid'])
'''
    child = subprocess.run([sys.executable,'-c',code,str(journal.path),str(args),'yes' if after_replace else 'no'],
                           capture_output=True,env=child_env(),timeout=15)
    assert child.returncode == 74, child.stderr.decode(errors='replace')
    reopened=LocalJournal(journal.path,batch.contract_digest); before=journal.path.read_bytes()
    if after_replace:
        assert reopened.recover(rid,batch,tag)['is_current'] is True
        assert reopened.read_versioned()[0]['revision']==1
    else:
        with pytest.raises(StateError,match='recovery_unresolved'):reopened.recover(rid,batch,tag)
        assert reopened.read_versioned()[0]['revision']==0
    assert journal.path.read_bytes()==before


def test_os_lock_is_released_on_process_death_without_stale_lock_steal(tmp_path):
    journal,batch,tag=fixture(tmp_path)
    code='''
import sys,time
from app.iphone_draft_journal import exclusive
with exclusive(sys.argv[1]):
 print('locked',flush=True)
 time.sleep(15)
'''
    child=subprocess.Popen([sys.executable,'-c',code,str(journal.lock_path)],stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE,env=child_env())
    try:
        assert child.stdout.readline().strip()==b'locked'
        with pytest.raises(StateError,match='busy'):journal.save(batch,tag,str(uuid.uuid4()))
    finally:
        child.kill(); child.communicate(timeout=5)
    assert journal.save(batch,tag,str(uuid.uuid4()))['is_current'] is True


@pytest.mark.parametrize('corruption',['latest_receipt','repeated_revision','missing_batch','zero_revision_batch'])
def test_committed_batch_and_receipt_chain_must_correspond(tmp_path,corruption):
    journal,batch,tag=fixture(tmp_path)
    a=journal.save(batch,tag,str(uuid.uuid4()))
    journal.save(batch,a['current_tag'],str(uuid.uuid4()))
    state,_=journal.read_versioned(); latest=max(state['requests'].values(),key=lambda r:r['revision'])
    if corruption=='latest_receipt':latest['batch_digest']='0'*64
    elif corruption=='repeated_revision':latest['revision']=1
    elif corruption=='missing_batch':state['batch']=None
    else:state.update(revision=0,requests={})
    journal.path.write_text(json.dumps(state),encoding='utf-8'); before=journal.path.read_bytes()
    with pytest.raises(StateError):journal.read_versioned()
    assert journal.path.read_bytes()==before


def test_atomic_replace_error_preserves_original_and_leaves_request_unresolved(tmp_path,monkeypatch):
    import app.iphone_draft_journal as module
    journal,batch,tag=fixture(tmp_path); before=journal.path.read_bytes(); rid=str(uuid.uuid4())
    def denied(*_):raise OSError('synthetic disk error')
    monkeypatch.setattr(module.os,'replace',denied)
    with pytest.raises(StateError,match='save_outcome_unknown'):journal.save(batch,tag,rid)
    with pytest.raises(StateError,match='recovery_unresolved'):journal.recover(rid,batch,tag)
    assert journal.path.read_bytes()==before
    assert not list(tmp_path.glob('.draft-*'))
