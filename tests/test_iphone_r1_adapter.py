"""Adapter isolation tests; HTTP auth and DOM tests live in separate modules."""
from copy import deepcopy
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import load, observe_ledger, planned_after
from app.unified_receipt_purposes import VerifiedRead, VerifiedPost
from app.unified_receipt_review import digest
from staging_shadow.iphone_confirmation_ui import OneTabConfirmation
from draft_runtime_fixtures import harness, NOW, set_value, RAW
from accounting_gate_fixtures import reader_for
from test_pdf_intake_authority import OWNER
from test_synthetic_legacy_writer import no_external_connections


@pytest.fixture(autouse=True)
def audit_dispatch(monkeypatch):
    from app.receipt_confirmation import ReceiptConfirmation
    original=ReceiptConfirmation._write_accounting_plan
    calls=[]
    def audited(service,*args,**kwargs):
        calls.append(1)
        return original(service,*args,**kwargs)
    monkeypatch.setattr(ReceiptConfirmation,'_write_accounting_plan',audited)
    yield calls
    assert len(calls)<=1


class Gate:
    def current(self):
        return None, SimpleNamespace(cookie='synthetic-session'), dict(request_id='synthetic-login',created_at=NOW,expires_at=NOW+600), SimpleNamespace(issuer='https://accounts.google.com',subject='synthetic-owner',actor_id=OWNER)
    def read(self,_):return VerifiedRead(OWNER,NOW+600,True)
    def post(self,context,purpose,rid,binding,revision):return VerifiedPost(OWNER,purpose,rid,binding,revision,NOW,NOW+600,csrf_valid=True,session_active=True)


def ui_fixture(kind,monkeypatch):
    h=harness(kind,monkeypatch)
    set_value(h.readers.authority,{})
    def ownership(inputs,plan):
        source=inputs['source_binding']['source']
        h.own=reader_for('legacy_confirmation',source,RAW,[],dict(namespace='receipt_confirmation',scope='whole',
            unit_id=None,receipt_index=None,input_snapshot=digest(inputs),authority_revision=7,
            ledger_targets=[r[0] for _,r in plan],case_id=h.f.case_id))
        return h.own.reader.observe('legacy_confirmation','u1').value()
    c=SimpleNamespace(store=h.store,shared=h.shared,readers=h.readers,kind=kind,source_id=h.f.sid,
        source=deepcopy(h.c['source']),prepared_at='2026-10-19 00:00:00',authority_revision=7,review_id=h.f.key,ownership=ownership)
    key=Fernet.generate_key()
    def rebuild():return OneTabConfirmation({h.f.case_id:c},{},key,owner=OWNER,clock=lambda:NOW,enabled=True,isolated_synthetic=True)
    return h,c,rebuild


@pytest.mark.parametrize('kind',['normal','medical'])
def test_adapter_real_writer_and_readonly_reconstruction(monkeypatch,kind):
    h,c,rebuild=ui_fixture(kind,monkeypatch);ui=rebuild();gate=Gate()
    proof,claims=ui.proof(h.f.case_id,'post',gate)
    payload=dict(proof=proof,value=claims['draft']['value'],confirmed=True)
    result=ui.post(payload,gate)
    assert result['status']=='COMPLETE',result
    request=ui._requests(h.f.case_id)[0]
    assert observe_ledger(h.readers.ledger)[0]==planned_after(request['before'],request['plan'])
    assert h.readers.ledger.writes==len(request['plan'])
    saved=[p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]
    assert rebuild().inspect(claims['request_id'],gate)['status']=='COMPLETE'
    assert rebuild().post(payload,gate)['status']=='COMPLETE'
    assert [p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]==saved


