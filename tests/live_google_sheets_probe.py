"""Explicit offline backend half of a bounded, connector-driven LIVE probe.

Run only with an operator-provisioned private config/checkpoint directory.
Imports existing fixture/native validators; never imports a live credential
factory. Google calls belong to the connector broker, not this process. This
single-host checkpoint is NOT a production/distributed CAS implementation.
"""
from base64 import b64encode,b64decode
from copy import deepcopy
from dataclasses import asdict
import json
from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet
from pytest import MonkeyPatch
from app.iphone_draft_journal import exclusive
from app.iphone_draft_journal import LocalJournal
from app.iphone_persistent_drafts import PersistentJournal
from app.iphone_sheet_reader import DraftBatch
from app.unified_receipt_review import digest
from registration_fixtures import Rig
from sheets_only_fixtures import no_clients
from staging_shadow.live_synthetic_sheets import LiveSyntheticBridge,SyntheticRegistrationPlanner,SLOT,PIN_SLOT,terminal_inputs
from test_legacy_contract_poc import mem


def encoded_io(io):
    return dict(raw=b64encode(io.raw).decode(),revision=io.revision,writes=io.writes)


def restore_io(io,v):
    io.raw=b64decode(v['raw']);io.revision=v['revision'];io.writes=v['writes']


def save(path,r,outer,outer_key,archives):
    expected=deepcopy(r.sheet.expected)
    for v in expected.values():v['batch']=asdict(v['batch'])
    value=dict(schema='live-synthetic-probe-checkpoint-v1',
        key=r.old.key.decode(),journal=encoded_io(r.old.io),policy=encoded_io(r.policy_io),pin=r.permission.pin,
        drafts={cid:encoded_io(c.readers.draft) for cid,c in r.old.old.native.items()},
        raw=r.sheet.raw,expected=expected,stable=r.worker.stable,uncertain=r.worker.uncertain,
        projection_unknown=sorted(r.worker.projection_unknown),outer=encoded_io(outer.transport.io),outer_key=outer_key.decode(),
        native_scope=r.old.journal.scope,outer_scope=outer.scope,archives=archives)
    data=json.dumps(value,ensure_ascii=True,allow_nan=False).encode()
    if len(data)>2_000_000:raise RuntimeError('bounded checkpoint full; hold')
    with tempfile.NamedTemporaryFile(dir=path.parent,prefix='checkpoint-',delete=False) as handle:
        handle.write(data);handle.flush();os.fsync(handle.fileno());temp=handle.name
    os.replace(temp,path)
    if os.name!='nt':
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


def restore(path,r):
    v=json.loads(path.read_bytes())
    if v['schema']!='live-synthetic-probe-checkpoint-v1':raise RuntimeError('foreign checkpoint')
    for identity in v.get('archives',[]):
        if len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity):raise RuntimeError('foreign archive')
        if sha256((path.parent/('archive-'+identity+'.json')).read_bytes()).hexdigest()!=identity:
            raise RuntimeError('archive changed; hold')
    if 'native_scope' in v:
        r.old.journal.scope=deepcopy(v['native_scope']);r.old.journal.transport.scope=deepcopy(v['native_scope'])
    r.old.key=v['key'].encode();r.old.journal.transport.cipher=Fernet(r.old.key)
    restore_io(r.old.io,v['journal']);restore_io(r.policy_io,v['policy']);r.permission.pin=v['pin']
    for cid,p in v['drafts'].items():restore_io(r.old.old.native[cid].readers.draft,p)
    r.sheet.raw=v['raw'];r.sheet.expected=v['expected']
    for e in r.sheet.expected.values():e['batch']=DraftBatch(**e['batch'])
    r.worker.stable={k:tuple(x) for k,x in v['stable'].items()}
    r.worker.uncertain=tuple(v['uncertain']) if v['uncertain'] else None
    r.worker.projection_unknown=set(v['projection_unknown'])
    return v


def archive_checkpoint(path,r,outer,request,config,saved):
    """Explicit local synthetic lifecycle, NEVER an automatic capacity retry.

    Keep the exact old checkpoint/ciphertext and hash, plus EVERY receipt,
    current detail/confirmation and uncertainty. No change to production
    journal code/limits. New generations use its existing migration API.
    This is test custody, not a production retention/revocation solution.
    """
    if config.get('allow_checkpoint_archive') is not True or not saved:
        raise RuntimeError('explicit synthetic archive not enabled')
    raw=path.read_bytes();identity=sha256(raw).hexdigest()
    if request.get('expected_checkpoint_sha256')!=identity:raise RuntimeError('checkpoint changed; hold')
    archives=list(saved.get('archives',[]))
    if len(archives)>=4:raise RuntimeError('bounded archive limit; hold')
    destination=path.parent/('archive-'+identity+'.json')
    with destination.open('xb') as handle:
        handle.write(raw);handle.flush();os.fsync(handle.fileno())
    if sha256(destination.read_bytes()).hexdigest()!=identity:raise RuntimeError('archive readback failed')
    for journal in (r.old.journal,outer):
        if getattr(journal.transport.io,'synthetic_memory_only',False) is not True:
            raise RuntimeError('memory-only archive required')
        state,_,tag=journal._state();scope=deepcopy(journal.scope)
        pins=terminal_inputs(journal,r.worker.slot) if journal is r.old.journal else None
        scope['generation']+=1;scope['initial_snapshot']=state['legacy']['batch']['snapshot_digest']
        # Reuse the existing cipher; keys never enter an API request or log.
        journal.scope=scope;journal.transport.scope=deepcopy(scope)
        details=deepcopy(state['details']);details['synthetic_checkpoint_archive']=identity
        if pins is not None:details[PIN_SLOT]=pins
        value=journal.transport.decode(journal.migration_payload(state['legacy'],tag,details=details))
        if 'confirmation' in state:value['confirmation']=deepcopy(state['confirmation'])
        journal.transport.io.raw=journal.transport.encode(value)
        journal.transport.io.revision+=1
        journal._state()
    return archives+[identity]


