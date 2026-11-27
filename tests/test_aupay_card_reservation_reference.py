"""Recover the PR213-shaped native123 reservation, retaining three UNKNOWNs."""
from copy import deepcopy
from datetime import timedelta,datetime
import hashlib
import json
from types import SimpleNamespace

import pytest

from app import aupay_card_reservation_reference as ref
from app import aupay_card_reconciliation_apply as repair
from app.aupay_card_window_preview import ReadOnlyState
from app.aupay_card_saved_readback import RepairReadbacks,standard
from app.aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
from app.aupay_card_reservation_recovery import summaries,unused_reservation_proof,digest
from app.aupay_card_incremental_recovery import preserved_unknown_proofs,_rows
from app.drive_run_state import DurableState,validate
from app.production_ledger import ProductionLedger
from app.aupay_card_recurring import SqliteRecurringRunState
from test_aupay_card_bank_premises import setup_case
from test_aupay_card_saved_readback import Drive
from test_aupay_card_reconciliation_apply import C0,C1

NEXT='e8f1a0d3-483b-4d56-a962-2acf8e94de99'


def reference_case(tmp_path,monkeypatch,mode='stable'):
    p=setup_case(tmp_path,monkeypatch)
    def stop(where):
        if where=='native_reserved':raise TimeoutError('synthetic independent-readback stop')
    p.args['boundary']=stop
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    assert json.loads(p.nt.payload)['generation']==123 and json.loads(p.pt.payload)['generation']==1416 and p.db.calls==0
    previous=ref.restore_failed_record(json.loads(p.nt.payload),p.args['run_id'])
    assert previous==p.record and digest(previous)==summaries(json.loads(p.nt.payload))['RESERVATION:'+p.args['run_id']]['record_sha256']
    p.failed_native=p.nt.payload;p.failed_parent=p.pt.payload;p.failed_record=previous
    native=Drive(p.nt,p.cb,mode);parent=Drive(p.pt,p.pb,metadata=p.proof['parent_metadata'])
    request=dict(schema=1,kind=ref.KIND,bindings=previous['bindings'],failed_run_uuid=p.args['run_id'],failed_record_sha256=digest(previous),
        native_before_sha256=repair.sha(p.nt.payload),native_before_metadata_digest=digest(standard(native.meta(0))),
        parent_before_sha256=repair.sha(p.pt.payload),parent_before_metadata_digest=digest(standard(parent.meta(0))),
        old_failure=p.args['execution_identity']|dict(status='completed',conclusion='failure'))
    expected=deepcopy(previous);expected.pop('business_record',None)
    for key in ('native_before_sha256','native_before_metadata_digest','parent_before_sha256','parent_before_metadata_digest','old_failure'):expected[key]=deepcopy(request[key])
    expected['recovery']=dict(previous_record=deepcopy(previous),failed_run_uuid=request['failed_run_uuid']);request['record_sha256']=digest(expected)
    monkeypatch.setattr(ref,'NEW_RECORD_SHA256',request['record_sha256'])
    record=ref.resolve_reference(request,json.loads(p.nt.payload),dict(sha256=request['native_before_sha256'],metadata_digest=request['native_before_metadata_digest']))
    later=p.now+timedelta(minutes=10);trace=ReconciliationDiagnostics()
    p.args.update(record=record,record_sha256=digest(record),run_id=NEXT,durable=DurableState(p.nt,p.cb),ledger=ProductionLedger(p.pt,p.pb),
        clock=lambda:later,state_dir=tmp_path/'reference-cache',boundary=lambda _:None,
        execution_identity=dict(id=31,head_sha='c'*40,head_branch='main',run_attempt=1,event='workflow_dispatch',path='.github/workflows/aupay-card-reservation-recovery.yml',created_at=later.isoformat()),
        state_observe=lambda:(dict(sha256=request['native_before_sha256'],metadata_digest=request['native_before_metadata_digest']),
            dict(sha256=request['parent_before_sha256'],metadata_digest=request['parent_before_metadata_digest'])),
        recovery_request=request,diagnostics=trace)
    state=SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(C0),alias_registry=lambda:())
    projected=repair.prepare_reconciliation(record,p.db,state,p.args['key_provider'].load())['projection']
    path=p.args['approval_provider'].path;approval=json.loads(path.read_bytes());approval.update(batch_ref=projected.batch_ref,expires_at=(later+timedelta(seconds=300)).isoformat());path.write_text(json.dumps(approval))
    readbacks=RepairReadbacks(ReadOnlyState(native,p.cb),ReadOnlyState(parent,p.pb),record,guard=p.args['guard'],deadline=100,
        diagnostics=trace,clock=lambda:native.now,sleeper=native.sleep)
    p.args.update(readbacks=readbacks,native_read=readbacks.sessions['native'].read,parent_read=readbacks.sessions['parent'].read)
    p.request,p.native_api,p.trace=request,native,trace
    return p


