"""Synthetic regression of the real 1414->1416/110->112/122 stop structure."""
import base64
from copy import deepcopy
from datetime import timedelta,datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app import aupay_card_bank_premises as bp
from app import aupay_card_reconciliation_apply as repair
from app.aupay_card_reservation_recovery import summaries,unused_reservation_proof
from app.aupay_card_incremental_recovery import preserved_unknown_proofs,_rows
from app.drive_run_state import StateBinding,envelope,snapshot,validate,DurableState
from app.production_ledger import ProductionLedger,_encode
from app.production_run import COUNT_KEYS
from app.aupay_card_recurring import SqliteRecurringRunState
from test_aupay_card_reservation_recovery import recovery_case,NEW
from test_aupay_card_reconciliation_apply import NOW,C0,C1


def node(raw,label,generation,when):
    return dict(ref=dict(id=f'{label}-{generation}',modifiedTime=when.isoformat(),sha256=bp.sha(raw)),raw_b64=base64.b64encode(raw).decode())


def bank_step(parent_raw,bank_raw,index,writes=0):
    parent=json.loads(parent_raw);bank=json.loads(bank_raw);start=NOW+timedelta(minutes=2*index)
    t=lambda n:(start+timedelta(seconds=n)).isoformat()
    pending=deepcopy(parent);pending['generation']+=1;pending['sources']['bank'].update(phase='pending',attempt=f'{index:032x}',error='')
    native_pending=deepcopy(bank);native_pending.update(generation=bank['generation']+1,phase='pending',attempt=f'{index+100:032x}')
    result=dict.fromkeys(bp.ZERO_COUNTS,0)
    result.update(written=writes,write_requests=writes,write_attempted=writes)
    result.update(run_id=f'bank-run-{index}',status='noop',safe_noop=True,source='bank_pdf_drive',source_window_start=C0,source_window_end=t(3),preview_cursor_epoch=index)
    db=sqlite3.connect(':memory:');db.deserialize(base64.b64decode(bank['files']['bank-recurring.sqlite3']['data']))
    db.execute('INSERT INTO recurring_runs VALUES (?,?,?,?,?,?)',(result['run_id'],t(4),'noop',C0,t(3),json.dumps(result,sort_keys=True,separators=(',',':'))));db.commit();data=db.serialize();db.close()
    done_bank=deepcopy(bank);done_bank['generation']+=2
    done_bank['files']['bank-recurring.sqlite3']=dict(data=base64.b64encode(data).decode(),sha256=bp.sha(data))
    done=deepcopy(parent);done['generation']+=2
    counts={k:v for k,v in result.items() if k in COUNT_KEYS and type(v) is int and v>=0}
    done['sources']['bank'].update(phase='ready',attempt='',error='',last_success=t(6),duration_seconds=5.0,counts=counts)
    code={p:(bp.ROOT/p).read_text(encoding='utf-8') for p in bp.CODE_PATHS}
    run=dict(id=100+index,head_sha='c'*40,head_branch='main',run_attempt=1,event='schedule',path=bp.BANK_WORKFLOW,created_at=t(0),updated_at=t(9),status='completed',conclusion='success')
    job=dict(id=1000+index,name='production',status='completed',conclusion='success',started_at=t(1),completed_at=t(8))
    log='EVENT_SCHEDULE: 47 21 * * *\n'+json.dumps(dict(scope='drive_bank',mode='apply',bank_mode='apply',success=True,
        sources=dict(bank=dict(status='success',last_success=t(6),written=0,write_requests=0)),
        category_operations=dict.fromkeys(('bank_home_write_requests','bank_ui_updates','bank_usage_metadata_writes','bank_rule_applications_new','bank_rule_counts_updated'),0)))
    previous_time=datetime.fromisoformat(parent['sources']['bank']['last_success'])+timedelta(seconds=1) if parent['sources']['bank']['last_success'] else NOW-timedelta(days=1)
    bn0=node(bank_raw,'bank',bank['generation'],previous_time)
    pn0=node(parent_raw,'parent',parent['generation'],previous_time)
    step=dict(parent=[pn0,node(_encode(pending),'parent',pending['generation'],start+timedelta(seconds=2)),node(_encode(done),'parent',done['generation'],start+timedelta(seconds=7))],
        bank=[bn0,node(repair.encode(native_pending),'bank',native_pending['generation'],start+timedelta(seconds=3)),node(repair.encode(done_bank),'bank',done_bank['generation'],start+timedelta(seconds=5))],
        run=run,job=job,log=log,code=code)
    descriptor=dict(parent=[n['ref'] for n in step['parent']],bank=[n['ref'] for n in step['bank']],run=run,job_id=job['id'],log_sha256=bp.sha(log.encode()),code_sha256={p:bp.sha(v.encode()) for p,v in code.items()})
    return step,descriptor


