"""All credentials, callbacks and external sends are synthetic."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import time
from base64 import urlsafe_b64encode
from urllib.parse import urlencode

import pytest

from app.drive_run_state import StateError
from staging_shadow import google_a0g as a0, google_a0g_local as local
from staging_shadow import google_a0g_loopback as loop
from staging_shadow.google_a0g_ledger import Authority,LocalLedger,SCHEMA
from a0g_local_fakes import install
from test_google_a0g import contract,SHA,grant,approval,Sender,transport


@pytest.fixture
def setup(tmp_path):
    root=tmp_path/'checkout'; root.mkdir()
    ledger=LocalLedger.provision(tmp_path/'attempts.sqlite3',repo_root=root)
    c=contract(); now=time.time()
    d=dict(schema=SCHEMA,candidate_sha=SHA,contract_digest=a0.contract_digest(c,expected_sha=SHA),
        registry_id=ledger.registry_id,registry_path=str(ledger.path),issued_at=now,
        authorization=dict(id='authorization-once-0001',approved=True,expires_at=now+300),
        metadata=dict(id='metadata-once-0000001',approved=True,expires_at=now+300),
        local_controls_reviewed=True,cleanup_plan_ref='c'*64,browser_executable='SYNTHETIC_BROWSER')
    path=tmp_path/'authority.json'; path.write_text(json.dumps(d),'utf8')
    digest=sha256(path.read_bytes()).hexdigest()
    cp=tmp_path/'contract.json'; cp.write_text(json.dumps(c),'utf8')
    authority=Authority.load(path,expected_digest=digest,repo_root=root,now=now)
    ledger.enroll(authority)
    return dict(root=root,ledger=ledger,authority=authority,path=path,digest=digest,cp=cp,counter=tmp_path/'counts.txt',d=d)


def run(s):
    return local.run_once(authority_path=s['path'],authority_digest=s['digest'],contract_path=s['cp'],expected_sha=SHA,enabled=True)


def marks(s):
    return s['counter'].read_text('utf8').splitlines() if s['counter'].exists() else []


def child(s,mode='normal'):
    return subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('a0g_local_worker.py')),
        str(s['root']),str(s['path']),s['digest'],str(s['cp']),SHA,str(s['counter']),mode],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)


def test_whole_local_run_consumes_before_browser_and_replay_sends_nothing(setup,monkeypatch,capsys):
    s=setup; captures=install(monkeypatch,s['root'],s['counter'])
    result=run(s)
    assert result['status']=='METADATA_VERIFIED_NO_GO' and not result['runner_write_allowed']
    before=marks(s); assert before==['browser','exchange']+['get']*6
    assert run(s)['status']=='STOPPED_NO_GO' and marks(s)==before
    assert all(x.closed for x in captures['receivers']+captures['browsers'])
    assert all(g.access_token=='' for g in captures['grants'])
    p=captures['exchange_params'][0]; q=captures['authorization'][0]
    assert q['code_challenge']==[urlsafe_b64encode(sha256(p['code_verifier'].encode()).digest()).rstrip(b'=').decode()]
    assert q['code_challenge_method']==['S256'] and q['scope']==[a0.DRIVE_FILE]
    assert q['redirect_uri']==[p['redirect_uri']] and q['client_id']==[p['client_id']]
    assert 'client_secret' not in p and p['grant_type']=='authorization_code'
    snap=s['ledger'].snapshot(s['authority'])
    assert snap['authorization']==(1,1,0,0,'COMPLETED_NO_GO')
    assert snap['metadata']==(1,0,6,1,'COMPLETED_NO_GO')
    assert capsys.readouterr()==('','')
    for p in s['root'].parent.iterdir():
        if p.is_file(): assert b'SYNTHETIC_ACCESS' not in p.read_bytes() and b'SYNTHETIC_REFRESH' not in p.read_bytes() and b'SYNTHETIC_CODE' not in p.read_bytes()


@pytest.mark.parametrize('mode,expected',[
    ('callback-denied',['browser']),('callback-timeout',['browser']),
    ('exchange-timeout',['browser','exchange']),('interrupt',['browser','exchange']),
    ('get-denied',['browser','exchange','get'])])
def test_failure_clears_refs_spends_both_and_replay_has_zero_sends(setup,monkeypatch,capsys,mode,expected):
    s=setup; captures=install(monkeypatch,s['root'],s['counter'],mode)
    result=run(s); assert result==dict(status='STOPPED_NO_GO',runner_write_allowed=False)
    assert marks(s)==expected
    assert run(s)==result and marks(s)==expected
    assert all(g.access_token=='' for g in captures['grants'])
    assert all(x.closed for x in captures['receivers']+captures['browsers'])
    assert all(row[0]==1 and row[-1]=='STOPPED_NO_GO' for row in s['ledger'].snapshot(s['authority']).values())
    assert capsys.readouterr()==('','')


@pytest.mark.parametrize('mode,expected',[
    ('crash-before-browser',[]),('crash-exchange',['browser','exchange']),
    ('crash-get',['browser','exchange','get']),('normal',['browser','exchange']+['get']*6)])
def test_restart_after_real_process_exit_cannot_restore_approval(setup,mode,expected):
    s=setup; p=child(s,mode); out,err=p.communicate(timeout=20)
    assert p.returncode in (0,92,93,94),err
    assert marks(s)==expected
    p=child(s); out,err=p.communicate(timeout=20)
    assert p.returncode==0 and json.loads(out)['status']=='STOPPED_NO_GO',err
    assert marks(s)==expected
    assert 'SYNTHETIC' not in out+err
    assert all(row[0]==1 for row in s['ledger'].snapshot(s['authority']).values())


def test_parallel_processes_have_one_winner_and_only_six_gets(setup):
    s=setup; procs=[child(s) for _ in range(4)]
    results=[]
    for p in procs:
        out,err=p.communicate(timeout=25); assert p.returncode==0,err
        results.append(json.loads(out)['status'])
    assert results.count('METADATA_VERIFIED_NO_GO')==1
    assert marks(s)==['browser','exchange']+['get']*6


def test_new_objects_same_approval_cannot_exchange_or_restart_metadata(setup,monkeypatch):
    s=setup; r=s['ledger'].consume(s['authority'],now=time.time())
    wires=local.RunWires(r,contract(),SHA)
    sent=[]
    monkeypatch.setattr(local.oauth,'token_post',lambda *a,**k:sent.append('exchange'))
    params=dict(client_id=contract()['preflight']['oauth_client_id'],grant_type='authorization_code')
    wires.exchange(params)
    with pytest.raises(StateError): local.RunWires(r,contract(),SHA).exchange(params)
    wires.start_metadata()
    with pytest.raises(StateError): local.RunWires(r,contract(),SHA).start_metadata()
    with pytest.raises(StateError): s['ledger'].consume(s['authority'],now=time.time())
    assert sent==['exchange']
    with pytest.raises(StateError): pickle.dumps(r)
    r.close()
    with pytest.raises(StateError): wires.exchange(params)


def test_durable_get_ordinals_cannot_reset_via_new_wires(setup,monkeypatch):
    s=setup; r=s['ledger'].consume(s['authority'],now=time.time())
    r.debit('exchange'); r.debit('session'); calls=[]
    monkeypatch.setattr(local.a0,'https_get',lambda *a,**k:calls.append(1))
    for base,q in a0.request_plan(contract(),expected_sha=SHA):
        encoded=urlencode({k:str(v).lower() if type(v) is bool else v for k,v in q.items()})
        local.RunWires(r,contract(),SHA).get(base+'?'+encoded,{})
    base,q=a0.request_plan(contract(),expected_sha=SHA)[0]
    with pytest.raises(StateError): local.RunWires(r,contract(),SHA).get(base+'?'+urlencode(q),{})
    assert len(calls)==6
    r.close()


def test_exchange_adapter_regeneration_does_not_restore_wire_permission(setup,monkeypatch):
    from staging_shadow import google_a0g_oauth as oauth
    from test_google_a0g_oauth import callback
    s=setup; r=s['ledger'].consume(s['authority'],now=time.time()); sent=[]
    def sender(*args,**kwargs):
        sent.append(1)
        return a0.Response(200,json.dumps(dict(access_token='SYNTHETIC_ACCESS',refresh_token='SYNTHETIC_REFRESH',scope=a0.DRIVE_FILE,expires_in=90,token_type='Bearer')).encode())
    monkeypatch.setattr(oauth,'token_post',sender)
    def attempt():
        adapter=oauth.ApprovedCodeExchange(contract(),expected_sha=SHA,approval=s['authority'].handoff(),enabled=True,sender=local.RunWires(r,contract(),SHA).exchange)
        return adapter.exchange(callback=callback(),expected_state='s'*32,code_verifier='v'*43,redirect_uri='http://127.0.0.1:8765/oauth2callback',client_secret=None)
    attempt().close()
    with pytest.raises(StateError): attempt()
    assert sent==[1]
    r.close()


def test_session_cannot_start_before_exchange_and_capability_is_process_bound(setup,monkeypatch):
    s=setup; r=s['ledger'].consume(s['authority'],now=time.time())
    with pytest.raises(StateError): r.debit('session')
    monkeypatch.setattr(os,'getpid',lambda:r._pid+1)
    with pytest.raises(StateError): r.debit('exchange')


@pytest.mark.parametrize('change',['sha','contract','digest','denied-auth','denied-get','expired','registry','missing','corrupt'])
def test_untrusted_or_changed_input_never_launches_or_sends(setup,monkeypatch,change):
    s=setup; install(monkeypatch,s['root'],s['counter'])
    if change=='contract':
        c=contract(); c['book_version']='10'; s['cp'].write_text(json.dumps(c),'utf8')
    elif change=='digest': s['digest']='0'*64
    elif change=='missing': s['ledger'].path.unlink()
    elif change=='corrupt': s['ledger'].path.write_bytes(b'not sqlite')
    else:
        d=deepcopy(s['d'])
        if change=='sha': d['candidate_sha']='0'*40
        elif change=='denied-auth': d['authorization']['approved']=False
        elif change=='denied-get': d['metadata']['approved']=False
        elif change=='expired': d['authorization']['expires_at']=0
        elif change=='registry': d['registry_id']='0'*64
        s['path'].write_text(json.dumps(d),'utf8'); s['digest']=sha256(s['path'].read_bytes()).hexdigest()
    assert run(s)['status']=='STOPPED_NO_GO' and marks(s)==[]
    if change=='missing': assert not s['ledger'].path.exists()


def test_unknown_and_terminal_state_cannot_be_reset_or_reenrolled(setup):
    s=setup; r=s['ledger'].consume(s['authority'],now=time.time())
    assert all(row[-1]=='CONSUMED_UNKNOWN' for row in s['ledger'].snapshot(s['authority']).values())
    with pytest.raises(StateError): s['ledger'].enroll(s['authority'])
    with pytest.raises(StateError):
        with s['ledger'].db() as db: db.execute('UPDATE approvals SET consumed=0')
    r.close()


def callback_query(**changes):
    d=dict(state='s'*32,code='SYNTHETIC_CODE',scope=a0.DRIVE_FILE,picked_file_ids='book,png')
    d.update(changes); return '/oauth2callback?'+urlencode(d)


@pytest.mark.parametrize('target',[
    '/wrong?code=x', '/oauth2callback?code=a&code=b', callback_query(state='x'*32),
    callback_query(picked_file_ids='book,other'),callback_query(scope=a0.DRIVE_FILE+' openid'),
    callback_query(code=''),callback_query(code='a b'),callback_query(extra='bad'),
    '/oauth2callback?error=access_denied','https://example.test/oauth2callback?x=1',
    callback_query()+'&state=again',callback_query()+'#fragment',callback_query(code='%XX').replace('%25XX','%XX')])
def test_callback_rejects_duplicates_mismatch_denial_and_scope(target):
    with pytest.raises(StateError): loop.parse_callback(target,contract=contract(),expected_sha=SHA,state='s'*32)


def test_receiver_binds_loopback_before_use_and_closes_without_logging(monkeypatch,capsys):
    servers=[]
    class Server:
        def __init__(self,address,handler):
            assert address==('127.0.0.1',0)
            self.server_address=('127.0.0.1',8765); self.closed=False; servers.append(self)
        def handle_request(self): receiver.accept(callback_query())
        def server_close(self): self.closed=True
    monkeypatch.setattr(loop,'HTTPServer',Server)
    receiver=loop.LoopbackReceiver(contract=contract(),expected_sha=SHA,state='s'*32,seconds=1)
    assert receiver.redirect_uri=='http://127.0.0.1:8765/oauth2callback'
    value=receiver.receive(); assert value['code']=='SYNTHETIC_CODE'
    assert receiver._state is None and receiver._value is None and servers[0].closed
    with pytest.raises(StateError): receiver.accept(callback_query())
    assert capsys.readouterr()==('','')


def test_http_callback_response_never_reflects_or_logs_code_and_abort_closes_socket(monkeypatch,capsys):
    handlers=[]; events=[]
    class Server:
        def __init__(self,address,handler): self.server_address=('127.0.0.1',8765); handlers.append(handler)
        def server_close(self): events.append('server_closed')
    class Connection:
        def shutdown(self,how): events.append('shutdown')
        def close(self): events.append('socket_closed')
    monkeypatch.setattr(loop,'HTTPServer',Server)
    receiver=loop.LoopbackReceiver(contract=contract(),expected_sha=SHA,state='s'*32,seconds=1)
    h=object.__new__(handlers[0]); h.path=callback_query()
    output=[]
    h.send_response_only=lambda x:output.append(x)
    h.send_header=lambda *args:output.append(args)
    h.end_headers=lambda:None
    h.do_GET()
    assert 303 in output and ('Location','/complete') in output
    assert 'SYNTHETIC_CODE' not in repr(output)
    h.log_message('SYNTHETIC_CODE'); h.log_error('SYNTHETIC_CODE')
    receiver._connection=Connection(); receiver.close()
    assert 'shutdown' in events and 'socket_closed' in events and receiver._value is None
    assert capsys.readouterr()==('','')


def test_failed_http_releases_grant_and_metadata_refs():
    g=grant(); http=transport(g=g,sender=lambda *a,**k:a0.Response(403,b'SYNTHETIC_ACCESS'))
    url,q=a0.request_plan(contract(),expected_sha=SHA)[0]
    with pytest.raises(StateError): http.request('GET',url,q)
    assert http._grant is None and http._sender is None and http._responses==[] and g.access_token==''


@pytest.mark.parametrize('mode',['empty','duplicate','timeout'])
def test_receiver_empty_duplicate_and_absolute_timeout_stop_without_exchange(monkeypatch,mode):
    class Server:
        def __init__(self,*a): self.server_address=('127.0.0.1',8765)
        def handle_request(self):
            if mode=='duplicate':
                receiver.accept(callback_query()); receiver.accept(callback_query())
            elif mode=='timeout': receiver._abort()
        def server_close(self): pass
    monkeypatch.setattr(loop,'HTTPServer',Server)
    receiver=loop.LoopbackReceiver(contract=contract(),expected_sha=SHA,state='s'*32,seconds=1)
    with pytest.raises(StateError): receiver.receive()
    assert receiver._state is None and receiver._value is None


def test_low_level_default_live_senders_require_durable_entry():
    from staging_shadow import google_a0g_oauth as oauth
    with pytest.raises(StateError,match='durable_local_entry'):
        a0.GuardedMetadataHTTP(contract(),expected_sha=SHA,approval=approval(),grant=grant(),enabled=True,wall_clock=lambda:1000)
    with pytest.raises(StateError,match='durable_local_entry'):
        oauth.ApprovedCodeExchange(contract(),expected_sha=SHA,approval=approval(),enabled=True,wall_clock=lambda:1000)


def test_default_entry_is_disabled_without_reading_or_network():
    assert local.run_once(authority_path=None,authority_digest=None,contract_path=None,expected_sha=None)==dict(status='DISABLED',runner_write_allowed=False)
