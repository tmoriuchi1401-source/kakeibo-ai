from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from app.drive_run_state import StateError
from app.iphone_persistent_drafts import LIMIT
from offline_registration_runner import provision, OfflinePort
from live_google_sheets_probe import run
from persistent_runner_worker import Server, Slots, control
from staging_shadow.persistent_registration import (Checkpoints, DriveCheckpointSlots,
    Slot, run_persistent, encoded, MAX_CHECKPOINT)
from staging_shadow.registration_runner import Control, ConditionalControl, fingerprint, MAX_STORAGE


@pytest.fixture
def shared(tmp_path):
    home = tmp_path/'shared'; home.mkdir(); remote = home/'remote'
    provision(remote)
    run(remote, dict(op='inspect'))  # explicit offline seed, never startup repair
    scope = dict(Control(remote).scope, config_sha256=fingerprint((remote/'config.json').read_bytes()))
    (home/'private.json').write_bytes(encoded(scope))
    key = (remote/'runner.key').read_bytes(); (home/'key').write_bytes(key)
    slots = home/'slots'; slots.mkdir()
    for i in range(128):(slots/f'{i}.enc').write_bytes(b'')
    pointer = Checkpoints(Slots(slots), key, scope).save(remote, 0)
    state = Control(remote).read()[0]; state['checkpoint'] = pointer
    payload = ConditionalControl(None, key, scope).migration_payload(state)
    with sqlite3.connect(home/'server.sqlite') as db:
        db.execute('create table document(payload blob, version int, writes int, fault text)')
        db.execute('insert into document values(?,1,0,NULL)', (payload,))
    return home