def setup_case(tmp_path,monkeypatch,mode='normal'):
    # These invented runs use this checkout, not a deployed historical SHA.
    # Bind their synthetic code contract explicitly; production's archived
    # allowlist stays unchanged when unrelated maintenance scopes are added.
    fixture_code={p:bp.sha((bp.ROOT/p).read_text(encoding='utf-8').encode()) for p in bp.CODE_PATHS}
    monkeypatch.setattr(bp,'REVIEWED_BANK_CODE_SHA256',bp.REVIEWED_BANK_CODE_SHA256|{bp.digest(fixture_code)})
    bankdir=tmp_path/'bank-state';bankdir.mkdir()
    bankstate=SqliteRecurringRunState(bankdir/'bank-recurring.sqlite3',repo_root=tmp_path/'repo')
    bankstate.record(dict(run_id='bank-baseline',status='noop',source_window_start=C0,source_window_end=C0),advance_checkpoint=True)
    def before(p):
        bb=StateBinding('bank_pdf_drive',p.db.sid,'folder','bank-file')
        bank_raw=envelope(bb,snapshot(bankdir,bb),generation=110)
        p.bank_raw=bank_raw
        p.baseline_metadata=dict(id=p.pb.file_id,parents=['folder'],trashed=False,mimeType='application/json',version='100',
            modifiedTime=(NOW-timedelta(days=1)).isoformat(),size=str(len(p.pt.payload)),md5Checksum=hashlib.md5(p.pt.payload).hexdigest())
        p.record.update(parent_before_metadata_digest=bp.digest(p.baseline_metadata),other_native=[dict(source='bank_pdf_drive',file_id='bank-file',sha256=bp.sha(bank_raw))],
            bindings=dict(spreadsheet_id=p.db.sid,folder_id='folder',parent_id=p.pb.file_id,card_id=p.cb.file_id))
    p=recovery_case(tmp_path,mode,before_reservation=before)
    business=deepcopy(p.record);monkeypatch.setattr(bp,'BUSINESS_RECORD_SHA256',bp.digest(business))
    step,descriptor=bank_step(p.reserved_parent,p.bank_raw,1)
    parent_raw=bp.raw_node(step['parent'][-1]);meta=deepcopy(p.baseline_metadata)
    meta.update(version='104',modifiedTime=descriptor['parent'][-1]['modifiedTime'],size=str(len(parent_raw)),md5Checksum=hashlib.md5(parent_raw).hexdigest())
    proof=dict(business_record=business,manifest=dict(schema=1,business_record_sha256=bp.digest(business),
        parent_snapshot=dict(sha256=bp.sha(parent_raw),metadata_digest=bp.digest(meta)),
        bank_snapshot=dict(sha256=descriptor['bank'][-1]['sha256'],metadata_digest='bank-112'),transitions=[descriptor]),
        steps=[step],lineages={label:[dict(id=n['ref']['id'],modifiedTime=n['ref']['modifiedTime']) for n in step[label]] for label in ('parent','bank')},
        baseline_parent_metadata=p.baseline_metadata,parent_metadata=meta,history=[step['run'],{k:business['old_failure'].get(k,'unused') for k in bp.RUN_KEYS}],history_complete=True,skipped_jobs={})
    p.business,p.proof=business,proof
    install(p,proof)
    return p


