from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

import pytest

from app.drive_run_state import StateError
from app.iphone_draft_journal import exclusive
from staging_shadow.registration_runner import (Control,ConditionalControl,ShortRunner,MODE,atomic,fingerprint)
from offline_registration_runner import OfflinePort,provision


class Port:
    mode=MODE;bounded_io=True
    def __init__(self,root):
        self.root=root;self.allowed=True;self.space=True;self.pending=False
        self.calls=[];self.snapshots=0;self.fault=None
    def checkpoint(self):return fingerprint((self.root/'native').read_bytes())
    def persist_checkpoint(self,budget):return self.checkpoint()
    def restored_pending(self):return self.pending
    def authorized(self):return self.allowed
    def capacity(self):return self.space
    def capture(self):self.snapshots+=1;return dict(value=1)
    def same_inputs(self,a,b):return a==b
    def poll(self,a,b,budget):
        self.calls.append('poll')
        if self.fault:return self.fault(self)
        atomic(self.root/'native',b'committed')
        return dict(results=[dict(case_id='SYN-G101',status='VALIDATED_UNPOSTED')],pending=[])


@pytest.fixture
def setup(tmp_path):
    root=tmp_path/'runner';root.mkdir()
    atomic(root/'config.json',b'{"synthetic":true}',create=True)
    atomic(root/'native',b'initial',create=True)
    control=Control.provision(root,fingerprint(b'initial'))
    port=Port(root)
    return root,control,port


def launch(setup,**kwargs):
    root,control,port=setup
    return ShortRunner(root,lambda _:port,clock=lambda:900).run(enabled=True,**kwargs)


def test_default_off_reads_nothing_and_constructs_no_port(tmp_path):
    def forbidden(*a):raise AssertionError('port constructed while OFF')
    assert ShortRunner(tmp_path/'missing',forbidden).run()['status']=='DISABLED'


@pytest.mark.parametrize('setting',[dict(max_seconds=181),dict(max_cycles=4),dict(poll_seconds=0),dict(slot_seconds=1)])
def test_bounds_before_state_access(tmp_path,setting):
    with pytest.raises(StateError,match='bounds_invalid'):
        ShortRunner(tmp_path/'missing',None).run(enabled=True,**setting)


def test_periodic_slot_restart_and_no_backfill(setup):
    root,control,port=setup
    assert launch(setup)['status']=='COMPLETE'
    assert launch(setup)['status']=='ALREADY_OBSERVED' and port.calls==['poll']
    assert ShortRunner(root,lambda _:port,clock=lambda:900*5).run(enabled=True)['status']=='UNCHANGED'
    assert port.calls==['poll']  # Missed slots are never replayed.
    versions=len(list(root.glob('control-*.enc')))
    for slot in range(10,110):
        assert ShortRunner(root,lambda _:port,clock=lambda:1800*slot).run(enabled=True)['status']=='UNCHANGED'
    assert len(list(root.glob('control-*.enc')))==versions  # No idle history growth.


@pytest.mark.parametrize('status',['RUNNING','PENDING','UNKNOWN','REVOKED','STOPPED','CAPACITY'])
def test_saved_fences_never_expire_or_create_a_port(setup,status):
    root,control,port=setup;state,tag=control.read()
    control.replace(tag,status=status,pending=dict(reason='retain'))
    def forbidden(*a):raise AssertionError('fenced runtime constructed')
    r=ShortRunner(root,forbidden,clock=lambda:10**12).run(enabled=True)
    assert r['status']==('UNKNOWN' if status in {'RUNNING','PENDING'} else status)
    assert Control(root).read()[0]['pending']==dict(reason='retain')


def test_single_host_parallel_launch_is_rejected(setup):
    root,_,port=setup
    with exclusive(root/'runner.lock'):
        with pytest.raises(StateError,match='busy'):launch(setup)
    assert not port.calls