def launch(shared, root, op='run', *, check=True):
    root.mkdir()
    child = subprocess.run([sys.executable,'tests/persistent_runner_worker.py',op,str(shared),str(root)],
        capture_output=True, text=True, timeout=180,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if check:assert child.returncode == 0, child.stdout+child.stderr
    return child


def state(shared, tmp_path):
    root = tmp_path/'reader'; root.mkdir(exist_ok=True)
    return control(shared, root)._read()[0]


def test_fresh_process_restart_receipts_history_and_small_cas(shared, tmp_path):
    first = tmp_path/'first'; a = json.loads(launch(shared, first).stdout)
    assert a['status'] == 'COMPLETE' and a['accounting_writes'] == a['authority_calls'] == 0
    before = (first/'checkpoint.json').read_bytes()
    second = tmp_path/'second'; b = json.loads(launch(shared, second).stdout)
    assert b['status'] in {'ALREADY_OBSERVED','UNCHANGED'} and b['backend_calls'] == 0
    assert (second/'checkpoint.json').read_bytes() == before
    observed = json.loads(launch(shared, tmp_path/'lookup', 'inspect').stdout)
    assert observed['draft_writes'] == 1 and observed['financial_writes'] == observed['authority_calls'] == 0
    from base64 import b64decode
    old = json.loads(before); new = json.loads((second/'checkpoint.json').read_bytes())
    # Complete native and delivery journals (receipts, history and details),
    # not a freshly generated empty journal, survive the independent worker.
    for k in ('journal','outer'):
        assert b64decode(new[k]['raw']) == b64decode(old[k]['raw'])
    with sqlite3.connect(shared/'server.sqlite') as db:payload, = db.execute('select payload from document').fetchone()
    assert len(payload) < LIMIT and len(payload) < 100_000
    assert state(shared,tmp_path)['checkpoint']['generation'] >= 1


def test_two_independent_environments_real_update_time_cas(shared, tmp_path):
    roots = [tmp_path/'one',tmp_path/'two']; children = []
    try:
        for root in roots:
            root.mkdir()
            children.append(subprocess.Popen([sys.executable,'tests/persistent_runner_worker.py','claim',str(shared),str(root)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)))
        deadline = time.monotonic()+30
        while not all((r/'barrier').exists() for r in roots) and time.monotonic()<deadline:time.sleep(.05)
        assert all((r/'barrier').exists() for r in roots)
        (shared/'go').touch()
        outputs = [p.communicate(timeout=30) for p in children]
        assert sorted(p.returncode for p in children) == [0,2], outputs
        assert state(shared,tmp_path)['run_id'] in {'one','two'}
        with sqlite3.connect(shared/'server.sqlite') as db:assert db.execute('select writes from document').fetchone()[0] == 1
        assert json.loads(launch(shared,tmp_path/'later').stdout)['status'] == 'UNKNOWN'
    finally:
        for p in children:
            if p.poll() is None:p.kill();p.wait(timeout=5)


@pytest.mark.parametrize('fault',['crash','before','after','corrupt','full','cas-before','cas-after'])
def test_loss_or_death_after_checkpoint_never_resends(shared, tmp_path, fault):
    marker = shared/fault if fault.startswith('cas-') else shared/'slots'/fault
    marker.touch()
    child = launch(shared,tmp_path/'faulted','change',check=False)
    assert child.returncode == (73 if fault == 'crash' else 2)
    before = state(shared,tmp_path)
    assert before['status'] == ('RUNNING' if fault == 'cas-after' else 'PENDING')
    with sqlite3.connect(shared/'server.sqlite') as db:writes = db.execute('select writes from document').fetchone()[0]
    Server(shared/'server.sqlite').set_fault(None)
    marker.unlink()
    restarted = json.loads(launch(shared,tmp_path/'new-environment').stdout)
    assert restarted['status'] == 'UNKNOWN' and restarted['backend_calls'] == 0
    assert state(shared,tmp_path) == before
    with sqlite3.connect(shared/'server.sqlite') as db:assert db.execute('select writes from document').fetchone()[0] == writes
    # CAS tip selects the acknowledged generation only, never orphan slots.
    assert before['checkpoint']['generation'] == (1 if fault == 'cas-after' else 0)


@pytest.mark.parametrize('fault',['missing','rollback','revoked','cas_missing','cas_expired'])
def test_missing_revoked_and_old_checkpoint_fail_closed(shared,tmp_path,fault):
    if fault == 'rollback':
        assert launch(shared,tmp_path/'update','change').returncode == 0
        (shared/'slots/1.enc').write_bytes((shared/'slots/0.enc').read_bytes())
    elif fault == 'missing':(shared/'slots/0.enc').unlink()
    elif fault == 'revoked':(shared/'slots/revoked').touch()
    elif fault == 'cas_missing':
        with sqlite3.connect(shared/'server.sqlite') as db:db.execute('update document set payload=?',(b'',))
    else:Server(shared/'server.sqlite').set_fault('read')
    result = launch(shared,tmp_path/'fresh',check=False)
    assert result.returncode == 2 and json.loads(result.stdout)['status'] == 'STOPPED_ERROR'
    assert not (tmp_path/'fresh/checkpoint.json').exists()


def test_unknown_attempting_preserved_no_native_retry(shared,tmp_path):
    # Change trusted synthetic fixture config only during explicit provisioning;
    # regenerate generation zero using an isolated seed before starting workers.
    root = shared/'remote'; config = json.loads((root/'config.json').read_bytes())
    config.update(scenario='native_unknown',allow_fault_injection=True)
    (root/'config.json').write_bytes(encoded(config))
    scope = json.loads((shared/'private.json').read_bytes());scope['config_sha256'] = fingerprint((root/'config.json').read_bytes())
    (shared/'private.json').write_bytes(encoded(scope));(shared/'slots/0.enc').write_bytes(b'')
    c = control(shared,tmp_path/'unused'); pointer = c.checkpoints.save(root,0)
    initial = dict(revision=0,previous=None,status='IDLE',phase=None,pending=None,checkpoint=pointer,last_slot=None,run_id=None)
    payload = c.control.migration_payload(initial)
    with sqlite3.connect(shared/'server.sqlite') as db:db.execute('update document set payload=?',(payload,))
    a = json.loads(launch(shared,tmp_path/'unknown').stdout)
    assert a['status'] == 'UNKNOWN'
    before = (tmp_path/'unknown/checkpoint.json').read_bytes()
    assert OfflinePort(tmp_path/'unknown').restored_pending()
    b = json.loads(launch(shared,tmp_path/'again').stdout)
    assert b['status'] == 'UNKNOWN' and b['backend_calls'] == 0
    assert (tmp_path/'again/checkpoint.json').read_bytes() == before


def test_capacity_no_pruning_or_initialization(shared,tmp_path):
    c = control(shared,tmp_path/'restore');c.root.mkdir();c.restore()
    original = (shared/'slots/0.enc').read_bytes()
    with pytest.raises(StateError,match='storage_capacity'):
        c.checkpoints.save(c.root,1,total_bytes=MAX_STORAGE)
    assert (shared/'slots/1.enc').read_bytes() == b''
    assert (shared/'slots/0.enc').read_bytes() == original
    (c.root/'checkpoint.json').write_bytes(b' '* (MAX_CHECKPOINT+1))
    with pytest.raises(StateError,match='capacity'):c.checkpoints.save(c.root,1)
    with pytest.raises(StateError,match='empty_restore'):c.restore()


def test_default_off_does_not_read_or_restore(tmp_path):
    c = SimpleControl(tmp_path)
    assert run_persistent(c,lambda _:pytest.fail('port constructed'))['status'] == 'DISABLED'
    assert not tmp_path.joinpath('checkpoint.json').exists()


def test_large_checkpoint_uses_existing_cipher_in_chunks_not_cas(shared,tmp_path):
    c=control(shared,tmp_path/'large');c.root.mkdir();c.restore()
    cp=json.loads((c.root/'checkpoint.json').read_bytes());cp['synthetic_padding']='x'*450_000
    (c.root/'checkpoint.json').write_bytes(encoded(cp))
    pointer=c.checkpoints.save(c.root,1)
    assert pointer['size']>750_000
    _,observed=c.checkpoints.load(pointer)
    assert observed==(c.root/'checkpoint.json').read_bytes()
    assert state(shared,tmp_path)['checkpoint']['generation']==0


def test_unknown_save_latches_same_worker_against_second_cas_write(shared,tmp_path):
    c=control(shared,tmp_path/'latch');c.root.mkdir();c.restore()
    _,tag=c.read();_,tag=c.replace(tag,status='RUNNING',run_id='one')
    _,tag=c.replace(tag,claim='one',status='PENDING',pending=dict(phase='poll'))
    cp=json.loads((c.root/'checkpoint.json').read_bytes());cp['marker']='new'
    (c.root/'checkpoint.json').write_bytes(encoded(cp));(shared/'slots/after').touch()
    with pytest.raises(TimeoutError):c.save_checkpoint(20)
    with pytest.raises(StateError,match='result_unknown'):c.replace(tag,claim='one',status='UNKNOWN')
    with sqlite3.connect(shared/'server.sqlite') as db:assert db.execute('select writes from document').fetchone()[0]==2


def test_journal_receipt_regression_rejected_before_slot_write(shared,tmp_path):
    from base64 import b64decode,b64encode
    from app.iphone_persistent_drafts import EncryptedDraftIO
    c=control(shared,tmp_path/'regression');c.root.mkdir();c.restore()
    _,tag=c.read();_,tag=c.replace(tag,status='RUNNING',run_id='one')
    c.replace(tag,claim='one',status='PENDING',pending=dict(phase='poll'))
    cp=json.loads((c.root/'checkpoint.json').read_bytes())
    cipher=EncryptedDraftIO(None,cp['key'].encode(),cp['native_scope'])
    journal=cipher.decode(b64decode(cp['journal']['raw']))
    journal['legacy']['requests']={}
    cp['journal']['raw']=b64encode(cipher.encode(journal)).decode()
    (c.root/'checkpoint.json').write_bytes(encoded(cp))
    with pytest.raises(StateError):c.save_checkpoint(20)
    assert (shared/'slots/1.enc').read_bytes()==b''


class SimpleControl:
    def __init__(self,root):self.root=root
    def restore(self):pytest.fail('read while disabled')