@pytest.mark.parametrize('mode',['stable','transient'])
def test_native123_to_c1_preserves_all_three_unknowns_and_only_two_rows(tmp_path,monkeypatch,mode):
    p=reference_case(tmp_path,monkeypatch,mode);before=deepcopy(p.db.rows);writes=(len(p.nt.writes),len(p.pt.writes))
    result=repair.execute_reconciliation(**p.args)
    assert result['success'] and result['written']==2 and result['import_yen']==4506 and result['expense_rows_written']==0
    assert p.db.calls==1 and p.db.rows[:10]==before and len(p.nt.writes)-writes[0]==4 and len(p.pt.writes)-writes[1]==2
    native=validate(p.nt.payload,p.cb);parent=json.loads(p.pt.payload)
    assert native['generation']==127 and parent['generation']==1418 and parent['sources']['aupay_card']['phase']=='ready'
    proofs=preserved_unknown_proofs(native,parent,C1);assert set(proofs)=={19,20,30}
    archived=summaries(native)['UNKNOWN:30:1'];assert archived['recovery_request']==p.request
    assert archived['recovery']['previous_record']==p.failed_record
    assert __import__('base64').b64decode(archived['native_before_raw_b64'])==p.failed_native
    assert __import__('base64').b64decode(archived['parent_before_raw_b64'])==p.failed_parent
    # Future fresh runners can restore the failed record of this new protocol
    # using its small exact reference, rather than adding another writer.
    assert ref.restore_failed_record(native,NEXT)==p.args['record']
    restored=tmp_path/'fresh-reference';DurableState(p.nt,p.cb).restore(restored)
    assert SqliteRecurringRunState(restored/'recurring.sqlite3',repo_root=p.args['repo_root']).successful_window_end().isoformat()==C1
    assert len([c for c in _rows(native,'capabilities.sqlite3','batch_capabilities') if c['terminal_reason']=='revoked_unused_no_dispatch'])==2
    with pytest.raises(RuntimeError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==1


@pytest.mark.parametrize('mode',['persistent','rollback','other_writer','deadline'])
def test_failed_get_retry_retains_three_unknown_archives_before_financial_writer(tmp_path,monkeypatch,mode):
    p=reference_case(tmp_path,monkeypatch,mode);writes=(len(p.nt.writes),len(p.pt.writes))
    with pytest.raises(RuntimeError):repair.execute_reconciliation(**p.args)
    assert p.db.calls==0 and len(p.nt.writes)-writes[0]==1 and len(p.pt.writes)==writes[1]
    n=json.loads(p.nt.payload);assert _rows(n,'recurring.sqlite3','recurring_checkpoint')[0]['successful_window_end']==C0
    assert {r['run_id'] for r in _rows(n,'recurring.sqlite3','recurring_runs') if r['status']=='UNKNOWN_NO_REPLAY'}=={'UNKNOWN:19:1','UNKNOWN:20:1','UNKNOWN:30:1'}
    assert 'INTENT:'+NEXT not in summaries(n)


@pytest.mark.parametrize('change',['request_digest','old_capability','native','binding','scope','prior_record'])
def test_reference_cannot_select_arbitrary_record_or_reuse_an_old_capability(tmp_path,monkeypatch,change):
    p=reference_case(tmp_path,monkeypatch);q=deepcopy(p.request);n=json.loads(p.failed_native)
    if change=='request_digest':q['record_sha256']='0'*64
    elif change=='old_capability':q['failed_run_uuid']=p.failed_record['recovery']['failed_run_uuid']
    elif change=='native':q['native_before_sha256']='0'*64
    elif change=='binding':q['bindings']['card_id']='different-target'
    elif change=='scope':q['import_yen']=999999
    elif change=='prior_record':q['failed_record_sha256']='0'*64
    with pytest.raises(RuntimeError):ref.resolve_reference(q,n,dict(sha256=p.request['native_before_sha256'],metadata_digest=p.request['native_before_metadata_digest']))
    assert p.db.calls==0 and p.nt.payload==p.failed_native


def test_small_reference_retains_existing_capsule_decoder_limit(tmp_path,monkeypatch):
    from app import aupay_card_reconciliation_entry as entry
    from app import aupay_card_reservation_recovery_entry as recovery
    from test_aupay_card_reconciliation_entry import capsule
    from cryptography.hazmat.primitives.asymmetric import rsa
    p=reference_case(tmp_path,monkeypatch);key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    monkeypatch.setattr(entry,'LABEL',recovery.LABEL)
    att=dict(owner_approved=True,record_sha256=digest(p.request),code_tree_sha256='d'*40,scope=recovery.SCOPE,
        approval=dict(expires_at=(p.args['clock']()+timedelta(seconds=240)).isoformat()))
    value=capsule(p.request,att,key)
    assert len(value)<60000 and len(entry.encode(p.request))<5000
    assert entry.decode_capsule(value,key,'d'*40,p.args['clock'](),record_sha256=digest(p.request),label=recovery.LABEL,scope=recovery.SCOPE)==(p.request,att)


def test_existing_bank_premises_still_extend_the_new_reference_anchor(tmp_path,monkeypatch):
    from app import aupay_card_bank_premises as bp
    from test_aupay_card_bank_premises import bank_step
    p=reference_case(tmp_path,monkeypatch);business=deepcopy(p.args['record'])
    old_step=p.proof['steps'][-1]
    step,descriptor=bank_step(p.pt.payload,bp.raw_node(old_step['bank'][-1]),6)
    for label in ('parent','bank'):
        step[label][0]=deepcopy(old_step[label][-1]);descriptor[label][0]=step[label][0]['ref']
    parent_raw=bp.raw_node(step['parent'][-1]);meta=deepcopy(p.proof['parent_metadata'])
    meta.update(version='108',modifiedTime=descriptor['parent'][-1]['modifiedTime'],size=str(len(parent_raw)),md5Checksum=hashlib.md5(parent_raw).hexdigest())
    proof=dict(business_record=business,manifest=dict(schema=1,business_record_sha256=digest(business),parent_snapshot=dict(sha256=repair.sha(parent_raw),metadata_digest=digest(meta)),
        bank_snapshot=dict(sha256=descriptor['bank'][-1]['sha256'],metadata_digest='bank-114'),transitions=[descriptor]),
        steps=[step],lineages={label:[dict(id=n['ref']['id'],modifiedTime=n['ref']['modifiedTime']) for n in step[label]] for label in ('parent','bank')},
        baseline_parent_metadata=p.proof['parent_metadata'],parent_metadata=meta,history=[step['run'],p.request['old_failure']],history_complete=True,skipped_jobs={})
    effective=bp.effective_record(business,proof);p.pt.payload=parent_raw
    native_api=Drive(p.nt,p.cb,'transient');parent_api=Drive(p.pt,p.pb,metadata=meta)
    readers=RepairReadbacks(ReadOnlyState(native_api,p.cb),ReadOnlyState(parent_api,p.pb),effective,guard=p.args['guard'],deadline=100,clock=lambda:native_api.now,sleeper=native_api.sleep)
    p.args.update(record=effective,record_sha256=digest(effective),ledger=ProductionLedger(p.pt,p.pb),readbacks=readers,native_read=readers.sessions['native'].read,parent_read=readers.sessions['parent'].read,
        state_observe=lambda:(dict(sha256=effective['native_before_sha256'],metadata_digest=effective['native_before_metadata_digest']),proof['manifest']['parent_snapshot']))
    state=SimpleNamespace(successful_window_end=lambda:datetime.fromisoformat(C0),alias_registry=lambda:())
    path=p.args['approval_provider'].path;approval=json.loads(path.read_bytes());approval['batch_ref']=repair.prepare_reconciliation(effective,p.db,state,p.args['key_provider'].load())['projection'].batch_ref;path.write_text(json.dumps(approval))
    assert repair.execute_reconciliation(**p.args)['success'] and p.db.calls==1
    assert set(preserved_unknown_proofs(json.loads(p.nt.payload),json.loads(p.pt.payload),C1))=={19,20,30}
    assert ref.restore_failed_record(json.loads(p.nt.payload),NEXT)==effective
