"""Google signature + multi-instance session + individual authority contracts."""
from copy import deepcopy
from dataclasses import replace
from html.parser import HTMLParser
import json
from urllib.parse import urlsplit, parse_qs

import pytest

from app.drive_run_state import StateError
from app.human_general_auth_transport import fingerprint, validate_auth_state
from services.human_general.shared_login import SharedLogins, Lifetime, METHOD
from services.human_general.web import COOKIE, LOGIN_COOKIE
from test_human_general_http import http, ORIGIN
from test_human_general_auth_transport import keys

DOMAIN='confirmation.example.test'


def form(response, action):
    fields={}
    class Parser(HTMLParser):
        active=False
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if tag=='form':self.active=attrs.get('action')==action
            if tag=='input' and self.active:fields[attrs['name']]=attrs.get('value','')
        def handle_endtag(self,tag):
            if tag=='form':self.active=False
    Parser().feed(response.get_data(as_text=True))
    return fields


def login(http):
    http.start()
    response=http.callback()
    assert response.status_code==303
    cookie=http.client.get_cookie(LOGIN_COOKIE,domain=DOMAIN).value
    assert 'Domain=' not in response.headers['Set-Cookie']
    assert all(s in response.headers['Set-Cookie'] for s in ('Secure','HttpOnly','SameSite=Lax'))
    assert not http.grants()['grants']
    return cookie


def next_request(http):
    link=http.runtime.seed()
    http.start_path=urlsplit(link).path+'?'+urlsplit(link).query
    http.rid=parse_qs(urlsplit(link).query)['request'][0]
    return form(http.get(http.start_path),'/start')


def resume(http,fields=None):
    response=http.post('/start',fields or next_request(http))
    assert response.status_code==303 and response.location=='/confirm'
    http.ticket=http.runtime.tickets.load(http.client.get_cookie(COOKIE,domain=DOMAIN).value)
    return response


def test_one_google_login_two_separate_hga_requests_explicit_posts_and_replay(http):
    cookie=login(http)
    assert http.confirm().status_code==303
    first=deepcopy(http.grants())
    first_rid=http.rid
    resume(http)
    state=http.runtime.gateway(http.rid).store.load()['requests'][http.rid]
    assert state['actor']['method']==METHOD and state['actor']['verification_revision']==2
    assert state['actor']['request_id']!=first_rid and http.exchanges==1
    assert not http.grants()['grants']  # Reused identity does not grant anything.
    assert http.get('/confirm').status_code==200
    assert http.confirm().status_code==303 and http.get('/result').status_code==200
    assert len(http.grants()['grants'])==len(http.grants()['audit'])==1
    assert json.loads(http.db.data['authorities/'+first_rid]['payload'])==first
    before=deepcopy(http.db.data)
    assert http.confirm().status_code==409 and http.get(http.start_path).status_code==409
    assert http.db.data==before and http.exchanges==1
    assert cookie==http.client.get_cookie(LOGIN_COOKIE,domain=DOMAIN).value


def test_shared_cookie_not_in_html_url_or_plaintext_firestore_and_multi_instance(http):
    cookie=login(http)
    stored=http.db.data['login_sessions/'+fingerprint(cookie)]
    assert set(stored)=={'ciphertext','expires_at'}
    for value in (cookie,http.runtime.identity.owner_subject,'owner@example.test'):
        assert value.encode() not in stored['ciphertext']
        assert value not in http.get('/confirm').get_data(as_text=True)
    second=SharedLogins(http.db,http.key,http.runtime.identity,clock=lambda:http.now)
    assert second.current(cookie)==http.runtime.logins.current(cookie)
    assert http.get('/confirm').headers['Cache-Control']=='no-store'


@pytest.mark.parametrize('case',['idle','absolute','policy','allowlist','tampered','revoked','missing'])
def test_invalid_session_reauth_without_authority(http,case):
    cookie=login(http)
    fields=next_request(http)
    if case=='idle':http.now+=3600
    elif case=='absolute':
        for _ in range(7):
            http.now+=3500;http.runtime.logins.touch(cookie)
        http.now+=28800-(http.now-http.runtime.logins.current(cookie)['authenticated_at'])
    elif case=='policy':http.runtime.identity.policy_revision+=1
    elif case=='allowlist':http.runtime.identity.owner_subject='999999'
    elif case=='tampered':http.db.data['login_sessions/'+fingerprint(cookie)]['ciphertext']=b'bad'
    elif case=='revoked':http.runtime.logins.revoke(cookie)
    elif case=='missing':http.client.delete_cookie(LOGIN_COOKIE,domain=DOMAIN)
    # Requests expire independently in ten minutes: create a fresh operation.
    if case!='tampered':fields=next_request(http)
    response=http.post('/start',fields)
    if case=='tampered':assert response.status_code==503
    else:assert response.status_code==303 and response.location.startswith('https://accounts.google.com/')
    assert not http.grants()['grants'] and http.exchanges==1


