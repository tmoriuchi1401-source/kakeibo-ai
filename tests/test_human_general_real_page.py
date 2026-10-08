"""Fixed p14 host tests; synthetic bytes/keys only, no live resources."""
from copy import deepcopy
from hashlib import sha256
from functools import partial
import json
from types import SimpleNamespace
import time
from cryptography.hazmat.primitives import serialization
import pytest
from app.drive_run_state import StateError
from app.page_receipt_model import PageUnit,SourceRef,stable_page,digest
from app.human_general_authority import review_identity
from services.human_general import real_page as real
from services.human_general.real_runtime import RealPageRuntime
from services.human_general.web import create_app
from test_human_general_http import HttpRig
from test_human_general_auth_transport import keys,OWNER


class FakeDriveHttp:
    def __init__(self,config,raw):
        self.config=config;self.raw=raw;self.version=1;self.requests=[];self.race=False
        self.payloads={k:('synthetic '+k).encode() for k in config['baseline_files']}
        self.payloads[config['authority_file']]=real.canonical(real.empty_verified_state(config['binding']))
    def request(self,method,url,*,params,data,headers,timeout,allow_redirects):
        assert 0<timeout<=25 and allow_redirects is False
        self.requests.append((method,url,deepcopy(headers)))
        fid=url.split('/')[-1];status=200
        if method=='PUT':
            assert fid==self.config['authority_file'] and set(headers)=={'If-Match','Content-Type'}
            if self.race or headers['If-Match']!='"v'+str(self.version)+'"':status=412;value={}
            else:self.payloads[fid]=data;self.version+=1;value={'id':fid,'etag':'"v'+str(self.version)+'"'}
        elif params.get('alt')=='media':
            raw=self.raw if fid==real.SOURCE else self.payloads[fid]
            return SimpleNamespace(status_code=200,content=raw)
        elif 'owners' in params['fields']:
            value={'id':fid,'owners':[{'emailAddress':'owner@example.test'}],
                'permissions':[{'type':'user','role':'owner','emailAddress':'owner@example.test'},
                    {'type':'user','role':'writer','emailAddress':'synthetic@example.test'}]}
        else:value={'id':fid,'etag':'"v'+str(self.version)+'"','labels':{'trashed':False},
            'mimeType':'application/pdf' if fid==real.SOURCE else 'application/json',
            'parents':[{'id':self.config['inbox'] if fid==real.SOURCE else self.config['folder']}]}
        return SimpleNamespace(status_code=status,content=json.dumps(value).encode(),json=lambda:value)


