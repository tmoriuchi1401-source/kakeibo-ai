"""Reservation-only recovery: existing APIs, synthetic data, no network."""
import base64
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from app import auto_expense_reconciliation as op
from app import aupay_card_incremental_recovery as proof
from app import aupay_card_first_window_apply as apply
from app.aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
from app.drive_run_state import DurableState,StateBinding,snapshot,envelope,validate
from app.production_ledger import ProductionLedger,initial_ledger,_encode
from test_card_selected_expenses import case,Transport,C1
from test_private_state_bindings import key


def reservation_case(tmp_path,key,failed_id=800):
    db,state,durable,registry,_=case(tmp_path)
    directory=state.path.parent
    for cls,name in ((apply.SqliteBatchManifestStore,'manifests'),(apply.SqliteBatchCapabilityStore,'capabilities'),
        (apply.SqliteBatchJournal,'journal'),(apply.SqliteLeaseManager,'leases')):
        cls(directory/(name+'.sqlite3'),repo_root=tmp_path/'repo')
    before=json.loads(envelope(durable.binding,snapshot(directory,durable.binding),generation=134))
    pending=deepcopy(before);pending.update(generation=135,phase='pending',attempt='a'*32)
    nt=Transport(proof._hash_bytes(pending))
    pb=StateBinding('production_run',db.sid,'folder','parent')
    parent_before=json.loads(initial_ledger(pb));parent_before['generation']=1428
    parent_pending=deepcopy(parent_before);parent_pending['generation']+=1
    parent_pending['sources']['aupay_card'].update(phase='pending',attempt='b'*32,error='')
    pt=Transport(_encode(parent_pending))
    revision=lambda rid:dict(id=rid,modifiedTime='2026-11-28T04:43:27+00:00',size='1',md5Checksum='f'*32)
    node=lambda v,rid:dict(sha256=proof._hash(v),generation=v['generation'],revision=revision(rid))
    record=dict(protocol=proof.RESERVATION_PROTOCOL,bindings=dict(spreadsheet_id=db.sid,folder_id='folder',
        card_id=durable.binding.file_id,parent_id=pb.file_id,projection_id='projection'),checkpoint=C1,
        checkpoint_row=proof._rows(before,'recurring.sqlite3','recurring_checkpoint'),
        anchor_summary_sha256=proof._hash(json.loads(proof._rows(before,'recurring.sqlite3','recurring_runs')[0]['summary_json'])),
        failed_execution=dict(id=failed_id,head_sha='c'*40,head_branch='main',run_attempt=1,status='completed',conclusion='failure',
            event='workflow_dispatch',path='.github/workflows/aupay-card-incremental-apply.yml',created_at='2026-11-28T04:43:00Z'),
        failed_job_id=900,native_before=node(before,'n0'),native_pending=dict(node(pending,'n1'),attempt='a'*32),
        parent_before=node(parent_before,'p0'),parent_pending=dict(node(parent_pending,'p1'),attempt='b'*32),
        tables=proof.reservation_content(before),financial_sha256='1'*64,documents_sha256='2'*64,acl_sha256='3'*64,
        source_contract_sha256='4'*64,window_end='2026-11-16T21:00:33+09:00',money_ids=['new-review'],previous_receipt_sha256='',
        evidence_mode='diagnostic',protected_heads={k:dict(file_id='doc-'+k,revision=revision(k)) for k in proof.NO_WRITE_MONEY_KEYS})
    rsa=serialization.load_pem_private_key(key.encode(),password=None)
    def args(index=0):
        return dict(record=record,native_before=before,native_pending=pending,parent_before=parent_before,parent_pending=parent_pending,
            durable=DurableState(nt,durable.binding),ledger=ProductionLedger(pt,pb),native_read=nt.read,parent_read=pt.read,
            observe=lambda:None,guard=lambda:None,key=rsa,state_dir=tmp_path/f'fresh-{index}',diagnostics=ReconciliationDiagnostics(),
            operator=dict(id=1000+index,head_sha='d'*40,head_branch='main',run_attempt=1,event='workflow_dispatch',
                path='.github/workflows/kakeibo-production.yml',created_at='2026-11-28T06:00:00Z'))
    return db,nt,pt,args


