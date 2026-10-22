"""Bounded, default-OFF registration coordinator; offline composition only.

No scheduler, Google credential/client factory, financial writer or authority.
The local encrypted control log reuses existing draft cipher/OS lock/CAS
conventions. It is NOT a distributed lease or an Actions state service.
"""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from cryptography.fernet import Fernet
from app.drive_run_state import StateError
from app.iphone_draft_journal import exclusive
from app.iphone_persistent_drafts import EncryptedDraftIO
from app.pdf_intake_registry import strong_etag

MODE='offline-synthetic-registration-runner-v1'
FENCED={'RUNNING','PENDING','UNKNOWN','REVOKED','STOPPED','CAPACITY'}
MAX_VERSIONS=128
MAX_STORAGE=8_000_000
TERMINAL={'UNKNOWN','REVOKED','STOPPED','CAPACITY'}


def fail(code):
    raise StateError('registration_runner_'+code)


def fingerprint(raw):
    return sha256(raw).hexdigest()


def atomic(path,raw,*,create=False):
    """Same fsync/replace convention as existing LocalJournal; no retries."""
    if create:
        with path.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.runner-',delete=False) as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno());name=stream.name
        os.replace(name,path)
    if os.name!='nt':
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


class Control:
    """Create-only encrypted versions + exact HEAD CAS, one local host.

    Never prune/choose an orphan version or silently initialize a missing log.
    A restored old HEAD is rejected while the newer immutable versions exist.
    Full-directory rollback still requires an external trusted tip/witness.
    """
    def __init__(self,directory):
        self.root=Path(directory)
        if self.root.is_symlink() or not self.root.is_dir():fail('private_directory_required')
        self.scope=json.loads((self.root/'scope.json').read_bytes())
        if self.scope.get('mode')!=MODE or self.scope.get('purpose')!='DRAFT_ONLY':fail('offline_scope_required')
        if fingerprint((self.root/'config.json').read_bytes())!=self.scope['config_sha256']:fail('configuration_changed')
        self.cipher=EncryptedDraftIO(None,(self.root/'runner.key').read_bytes(),self.scope)

    @classmethod
    def provision(cls,directory,checkpoint):
        root=Path(directory)
        scope=dict(mode=MODE,purpose='DRAFT_ONLY',instance=str(uuid.uuid4()),
                   config_sha256=fingerprint((root/'config.json').read_bytes()))
        atomic(root/'scope.json',json.dumps(scope,sort_keys=True).encode(),create=True)
        atomic(root/'runner.key',Fernet.generate_key(),create=True)
        obj=cls(root)
        value=dict(revision=0,previous=None,status='IDLE',phase=None,pending=None,
                   checkpoint=checkpoint,last_slot=None,run_id=None)
        raw=obj.cipher.encode(value)
        atomic(root/'control-000000.enc',raw,create=True)
        atomic(root/'head.json',json.dumps(dict(revision=0,sha256=fingerprint(raw))).encode(),create=True)
        return obj

    def read(self):
        files=sorted(self.root.glob('control-*.enc'))
        if not files or len(files)>MAX_VERSIONS or sum(p.stat().st_size for p in files)>MAX_STORAGE:
            fail('control_capacity_or_missing')
        head=json.loads((self.root/'head.json').read_bytes());previous=None;value=None
        for n,path in enumerate(files):
            if path.name!=f'control-{n:06d}.enc':fail('control_history_gap')
            raw=path.read_bytes();value=self.cipher.decode(raw)
            if (set(value)!={'revision','previous','status','phase','pending','checkpoint','last_slot','run_id'}
                    or value['revision']!=n or value['previous']!=previous
                    or value['status'] not in FENCED|{'IDLE','HELD'}):fail('control_history_invalid')
            previous=fingerprint(raw)
        if head!=dict(revision=len(files)-1,sha256=previous):fail('stale_or_incomplete_head')
        return value,'"'+previous+'"'

    def replace(self,expected,*,claim=None,**changes):
        with exclusive(self.root/'control.lock'):
            before,tag=self.read()
            if expected!=tag:fail('control_cas_conflict')
            after=dict(before,**changes,revision=before['revision']+1,previous=tag.strip('"'))
            if before['status'] in TERMINAL:fail('control_fenced')
            if before['status'] in {'RUNNING','PENDING'} and before['run_id']!=claim:fail('run_owner_changed')
            raw=self.cipher.encode(after)
            size=sum(p.stat().st_size for p in self.root.glob('control-*.enc'))+len(raw)
            if after['revision']>=MAX_VERSIONS or size>MAX_STORAGE:fail('control_capacity')
            atomic(self.root/f"control-{after['revision']:06d}.enc",raw,create=True)
            atomic(self.root/'head.json',json.dumps(dict(revision=after['revision'],sha256=fingerprint(raw))).encode())
            actual,actual_tag=self.read()
            if actual!=after:fail('control_readback_unknown')
            return actual,actual_tag