def install(p,proof):
    effective=bp.effective_record(p.business,proof);p.pt.payload=bp.raw_node(proof['steps'][-1]['parent'][-1])
    p.record=effective
    p.args.update(record=effective,record_sha256=bp.digest(effective),ledger=ProductionLedger(p.pt,p.pb),
        state_observe=lambda:(dict(sha256=effective['native_before_sha256'],metadata_digest=effective['native_before_metadata_digest']),
            proof['manifest']['parent_snapshot']))
    state=SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(C0),alias_registry=lambda:())
    projected=repair.prepare_reconciliation(effective,p.db,state,p.args['key_provider'].load())['projection']
    path=p.args['approval_provider'].path;approval=json.loads(path.read_bytes());approval['batch_ref']=projected.batch_ref;path.write_text(json.dumps(approval))


def test_actual_stop_shape_then_full_existing_repair_and_checkpoint(tmp_path,monkeypatch):
    p=setup_case(tmp_path,monkeypatch);original=deepcopy(p.db.rows)
    assert json.loads(p.nt.payload)['generation']==122 and json.loads(p.pt.payload)['generation']==1416
    with pytest.raises(RuntimeError):
        unused_reservation_proof(json.loads(p.nt.payload),p.pt.payload,p.business['recovery'],p.proof['manifest']['parent_snapshot']['metadata_digest'],p.now,p.business['old_failure'])
    result=repair.execute_reconciliation(**p.args)
    assert result['success'] and result['import_yen']==4506 and result['written']==2 and p.db.calls==1 and p.db.rows[:10]==original
    native=validate(p.nt.payload,p.cb);parent=json.loads(p.pt.payload)
    assert native['generation']==126 and parent['generation']==1418 and parent['sources']['bank']==json.loads(bp.raw_node(p.proof['steps'][-1]['parent'][-1]))['sources']['bank']
    assert set(preserved_unknown_proofs(native,parent,C1))=={19,20}
    runs=summaries(native)
    assert runs['UNKNOWN:20:1']['recovery']['previous_record']==p.business['recovery']['previous_record']
    assert runs['UNKNOWN:20:1']['recovery']['bank_transitions']==p.proof
    assert runs['UNKNOWN:19:1']==summaries(json.loads(p.reserved_native))['UNKNOWN:19:1']
    fresh=DurableState(p.nt,p.cb);fresh.restore(tmp_path/'fresh-bank')
    assert SqliteRecurringRunState(tmp_path/'fresh-bank/recurring.sqlite3',repo_root=p.args['repo_root']).successful_window_end().isoformat()==C1
    from app.aupay_card_incremental_guard import controls
    from test_aupay_card_resume_recovery import NoWriteGitHub
    from test_aupay_card_incremental_apply import env
    proofs=preserved_unknown_proofs(native,parent,C1)
    class History(NoWriteGitHub):
        def get(self,path):
            if path=='actions/runs/30':return p.args['execution_identity']|dict(status='completed',conclusion='success')
            for fid,item in proofs.items():
                if path==f'actions/runs/{fid}':return item
            if path=='actions/runs/17':return super().get(path)|dict(created_at='2026-11-28T00:00:00Z')
            return super().get(path)
    history=History();history.prior.append(proofs[19])
    assert controls(env()|dict(EXPECTED_CHECKPOINT=C1),'a'*40,history,reconciliation_state=(native,parent))
    with pytest.raises(RuntimeError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==1


@pytest.mark.parametrize('change',['card','other_source','missing_parent','extra_revision','version','bank_pending','bank_write','history_missing','other_writer','code','business','financial','documents','original','acl','reuse_old_capability'])
def test_unproved_change_denied_before_any_effect(tmp_path,monkeypatch,change):
    p=setup_case(tmp_path,monkeypatch);proof=deepcopy(p.proof)
    before=(p.nt.payload,p.pt.payload,len(p.nt.writes),len(p.pt.writes))
    if change in ('card','other_source'):
        node=proof['steps'][0]['parent'][1];v=json.loads(bp.raw_node(node));v['sources']['aupay_card' if change=='card' else 'amazon']['attempt']='d'*32
        node['raw_b64']=base64.b64encode(_encode(v)).decode();node['ref']['sha256']=bp.sha(_encode(v));proof['manifest']['transitions'][0]['parent'][1]=deepcopy(node['ref'])
    elif change=='missing_parent':proof['steps'][0]['parent'].pop(1)
    elif change=='extra_revision':proof['lineages']['parent'].insert(1,dict(id='unexplained',modifiedTime=NOW.isoformat()))
    elif change=='version':proof['parent_metadata']['version']='105';proof['manifest']['parent_snapshot']['metadata_digest']=bp.digest(proof['parent_metadata'])
    elif change=='bank_pending':
        node=proof['steps'][0]['bank'][2];v=json.loads(bp.raw_node(node));v['phase']='pending';v['attempt']='f'*32
        raw=repair.encode(v);node['raw_b64']=base64.b64encode(raw).decode();node['ref']['sha256']=bp.sha(raw);proof['manifest']['transitions'][0]['bank'][2]=deepcopy(node['ref'])
    elif change=='bank_write':
        step,descriptor=bank_step(p.reserved_parent,p.bank_raw,1,writes=1);proof['steps']=[step];proof['manifest']['transitions']=[descriptor]
    elif change=='history_missing':proof['history_complete']=False
    elif change=='other_writer':proof['history'].append(dict(id=900,path='unknown-writer',run_attempt=1,status='completed',conclusion='success'))
    elif change=='code':proof['steps'][0]['code'][bp.BANK_WORKFLOW]='missing concurrency'
    elif change=='business':p.business['import_yen']+=1
    elif change=='financial':p.args['financial_read']=lambda:{}
    elif change=='documents':p.documents['money']['records']['old']['state']='changed'
    elif change=='original':p.business['source']['transactions'][0]['merchant']='changed source'
    elif change=='acl':
        def changed_acl():raise RuntimeError('repair_acl_changed')
        p.args['guard']=changed_acl
    elif change=='reuse_old_capability':p.args['run_id']=p.business['recovery']['failed_run_uuid']
    with pytest.raises((RuntimeError,KeyError,StopIteration,AssertionError)):
        if change not in ('financial','documents','acl','reuse_old_capability'):install(p,proof)
        repair.execute_reconciliation(**p.args)
    assert p.db.calls==0 and (p.nt.payload,p.pt.payload,len(p.nt.writes),len(p.pt.writes))==before


@pytest.mark.parametrize('mode',['unknown','partial'])
def test_new_premises_keep_financial_unknown_no_replay(tmp_path,monkeypatch,mode):
    p=setup_case(tmp_path,monkeypatch,mode)
    with pytest.raises(RuntimeError,match='unknown_no_replay'):repair.execute_reconciliation(**p.args)
    assert p.db.calls==1 and json.loads(p.nt.payload)['phase']=='pending'
    assert _rows(json.loads(p.nt.payload),'recurring.sqlite3','recurring_checkpoint')[0]['successful_window_end']==C0
    assert summaries(json.loads(p.nt.payload))['OUTCOME:'+NEW]['replay_allowed'] is False


def test_next_bank_noop_updates_only_signed_premises_without_new_code(tmp_path,monkeypatch):
    p=setup_case(tmp_path,monkeypatch);proof=deepcopy(p.proof)
    step,descriptor=bank_step(bp.raw_node(proof['steps'][-1]['parent'][-1]),bp.raw_node(proof['steps'][-1]['bank'][-1]),2)
    # Shared endpoints are exact revision references, never regenerated nodes.
    for label in ('parent','bank'):
        step[label][0]=deepcopy(proof['steps'][-1][label][-1]);descriptor[label][0]=step[label][0]['ref']
        proof['lineages'][label].extend(dict(id=n['ref']['id'],modifiedTime=n['ref']['modifiedTime']) for n in step[label][1:])
    proof['steps'].append(step);proof['manifest']['transitions'].append(descriptor);proof['history'].append(step['run'])
    raw=bp.raw_node(step['parent'][-1]);meta=proof['parent_metadata'];meta.update(version='108',modifiedTime=descriptor['parent'][-1]['modifiedTime'],size=str(len(raw)),md5Checksum=hashlib.md5(raw).hexdigest())
    proof['manifest']['parent_snapshot']=dict(sha256=bp.sha(raw),metadata_digest=bp.digest(meta));proof['manifest']['bank_snapshot']=dict(sha256=descriptor['bank'][-1]['sha256'],metadata_digest='bank-114')
    old_business=repair.encode(p.business);old_ref=repair.prepare_reconciliation(p.record,p.db,SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(C0),alias_registry=lambda:()),p.args['key_provider'].load())['projection'].batch_ref
    install(p,proof)
    assert repair.encode(p.business)==old_business and p.record['business_record']==p.business
    assert repair.prepare_reconciliation(p.record,p.db,SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(C0),alias_registry=lambda:()),p.args['key_provider'].load())['projection'].batch_ref!=old_ref
    assert repair.execute_reconciliation(**p.args)['success'] and p.db.calls==1