@pytest.mark.parametrize('failed_id',[800,801])
def test_generic_release_fresh_restore_and_preserved_tables(tmp_path,key,failed_id):
    db,nt,pt,args=reservation_case(tmp_path,key,failed_id);kw=args();old=deepcopy(kw['native_before'])
    result=op.confirm_unused_reservation(**kw)
    assert result['native_state_writes']==result['parent_state_writes']==1
    assert result['financial_write_requests']==0 and result['checkpoint']==C1 and not db.writes
    ready=validate(nt.read(),kw['durable'].binding);parent=json.loads(pt.read())
    assert ready['generation']==136 and parent['generation']==1430
    receipts=proof.reservation_receipts(ready,parent,C1,public_key=kw['key'].public_key())
    assert len(receipts)==1 and receipts[0]['record']['failed_execution']['id']==failed_id
    assert proof.reservation_content(ready)==proof.reservation_content(old)
    assert all(ready['files'][f]==old['files'][f] for f in ready['files'] if f!='recurring.sqlite3')
    again=op.confirm_unused_reservation(**args(1))
    assert again['native_state_writes']==again['parent_state_writes']==0
    assert len(nt.writes)==len(pt.writes)==1


@pytest.mark.parametrize('boundary',['native_before','native_after','native_readback','parent_before','parent_after','final_get'])
def test_loss_requires_new_approval_and_skips_confirmed_saves(tmp_path,key,boundary):
    _,nt,pt,args=reservation_case(tmp_path,key);nw,pw=nt.write,pt.write
    def native_write(raw):
        if boundary=='native_before':raise TimeoutError('secret')
        nw(raw)
        if boundary=='native_after':raise TimeoutError('secret')
    def parent_write(raw):
        if boundary=='parent_before':raise TimeoutError('secret')
        pw(raw)
        if boundary=='parent_after':raise TimeoutError('secret')
    nt.write=native_write;pt.write=parent_write;kw=args()
    nr,pr=kw['native_read'],kw['parent_read']
    def native_get():
        if boundary=='native_readback' and nt.writes:raise TimeoutError('secret')
        return nr()
    def parent_get():
        if boundary=='final_get' and pt.writes:raise TimeoutError('secret')
        return pr()
    kw.update(native_read=native_get,parent_read=parent_get)
    with pytest.raises(TimeoutError):op.confirm_unused_reservation(**kw)
    assert len(nt.writes)<=1 and len(pt.writes)<=1
    nt.write=nw;pt.write=pw
    op.confirm_unused_reservation(**args(1))
    assert len(nt.writes)==len(pt.writes)==1


@pytest.mark.parametrize('change',['attempt','generation','files','checkpoint','parent_source','tables','guard','finance','deadline'])
def test_incomplete_or_changed_evidence_denies_before_save(tmp_path,key,change):
    _,nt,pt,args=reservation_case(tmp_path,key);kw=args()
    if change=='attempt':kw['native_pending']['attempt']='c'*32
    elif change=='generation':kw['parent_pending']['generation']+=1
    elif change=='files':kw['native_pending']['files'].pop('leases.sqlite3')
    elif change=='checkpoint':kw['record']['checkpoint']='2026-11-05T21:00:33+09:00'
    elif change=='parent_source':kw['parent_pending']['sources']['bank']['counts']={'written':1}
    elif change=='tables':kw['record']['tables']['journal.sqlite3']['batch_events']['rows']=1
    elif change in {'guard','deadline'}:kw['guard']=lambda:(_ for _ in ()).throw(RuntimeError('competing_run'))
    elif change=='finance':kw['observe']=lambda:(_ for _ in ()).throw(RuntimeError('reservation_financial_changed'))
    with pytest.raises((RuntimeError,KeyError,ValueError)):op.confirm_unused_reservation(**kw)
    assert not nt.writes and not pt.writes


@pytest.mark.parametrize('change',['signature','financial_effect','missing','money_save','wrong_hash','duplicate'])
def test_only_exact_prewrite_diagnostic_can_prove_failure(tmp_path,key,change):
    _,_,_,args=reservation_case(tmp_path,key);r=args()['record']
    evidence=dict(native_before=r['native_before']['sha256'],parent_before=r['parent_before']['sha256'],
        checkpoint=r['checkpoint'],window_end=r['window_end'],source_contract=r['source_contract_sha256'],
        financial=r['financial_sha256'],documents=r['documents_sha256'])
    value=dict(error='incremental_apply_stopped_no_retry',reservation_evidence=evidence,
        diagnostic=dict(last_stage='money_guard',effects=dict(financial='not_reached',native_reservation='readback_confirmed')))
    op.verify_reservation_diagnostic(json.dumps(value),r)
    if change=='financial_effect':value['diagnostic']['effects']['financial']='writer_entered'
    elif change=='money_save':value['diagnostic']['last_stage']='money_save'
    elif change=='wrong_hash':evidence['financial']='0'*64
    elif change=='signature':value['error']='other'
    log='' if change=='missing' else json.dumps(value)
    if change=='duplicate':log+='\n'+log
    with pytest.raises(RuntimeError,match='unproven'):op.verify_reservation_diagnostic(log,r)


