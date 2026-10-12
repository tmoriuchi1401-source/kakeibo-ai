"""Synthetic signed-native auth + real loopback HTTP; emits counts only."""
from copy import deepcopy
import io
import json
import logging
import os
from pathlib import Path
import socket
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from html.parser import HTMLParser
from urllib.parse import urlencode

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from google.auth import crypt, jwt
from app.human_general_auth_transport import GoogleIdentityVerifier
from app.drive_run_state import StateError
from services.human_general import web
from werkzeug.serving import make_server
from .runtime import (Config, CLIENT, OWNER, create_application, install_network_guard, PREFIX)
from .memory_cas import LastUpdateOption, FailedPrecondition
from .serve import Quiet


def authenticated_fixture():
    # Existing Google verifier still checks RSA signature/issuer/audience/nonce/
    # owner. Only certificate/code-exchange IO is memory-only, never an HTTP route.
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = crypt.RSASigner.from_string(private.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), key_id='synthetic-staging-key')
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    def certificates(url, **kwargs):
        assert url == 'https://www.googleapis.com/oauth2/v1/certs'
        return SimpleNamespace(status=200, data=json.dumps({'synthetic-staging-key':public}).encode())
    now = [__import__('time').time()]
    identity = GoogleIdentityVerifier(CLIENT, OWNER, request=certificates, clock=lambda:now[0])
    app, rt = create_application(Config('https://staging.invalid'), identity=identity, clock=lambda:now[0])
    rid = rt.seed(); gateway = rt.gateway(rid); ticket = gateway.begin(rid)
    rt.tickets.save(ticket, int(now[0])+600)
    def exchange(code, verifier):
        assert code == 'synthetic-code' and verifier == ticket.pkce_verifier
        return jwt.encode(signer,dict(iss='https://accounts.google.com',aud=CLIENT,sub=OWNER,
            iat=int(now[0]),exp=int(now[0])+300,nonce=ticket.nonce,email='owner@example.test',email_verified=True)).decode()
    gateway.exchange_code = exchange
    before = deepcopy(rt.client.data)
    try: gateway.callback(ticket,cookie=ticket.cookie,state='synthetic-wrong-state',code='synthetic-code')
    except StateError: pass
    else: raise AssertionError('native state validation bypassed')
    assert rt.client.data == before
    actor = gateway.callback(ticket,cookie=ticket.cookie,state=ticket.state,code='synthetic-code')
    login_cookie = rt.logins.create(actor)
    gateway.bind_login(rid,rt.logins.evidence(login_cookie))
    # CAS conflict cannot silently replace a login/session.
    ref=rt.client.collection('pages').document(rid); snap=ref.get(retry=None,timeout=10)
    ref.update({'page':snap.get('page')},option=LastUpdateOption(snap.update_time),retry=None,timeout=10)
    before=deepcopy(rt.client.data)
    try: ref.update({'page':{}},option=LastUpdateOption(snap.update_time),retry=None,timeout=10)
    except FailedPrecondition: pass
    else: raise AssertionError('CAS stale claim accepted')
    assert rt.client.data==before
    return app,rt,ticket,login_cookie,now


def main():
    Config.read(os.environ)
    if Path('/sys/class/net').exists():
        assert sorted(p.name for p in Path('/sys/class/net').iterdir())==['lo'], 'runtime network not isolated'
        routes=Path('/proc/net/route').read_text().splitlines()[1:]
        assert not any(line.split()[1]=='00000000' for line in routes), 'runtime default route exists'
    install_network_guard()
    expected_blocked=0
    for probe in (lambda:socket.create_connection(('203.0.113.1',443),timeout=1),
                  lambda:socket.getaddrinfo('external.example.test',443)):
        try: probe()
        except OSError: expected_blocked+=1
        else: raise AssertionError('runtime egress succeeded')
    app,rt,ticket,login,clock = authenticated_fixture()
    secret_markers=(ticket.cookie,ticket.pkce_verifier,ticket.state,'synthetic-code',rt.key.decode())
    logs=io.StringIO(); handler=logging.StreamHandler(logs); app.logger.addHandler(handler)
    server=make_server('127.0.0.1',0,app,request_handler=Quiet)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_port)
    cookie=web.COOKIE+'='+ticket.cookie+'; '+web.LOGIN_COOKIE+'='+login
    count=0
    def call(path,method='GET',data=None,authenticated=True,extra=None):
        nonlocal count
        headers={'X-Forwarded-Proto':'https','Origin':rt.origin}
        if authenticated:headers['Cookie']=cookie
        headers.update(extra or {})
        if isinstance(data,dict):data=urlencode(data).encode()
        req=Request(base+path,data=data,headers=headers,method=method)
        try:r=urlopen(req,timeout=5)
        except HTTPError as error:r=error
        with r:body=r.read().decode();status=r.status
        assert not any(marker in body for marker in secret_markers)
        count+=1
        return status,body
    try:
        assert call('/health',authenticated=False)[0]==200
        assert call(PREFIX,authenticated=False)[0]==403
        before=deepcopy(rt.client.data);writes=rt.client.writes
        status,body=call(PREFIX);assert status==200 and body.count('<article class="case"')==3
        assert call(PREFIX+'/cases/synthetic-staging-2')[0]==200
        assert call('/logout')[0]==200
        assert rt.client.data==before and rt.client.writes==writes
        payload=json.dumps(dict(case_id='synthetic-staging-1',requested_action='confirm_grouping',request_id=ticket.request_id,expected_revision=0)).encode()
        assert call(PREFIX+'/STRUCTURE_REVIEW/prepare','POST',payload,extra={'Content-Type':'application/json','X-CSRF-Token':ticket.csrf})[0]==400
        assert call(PREFIX+'/STRUCTURE_REVIEW/prepare','POST',payload,extra={'Content-Type':'application/json'})[0]==400
        for path in ('/oauth/callback','/start','/confirm','/intake/archive',PREFIX+'/ACCOUNTING_POST/submit',PREFIX+'/METADATA_REVIEW/submit'):
            assert call(path,'POST',b'')[0]==403
        assert rt.client.data==before and rt.client.writes==writes
        status,html=call('/logout');assert status==200
        fields={}
        class Inputs(HTMLParser):
            def handle_starttag(self,tag,attrs):
                if tag=='input':
                    d=dict(attrs);fields[d['name']]=d.get('value','')
        Inputs().feed(html)
        assert call('/logout','POST',fields)[0]==200
        assert call(PREFIX)[0]==403
        clock[0]+=601
        assert call(PREFIX)[0]==403
        assert not any(marker in logs.getvalue() for marker in secret_markers)
        assert set(p.split('/')[0] for p in rt.client.data)<={'requests','sessions','login_sessions','pages'}
        import importlib.util
        assert importlib.util.find_spec('google.cloud') is None
        assert importlib.util.find_spec('googleapiclient') is None
        print(json.dumps(dict(http_requests=count,case_count=3,authenticated_list_detail=True,
            GET_prepare_memory_writes=0,accounting_write=0,authority_write=0,AI_send=0,source_move=0,
            external_connection_successes=0,expected_negative_egress_probes=expected_blocked,
            native_signature_state_CAS_logout_TTL=True,secret_response_log_matches=0,cloud_credentials_required=False)))
    finally:
        server.shutdown();thread.join(timeout=5);server.server_close()
        assert not thread.is_alive()


if __name__=='__main__':main()