@pytest.mark.parametrize('origin',[None,'null','https://other.test',ORIGIN+'.evil'])
def test_reuse_start_origin_and_csrf_remain_required(http,origin):
    login(http);fields=next_request(http);before=deepcopy(http.db.data)
    headers={} if origin is None else {'Origin':origin}
    assert http.client.post('/start',data=fields,base_url=ORIGIN,headers=headers).status_code==400
    assert http.db.data==before and not http.grants()['grants']


def test_bad_csrf_and_stale_source_never_resume_or_grant(http):
    login(http);fields=next_request(http)
    before=deepcopy(http.db.data)
    assert http.post('/start',{**fields,'csrf':'wrong'}).status_code==400
    assert http.db.data==before
    http.db.data['pages/'+http.rid]['page']['authority_revision']+=1
    assert http.post('/start',fields).status_code in {400,409}
    assert not http.grants()['grants']


def test_logout_revokes_pending_operation_and_old_cookie_then_reauth(http):
    cookie=login(http)
    operation_cookie=http.ticket.cookie
    logout=form(http.get('/confirm'),'/logout')
    assert http.post('/logout',logout).status_code==200
    assert http.runtime.logins.current(cookie) is None
    http.client.set_cookie(LOGIN_COOKIE,cookie,domain=DOMAIN)
    http.client.set_cookie(COOKIE,operation_cookie,domain=DOMAIN)
    assert http.confirm().status_code==403 and not http.grants()['grants']
    fields=next_request(http)
    response=http.post('/start',fields)
    assert response.location.startswith('https://accounts.google.com/')
    http.ticket=http.runtime.tickets.load(http.client.get_cookie(COOKIE,domain=DOMAIN).value)
    assert http.callback().status_code==303
    assert http.client.get_cookie(LOGIN_COOKIE,domain=DOMAIN).value!=cookie
    assert http.exchanges==2


def test_logout_origin_and_csrf_and_get_cannot_revoke(http):
    cookie=login(http);fields=form(http.get('/confirm'),'/logout')
    before=deepcopy(http.db.data)
    assert http.get('/logout').status_code==405
    assert http.post('/logout',fields,origin='null').status_code==400
    assert http.post('/logout',{'csrf':'wrong'}).status_code==400
    assert http.runtime.logins.current(cookie) is not None and http.db.data==before


def test_account_switch_revokes_login_forces_google_and_rotates_id(http):
    cookie=login(http);fields=next_request(http)
    response=http.post('/start',{**fields,'login':'reauth'})
    assert response.location.startswith('https://accounts.google.com/')
    assert http.runtime.logins.current(cookie) is None
    http.ticket=http.runtime.tickets.load(http.client.get_cookie(COOKIE,domain=DOMAIN).value)
    assert http.callback().status_code==303
    assert http.client.get_cookie(LOGIN_COOKIE,domain=DOMAIN).value!=cookie
    assert http.exchanges==2


def test_supplied_fixation_cookie_never_upgraded(http):
    chosen='x'*43
    http.client.set_cookie(LOGIN_COOKIE,chosen,domain=DOMAIN)
    actual=login(http)
    assert actual!=chosen and http.runtime.logins.current(chosen) is None


def test_session_cas_conflict_no_fallback_and_request_stays_prepared(http):
    login(http);fields=next_request(http);http.db.race=True
    assert http.post('/start',fields).status_code==412
    record=http.runtime.gateway(http.rid).store.load()['requests'][http.rid]
    assert record['status']=='prepared' and not http.grants()['grants']


def test_two_tabs_cannot_mix_request_and_ticket_cookie_or_authority(http):
    login(http);resume(http);one=deepcopy(http.ticket);first=http.rid
    resume(http);two=http.ticket
    assert one.request_id!=two.request_id and one.cookie!=two.cookie
    current=http.runtime.gateway(first)
    current.verify_login=lambda r:http.runtime.logins.verify_bound(r,http.client.get_cookie(LOGIN_COOKIE,domain=DOMAIN).value)
    _,record=current.record(first,'authenticated')
    with pytest.raises(StateError,match='browser_binding'):
        current.browser(record,one,two.cookie)
    assert http.confirm().status_code==303
    assert not json.loads(http.db.data['authorities/'+first]['payload'])['grants']


