"""Registered new-source OIDC -> private conditional proof, offline only."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs
import pytest
from app.drive_run_state import StateError
from app.page_receipt_model import PageUnit,SourceRef,stable_page
from app.pdf_intake_authority import digest,review_identity,page_key,binding
from app.pdf_intake_registry import RegistryStore,empty_registry,canonical
from services.human_general.generic_intake import RegisteredPageRuntime,verify_machine_token
from services.human_general.web import create_app
from test_human_general_http import HttpRig
from test_human_general_auth_transport import keys
from test_google_shared_login import form,next_request,login,resume

RAW=b'new-source synthetic immutable PDF bytes'

class Memory:
    def __init__(self):self.raw=canonical(empty_registry('anchor'));self.tag='"0"';self.puts=0
    def read_versioned(self):return self.raw,self.tag
    def replace_versioned(self,before,tag,after):
        if before!=self.raw or tag!=self.tag:raise StateError('HTTP_412')
        self.raw=after;self.puts+=1;self.tag='"'+str(self.puts)+'"'

class Drive:
    def __init__(self):
        source=SourceRef(source_file_id='new-independent-pdf',source_content_hash=sha256(RAW).hexdigest(),page_count=3)
        value=PageUnit(source=source,page_number=2,stable_page_identity=stable_page(source,2),automatic_classification='sensitive_unknown',
            automatic_reason='privacy_unresolved',observation_complete=True,extraction_status='extracted',
            observation_render_hash='a'*64,review_identity='0'*64,authority_revision=1).model_dump()
        value['review_identity']=review_identity(value);self.page=PageUnit.model_validate(value);self.expected=value
        self.store=RegistryStore(Memory(),'anchor');self.target=self.store.register(value)
        self.config={'binding':digest(['new-page',value])};self._source_cache=None
    def fresh(self):
        if self.store.load()['pages'][self.target]['page']!=self.expected:raise StateError('registered_page_stale')
        return self.page
    def source(self,sid):
        if sid!=self.page.source.source_file_id:raise StateError('registered_page_source_forbidden')
        return RAW
    def begin_request(self):pass
    def end_request(self):pass


def registered(monkeypatch,keys):
    rig=HttpRig(keys);drive=Drive()
    monkeypatch.setattr('services.human_general.generic_intake.RegisteredPageDrive',lambda *_:drive)
    rig.runtime=RegisteredPageRuntime(rig.db,rig.settings,rig.key,{},drive.target,{},clock=lambda:rig.now,
        identity=rig.runtime.identity,exchange=rig.exchange)
    rig.app=create_app(lambda:rig.runtime);rig.app.testing=True;rig.client=rig.app.test_client()
    rig.link=rig.runtime.seed();rig.start_path=urlsplit(rig.link).path+'?'+urlsplit(rig.link).query
    rig.rid=parse_qs(urlsplit(rig.link).query)['request'][0]
    return rig,drive


def test_new_registered_pdf_owner_oidc_explicit_post_proof_and_replay(monkeypatch,keys):
    rig,drive=registered(monkeypatch,keys)
    rig.start();assert rig.callback().status_code==303
    assert not drive.store.load()['pages'][drive.target]['authority']
    assert rig.confirm().status_code==303
    saved=drive.store.load()['pages'][drive.target]['authority']['single_page_ai']
    result=rig.runtime.sealer.verify(saved['proof'],binding(drive.expected,'single_page_ai'))
    assert result['request_id']==rig.rid and len(saved['state']['grants'])==len(saved['state']['audit'])==1
    before=drive.store.io.raw,drive.store.io.tag,drive.store.io.puts
    assert rig.get('/result').status_code==200
    assert rig.get(rig.start_path).status_code==409 and rig.confirm().status_code==409
    assert (drive.store.io.raw,drive.store.io.tag,drive.store.io.puts)==before


@pytest.mark.parametrize('change',['issuer','audience','owner','nonce','expired'])
def test_registered_owner_oidc_rejection_no_authority(monkeypatch,keys,change):
    rig,drive=registered(monkeypatch,keys);rig.start()
    rig.claim_changes={'issuer':{'iss':'https://evil.test'},'audience':{'aud':'wrong'},'owner':{'sub':'not-owner'},
        'nonce':{'nonce':'wrong'},'expired':{'exp':rig.now-1}}[change]
    assert rig.callback().status_code==403
    assert not drive.store.load()['pages'][drive.target]['authority']


@pytest.mark.parametrize('origin',[None,'null','https://else.test'])
def test_registered_confirm_origin_cannot_fallback(monkeypatch,keys,origin):
    rig,drive=registered(monkeypatch,keys);rig.start();rig.callback()
    _,etag=rig.runtime.state(rig.rid,'authorities').read_versioned()
    response=rig.post('/confirm',{'action':'confirm','csrf':rig.ticket.csrf,'etag':etag},origin=origin)
    assert response.status_code==400 and not drive.store.load()['pages'][drive.target]['authority']


def test_machine_identity_is_google_signature_audience_and_exact_existing_sa(keys):
    from google.auth import jwt
    from google.oauth2 import id_token
    signer,public=keys
    now=__import__('time').time();aud='https://confirmation.example.test'
    claims={'iss':'https://accounts.google.com','sub':'existing-sa-sub','aud':aud,'iat':int(now),'exp':int(now)+300,'email_verified':True}
    token=jwt.encode(signer,claims).decode()
    request=lambda url,**kw:SimpleNamespace(status=200,data=json.dumps({'synthetic-key':public}).encode())
    verify=lambda raw,audience:id_token.verify_oauth2_token(raw,request,audience)
    verify_machine_token(token,aud,'existing-sa-sub',verifier=verify,clock=lambda:now)
    for wrong in ['other-sa-sub',None]:
        with pytest.raises(StateError):verify_machine_token(token,aud,wrong,verifier=verify,clock=lambda:now)
    with pytest.raises(StateError):verify_machine_token(token,aud+'.evil','existing-sa-sub',verifier=verify,clock=lambda:now)
    with pytest.raises(StateError):verify_machine_token(token[:-10]+'abcdefghij',aud,'existing-sa-sub',verifier=verify,clock=lambda:now)


def review_registered(monkeypatch,keys):
    from test_receipt_item_review import candidate,CATS
    from app.general_receipt_completion import draft
    from app.models import ReceiptResult
    from app.receipt_item_review import prepare,fields,card,snapshot,evaluate
    from app.receipt_item_confirmation import annotated,ConfirmedItems
    from app.receipt_audit import MemoryAuditRepository
    from app.receipt_item_review_ui import encoded_rows
    from services.human_general.generic_review import RegisteredReviewRuntime,RegisteredReaders
    record,p,raw,manifest=candidate()
    page=p.model_dump();page['review_identity']=review_identity(page);p=PageUnit.model_validate(page)
    parsed=ReceiptResult.model_validate(record['legacy']['parsed']);uid=record['legacy']['identity']['receipt_unit_id']
    record=prepare(draft(p,manifest,uid,[parsed,parsed],categories=CATS),CATS)
    record=annotated(record,[parsed,parsed],CATS)
    current=fields(record);current.update(action='記帳する',structure_confirmation='確認済み')
    s=snapshot(card(record,categories=CATS),current)
    pseudo=ConfirmedItems('00000000-0000-4000-8000-000000000009',record['digest'],digest(s),digest(current),digest(record['legacy']['identity']),'a'*64,1,'a'*64)
    validation=evaluate(record,current,CATS,confirmation=pseudo)
    drive=Drive();drive.store=RegistryStore(Memory(),'anchor');drive.expected=page;drive.page=p
    drive.target=drive.store.register(page);drive.config={'binding':digest(['new-review-page',page])}
    state=drive.store.load();state['pages'][drive.target]['units'][uid]={
        'status':'needs_review','candidate':{'record':record,'view':card(record,categories=CATS),'manifest':manifest,
            'posting':{'snapshot':s,'parsed':validation['parsed'],'plan_digest':'c'*64,
                'unit_binding':{'receipt_unit_id':uid,'segmentation_digest':manifest['segmentation_digest'],
                    'item_identities':[i['item_id'] for i in record['items']]}}},
        'posting_authority':None,'intent':None,'readback':None}
    state['generation']+=1;drive.store.save(state)
    rig=HttpRig(keys)
    class Readers(RegisteredReaders):
        def __init__(self,*_):
            self.drive=drive;self.uid=uid;self.cache=None;self.sid='synthetic'
            self.owner_actor_id=digest(['https://accounts.google.com',rig.runtime.identity.owner_subject])
            self.config={'scope':digest(['registered-item-confirmation-v1',drive.config['binding'],uid,record['digest']])}
        def sheet_rows(self,region):
            if region.startswith("'カテゴリ'"):return [['大分類','小分類']]+[list(x) for x in CATS]
            assert region=="'PDFページ確認'!A1:W5000"
            return encoded_rows(card(record,current,categories=CATS))
    monkeypatch.setattr('services.human_general.generic_review.RegisteredReaders',Readers)
    monkeypatch.setattr('services.human_general.generic_review.FirestoreAuditRepository',lambda *_a,**_k:MemoryAuditRepository())
    rig.runtime=RegisteredReviewRuntime(rig.db,rig.settings,rig.key,{},drive.target,uid,{},'synthetic',
        clock=lambda:rig.now,identity=rig.runtime.identity,exchange=rig.exchange)
    rig.errors=[]
    original_gateway=rig.runtime.gateway
    def gateway(rid):
        result=original_gateway(rid);confirm=result.confirm
        def checked(*args,**kwargs):
            try:return confirm(*args,**kwargs)
            except Exception as error:
                rig.errors.append((type(error).__name__,str(error)));raise
        result.confirm=checked;return result
    rig.runtime.gateway=gateway
    rig.app=create_app(lambda:rig.runtime);rig.app.testing=True;rig.client=rig.app.test_client()
    rig.link=rig.runtime.seed();rig.start_path=urlsplit(rig.link).path+'?'+urlsplit(rig.link).query
    rig.rid=parse_qs(urlsplit(rig.link).query)['request'][0]
    return rig,drive,uid


def test_registered_item_oidc_separate_posting_proof_exact_snapshot_replay(monkeypatch,keys):
    rig,drive,uid=review_registered(monkeypatch,keys)
    rig.start();assert rig.callback().status_code==303
    assert drive.store.load()['pages'][drive.target]['units'][uid]['posting_authority'] is None
    response=rig.confirm();assert response.status_code==303,rig.errors
    unit=drive.store.load()['pages'][drive.target]['units'][uid];p=unit['candidate']['posting']
    expected=binding(drive.expected,'receipt_posting',unit=p['unit_binding'],snapshot_digest=digest(p['snapshot']),plan_digest=p['plan_digest'])
    assert rig.runtime.sealer.verify(unit['posting_authority'],expected)['request_id']==rig.rid
    assert len(unit['candidate']['confirmation_journal']['requests'])==1
    assert rig.get('/result').status_code==200
    before=drive.store.io.raw,drive.store.io.tag,drive.store.io.puts
    assert rig.get(rig.start_path).status_code==409 and rig.confirm().status_code==409
    assert (drive.store.io.raw,drive.store.io.tag,drive.store.io.puts)==before


def test_registered_item_changed_snapshot_before_confirm_never_seals(monkeypatch,keys):
    rig,drive,uid=review_registered(monkeypatch,keys)
    rig.start();rig.callback()
    state=drive.store.load();changed=state['pages'][drive.target]['units'][uid]['candidate']['posting']['snapshot']
    next(row for row in changed['rows'] if row[0]=='date')[2]='2026/10/09'
    state['generation']+=1;drive.store.save(state)
    assert rig.confirm().status_code in {400,409}
    assert drive.store.load()['pages'][drive.target]['units'][uid]['posting_authority'] is None