def test_signed_execution_manifest_cannot_change_business_or_be_tampered(tmp_path,monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app import aupay_card_reconciliation_entry as entry
    from app import aupay_card_reservation_recovery_entry as recovery_entry
    from test_aupay_card_reconciliation_entry import capsule
    p=setup_case(tmp_path,monkeypatch)
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    monkeypatch.setattr(entry,'LABEL',recovery_entry.LABEL)
    att=dict(owner_approved=True,record_sha256=bp.digest(p.business),code_tree_sha256='a'*40,scope=recovery_entry.SCOPE,
        execution_premises=p.proof['manifest'],baseline_parent_metadata=p.baseline_metadata,
        approval=dict(expires_at=(p.now+timedelta(seconds=240)).isoformat()))
    encrypted=capsule(p.business,att,key)
    assert entry.decode_capsule(encrypted,key,'a'*40,p.now,record_sha256=bp.digest(p.business),label=recovery_entry.LABEL,scope=recovery_entry.SCOPE)==(p.business,att)
    changed=deepcopy(p.business);changed['import_yen']+=1
    with pytest.raises(Exception):entry.decode_capsule(capsule(changed,att,key),key,'a'*40,p.now,record_sha256=bp.digest(p.business),label=recovery_entry.LABEL,scope=recovery_entry.SCOPE)
    # Change attestation after signing inside an otherwise valid RSA/AES envelope.
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import zlib
    sealed=json.loads(base64.b64decode(encrypted));aes=key.decrypt(base64.b64decode(sealed['key']),padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=recovery_entry.LABEL))
    value=json.loads(zlib.decompress(AESGCM(aes).decrypt(base64.b64decode(sealed['nonce']),base64.b64decode(sealed['data']),recovery_entry.LABEL)))
    value['attestation']['execution_premises']['parent_snapshot']['sha256']='0'*64
    sealed['data']=base64.b64encode(AESGCM(aes).encrypt(base64.b64decode(sealed['nonce']),zlib.compress(entry.encode(value)),recovery_entry.LABEL)).decode()
    tampered=base64.b64encode(entry.encode(sealed)).decode()
    with pytest.raises(Exception):entry.decode_capsule(tampered,key,'a'*40,p.now,record_sha256=bp.digest(p.business),label=recovery_entry.LABEL,scope=recovery_entry.SCOPE)