def test_session_evidence_cannot_move_to_other_request_and_legacy_state_remains_valid(http):
    login(http);resume(http)
    value=http.runtime.gateway(http.rid).store.load()
    assert validate_auth_state(value,value['binding'])==value
    r=value['requests'][http.rid]
    r['actor']['request_id']='00000000-0000-4000-8000-000000000001'
    with pytest.raises(StateError):validate_auth_state(value,value['binding'])


@pytest.mark.parametrize('lifetime',[(0,0),(3600,4000),(True,60),(90000,60)])
def test_invalid_lifetimes_rejected(lifetime):
    with pytest.raises(StateError):Lifetime(*lifetime)


def test_idle_expiry_then_new_google_signature_and_rotated_login(http,monkeypatch):
    from datetime import datetime,timezone
    from google.auth import _helpers
    cookie=login(http)
    http.now+=3601
    monkeypatch.setattr(_helpers,'utcnow',lambda:datetime.fromtimestamp(http.now,timezone.utc).replace(tzinfo=None))
    fields=next_request(http)
    response=http.post('/start',fields)
    assert response.location.startswith('https://accounts.google.com/')
    http.ticket=http.runtime.tickets.load(http.client.get_cookie(COOKIE,domain=DOMAIN).value)
    assert http.callback().status_code==303
    assert http.exchanges==2 and http.client.get_cookie(LOGIN_COOKIE,domain=DOMAIN).value!=cookie
    assert not http.grants()['grants'] and http.runtime.logins.current(cookie) is None


def test_reused_actor_and_login_binding_tamper_rejected_before_authority(http):
    cookie=login(http);resume(http)
    gateway=http.runtime.gateway(http.rid)
    gateway.verify_login=lambda record:http.runtime.logins.verify_bound(record,cookie)
    _,record=gateway.record(http.rid,'authenticated')
    for field,value in [('handle','f'*64),('policy','f'*64)]:
        changed=deepcopy(record);changed['login_evidence'][field]=value
        with pytest.raises(StateError,match='binding_invalid'):http.runtime.logins.verify_bound(changed,cookie)
    for field,value in [('subject','999999'),('issuer','https://other.test'),('email','other@example.test')]:
        changed=deepcopy(record);changed['actor'][field]=value
        with pytest.raises(StateError,match='binding_invalid'):http.runtime.logins.verify_bound(changed,cookie)
    assert not http.grants()['grants']


def test_reused_session_item_review_and_reconciliation_use_existing_authorities(http,keys):
    from test_receipt_item_confirmation import session as item_setup,CATS
    from test_receipt_reconciliation_auth_transport import setup as reconciliation_setup
    from app.human_general_auth_transport import empty_auth_state
    from app.page_receipt_model import digest
    from app.receipt_item_confirmation import binding as item_binding
    login_cookie=login(http)
    original_exchanges=http.exchanges
    item,ticket,record,current,snapshot,rig,data=item_setup(keys)
    # Reset only isolated fixture auth state; existing candidate/snapshot unchanged.
    rig.transport.payload=json.dumps(empty_auth_state(item.store.binding)).encode()
    item.prepare(item_binding(record,snapshot,ticket.request_id),request_id=ticket.request_id)
    item.verify_login=lambda r:http.runtime.logins.verify_bound(r,login_cookie)
    protected=item.store.load()['requests'][ticket.request_id]
    new_ticket=item.resume_login(ticket.request_id,http.runtime.logins.actor(login_cookie,protected),
                                http.runtime.logins.evidence(login_cookie))
    saved=[]
    class ItemAuthority:
        def confirm(self,record,snapshot,current,proof):
            saved.append(proof)
            return {'authority_digest':proof.authority_digest}
    response=item.confirm(new_ticket,cookie=new_ticket.cookie,csrf=new_ticket.csrf,origin=ORIGIN,
                          method='POST',confirmation_factory=lambda _:ItemAuthority())
    assert response['status']=='complete' and len(saved)==1
    assert rig.exchange_calls==1  # Fixture setup only; reuse adds no exchange.
    recon=reconciliation_setup(keys)
    recon.gateway.store.transport.payload=json.dumps(empty_auth_state(recon.gateway.store.binding)).encode()
    recon.gateway.prepare(recon.expected)
    recon.gateway.verify_login=lambda r:http.runtime.logins.verify_bound(r,login_cookie)
    protected=recon.gateway.store.load()['requests'][recon.rid]
    new_ticket=recon.gateway.resume_login(recon.rid,http.runtime.logins.actor(login_cookie,protected),
                                         http.runtime.logins.evidence(login_cookie))
    assert recon.gateway.confirm(new_ticket,cookie=new_ticket.cookie,csrf=new_ticket.csrf,origin=ORIGIN,
        method='POST',confirmation_factory=lambda actor:recon.gateway.confirmation(actor.actor_id))['accounting_write']==0
    assert recon.repo.writes==1 and recon.exchange_calls==0
    assert http.exchanges==original_exchanges and not http.grants()['grants']