@pytest.mark.parametrize('stage',['native_write','native_transport_readback','native_reserved','money_guard','money_preflight'])
def test_complete_diagnostic_accepts_saved_reservation_before_any_side_effect(tmp_path,key,stage):
    _,_,_,args=reservation_case(tmp_path,key);r=args()['record']
    evidence=dict(native_before=r['native_before']['sha256'],parent_before=r['parent_before']['sha256'],
        checkpoint=r['checkpoint'],window_end=r['window_end'],source_contract=r['source_contract_sha256'],
        financial=r['financial_sha256'],documents=r['documents_sha256'])
    value=dict(error='incremental_apply_stopped_no_retry',reservation_evidence=evidence,
        diagnostic=dict(last_stage=stage,effects=dict(financial='not_reached',money='not_started',native_reservation='write_possible')))
    op.verify_reservation_diagnostic(json.dumps(value),r)
    value['diagnostic']['effects']['native_reservation']='not_started'
    with pytest.raises(RuntimeError,match='unproven'):op.verify_reservation_diagnostic(json.dumps(value),r)


def test_receipt_tamper_and_unknown_failure_are_not_authority(tmp_path,key):
    _,nt,pt,args=reservation_case(tmp_path,key);kw=args();op.confirm_unused_reservation(**kw)
    receipts=proof.reservation_receipts(json.loads(nt.read()),json.loads(pt.read()),C1,public_key=kw['key'].public_key())
    altered=deepcopy(receipts[0]);altered['record']['failed_execution']['id']+=1
    with pytest.raises(RuntimeError,match='signature'):proof.verify_reservation_receipt(altered,kw['key'].public_key())
    from app import aupay_card_incremental_guard as guard
    github=type('GH',(),{'get':lambda self,path:dict(total_count=1,workflow_runs=[dict(id=777,status='completed',
        conclusion='failure',run_attempt=1,created_at='2026-11-28T06:00:00Z')])})()
    with pytest.raises(RuntimeError,match='previous_dispatch_no_retry'):
        guard.history_controls(github,'a'*40,checkpoint_value=None)


def test_normal_apply_diagnostics_preserve_prewrite_boundary(tmp_path):
    from test_aupay_card_first_window_apply import fixture
    p=fixture(tmp_path);d=ReconciliationDiagnostics();p.execution['diagnostics']=d
    def guard():
        if p.durable._pending is not None:raise RuntimeError('competing_run')
    p.execution['guard']=guard
    with pytest.raises(RuntimeError,match='competing_run'):apply.execute_fixed(**p.execution)
    assert d.stage=='money_guard' and d.effects['financial']=='not_reached'
    assert len(p.durable.transport.writes)==len(p.ledger.transport.writes)==1 and not p.db.write_calls
    assert d.report(RuntimeError('competing_run'))['error_code']=='competing_run'


@pytest.mark.parametrize('fault,stage',[('native_write','native_write'),('native_readback','native_transport_readback'),
    ('money_write','money_save'),('financial_unknown','before_financial_append'),('checkpoint','checkpoint_commit')])
def test_diagnostics_identify_actual_stop_boundary_without_exception_body(tmp_path,fault,stage):
    from test_aupay_card_first_window_apply import fixture
    p=fixture(tmp_path);d=ReconciliationDiagnostics();p.execution['diagnostics']=d
    def fail(*a,**k):raise TimeoutError('private-body-should-never-appear')
    if fault=='native_write':p.durable.transport.write=fail
    elif fault=='native_readback':
        read=p.durable.transport.read
        p.durable.transport.read=lambda:fail() if p.durable.transport.writes else read()
    elif fault=='money_write':p.store.write=fail
    elif fault=='financial_unknown':p.db.mode='unknown'
    elif fault=='checkpoint':p.durable.commit=fail
    with pytest.raises(RuntimeError if fault=='financial_unknown' else TimeoutError) as captured:apply.execute_fixed(**p.execution)
    report=d.report(captured.value)
    assert report['last_stage']==stage and 'private-body' not in json.dumps(report)
    if fault.startswith('native') or fault=='money_write':assert not p.db.write_calls
    if fault=='financial_unknown':assert report['effects']['financial']=='append_possible'