@pytest.mark.parametrize('boundary',['native_reserved','parent_reserved','dispatch_intent_persisted'])
def test_bank_path_runner_loss_consumes_reservation_without_append(tmp_path,monkeypatch,boundary):
    p=setup_case(tmp_path,monkeypatch)
    def lose(stage):
        if stage==boundary:raise TimeoutError('synthetic runner disappeared')
    p.args['boundary']=lose
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==0 and json.loads(p.nt.payload)['phase']=='pending'
    assert _rows(json.loads(p.nt.payload),'recurring.sqlite3','recurring_checkpoint')[0]['successful_window_end']==C0
    assert summaries(json.loads(p.nt.payload))['UNKNOWN:20:1']['recovery']['bank_transitions']==p.proof


@pytest.mark.parametrize('fault',[None,'extra_revision','foreign_commit','unreviewed_code','unfinished_run','incomplete_inventory'])
def test_get_only_collector_independently_checks_signed_references(tmp_path,monkeypatch,fault):
    p=setup_case(tmp_path,monkeypatch);proof=deepcopy(p.proof);calls=[]
    bank_raw=bp.raw_node(proof['steps'][-1]['bank'][-1]);bank_meta=deepcopy(proof['parent_metadata'])
    bank_meta.update(id='bank-file',size=str(len(bank_raw)),md5Checksum=hashlib.md5(bank_raw).hexdigest())
    proof['manifest']['bank_snapshot']['metadata_digest']=bp.digest(bank_meta)
    class Request:
        method='GET'
        def __init__(self,value):self.value=value
        def execute(self,**kwargs):calls.append('GET');return deepcopy(self.value)
    class Resource:
        def list(self,**kwargs):
            label='parent' if kwargs['fileId']==p.pb.file_id else 'bank'
            refs=deepcopy(proof['lineages'][label])
            if fault=='extra_revision' and label=='parent':refs.insert(1,dict(id='unexplained',modifiedTime=NOW.isoformat()))
            return Request(dict(revisions=refs))
        def get_media(self,**kwargs):
            label='parent' if kwargs['fileId']==p.pb.file_id else 'bank'
            candidates=proof['steps'][-1][label]
            node=next((n for n in candidates if n['ref']['id']==kwargs.get('revisionId')),candidates[-1])
            return Request(bp.raw_node(node))
        def get(self,**kwargs):return Request(proof['parent_metadata'] if kwargs['fileId']==p.pb.file_id else bank_meta)
    class Drive:
        def files(self):return Resource()
        def revisions(self):return Resource()
    old=deepcopy(p.business['old_failure']);old['updated_at']=(NOW+timedelta(seconds=50)).isoformat()
    class Github:
        def get(self,path):
            step=proof['steps'][0]
            if path.startswith('compare/'):
                return dict(status='diverged' if fault=='foreign_commit' else 'ahead',behind_by=int(fault=='foreign_commit'))
            if path.startswith('contents/'):
                code=step['code'][path[len('contents/'):].split('?')[0]]
                if fault=='unreviewed_code':code+='\n# changed bank implementation'
                return dict(content=base64.b64encode(code.encode()).decode())
            if path.endswith('/jobs?per_page=100'):return dict(total_count=1,jobs=[step['job']])
            if path==f"actions/runs/{step['run']['id']}":return step['run']|({'status':'in_progress','conclusion':None} if fault=='unfinished_run' else {})
            if path.startswith('actions/runs?'):return dict(total_count=3 if fault=='incomplete_inventory' else 2,workflow_runs=[step['run'],old])
            raise AssertionError('unexpected API')
    run=lambda:bp.collect_bank_premises(p.business,proof['manifest'],p.baseline_metadata,Drive(),Github(),p.now,
        head='a'*40,log_read=lambda jid:proof['steps'][0]['log'])
    if fault:
        with pytest.raises(RuntimeError):run()
    else:
        captured=run();assert bp.effective_record(p.business,captured)['business_record']==p.business
    assert calls and set(calls)=={'GET'} and p.db.calls==0