@pytest.mark.parametrize('kind',['normal','medical'])
@pytest.mark.parametrize('count',[2,16])
def test_independent_ui_instances_race_never_dispatch_twice(monkeypatch,kind,count):
    h,c,rebuild=ui_fixture(kind,monkeypatch);ui=rebuild();gate=Gate();proof,claims=ui.proof(h.f.case_id,'post',gate)
    instances=[rebuild() for _ in range(count)]
    def post(instance):
        try:return instance.post(dict(proof=proof,value=claims['draft']['value'],confirmed=True),gate)
        except (StateError,TimeoutError):return None
    with ThreadPoolExecutor(count) as pool:results=list(pool.map(post,instances))
    assert h.readers.ledger.writes in {0,len(claims['snapshot']['plan'])}
    assert len(ui._requests(h.f.case_id))<=1
    before=h.readers.ledger.writes
    for instance in instances:instance.inspect(claims['request_id'],gate)
    assert h.readers.ledger.writes==before


@pytest.mark.parametrize('kind',['normal','medical'])
@pytest.mark.parametrize('point',['before_dispatch','after_ledger_1','before_completion','after_native_completion'])
def test_stop_classification_and_no_redispatch(monkeypatch,kind,point):
    h,c,rebuild=ui_fixture(kind,monkeypatch);ui=rebuild();gate=Gate();proof,claims=ui.proof(h.f.case_id,'post',gate)
    def stop(p):
        if p==point:raise RuntimeError('synthetic process interruption')
    c.checkpoint=stop
    payload=dict(proof=proof,value=claims['draft']['value'],confirmed=True)
    result=ui.post(payload,gate)
    assert result['status']==('PARTIAL' if point=='after_ledger_1' else 'UNKNOWN')
    before=[p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]
    assert rebuild().post(payload,gate)['status']==result['status']
    assert [p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]==before


@pytest.mark.parametrize('kind',['normal','medical'])
@pytest.mark.parametrize('backend,number',[('native',1),('native',2),('native',3),('native',4),
    ('shared',1),('shared',2),('shared',3),('shared',4),('ledger',1),('ledger',2)])
@pytest.mark.parametrize('fault',['412','lost'])
def test_each_cas_response_loss_stops_without_followup_writes(monkeypatch,kind,backend,number,fault):
    h,c,rebuild=ui_fixture(kind,monkeypatch);ui=rebuild();gate=Gate();proof,claims=ui.proof(h.f.case_id,'post',gate)
    port={'native':h.native,'shared':h.shared.io,'ledger':h.readers.ledger}[backend]
    original=port.replace_versioned;attempts=[]
    def broken(*args):
        attempts.append(1)
        if len(attempts)==number:
            if fault=='lost':original(*args);raise TimeoutError('synthetic lost response')
            raise StateError('HTTP_412')
        return original(*args)
    monkeypatch.setattr(port,'replace_versioned',broken)
    payload=dict(proof=proof,value=claims['draft']['value'],confirmed=True)
    result=ui.post(payload,gate)
    assert len(attempts)==number
    assert result['status'] in {'UNKNOWN','PARTIAL','COMPLETE'}
    before=[p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]
    # Old request may have no persisted qualification if its very first CAS
    # failed. Inspection still never qualifies, repairs, or dispatches it.
    rebuild().inspect(claims['request_id'],gate)
    if ui._requests(h.f.case_id):rebuild().post(payload,gate)
    assert len(attempts)==number
    assert [p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]==before


@pytest.mark.parametrize('kind',['normal','medical'])
def test_posted_snapshot_never_changes_when_draft_is_edited(monkeypatch,kind):
    h,c,rebuild=ui_fixture(kind,monkeypatch);ui=rebuild();gate=Gate();proof,claims=ui.proof(h.f.case_id,'post',gate)
    result=ui.post(dict(proof=proof,value=claims['draft']['value'],confirmed=True),gate)
    assert result['status']=='COMPLETE'
    request=deepcopy(ui._requests(h.f.case_id)[0])
    d=load(h.readers.draft.read_versioned()[0]);d['value']['fields']['memo']='later draft'
    d['revision']+=1;d['input_digest']=digest(d['value']);set_value(h.readers.draft,d)
    saved=h.native.read_versioned(),h.readers.ledger.read_versioned(),h.shared.io.read_versioned()
    observed=rebuild().inspect(claims['request_id'],gate)
    assert observed['snapshot']==request['inputs']['draft']
    assert ui._requests(h.f.case_id)==[request]
    assert saved==(h.native.read_versioned(),h.readers.ledger.read_versioned(),h.shared.io.read_versioned())


