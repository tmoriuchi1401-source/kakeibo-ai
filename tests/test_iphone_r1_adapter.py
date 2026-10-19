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


class Gate:
    def current(self):
        return None, SimpleNamespace(cookie='synthetic-session'), dict(request_id='synthetic-login',created_at=NOW,expires_at=NOW+600), SimpleNamespace(issuer='https://accounts.google.com',subject='synthetic-owner',actor_id=OWNER)
    def read(self,_):return VerifiedRead(OWNER,NOW+600,True)
    def post(self,context,purpose,rid,binding,revision):return VerifiedPost(OWNER,purpose,rid,binding,revision,NOW,NOW+600,csrf_valid=True,session_active=True)


def ui_fixture(kind,monkeypatch):
    h=harness(kind,monkeypatch)
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