def test_unsigned_alias_injection_in_receipt_summary_is_denied(tmp_path,key):
    import sqlite3,hashlib
    _,nt,pt,args=reservation_case(tmp_path,key);kw=args();op.confirm_unused_reservation(**kw)
    native=json.loads(nt.read());raw=base64.b64decode(native['files']['recurring.sqlite3']['data'])
    with sqlite3.connect(':memory:') as con:
        con.deserialize(raw)
        rid=proof.RESERVATION_PREFIX+str(kw['operator']['id'])
        s=json.loads(con.execute('SELECT summary_json FROM recurring_runs WHERE run_id=?',(rid,)).fetchone()[0])
        s['card_aliases']=[{'owner':'not-approved'}]
        # The normal API is append-only. Simulate a corrupted remote file as
        # well, so proof validation cannot depend only on SQLite's triggers.
        with pytest.raises(sqlite3.IntegrityError,match='append_only_run'):
            con.execute('UPDATE recurring_runs SET summary_json=? WHERE run_id=?',(json.dumps(s),rid))
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            assert name.isidentifier();con.execute('DROP TRIGGER "'+name+'"')
        con.execute('UPDATE recurring_runs SET summary_json=? WHERE run_id=?',(json.dumps(s),rid));con.commit();raw=con.serialize()
    native['files']['recurring.sqlite3']=dict(data=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(RuntimeError,match='receipt_invalid'):
        proof.reservation_receipts(native,json.loads(pt.read()),C1,public_key=kw['key'].public_key())


def test_schema_change_with_identical_rows_and_valid_receipt_is_denied(tmp_path,key):
    import sqlite3,hashlib
    _,nt,pt,args=reservation_case(tmp_path,key);kw=args();op.confirm_unused_reservation(**kw)
    native=json.loads(nt.read());raw=base64.b64decode(native['files']['recurring.sqlite3']['data'])
    with sqlite3.connect(':memory:') as con:
        con.deserialize(raw);con.execute('CREATE INDEX unexpected ON recurring_runs(status)');con.commit();raw=con.serialize()
    native['files']['recurring.sqlite3']=dict(data=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(RuntimeError,match='reservation_content_changed'):
        proof.reservation_receipts(native,json.loads(pt.read()),C1,public_key=kw['key'].public_key())


def test_dynamic_record_still_needs_signed_exact_digest_and_strict_schema(tmp_path,key,monkeypatch):
    from app import aupay_card_reconciliation_entry as entry
    from test_aupay_card_reconciliation_entry import capsule
    _,_,_,args=reservation_case(tmp_path,key);kw=args();r=kw['record'];now=datetime.now(timezone.utc)
    monkeypatch.setattr(entry,'LABEL',op.LABEL)
    att=dict(record_sha256=proof._hash(r),code_tree_sha256='f'*40,owner_approved=True,
        scope='card_unused_reservation_state_only',approval=dict(expires_at=(now+timedelta(seconds=200)).isoformat()))
    text=capsule(r,att,kw['key'])
    decoded,_=entry.decode_capsule(text,kw['key'],'f'*40,now,label=op.LABEL,scope=att['scope'],record_validator=proof.validate_reservation_record)
    assert decoded==r
    att['record_sha256']='0'*64
    with pytest.raises(RuntimeError):entry.decode_capsule(capsule(r,att,kw['key']),kw['key'],'f'*40,now,
        label=op.LABEL,scope=att['scope'],record_validator=proof.validate_reservation_record)


def hosted_case(tmp_path,key,monkeypatch,*,apply_mode):
    """Exercise the existing hosted entry and codec, not only the core function."""
    from types import SimpleNamespace
    from app import google_clients,projection_store,aupay_card_window_preview as window
    from app import aupay_card_reconciliation_entry as entry,drive_run_state,aupay_card_bank_premises as bank
    from test_aupay_card_reconciliation_entry import capsule
    _,nt,pt,args=reservation_case(tmp_path,key);kw=args();r=kw['record'];rsa=kw['key']
    head='d'*40;tree='e'*40;now=datetime.now(timezone.utc).replace(microsecond=0)
    revisions={};media={}
    for label,binding in [('native',kw['durable'].binding),('parent',kw['ledger'].binding)]:
        revisions[binding.file_id]=[]
        for stage in ('before','pending'):
            node=r[label+'_'+stage]
            node['revision']['modifiedTime']='2026-11-28T04:42:00Z' if stage=='before' else '2026-11-28T04:43:27Z'
            import hashlib
            raw=proof._hash_bytes(kw[label+'_'+stage]);node['revision'].update(size=str(len(raw)),md5Checksum=hashlib.md5(raw).hexdigest())
            revisions[binding.file_id].append(node['revision']);media[(binding.file_id,node['revision']['id'])]=raw
    docs={k:dict(records={}) if k=='money' else {} for k in proof.NO_WRITE_MONEY_KEYS}
    r['documents_sha256']=proof._hash(docs)
    finance={('支出明細','FORMULA'):[['header']]}
    r['financial_sha256']=proof._hash({t+'|'+v:rows for (t,v),rows in finance.items()})
    acl={k:dict(id=k) for k in ('card','parent','folder','ledger')};r['acl_sha256']=proof._hash(acl)
    for name,node in r['protected_heads'].items():
        node['revision']['modifiedTime']='2026-11-28T04:00:00Z';revisions[node['file_id']]=[node['revision']]
    class Request:
        method='GET'
        def __init__(self,value):self.value=value
        def execute(self,**kwargs):assert kwargs.get('num_retries')==0;return deepcopy(self.value)
    class Drive:
        def revisions(self):return self
        def files(self):return self
        def list(self,**kwargs):return Request(dict(revisions=revisions[kwargs['fileId']]))
        def get_media(self,**kwargs):return Request(media[(kwargs['fileId'],kwargs['revisionId'])])
        def get(self,**kwargs):
            name={r['bindings'][field]:name for name,field in [('card','card_id'),('parent','parent_id'),
                ('folder','folder_id'),('ledger','spreadsheet_id')]}[kwargs['fileId']]
            return Request(acl[name])
    ro=Drive();factories=[]
    monkeypatch.setattr(google_clients,'read_only_drive_service',lambda:ro)
    monkeypatch.setattr(google_clients,'read_only_sheets_service',lambda:object())
    monkeypatch.setattr(google_clients,'drive_service',lambda:factories.append('state') or ro)
    monkeypatch.setattr(drive_run_state,'DriveStateTransport',lambda service,binding:nt if binding.source!='production_run' else pt)
    monkeypatch.setattr(apply,'financial_data',lambda *a,**k:finance)
    class Docs:
        def __init__(self,*a,**k):pass
        def read(self,k):return deepcopy(docs[k])
        def _file(self,k):return r['protected_heads'][k]['file_id']
    monkeypatch.setattr(projection_store,'DriveProjectionStore',Docs)
    monkeypatch.setattr(proof,'preserved_unknown_proofs',lambda *a:{})
    def state_proof(transport):return dict(sha256=op.sha(transport.payload),metadata_digest='a'*64)
    class Reader:
        def __init__(self,service,binding):self.binding=binding;self.source=nt if binding.source!='production_run' else pt
        def read(self):return self.source.read()
        def observe(self):return self.read(),state_proof(self.source)
    monkeypatch.setattr(window,'ReadOnlyState',Reader)
    operator=dict(kw['operator'],id=1000,status='in_progress',created_at=now.isoformat(),display_title=op.RELEASE_TITLE)
    if not apply_mode:operator['display_title']=op.RELEASE_TITLE.removesuffix('apply')+'preview'
    failure=dict(r['failed_execution'],updated_at='2026-11-28T04:44:00Z')
    active=[]
    def get(path):
        if path=='git/ref/heads/main':return dict(object=dict(sha=head))
        if path=='actions/runs/1000':return operator
        if path==f"actions/runs/{failure['id']}":return failure
        if path.endswith('/jobs?per_page=100'):return dict(total_count=1,jobs=[dict(id=900,status='completed',conclusion='failure')])
        if path.startswith('compare/'):return dict(status='ahead',behind_by=0)
        if path.startswith('actions/workflows/'):return dict(total_count=1,workflow_runs=[operator])
        assert path.startswith('actions/runs?status=');return dict(total_count=len(active),workflow_runs=active)
    monkeypatch.setattr(window,'GitHub',lambda env:SimpleNamespace(get=get))
    diagnostic=dict(error='incremental_apply_stopped_no_retry',reservation_evidence=dict(native_before=r['native_before']['sha256'],
        parent_before=r['parent_before']['sha256'],checkpoint=C1,window_end=r['window_end'],source_contract=r['source_contract_sha256'],
        financial=r['financial_sha256'],documents=r['documents_sha256']),diagnostic=dict(last_stage='money_guard',
        effects=dict(financial='not_reached',native_reservation='readback_confirmed')))
    monkeypatch.setattr(bank,'_job_log',lambda *a:json.dumps(diagnostic))
    att=dict(record_sha256=proof._hash(r),code_tree_sha256=tree,owner_approved=True,scope='card_unused_reservation_state_only',
        main_sha=head,observed_at=now.isoformat(),financial_write_authority=False,
        operation='confirm_unused_card_reservation' if apply_mode else 'preview_unused_card_reservation',
        approval=dict(expires_at=(now+timedelta(seconds=600)).isoformat(),approval_reference='fresh-explicit-approval'),
        state_snapshot=dict(native=state_proof(nt),parent=state_proof(pt)))
    monkeypatch.setattr(entry,'LABEL',op.LABEL)
    env=dict(GITHUB_ACTIONS='true',GITHUB_REF='refs/heads/main',GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',
        KAKEIBO_PRODUCTION_ENABLED='true',GITHUB_SHA=head,KAKEIBO_VALIDATED_MAIN_SHA=head,GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_ACTOR='tmoriuchi1401-source',GITHUB_TRIGGERING_ACTOR='tmoriuchi1401-source',GITHUB_RUN_ATTEMPT='1',
        GITHUB_RUN_ID='1000',RUNNER_ENVIRONMENT='github-hosted',WORKFLOW_SHA=head,
        WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main',MANUAL_SCOPE='card_expenses',
        CONFIRM='APPLY',GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps(dict(private_key=key)),RUNNER_TEMP=str(tmp_path),
        SPREADSHEET_ID=r['bindings']['spreadsheet_id'],KAKEIBO_STATE_FOLDER_ID='folder',AUPAY_CARD_STATE_FILE_ID=r['bindings']['card_id'],
        KAKEIBO_RUN_LEDGER_FILE_ID='parent',KAKEIBO_PROJECTION_FOLDER_ID='projection')
    from app.private_state_bindings import wrap
    # production_flow decodes the five native/parent IDs, while the projection
    # binding reaches this entry encrypted and must use the existing unwrap.
    env['KAKEIBO_PROJECTION_FOLDER_ID']=wrap('KAKEIBO_PROJECTION_FOLDER_ID',r['bindings']['projection_id'],key)
    def sign():env['CARD_EXPENSE_RECONCILIATION']=op.RESERVATION_PREFIX+capsule(r,att,rsa)
    sign();monkeypatch.setattr('subprocess.check_output',lambda *a,**k:tree+'\n')
    return env,nt,pt,r,att,sign,active,factories


@pytest.mark.parametrize('apply_mode',[False,True])
def test_hosted_signed_entry_preview_and_apply(tmp_path,key,monkeypatch,apply_mode):
    env,nt,pt,_,_,_,_,factories=hosted_case(tmp_path,key,monkeypatch,apply_mode=apply_mode)
    result=op.run_from_environment(env,apply=apply_mode)
    assert result['success'] and result['financial_write_requests']==0
    assert len(nt.writes)==len(pt.writes)==int(apply_mode)
    assert len(factories)==2*int(apply_mode)


@pytest.mark.parametrize('change',['snapshot','deadline','revision','unknown_money','finance','competing'])
def test_hosted_rejects_changed_or_incomplete_evidence_before_writer_factory(tmp_path,key,monkeypatch,change):
    env,nt,pt,r,att,sign,active,factories=hosted_case(tmp_path,key,monkeypatch,apply_mode=True)
    if change=='snapshot':att['state_snapshot']['native']['sha256']='0'*64
    elif change=='deadline':att['approval']['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    elif change=='revision':r['native_before']['revision']['id']='missing'
    elif change=='unknown_money':r['documents_sha256']='0'*64
    elif change=='finance':r['financial_sha256']='0'*64
    elif change=='competing':active.append(dict(id=888,status='queued'))
    att['record_sha256']=proof._hash(r);sign()
    with pytest.raises(op.AutoExpenseReconciliationFailure):op.run_from_environment(env,apply=True)
    assert not nt.writes and not pt.writes and not factories