def run(directory,request):
    config=json.loads((directory/'config.json').read_bytes())
    m=MonkeyPatch();no_clients(m)
    fixture=tempfile.TemporaryDirectory(dir=directory,prefix='fixture-')
    try:
        r=Rig(m,Path(fixture.name));checkpoint=directory/'checkpoint.json'
        saved=restore(checkpoint,r) if checkpoint.exists() else None
        archives=list(saved.get('archives',[])) if saved else []
        r.old.h.now=int(time.time())
        old=r.worker
        r.worker=SyntheticRegistrationPlanner(r.adapter,r.sheet,r.permission,enabled=True,isolated_synthetic=True)
        r.worker.stable=old.stable;r.worker.uncertain=old.uncertain;r.worker.projection_unknown=old.projection_unknown
        probe=LiveSyntheticBridge(r.worker,config['baseline'],config['sources'],config['profile'],config['acl'],enabled=True)
        batch=probe.latest_batch;outer_key=Fernet.generate_key();io=mem({})
        outer=PersistentJournal(io,outer_key,owner=probe.principal,contract_digest=batch.contract_digest,
            generation=1,initial_snapshot=batch.snapshot_digest)
        if saved and 'outer' in saved:
            outer_key=saved['outer_key'].encode();outer.transport.cipher=Fernet(outer_key);restore_io(io,saved['outer'])
            if 'outer_scope' in saved:
                outer.scope=deepcopy(saved['outer_scope']);outer.transport.scope=deepcopy(saved['outer_scope'])
        else:
            # Preserve old receipts/uncertainty verbatim; do not erase the old
            # encrypted journal or compact its history. Only delivery is split
            # into another instance of the EXISTING DRAFT_ONLY journal engine.
            import uuid
            local=LocalJournal(Path(fixture.name)/'outer-seed.json',batch.contract_digest)
            _,tag=local.initialize();local.save(batch,tag,str(uuid.uuid4()));legacy,tag=local.read_versioned()
            old_state=r.old.journal._state()[0]
            io.raw=outer.migration_payload(legacy,tag,details={SLOT:deepcopy(old_state['details'].get(SLOT,{})),
                'imported_native_archive_digest':sha256(r.old.io.raw).hexdigest()})
        b=LiveSyntheticBridge(r.worker,config['baseline'],config['sources'],config['profile'],config['acl'],enabled=True,journal=outer,
                              display_cases=config.get('display_cases'))
        op=request['op']
        if op=='fault_poll':
            if config.get('allow_fault_injection') is not True:raise RuntimeError('fault injection not enabled for this probe')
            def lost(*args,**kwargs):raise TimeoutError('synthetic draft response lost')
            m.setattr(r.old.old.native['SYN-G101'].readers.draft,'replace_versioned',lost)
            op='poll'
        if op=='archive_checkpoint':
            archives=archive_checkpoint(checkpoint,r,outer,request,config,saved)
            result=dict(archived=archives[-1],financial_writes=0,authority_calls=0,
                        native_generation=r.old.journal.scope['generation'],delivery_generation=outer.scope['generation'])
        elif op=='abandon_unsent':
            # Manual test maintenance for pre-single-use-protocol intents.
            # The operator must independently establish NO dispatch; this is
            # never inferred from an absent Google result or a timeout.
            if (config.get('allow_checkpoint_archive') is not True
                    or request.get('verified_never_dispatched') is not True
                    or request.get('expected_checkpoint_sha256')!=sha256(checkpoint.read_bytes()).hexdigest()):
                raise RuntimeError('explicit no-dispatch evidence required')
            records=b._records();record=records[request['id']]
            if record['state']!='ATTEMPTING':raise RuntimeError('claimed writes cannot be abandoned')
            record['state']='CANCELLED';b._journal(records,request['id'],'cancelled')
            result=dict(cancelled_unsent=request['id'],financial_writes=0,authority_calls=0)
        elif op=='poll':
            if request['sources']!=request.get('second_sources',request['sources']):
                raise RuntimeError('independent source inventory changed')
            result=b.poll(request['first'],request['second'],request['sources'],request['profile'],request['acl'])
        elif op=='ready':
            ids=request.get('ids',[request.get('id')]);requests=[]
            for key in ids:requests.extend(b.ready(key,request['raw'],request['sources'],request['profile'],request['acl']))
            result=dict(requests=requests)
        elif op=='inspect':
            result=dict(outbox=b._records(),registrations=r.worker._state()[1],
                        draft_writes=r.draft_writes(),financial_writes=0,authority_calls=0)
        else:raise RuntimeError('unsupported probe operation')
        r.assert_safe()
        save(checkpoint,r,outer,outer_key,archives)  # MUST be durable before releasing a Google outbox.
        return result
    finally:
        m.undo();fixture.cleanup()


if __name__=='__main__':
    directory=Path(sys.argv[1]).resolve();request_path=Path(sys.argv[2]).resolve()
    if not directory.is_dir() or directory not in request_path.parents:
        raise SystemExit('private request directory required')
    with exclusive(directory/'probe.lock'):
        try:result=run(directory,json.loads(request_path.read_bytes()))
        except Exception as error:
            result=dict(error=type(error).__name__,reason=str(error),financial_writes=0,authority_calls=0)
        output=directory/'response.json'
        output.write_bytes(json.dumps(result,ensure_ascii=False,allow_nan=False).encode())
        print(json.dumps(dict(response=str(output),error=result.get('error')),ensure_ascii=False))