class ConditionalControl:
    """Same coordinator contract over the EXISTING versioned IO protocol.

    Inject e.g. the pinned existing FirestoreConditionalState, never a client
    factory. This gate does not persist the native checkpoint bundle for you:
    that bundle must be durably stored/read back BEFORE acknowledging a step.
    No live implementation is provisioned or selected by the offline CLI.
    """
    def __init__(self,io,key,scope):
        if scope.get('mode')!=MODE or scope.get('purpose')!='DRAFT_ONLY':fail('offline_scope_required')
        self.transport=EncryptedDraftIO(io,key,scope)

    def migration_payload(self,state):
        if state['revision']!=0 or state['status']!='IDLE':fail('explicit_initial_state_required')
        return self.transport.encode(dict(schema=MODE,current=deepcopy(state),history=[]))

    def _read(self):
        value,raw,tag=self.transport.read()
        if (set(value)!={'schema','current','history'} or value['schema']!=MODE
                or len(value['history'])>=MAX_VERSIONS):fail('conditional_control_invalid')
        records=value['history']+[value['current']]
        for n,r in enumerate(records):
            if (set(r)!={'revision','previous','status','phase','pending','checkpoint','last_slot','run_id'}
                    or r['revision']!=n or r['status'] not in FENCED|{'IDLE','HELD'}):fail('conditional_control_invalid')
        return value,raw,tag

    def read(self):
        value,_,tag=self._read();return deepcopy(value['current']),tag

    def replace(self,expected,*,claim=None,**changes):
        before,raw,tag=self._read()
        if not strong_etag(expected) or tag!=expected:fail('control_cas_conflict')
        if before['current']['status'] in TERMINAL:fail('control_fenced')
        if before['current']['status'] in {'RUNNING','PENDING'} and before['current']['run_id']!=claim:fail('run_owner_changed')
        after=deepcopy(before);after['history'].append(deepcopy(before['current']))
        if len(after['history'])>=MAX_VERSIONS:fail('control_capacity')
        after['current']=dict(before['current'],**changes,revision=before['current']['revision']+1,previous=tag)
        self.transport.io.replace_versioned(raw,tag,self.transport.encode(after))
        observed,_,actual_tag=self._read()
        if observed!=after:fail('control_readback_unknown')
        return deepcopy(after['current']),actual_tag


class CaptureConflict(Exception):
    pass