@pytest.mark.parametrize('change',[None,'other_run','changed_operator','changed_evidence','unfinished'])
def test_posted_owner_proof_explains_only_its_exact_completed_operator(tmp_path,monkeypatch,change):
    from app import auto_expense_reconciliation as op
    p=setup_case(tmp_path,monkeypatch);record=deepcopy(p.business);proof=deepcopy(p.proof)
    record.update(checkpoint=C1,owner_updates_sha256=bp.digest([]),failed_run=record['old_failure'],
        business_contract='a'*64,financial_before_sha256='b'*64,financial_sha256='c'*64)
    monkeypatch.setattr(op,'POSTED_RECORD_SHA256',bp.digest(record))
    operator=dict(id=901,head_sha='d'*40,run_attempt=1,event='workflow_dispatch',path=bp.BANK_WORKFLOW)
    saved=op.posted_summary(record,[],operator,NOW.isoformat())
    proof['business_record']=record;proof['manifest']['business_record_sha256']=bp.digest(record)
    proof['posted_card_expenses']=saved
    prior={k:'unused' for k in bp.RUN_KEYS};prior.update(operator,status='completed',conclusion='failure')
    proof['history'].append(prior)
    if change=='other_run':prior['id']+=1
    if change=='changed_operator':prior['head_sha']='e'*40
    if change=='changed_evidence':saved['replay_allowed']=True
    if change=='unfinished':prior['status']='in_progress'
    run=lambda:bp.validate_bank_chain(record,proof,bp.raw_node(proof['steps'][-1]['parent'][-1]))
    if change:
        with pytest.raises(RuntimeError):run()
    else:assert run()==bp.digest(proof)