def fixture(monkeypatch,keys):
    raw=b'synthetic source with immutable fourteen-page baseline'
    monkeypatch.setattr(real,'HASH',sha256(raw).hexdigest())
    src=SourceRef(source_file_id=real.SOURCE,source_content_hash=real.HASH,page_count=14)
    p=PageUnit(source=src,page_number=14,stable_page_identity=stable_page(src,14),
        automatic_classification='sensitive_unknown',automatic_reason='privacy_unresolved',
        observation_complete=True,extraction_status='extracted',human_page_kind='general_receipt',
        observation_render_hash='a'*64,review_identity='0'*64,authority_revision=2)
    p=p.model_copy(update={'review_identity':review_identity(p)})
    ids=['1ju2rEDWrlpALr-9d4JEN9yaPfTuKtFCq','1atHszVu7J-OXPbJkhCMhsvhiyz6QEdsR','1Gss6WvRvKbSWIKVSzrkApYkxVRFdQ6g_','1J6hjORzCDEauxg39o1fZbwRS41ROe3jE']
    config={'page':p.model_dump(),'folder':'synthetic-private-folder','inbox':'synthetic-inbox-folder',
        'authority_file':'synthetic-dedicated-authority','owner_digest':digest('owner@example.test'),
        'baseline_files':{fid:sha256(('synthetic '+fid).encode()).hexdigest() for fid in ids}}
    config['binding']=digest([real.SCHEMA,config['folder'],config['authority_file'],p.source.model_dump(),14,p.review_identity])
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem=rsa.generate_private_key(public_exponent=65537,key_size=2048).private_bytes(
        serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    info=dict(type='service_account',client_email='synthetic@example.test',token_uri='https://oauth2.googleapis.com/token',private_key=pem)
    http=FakeDriveHttp(config,raw);drive=real.RealPageDrive(config,info,session=http)
    return drive,http,config,info


def test_fixed_source_readonly_and_exact_conditional_fence(monkeypatch,keys):
    drive,http,cfg,info=fixture(monkeypatch,keys)
    assert drive.fresh()==drive.page
    assert all(r[0]=='GET' for r in http.requests)
    for target in [real.SOURCE,*cfg['baseline_files'],cfg['folder']]:
        with pytest.raises(StateError):drive.request(target,put=b'{}',tag='"v1"')
    for tag in [None,'*','W/"v1"','bad','null']:
        with pytest.raises(StateError):drive.request(cfg['authority_file'],put=b'{}',tag=tag)
    assert all(r[0]=='GET' for r in http.requests)


@pytest.mark.parametrize('change',['source','baseline'])
def test_freshness_changed_refused(monkeypatch,keys,change):
    drive,http,cfg,info=fixture(monkeypatch,keys)
    if change=='source':http.raw+=b'changed'
    else:http.payloads[next(iter(cfg['baseline_files']))]+=b'changed'
    with pytest.raises(StateError):drive.fresh()
    assert all(r[0]=='GET' for r in http.requests)


@pytest.mark.parametrize('changed',[{'page_number':4},{'authority_revision':3},{'automatic_classification':'medical'},
    {'automatic_classification':'payroll'},{'clearly_sensitive':True},{'review_identity':'f'*64}])
def test_real_config_rejects_other_pages_sensitive_stale(monkeypatch,keys,changed):
    _,_,cfg,_=fixture(monkeypatch,keys);cfg=deepcopy(cfg);cfg['page'].update(changed)
    with pytest.raises(StateError):real.validate_config(cfg)


def real_http(monkeypatch,keys,*,past_seconds=0):
    drive,http,cfg,info=fixture(monkeypatch,keys)
    rig=HttpRig(keys)
    # Start an old request in the past so the next signed JWT uses wall time.
    rig.now-=past_seconds
    monkeypatch.setattr('services.human_general.real_runtime.RealPageDrive',lambda *_:drive)
    rig.runtime=RealPageRuntime(rig.db,rig.settings,rig.key,cfg,info,clock=lambda:rig.now,
        identity=rig.runtime.identity,exchange=rig.exchange)
    rig.app=create_app(lambda:rig.runtime);rig.app.testing=True;rig.client=rig.app.test_client()
    rig.link=rig.runtime.seed()
    from urllib.parse import urlsplit,parse_qs
    rig.start_path=urlsplit(rig.link).path+'?'+urlsplit(rig.link).query
    rig.rid=parse_qs(urlsplit(rig.link).query)['request'][0]
    return rig,drive,http,cfg


def test_live_host_offline_owner_explicit_ai_consent_readback_and_replay(monkeypatch,keys):
    rig,drive,http,cfg=real_http(monkeypatch,keys)
    assert rig.get(rig.start_path).status_code==200
    assert 'p14' in rig.get(rig.start_path).get_data(as_text=True)
    assert rig.start().status_code==303
    assert rig.callback().status_code==303
    assert not json.loads(http.payloads[cfg['authority_file']])['authority']['grants']
    confirm=rig.get('/confirm')
    assert confirm.status_code==200 and '一般レシートとして確定しGemini送信を許可' in confirm.get_data(as_text=True)
    assert rig.confirm().status_code==303
    saved=deepcopy(http.payloads);version=http.version;writes=len([r for r in http.requests if r[0]=='PUT'])
    assert writes==1 and rig.get('/result').status_code==200
    value=real.validate_verified(json.loads(saved[cfg['authority_file']]),cfg,OWNER)
    assert len(value['authority']['grants'])==len(value['actor_evidence'])==1
    evidence=value['actor_evidence'][rig.rid]
    assert evidence['binding']['requested_action']=='general_receipt_and_gemini_permission'
    assert evidence['actor']['subject']==OWNER and evidence['actor']['issuer']=='https://accounts.google.com'
    assert drive.page.automatic_classification=='sensitive_unknown'
    assert rig.confirm().status_code==rig.callback().status_code==rig.get(rig.start_path).status_code==409
    assert http.payloads==saved and http.version==version and len([r for r in http.requests if r[0]=='PUT'])==writes
    assert all(http.payloads[fid]==('synthetic '+fid).encode() for fid in cfg['baseline_files'])
    with pytest.raises(StateError):rig.runtime.seed()


@pytest.mark.parametrize('changes',[{'sub':'987654321'},{'iss':'https://evil.example'},{'aud':'wrong'},
    {'email_verified':False},{'nonce':'wrong'}])
def test_wrong_actor_claims_no_authority(monkeypatch,keys,changes):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.claim_changes=changes
    rig.start();assert rig.callback().status_code==403
    assert not any(r[0]=='PUT' for r in http.requests)


@pytest.mark.parametrize('negative',['etag','origin_null','origin_absent','csrf','source','baseline'])
def test_confirm_stale_csrf_origin_rejected_no_authority(monkeypatch,keys,negative):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.start();assert rig.callback().status_code==303
    if negative=='etag':response=rig.confirm(etag='"stale"')
    elif negative in {'origin_null','origin_absent'}:
        _,tag=rig.runtime.state(rig.rid,'authorities').read_versioned()
        response=rig.post('/confirm',dict(action='confirm',csrf=rig.ticket.csrf,etag=tag),origin='null' if negative=='origin_null' else '')
    elif negative=='csrf':response=rig.confirm(csrf='bad')
    else:
        if negative=='source':http.raw+=b'changed'
        else:http.payloads[next(iter(cfg['baseline_files']))]+=b'changed'
        response=rig.confirm()
    assert response.status_code in {400,409,412}
    assert not any(r[0]=='PUT' for r in http.requests)


def test_native_412_never_retried(monkeypatch,keys):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.start();assert rig.callback().status_code==303
    http.race=True;assert rig.confirm().status_code==412
    assert len([r for r in http.requests if r[0]=='PUT'])==1
    assert not json.loads(http.payloads[cfg['authority_file']])['authority']['grants']


def test_expired_incomplete_request_new_session_is_independent(monkeypatch,keys):
    from urllib.parse import urlsplit,parse_qs
    rig,drive,http,cfg=real_http(monkeypatch,keys,past_seconds=601)
    rig.start();old_rid=rig.rid;old_path=rig.start_path;old_ticket=rig.ticket
    old_record=rig.runtime.gateway(old_rid).store.load()['requests'][old_rid]
    rig.now+=601
    assert rig.get(old_path).status_code in {400,409,410}
    assert not json.loads(http.payloads[cfg['authority_file']])['authority']['grants']
    rig.link=rig.runtime.seed()
    rig.start_path=urlsplit(rig.link).path+'?'+urlsplit(rig.link).query
    rig.rid=parse_qs(urlsplit(rig.link).query)['request'][0]
    record=rig.runtime.gateway(rig.rid).store.load()['requests'][rig.rid]
    assert rig.rid!=old_rid and record['digest']!=old_record['digest']
    assert record['binding']['requested_action']=='general_receipt_and_gemini_permission'
    assert record['status']=='prepared' and record['expires_at']-record['created_at']==600
    rig.start()
    for field in ('state','nonce','cookie','csrf','pkce_verifier'):
        assert getattr(rig.ticket,field)!=getattr(old_ticket,field)
    assert rig.callback().status_code==303
    assert not json.loads(http.payloads[cfg['authority_file']])['authority']['grants']
    assert rig.confirm().status_code==303
    assert 'p14の送信許可を保存しました' in rig.get('/result').get_data(as_text=True)
    assert len(json.loads(http.payloads[cfg['authority_file']])['authority']['grants'])==1
    saved=deepcopy(http.payloads);version=http.version
    assert rig.get(rig.start_path).status_code==409
    assert http.payloads==saved and http.version==version


def test_previous_consent_action_not_silently_promoted(monkeypatch,keys):
    from app.human_general_auth_transport import request_binding
    drive,_,_,_=fixture(monkeypatch,keys)
    with pytest.raises(StateError):
        request_binding(drive.page,'00000000-0000-0000-0000-000000000001','general_receipt_and_gemini')


def page_config(cfg,number):
    value=deepcopy(cfg)
    page=PageUnit.model_validate(value['page'])
    page=page.model_copy(update={'page_number':number,'stable_page_identity':stable_page(page.source,number)})
    page=page.model_copy(update={'review_identity':review_identity(page)})
    value['page']=page.model_dump();value['authority_file']='synthetic-authority-p'+str(number)
    value['binding']=digest([real.SCHEMA,value['folder'],value['authority_file'],page.source.model_dump(),number,page.review_identity])
    return value


@pytest.mark.parametrize('number',[4,10,14])
def test_independent_pinned_profiles_exact_identity_and_no_cross_page(monkeypatch,keys,number):
    drive,http,cfg,info=fixture(monkeypatch,keys)
    cfg=page_config(cfg,number);http=FakeDriveHttp(cfg,http.raw)
    drive=real.RealPageDrive(cfg,info,session=http)
    assert drive.fresh().page_number==number
    rig=HttpRig(keys)
    monkeypatch.setattr('services.human_general.real_runtime.RealPageDrive',lambda *_:drive)
    rt=RealPageRuntime(rig.db,rig.settings,rig.key,cfg,info,clock=lambda:rig.now,
        identity=rig.runtime.identity,exchange=rig.exchange)
    assert rt.current_page('request',real.SOURCE,number).page_number==number
    for other in {1,4,10,14}-{number}:
        with pytest.raises(StateError):rt.current_page('request',real.SOURCE,other)
    assert all(row[0]=='GET' for row in http.requests)


def test_profiles_duplicate_authority_and_cross_source_rejected(monkeypatch,keys):
    from services.human_general.page_router import validate_profiles,SCHEMA
    _,_,cfg,_=fixture(monkeypatch,keys)
    profiles={'schema':SCHEMA,'profiles':{'p4':page_config(cfg,4),'p10':page_config(cfg,10),'p14':cfg}}
    assert validate_profiles(profiles)==profiles
    bad=deepcopy(profiles);bad['profiles']['p4']=cfg
    with pytest.raises(StateError):validate_profiles(bad)
    bad=deepcopy(profiles);bad['profiles']['p4']['authority_file']=cfg['authority_file']
    with pytest.raises(StateError):validate_profiles(bad)


def test_router_does_not_trust_browser_page_selection(monkeypatch,keys):
    from services.human_general.page_router import PageRouter,SCHEMA
    from services.human_general.backend import timestamp
    from uuid import uuid4
    drive,http,cfg,info=fixture(monkeypatch,keys);rig=HttpRig(keys)
    profiles={'schema':SCHEMA,'profiles':{'p4':page_config(cfg,4),'p14':cfg}}
    router=PageRouter(rig.db,rig.settings,rig.key,profiles,info,clock=lambda:rig.now,
        identity=rig.runtime.identity,exchange=rig.exchange)
    unknown=str(uuid4())
    with pytest.raises(StateError):router.gateway(unknown)
    rid=str(uuid4());rig.db.collection('real_request_profiles').document(rid).create(
        dict(profile='p14',profile_digest=digest(cfg),expires_at=timestamp(rig.now+600)),retry=None,timeout=10)
    monkeypatch.setattr('services.human_general.real_runtime.RealPageDrive',lambda *_:drive)
    assert router.label(rid).startswith('p14')
    with pytest.raises(StateError,match='cross_binding'):router.gateway(unknown)
    router.drive.end_request()


def test_confirm_bounded_http_local_source_snapshot_fresh_again_before_put(monkeypatch,keys):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.start();rig.callback()
    http.requests.clear()
    assert rig.confirm().status_code==303
    downloads=[r for r in http.requests if r[0]=='GET' and r[1].endswith('/'+real.SOURCE)]
    # Each full read has metadata/media/metadata: two distinct SHA256 reads.
    assert len(downloads)==6
    assert len([r for r in http.requests if r[0]=='PUT'])==1
    assert drive._request_cache is None and drive._deadline is None
    http.requests.clear();assert rig.get('/result').status_code==200
    assert len([r for r in http.requests if r[1].endswith('/'+real.SOURCE)])==3


@pytest.mark.parametrize('target',['source','baseline'])
def test_http_snapshot_cannot_hide_change_before_write(monkeypatch,keys,target):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.start();rig.callback()
    original=drive.refresh_before_write
    def changed():
        if target=='source':http.raw+=b'changed'
        else:http.payloads[next(iter(cfg['baseline_files']))]+=b'changed'
        original()
    drive.refresh_before_write=changed
    assert rig.confirm().status_code in {400,409}
    assert not any(r[0]=='PUT' for r in http.requests)
    assert rig.runtime.gateway(rig.rid).store.load()['requests'][rig.rid]['status']=='claimed'
    assert rig.runtime.reconcile(rig.rid)=='not_written'


def test_source_read_timeout_after_claim_is_not_written_no_retry(monkeypatch,keys):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.start();rig.callback()
    original=drive.refresh_before_write
    def failure():
        drive._request_cache.clear()
        raise StateError('real_page_drive_unavailable')
    drive.refresh_before_write=failure
    assert rig.confirm().status_code==400
    assert rig.runtime.reconcile(rig.rid)=='not_written'
    assert rig.runtime.gateway(rig.rid).store.load()['requests'][rig.rid]['status']=='claimed'
    assert rig.confirm().status_code==409
    assert not any(r[0]=='PUT' for r in http.requests)


def test_authority_read_timeout_unknown_never_retries(monkeypatch,keys):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.start();rig.callback()
    _,tag=rig.runtime.state(rig.rid,'authorities').read_versioned()
    original=drive.read
    def failure(fid):
        if fid==cfg['authority_file']:raise StateError('real_page_drive_unavailable')
        return original(fid)
    drive.read=failure
    assert rig.post('/confirm',dict(action='confirm',csrf=rig.ticket.csrf,etag=tag)).status_code==400
    assert rig.runtime.reconcile(rig.rid)=='unknown'
    assert not any(r[0]=='PUT' for r in http.requests)


def test_write_saved_then_request_response_failure_readback_written_no_reapply(monkeypatch,keys):
    rig,drive,http,cfg=real_http(monkeypatch,keys);rig.start();rig.callback()
    original=rig.runtime.gateway
    def gateway(rid):
        result=original(rid);save=result.store.save
        def fail_complete(value):
            if value['requests'][rid]['status']=='complete':raise StateError('synthetic_conditional_save_unknown')
            return save(value)
        result.store.save=fail_complete
        return result
    rig.runtime.gateway=gateway
    assert rig.confirm().status_code==400
    assert rig.runtime.reconcile(rig.rid)=='written'
    assert rig.runtime.gateway(rig.rid).store.load()['requests'][rig.rid]['status']=='claimed'
    before=deepcopy(http.payloads);version=http.version
    assert rig.confirm().status_code==409
    assert http.payloads==before and http.version==version
    assert len([r for r in http.requests if r[0]=='PUT'])==1


def test_http_budget_expired_fails_before_network_or_write(monkeypatch,keys):
    drive,http,cfg,_=fixture(monkeypatch,keys)
    drive.begin_request();drive._deadline=time.monotonic()-1
    with pytest.raises(StateError):drive.read(real.SOURCE)
    with pytest.raises(StateError):drive.request(cfg['authority_file'],put=b'{}',tag='"v1"')
    assert http.requests==[]
    drive.end_request();assert drive._request_cache is None


def test_stage_logs_are_allowlisted_and_do_not_emit_exception_text(monkeypatch,capsys):
    from services.human_general.stages import Stages
    monkeypatch.setenv('HGA_STAGE_DIAGNOSTICS','1')
    trace=Stages('00000000-0000-0000-0000-000000000001')
    trace('confirm_failed',outcome='unknown',exception=StateError('secret-token-must-not-appear'))
    trace('response_sent',http_status=400)
    trace('untrusted-stage-secret');trace('confirm_failed',outcome='untrusted-secret')
    lines=capsys.readouterr().out.splitlines();assert len(lines)==2
    assert 'secret' not in ''.join(lines) and '00000000-0000' not in ''.join(lines)
    assert json.loads(lines[0])['exception_class']=='StateError'
    assert json.loads(lines[1])['http_status']==400