class ShortRunner:
    """Ports must implement bounded offline IO, never obtain real credentials.

    A durable PENDING record precedes every backend/publisher call. A crash,
    timeout, unknown response or lost checkpoint fences later launches. No
    expiry-based lock stealing, pending release, archive or write retry.
    """
    def __init__(self,directory,port_factory,*,clock=time.time,monotonic=time.monotonic,sleep=time.sleep,control_factory=Control):
        self.root=Path(directory);self.factory=port_factory
        self.clock=clock;self.monotonic=monotonic;self.sleep=sleep
        self.control_factory=control_factory

    def run(self,*,enabled=False,max_seconds=120,max_cycles=2,poll_seconds=3,slot_seconds=1800):
        if enabled is not True:return self.summary('DISABLED')
        if (type(max_seconds) not in {int,float} or not 1<=max_seconds<=180
                or type(max_cycles) is not int or not 1<=max_cycles<=3
                or not 3<=poll_seconds<=30 or not 300<=slot_seconds<=3600):fail('bounds_invalid')
        self.deadline=self.monotonic()+max_seconds;self.calls=0
        with exclusive(self.root/'runner.lock'):
            self.control=self.control_factory(self.root);self.state,self.tag=self.control.read()
            if self.state['status'] in FENCED:return self.summary('UNKNOWN' if self.state['status'] in {'PENDING','RUNNING'} else self.state['status'])
            self.port=self.factory(self.root)
            if self.port.mode!=MODE or self.port.bounded_io is not True:fail('offline_port_required')
            if self.port.checkpoint()!=self.state['checkpoint']:
                self.commit(status='UNKNOWN',phase='restore',pending=dict(reason='checkpoint_changed'))
                return self.summary('UNKNOWN')
            if self.port.restored_pending():
                self.commit(status='UNKNOWN',phase='restore_pending',pending=dict(reason='saved_pending'))
                return self.summary('UNKNOWN')
            self.slot=int(self.clock()//slot_seconds);self.run_id=str(uuid.uuid4())
            self.delivery_pending=False
            try:
                self.guard()
                if self.state['last_slot']==self.slot:return self.summary('ALREADY_OBSERVED')
                a=self.port.capture();b=self.port.capture()
                if a!=b:raise CaptureConflict()
                observation='observed:'+fingerprint(json.dumps(b,sort_keys=True).encode())
                if self.state['phase']==observation:return self.summary('UNCHANGED')
                # Whole-run shared CAS claim survives gaps between phases.
                # Another host must never mistake an acknowledged phase for
                # the end of this run. No time-based stealing of this claim.
                self.commit(status='RUNNING',run_id=self.run_id,phase='run',pending=None)
                results=[]
                for cycle in range(max_cycles):
                    self.guard()
                    if cycle:a=self.port.capture();b=self.port.capture()
                    if a!=b:raise CaptureConflict()
                    outcome=self.step('poll',lambda budget:self.port.poll(a,b,budget))
                    results=outcome['results']
                    # An unresolved native result fences the whole run BEFORE
                    # claiming any result-display intent. In particular, do
                    # not spend more custody generations publishing holds
                    # while an ATTEMPTING receipt already needs reconciliation.
                    if any(r['status']=='UNKNOWN' for r in results):
                        self.commit(status='UNKNOWN',phase='backend_result',pending=dict(reason='backend_unknown'))
                        return self.summary('UNKNOWN',results)
                    for item in outcome['pending']:
                        self.guard();fresh=self.port.capture()
                        ready=self.step('ready',lambda budget:self.port.ready(item['id'],fresh,budget))
                        self.delivery_pending=True
                        self.step('publish',lambda budget:self.port.publish(ready['requests'],fresh,budget))
                    if outcome['pending']:
                        self.guard();a=self.port.capture();b=self.port.capture()
                        if a!=b:raise CaptureConflict()
                        observed=self.step('readback',lambda budget:self.port.poll(a,b,budget))
                        self.delivery_pending=False
                        # Do not silently drop or execute a newly generated intent.
                        if observed['pending']:
                            self.commit(status='UNKNOWN',pending=dict(reason='unfinished_display_intent'))
                            return self.summary('UNKNOWN',observed['results'])
                        results=observed['results']
                    if any(r['status']=='UNKNOWN' for r in results):
                        self.commit(status='UNKNOWN',phase='backend_result',pending=dict(reason='backend_unknown'))
                        return self.summary('UNKNOWN',results)
                    if not any(r['status'] in {'SYNC_WAIT','WAITING'} for r in results):
                        self.guard();final=self.port.capture();again=self.port.capture()
                        stable=final==again and self.port.same_inputs(b,final)
                        phase='observed:'+fingerprint(json.dumps(final,sort_keys=True).encode()) if stable else None
                        self.commit(status='IDLE',run_id=None,phase=phase,last_slot=self.slot if stable else None)
                        if not stable:return self.summary('WAITING',results)
                        return self.summary('COMPLETE',results)
                    if cycle+1<max_cycles:
                        self.guard(reserve=poll_seconds+2);self.sleep(poll_seconds)
                self.commit(status='IDLE',run_id=None,phase=None)
                return self.summary('WAITING',results)
            except CaptureConflict:
                if self.delivery_pending:
                    self.commit(status='UNKNOWN',phase='readback_conflict',pending=dict(reason='delivery_not_observed'))
                    return self.summary('UNKNOWN')
                self.commit(status='HELD',run_id=None,phase='capture_conflict',pending=None)
                return self.summary('HELD')
            except PermissionError:
                self.commit(status='REVOKED')
                return self.summary('REVOKED')
            except StopIteration:
                self.commit(status='STOPPED')
                return self.summary('STOPPED')
            except Deadline:
                if self.delivery_pending or self.port.restored_pending():
                    self.commit(status='UNKNOWN',pending=dict(reason='time_limit_with_pending'))
                    return self.summary('UNKNOWN')
                if self.state['status']=='RUNNING':self.commit(status='IDLE',run_id=None,phase=None)
                return self.summary('TIME_LIMIT')  # No in-flight call at this boundary.
            except Capacity:
                self.commit(status='CAPACITY')
                return self.summary('CAPACITY')
            except Exception:
                # A failed control write may itself be uncertain; never repair
                # or clear an existing PENDING marker to resume the operation.
                try:self.commit(status='UNKNOWN')
                except Exception:pass
                return self.summary('UNKNOWN')

    def commit(self,**changes):
        self.state,self.tag=self.control.replace(self.tag,claim=getattr(self,'run_id',None),**changes)

    def guard(self,*,reserve=2):
        if (self.root/'STOP').exists():raise StopIteration()
        if self.deadline-self.monotonic()<=reserve:raise Deadline()
        if not self.port.authorized():raise PermissionError()
        if not self.port.capacity():raise Capacity()

    def step(self,name,operation):
        self.guard();self.commit(status='PENDING',phase=name,run_id=self.run_id,
            pending=dict(run_id=self.run_id,phase=name,slot=self.slot))
        self.calls+=1
        result=operation(max(0.1,self.deadline-self.monotonic()-2))
        if type(result) is not dict or result.get('error'):raise RuntimeError('operation outcome unknown')
        if result.get('financial_writes',0)!=0 or result.get('authority_calls',0)!=0:fail('financial_boundary_violation')
        # Future remote composition MUST persist/read back the checkpoint
        # here, while PENDING, before allowing the next phase (notably publish).
        stored=self.port.persist_checkpoint(max(.1,self.deadline-self.monotonic()-2))
        if stored!=self.port.checkpoint():fail('checkpoint_readback_unknown')
        self.commit(status='RUNNING',phase=None,pending=None,checkpoint=stored)
        return result

    def summary(self,status,results=()):
        return dict(status=status,results=[dict(case_id=r.get('case_id'),status=r['status']) for r in results],
                    backend_calls=getattr(self,'calls',0),synthetic_only=True,accounting_writes=0,authority_calls=0)


class Deadline(Exception):pass
class Capacity(Exception):pass