@pytest.mark.parametrize('field,value',[('date','2026/1/3'),('merchant',' Synthetic clinic ')])
def test_medical_parser_normalization_requires_an_explicit_edit(monkeypatch,field,value):
    h,c,rebuild=ui_fixture('medical',monkeypatch);ui=rebuild();gate=Gate()
    d=load(h.readers.draft.read_versioned()[0]);d['value']['fields'][field]=value
    d['revision']+=1;d['input_digest']=digest(d['value']);set_value(h.readers.draft,d)
    with pytest.raises(StateError,match='normalization_requires_explicit_edit'):ui.proof(h.f.case_id,'post',gate)
    assert h.readers.ledger.writes==0


@pytest.mark.parametrize('kind',['normal','medical'])
def test_new_request_cannot_replace_completed_authority_evidence(monkeypatch,kind):
    h,c,rebuild=ui_fixture(kind,monkeypatch);ui=rebuild();gate=Gate()
    proof,claims=ui.proof(h.f.case_id,'post',gate)
    class OtherSession(Gate):
        def current(self):
            rt,ticket,record,actor=super().current();ticket.cookie='other-session'
            return rt,ticket,record,actor
    second=OtherSession();newproof,newclaims=ui.proof(h.f.case_id,'post',second)
    assert claims['request_id']!=newclaims['request_id']
    assert ui.post(dict(proof=proof,value=claims['draft']['value'],confirmed=True),gate)['status']=='COMPLETE'
    before=[p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]
    with pytest.raises(StateError,match='request_reuse'):
        rebuild().post(dict(proof=newproof,value=newclaims['draft']['value'],confirmed=True),second)
    assert [p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]==before
    assert rebuild().inspect(claims['request_id'],gate)['status']=='COMPLETE'


@pytest.mark.parametrize('kind',['normal','medical'])
@pytest.mark.parametrize('point',['evidence_response_lost','before_qualification'])
def test_unknown_before_native_qualification_remains_fenced_after_rebuild(monkeypatch,kind,point):
    h,c,rebuild=ui_fixture(kind,monkeypatch);ui=rebuild();gate=Gate()
    proof,claims=ui.proof(h.f.case_id,'post',gate)
    payload=dict(proof=proof,value=claims['draft']['value'],confirmed=True)
    if point=='evidence_response_lost':
        original=h.readers.authority.replace_versioned
        def lost(*args):original(*args);raise TimeoutError('synthetic evidence response loss')
        monkeypatch.setattr(h.readers.authority,'replace_versioned',lost)
        assert ui.post(payload,gate)['status']=='UNKNOWN'
    else:
        def stop(name):
            if name==point:raise RuntimeError('synthetic stop before native CAS')
        c.checkpoint=stop
        assert ui.post(payload,gate)['status']=='UNKNOWN'
    assert not ui._requests(h.f.case_id) and h.readers.ledger.writes==0
    before=[p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]
    rebuilt=rebuild();assert rebuilt.read(gate)[0]['result']['status']=='UNKNOWN'
    with pytest.raises(StateError,match='UNKNOWN'):rebuilt.proof(h.f.case_id,'post',gate)
    with pytest.raises(StateError,match='UNKNOWN'):rebuilt.post(payload,gate)
    assert [p.read_versioned() for p in (h.native,h.shared.io,h.readers.ledger,h.readers.authority)]==before