def test_deployable_synthetic_hga_then_items_same_cookie_one_google_exchange(http):
    from services.human_general.session_canary import SessionCanaryRuntime
    from services.human_general.web import create_app
    runtime=SessionCanaryRuntime(http.db,{**http.settings,'session_canary_enabled':True},http.key,
        clock=lambda:http.now,identity=http.runtime.identity,exchange=http.exchange)
    http.runtime=runtime
    http.app=create_app(lambda:runtime);http.client=http.app.test_client()
    http.grants=lambda:json.loads(runtime.state(http.rid,'authorities').read_versioned()[0])
    def seed(operation):
        link=runtime.seed_operation(operation)
        http.start_path=urlsplit(link).path+'?'+urlsplit(link).query
        http.rid=parse_qs(urlsplit(link).query)['request'][0]
    seed('hga');cookie=login(http)
    assert http.confirm().status_code==303 and http.get('/result').status_code==200
    first=deepcopy(http.grants())
    seed('items');resume(http,form(http.get(http.start_path),'/start'))
    assert http.get('/confirm').status_code==200
    assert http.confirm().status_code==303 and http.get('/result').status_code==200
    state=http.grants()
    assert len(state['requests'])==len(state['audit'])==1
    assert state['requests'][http.rid]['binding']['requested_action']=='confirm_receipt_item_snapshot'
    assert first['audit'][0]['operation']=='confirm_general_receipt_ai'
    assert http.exchanges==1 and http.client.get_cookie(LOGIN_COOKIE,domain=DOMAIN).value==cookie
    before=deepcopy(http.db.data)
    assert http.confirm().status_code==409 and http.db.data==before
    assert not any(path.startswith(('real_requests/','kakeibo_receipt_audit/')) for path in http.db.data)


def test_canary_admin_seed_disabled_and_no_public_seed_endpoint(http):
    from services.human_general.session_canary import SessionCanaryRuntime
    runtime=SessionCanaryRuntime(http.db,http.settings,http.key,identity=http.runtime.identity)
    with pytest.raises(StateError,match='canary_disabled'):runtime.seed_operation('items')
    assert http.post('/seed',{}).status_code==404


def test_existing_page_router_multi_instance_canary_hga_items_and_replay(http,keys,monkeypatch):
    from test_human_general_real_page import fixture,page_config
    from services.human_general.page_router import PageRouter,SCHEMA
    from services.human_general.web import create_app
    _,drive_http,cfg,info=fixture(monkeypatch,keys)
    profiles={'schema':SCHEMA,'profiles':{'p4':page_config(cfg,4),'p14':cfg}}
    settings={**http.settings,'session_canary_enabled':True}
    identity=http.runtime.identity
    def make():
        rt=PageRouter(http.db,settings,http.key,profiles,info,clock=lambda:http.now,
                      identity=identity,exchange=http.exchange)
        http.runtime=rt
        return rt
    administrator=make()
    http.app=create_app(make);http.client=http.app.test_client()
    http.grants=lambda:json.loads(http.runtime.state(http.rid,'authorities').read_versioned()[0])
    def seed(operation):
        link=administrator.seed_session_canary(operation)
        http.start_path=urlsplit(link).path+'?'+urlsplit(link).query
        http.rid=parse_qs(urlsplit(link).query)['request'][0]
    seed('hga');login(http)
    assert http.confirm().status_code==303 and http.get('/result').status_code==200
    seed('items');resume(http,form(http.get(http.start_path),'/start'))
    assert http.confirm().status_code==303 and http.get('/result').status_code==200
    before=deepcopy(http.db.data)
    assert http.confirm().status_code==409 and http.get(http.start_path).status_code==409
    assert http.db.data==before and http.exchanges==1
    assert not drive_http.requests  # Synthetic requests never touch real Drive.