def test_os_lock_releases_after_process_death_without_lease_stealing(setup):
    root,_,port=setup
    script="from pathlib import Path; import sys,time,os; from app.iphone_draft_journal import exclusive\nwith exclusive(Path(sys.argv[1])/'runner.lock'):\n (Path(sys.argv[1])/'locked').write_text(str(os.getpid()))\n time.sleep(60)"
    child=subprocess.Popen([sys.executable,'-c',script,str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+10
        while not (root/'locked').exists() and time.monotonic()<deadline:time.sleep(.05)
        assert (root/'locked').exists()
        with pytest.raises(StateError,match='busy'):launch(setup)
    finally:
        if (root/'locked').exists():os.kill(int((root/'locked').read_text()),signal.SIGTERM)
        else:child.kill()
        child.wait(timeout=10)
    assert launch(setup)['status']=='COMPLETE'


def test_revocation_after_ready_stops_publisher_and_keeps_pending(setup):
    root,control,port=setup
    port.fault=lambda p:dict(results=[],pending=[dict(id='first')])
    def ready(key,capture,budget):
        port.calls.append('ready');port.pending=True;port.allowed=False
        return dict(requests=[])
    port.ready=ready
    def forbidden(*a):raise AssertionError('publisher called after revocation')
    port.publish=forbidden
    assert launch(setup)['status']=='REVOKED'
    assert port.pending and port.calls==['poll','ready']
    assert launch(setup)['status']=='REVOKED'


def test_crash_after_native_effect_never_reexecutes(setup):
    root,control,port=setup
    def lost(p):
        atomic(root/'native',b'committed before lost reply')
        raise TimeoutError()
    port.fault=lost
    assert launch(setup)['status']=='UNKNOWN'
    state,_=control.read();assert state['pending']['phase']=='poll'
    assert launch(setup)['status']=='UNKNOWN' and port.calls==['poll']


def test_restored_backend_pending_stops_before_poll(setup):
    root,control,port=setup;port.pending=True
    assert launch(setup)['status']=='UNKNOWN' and not port.calls
    assert control.read()[0]['phase']=='restore_pending'


def test_revocation_is_durable_and_does_not_auto_resume(setup):
    root,control,port=setup;port.allowed=False
    assert launch(setup)['status']=='REVOKED' and not port.calls
    port.allowed=True
    assert launch(setup)['status']=='REVOKED' and not port.calls


def test_stop_before_start_and_during_work(setup):
    root,control,port=setup
    def stop(p):
        atomic(root/'STOP',b'stop',create=True)
        return dict(results=[],pending=[dict(id='unsent')])
    port.fault=stop
    assert launch(setup)['status']=='STOPPED' and port.calls==['poll']
    assert launch(setup)['status']=='STOPPED' and port.calls==['poll']


def test_capacity_holds_without_archive_or_pruning(setup):
    root,control,port=setup;port.space=False
    before=(root/'control-000000.enc').read_bytes()
    assert launch(setup)['status']=='CAPACITY' and not port.calls
    assert (root/'control-000000.enc').read_bytes()==before
    assert not list(root.glob('archive-*'))


def test_double_capture_conflict_is_held_before_import(setup):
    root,control,port=setup
    def changing():port.snapshots+=1;return dict(value=port.snapshots)
    port.capture=changing
    assert launch(setup)['status']=='HELD' and not port.calls


def test_deadline_before_io_is_clean_stop(setup):
    root,control,port=setup
    assert launch(setup,max_seconds=1)['status']=='TIME_LIMIT'
    assert not port.calls and control.read()[0]['status']=='IDLE'


def test_unknown_backend_result_fences_later_slots(setup):
    root,control,port=setup
    port.fault=lambda p:dict(results=[dict(case_id='SYN-G101',status='UNKNOWN')],pending=[dict(id='must-not-publish')])
    assert launch(setup)['status']=='UNKNOWN'
    assert control.read()[0]['phase']=='backend_result'
    assert launch(setup)['status']=='UNKNOWN' and port.calls==['poll']


def test_control_stale_cas_head_rollback_and_missing_state_rejected(setup):
    root,control,port=setup;_,tag=control.read()
    old=(root/'head.json').read_bytes()
    control.replace(tag,phase='known')
    with pytest.raises(StateError,match='cas_conflict'):control.replace(tag,phase='stale')
    atomic(root/'head.json',old)
    with pytest.raises(StateError,match='stale_or_incomplete'):control.read()
    assert not port.calls


def test_control_write_failure_never_starts_backend(setup,monkeypatch):
    root,control,port=setup
    from staging_shadow import registration_runner as module
    real=module.atomic
    def interrupted(path,raw,**kwargs):
        if path.name=='head.json':raise OSError('synthetic fsync interruption')
        return real(path,raw,**kwargs)
    monkeypatch.setattr(module,'atomic',interrupted)
    assert launch(setup)['status']=='UNKNOWN' and not port.calls
    with pytest.raises(StateError,match='stale_or_incomplete'):control.read()


def test_control_configuration_and_missing_files_never_initialize(setup):
    root,control,port=setup
    atomic(root/'config.json',b'{}')
    with pytest.raises(StateError,match='configuration_changed'):launch(setup)
    assert not port.calls


def cli(root,*args,timeout=180):
    result=subprocess.run([sys.executable,'tests/offline_registration_runner.py',*args,str(root)],
                          capture_output=True,text=True,timeout=timeout)
    assert result.returncode==0,result.stderr
    return json.loads(result.stdout)


def run_cli(root,*,seconds=150):
    p=subprocess.run([sys.executable,'tests/offline_registration_runner.py','run',str(root),
                      '--enable-offline','--max-seconds',str(seconds)],capture_output=True,text=True,timeout=seconds+15)
    result=json.loads(p.stdout)
    assert p.returncode==(3 if result['status'] in {'UNKNOWN','REVOKED','CAPACITY'} else 0),p.stderr
    return result


@pytest.mark.parametrize('scenario',['normal','publish_lost','native_unknown'])
def test_existing_adapter_across_real_process_restart_and_readonly_recovery(tmp_path,scenario):
    root=tmp_path/'offline';provision(root,scenario=scenario)
    before=json.loads((root/'sheet.json').read_bytes())
    result=run_cli(root)
    expected='COMPLETE' if scenario=='normal' else 'UNKNOWN'
    assert result['status']==expected,result
    checkpoint=(root/'checkpoint.json').read_bytes()
    p=OfflinePort(root);inspect=p.inspect_readonly()
    assert (root/'checkpoint.json').read_bytes()==checkpoint
    assert inspect['financial_writes']==inspect['authority_calls']==0
    assert inspect['draft_writes']==(0 if scenario=='native_unknown' else 1)
    after=json.loads((root/'sheet.json').read_bytes())
    for a,b in zip(before['sheets'][0]['data'][0]['rowData'],after['sheets'][0]['data'][0]['rowData']):
        assert a['values'][:1]+a['values'][3:]==b['values'][:1]+b['values'][3:]
    repeated=run_cli(root)
    # A subprocess can cross the 30-minute slot boundary. Both observations
    # are read-only and must leave receipts/checkpoint/backend untouched.
    allowed={'ALREADY_OBSERVED','UNCHANGED'} if scenario=='normal' else {'UNKNOWN'}
    assert repeated['status'] in allowed
    assert repeated['backend_calls']==0 and (root/'checkpoint.json').read_bytes()==checkpoint
    # Private export/restore to an EMPTY test directory preserves ciphertext,
    # immutable history and pending. No lifecycle reset is performed.
    target=tmp_path/'restored';shutil.copytree(root,target)
    assert Control(target).read()==Control(root).read()
    recovered=run_cli(target)
    assert recovered['status'] in allowed and recovered['backend_calls']==0
    if scenario=='native_unknown':
        assert any(r['status']=='ATTEMPTING' for r in inspect['registrations']['requests'].values())


def test_supervisor_hard_timeout_preserves_pending(tmp_path):
    root=tmp_path/'timeout';provision(root,scenario='worker_timeout')
    began=time.monotonic();result=run_cli(root,seconds=5)
    assert result['status']=='UNKNOWN' and time.monotonic()-began<12
    state,_=Control(root).read()
    assert state['pending']['phase']=='poll'
    assert run_cli(root,seconds=5)['status']=='UNKNOWN'


def test_live_config_is_refused_without_constructing_google_client(tmp_path):
    root=tmp_path/'live';root.mkdir()
    atomic(root/'config.json',json.dumps(dict(runner_mode='live',baseline=dict(spreadsheetId='real-book'))).encode(),create=True)
    with pytest.raises(RuntimeError,match='offline provision'):OfflinePort(root)


@pytest.fixture
def shared(setup,tmp_path):
    from iphone_persistent_fixtures import provision as persistent_fixture
    folder=tmp_path/'existing-cas';folder.mkdir()
    repo,document,key,*_=persistent_fixture(folder)
    scope=dict(mode=MODE,purpose='DRAFT_ONLY',owner=repo.scope['owner'],instance='synthetic-runner-cas')
    gate=ConditionalControl(repo.transport.io,key,scope)
    state=setup[1].read()[0]
    document.payload=gate.migration_payload(state)
    return gate,document,repo.transport.io,key,scope


def test_existing_firestore_cas_implementation_rejects_stale_claim(shared):
    gate,document,io,key,scope=shared
    other=ConditionalControl(io,key,scope)
    _,tag=gate.read();_,stale=other.read()
    gate.replace(tag,status='PENDING',pending=dict(run_id='one',phase='poll'))
    with pytest.raises(StateError,match='cas_conflict'):
        other.replace(stale,status='PENDING',pending=dict(run_id='two',phase='poll'))
    assert document.writes==1 and other.read()[0]['pending']['run_id']=='one'


def test_whole_run_claim_cannot_be_stolen_between_phases(shared,setup):
    gate,document,io,key,scope=shared;root,_,port=setup
    _,tag=gate.read()
    _,tag=gate.replace(tag,status='RUNNING',run_id='one',phase='between_phases')
    with pytest.raises(StateError,match='run_owner_changed'):
        gate.replace(tag,claim='two',status='IDLE',run_id=None)
    result=ShortRunner(root,lambda _:port,control_factory=lambda _:ConditionalControl(io,key,scope)).run(enabled=True)
    assert result['status']=='UNKNOWN' and not port.calls and document.writes==1


def test_checkpoint_persistence_failure_after_ready_never_publishes(setup):
    root,control,port=setup;count=0
    port.fault=lambda p:dict(results=[],pending=[dict(id='first')])
    def ready(key,capture,budget):
        port.calls.append('ready');port.pending=True
        return dict(requests=[])
    port.ready=ready
    def persist(budget):
        nonlocal count
        count+=1
        if count==2:raise OSError('synthetic remote checkpoint ACK lost')
        return port.checkpoint()
    port.persist_checkpoint=persist
    def forbidden(*a):raise AssertionError('published before durable checkpoint')
    port.publish=forbidden
    assert launch(setup)['status']=='UNKNOWN' and port.calls==['poll','ready']
    assert control.read()[0]['pending']['phase']=='ready'
    assert launch(setup)['status']=='UNKNOWN'


@pytest.mark.parametrize('fault',['before','after'])
def test_existing_cas_lost_response_never_dispatches_native(shared,setup,fault):
    gate,document,io,key,scope=shared;root,_,port=setup
    document.fault=fault
    result=ShortRunner(root,lambda _:port,control_factory=lambda _:gate).run(enabled=True)
    assert result['status']=='UNKNOWN' and not port.calls
    document.fault=None
    if fault=='after':
        assert gate.read()[0]['status']=='RUNNING' and document.writes==1
        def forbidden(*a):raise AssertionError('uncertain remote claim resumed')
        retry=ShortRunner(root,forbidden,control_factory=lambda _:ConditionalControl(io,key,scope)).run(enabled=True)
        assert retry['status']=='UNKNOWN'


def test_remote_control_retains_history_and_never_clears_unknown(shared):
    gate,document,io,key,scope=shared;_,tag=gate.read()
    _,tag=gate.replace(tag,status='PENDING',pending=dict(run_id='one',phase='publish'))
    _,tag=gate.replace(tag,status='UNKNOWN')
    with pytest.raises(StateError,match='control_fenced'):gate.replace(tag,status='IDLE',pending=None)
    state,_,_=gate._read()
    assert len(state['history'])==2 and state['current']['pending']['phase']=='publish'
