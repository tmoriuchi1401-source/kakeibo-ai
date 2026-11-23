"""Real installed SDK + fake HTTP transport, fabricated bytes and temporary DBs."""
import json
import socket
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.gemini_ai import GeminiAI
from app.receipt_pipeline import ReceiptPipeline
from app.receipt_review_cases import ReviewPipeline
from app.receipt_review_worker import AttemptFence
from app.receipt_review_diagnostics import (ReviewDiagnostics, SingleSendTransport,
    DiagnosticFailure, SingleSendBlocked, prb_client)

CANARY = 'SECRET_KEY_TOKEN_PERSON_PRODUCT_CANARY'
CATS = [['food', 'groceries']]
GOOD = dict(merchant=CANARY, date='2026-11-22', total=100,
    items=[dict(name=CANARY, amount=100, major_category='food', minor_category='groceries')])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    attempted=[]
    def deny(*a, **k):
        attempted.append(True)
        raise AssertionError('real network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB,'__init__',deny)
    monkeypatch.setattr(ReceiptPipeline,'_materialize_result',deny)
    from app import receipt_privacy_gate, gemini_ai
    monkeypatch.setattr(receipt_privacy_gate, 'evaluate_receipt_privacy',
        lambda *a, **k: SimpleNamespace(classification='normal', gemini_allowed=True, buyback_evidence=False))
    monkeypatch.setattr(receipt_privacy_gate, 'require_receipt_ai_permission', lambda *a, **k: None)
    monkeypatch.setattr(gemini_ai, 'require_receipt_ai_permission', lambda *a, **k: None)
    yield
    assert not attempted


def sdk_response(text, status='completed'):
    return dict(id='synthetic-interaction', object='interaction', status=status,
        continuation_token='synthetic-continuation',
        steps=[dict(type='model_output', content=[dict(type='text', text=text)])])


def build(tmp_path, monkeypatch, handler):
    from app import receipt_review_diagnostics as module
    d=ReviewDiagnostics(tmp_path/'diagnostic.jsonl')
    original=module.prb_client
    monkeypatch.setattr(module, 'prb_client', lambda key, diag: original(key, diag, transport=httpx.MockTransport(handler)))
    ai=GeminiAI('synthetic-api-key', request_attempts=1, prb_diagnostics=d)
    port=SimpleNamespace(lookup_source=lambda *a: None, intake_calls=[])
    port.intake=lambda *a: port.intake_calls.append(a)
    metadata=lambda sid: dict(id=sid, version='1', mimeType='image/png', trashed=False)
    fence=AttemptFence(tmp_path/'synthetic-attempts.sqlite')
    pipeline=ReceiptPipeline(SimpleNamespace(categories=lambda: [tuple(c) for c in CATS]), ai)
    review=ReviewPipeline(pipeline, port, metadata=metadata, fence=fence, diagnostics=d)
    return d, ai, port, fence, review


@pytest.mark.parametrize('mode', ['success','delayed_success','401','429','503','timeout','connect_error','invalid_json',
    'model_invalid','response_loss','continuation','redirect','amount_invalid'])
def test_real_sdk_single_http_attempt_and_preserved_fence(tmp_path, monkeypatch, capsys, mode):
    calls=[]
    elapsed=[0.0]
    monkeypatch.setattr('app.receipt_review_diagnostics.time.monotonic',lambda:elapsed[0])
    class LostBody(httpx.SyncByteStream):
        def __iter__(self): raise httpx.ReadError(CANARY)
    def handler(request):
        calls.append(request.method)
        assert request.extensions['timeout']==dict(connect=60.0,read=60.0,write=60.0,pool=60.0)
        if mode=='delayed_success': elapsed[0]+=12.0
        if mode=='timeout': raise httpx.ReadTimeout(CANARY, request=request)
        if mode=='connect_error': raise httpx.ConnectError(CANARY, request=request)
        if mode in {'401','429','503'}:
            return httpx.Response(int(mode),json={'error':{'message':CANARY,'code':int(mode)}},request=request)
        if mode=='response_loss': return httpx.Response(200, stream=LostBody(), request=request)
        if mode=='redirect': return httpx.Response(307,headers={'location':'https://generativelanguage.googleapis.com/v1/interactions'},request=request)
        value=dict(GOOD)
        if mode=='amount_invalid': value['total']=101
        text='not-json-'+CANARY if mode=='invalid_json' else json.dumps({'total':100}) if mode=='model_invalid' else json.dumps(value)
        return httpx.Response(200,json=sdk_response(text,'requires_continuation' if mode=='continuation' else 'completed'),request=request)
    d,ai,port,fence,review=build(tmp_path,monkeypatch,handler)
    try:
        if mode in {'success','delayed_success'}:
            assert review.process_bytes(b'fabricated image','image/png','synthetic-source-123')['status']=='review_created'
        else:
            with pytest.raises(Exception):review.process_bytes(b'fabricated image','image/png','synthetic-source-123')
        assert calls==['POST']
        assert len(port.intake_calls)==(1 if mode in {'success','delayed_success'} else 0)
        assert d.ai_count==1
        with sqlite3.connect(fence.path) as db: assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==2
        # Reopening does not erase history, and a rerun cannot reach HTTP.
        AttemptFence(fence.path)
        with pytest.raises(Exception):review.process_bytes(b'fabricated image','image/png','synthetic-source-123')
        assert calls==['POST']
        text=d.path.read_text()
        assert CANARY not in text
        captured=capsys.readouterr()
        assert CANARY not in captured.out+captured.err
        records=[json.loads(line) for line in text.splitlines()]
        assert all(set(r)=={'stage','result','exception_class','http_status','error_category',
            'ai_attempted','ai_http_attempt_count','intake_attempted','elapsed_ms','http_elapsed_ms'} for r in records)
        if mode=='delayed_success':
            assert any(r['stage']=='response' and r['http_elapsed_ms']==12000 for r in records)
        if mode in {'401','429','503'}:
            assert any(r['http_status']==int(mode) for r in records)
        if mode in {'timeout','response_loss','connect_error'}:
            assert any(r['result']=='UNKNOWN' and r['error_category']=='TRANSPORT_UNKNOWN' for r in records)
    finally:ai.client.close()


def test_transport_blocks_second_delegate_even_if_sdk_retries(tmp_path):
    d=ReviewDiagnostics(tmp_path/'diag');calls=[]
    guard=SingleSendTransport(d,httpx.MockTransport(lambda r:(calls.append(1) or httpx.Response(503))))
    req=httpx.Request('POST','https://generativelanguage.googleapis.com/v1/interactions')
    guard.handle_request(req)
    with pytest.raises(SingleSendBlocked):guard.handle_request(req)
    assert len(calls)==1


@pytest.mark.parametrize('url,method', [
    ('https://evil.invalid/v1/interactions','POST'),
    ('https://generativelanguage.googleapis.com/v1/interactions','GET'),
    ('https://generativelanguage.googleapis.com/v1beta/interactions','POST')])
def test_unexpected_transport_route_fails_closed(tmp_path,url,method):
    d=ReviewDiagnostics(tmp_path/'diag');calls=[]
    guard=SingleSendTransport(d,httpx.MockTransport(lambda r:calls.append(1)))
    with pytest.raises(SingleSendBlocked):guard.handle_request(httpx.Request(method,url))
    assert calls==[]


def test_diagnostic_failure_before_send_stops_without_intake_and_keeps_fence(tmp_path,monkeypatch):
    calls=[]
    d,ai,port,fence,review=build(tmp_path,monkeypatch,lambda r:calls.append(1))
    original=d.path.__class__.open
    def fail_on_http(path,*a,**k):
        if path==d.path and d.ai_count:raise OSError(CANARY)
        return original(path,*a,**k)
    monkeypatch.setattr(d.path.__class__,'open',fail_on_http)
    with pytest.raises(Exception):review.process_bytes(b'fake','image/png','synthetic-source-123')
    assert not calls and not port.intake_calls and d.failed
    with sqlite3.connect(fence.path) as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==2
    ai.client.close()


def test_unknown_instrumentation_never_allows_intake(tmp_path,monkeypatch):
    d,ai,port,fence,review=build(tmp_path,monkeypatch,lambda r:None)
    monkeypatch.setattr(ai.client.interactions,'create',lambda **k:SimpleNamespace(output_text=json.dumps(GOOD),status='completed'))
    with pytest.raises(Exception):review.process_bytes(b'fake','image/png','synthetic-source-123')
    assert not port.intake_calls
    assert any(json.loads(x)['error_category']=='COUNT_UNKNOWN' for x in d.path.read_text().splitlines())
    assert json.loads(d.path.read_text().splitlines()[-1])['ai_http_attempt_count'] is None
    ai.client.close()


def test_existing_diagnostic_is_not_truncated(tmp_path):
    p=tmp_path/'diag';p.write_text('earlier UNKNOWN')
    with pytest.raises(DiagnosticFailure):ReviewDiagnostics(p)
    assert p.read_text()=='earlier UNKNOWN'


def test_exception_text_and_untrusted_class_name_never_saved(tmp_path):
    d=ReviewDiagnostics(tmp_path/'diag');error=type(CANARY,(Exception,),{})(CANARY)
    d.failure('runner',error)
    assert CANARY not in d.path.read_text()


def test_runner_json_categories_are_hashable_without_changing_production():
    from scripts.run_receipt_review import review_categories
    from app.models import ReceiptResult
    from app.receipt_validation import validate_receipt_result
    result=ReceiptResult.model_validate(GOOD)
    with pytest.raises(TypeError):validate_receipt_result(result,CATS)
    assert validate_receipt_result(result,review_categories(CATS))[0]


@pytest.mark.parametrize('value',[None,[],[['food']],[[CANARY,None]],'food'])
def test_bad_categories_stop_locally(value):
    from scripts.run_receipt_review import review_categories
    with pytest.raises(ValueError):review_categories(value)


def test_unsupported_sdk_stops_before_transport(tmp_path,monkeypatch):
    import importlib.metadata
    d=ReviewDiagnostics(tmp_path/'diag')
    monkeypatch.setattr(importlib.metadata,'version',lambda name:'unsupported')
    with pytest.raises(Exception):prb_client('synthetic',d)
    assert d.ai_count==0


def test_offline_sdk_retry_translation_is_zero_retries():
    from google.genai._gaos.google_genai import _translate_retry_config
    from google.genai import types
    assert _translate_retry_config(types.HttpOptions(retry_options={'attempts':1})).max_retries==0


def test_client_sdk_and_interaction_use_same_explicit_timeout(tmp_path):
    seen=[]
    d=ReviewDiagnostics(tmp_path/'diag')
    client=prb_client('synthetic',d,transport=httpx.MockTransport(lambda r:
        (seen.append(r.extensions['timeout']) or httpx.Response(200,json=sdk_response('{}')))))
    try:
        assert client._api_client._httpx_client.timeout.as_dict()==dict(connect=60,read=60,write=60,pool=60)
        assert client._api_client._http_options.timeout==60000
        assert client._api_client._http_options.retry_options.attempts==1
        client.interactions.create(model='synthetic',input='synthetic',timeout=60,automatic_continuation=False)
        assert seen==[dict(connect=60,read=60,write=60,pool=60)]
    finally: client.close()


def test_late_window_refuses_send_and_preserves_cleanup_reserve(tmp_path,monkeypatch):
    from app.receipt_review_diagnostics import AnalysisUnknown
    monkeypatch.setattr('app.receipt_review_diagnostics.time.time',lambda:200)
    d=ReviewDiagnostics(tmp_path/'diag',deadline_epoch=300)
    calls=[]
    guard=SingleSendTransport(d,httpx.MockTransport(lambda r:calls.append(1)))
    with pytest.raises(AnalysisUnknown):
        guard.handle_request(httpx.Request('POST','https://generativelanguage.googleapis.com/v1/interactions'))
    assert calls==[] and d.ai_count==0


def test_medical_review_never_enters_ai_transport(tmp_path,monkeypatch):
    from app import receipt_privacy_gate
    calls=[]
    d,ai,port,fence,review=build(tmp_path,monkeypatch,lambda r:calls.append(1))
    monkeypatch.setattr(receipt_privacy_gate,'evaluate_receipt_privacy',lambda *a,**k:
        SimpleNamespace(classification='medical',gemini_allowed=False,buyback_evidence=False))
    try:
        assert review.process_bytes(b'fabricated medical','image/png','synthetic-medical-source')['status']=='privacy_blocked'
        assert calls==[] and d.ai_count==0 and port.intake_calls==[]
    finally: ai.client.close()


@pytest.mark.parametrize('mode',['success','401','429','503','timeout','response_loss','connect_error'])
def test_actual_httpcore_backend_never_replays_http(tmp_path,mode):
    import httpcore
    writes=[];connections=[]
    class Stream:
        def start_tls(self,**kwargs):return self
        def get_extra_info(self,name):return None
        def close(self):pass
        def write(self,buffer,timeout=None):writes.append(buffer)
        def read(self,max_bytes,timeout=None):
            if mode=='timeout':raise httpcore.ReadTimeout(CANARY)
            if mode=='response_loss':return b''
            status=int(mode) if mode.isdigit() else 200
            return f'HTTP/1.1 {status} Test\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}'.encode()
    class Backend:
        def connect_tcp(self,**kwargs):
            connections.append(1)
            if mode=='connect_error':raise httpcore.ConnectError(CANARY)
            return Stream()
        def sleep(self,seconds):raise AssertionError('unexpected retry backoff')
    d=ReviewDiagnostics(tmp_path/'diag')
    delegate=httpx.HTTPTransport(retries=0,http2=False)
    delegate._pool._network_backend=Backend()
    with httpx.Client(transport=SingleSendTransport(d,delegate),trust_env=False) as client:
        if mode in {'timeout','response_loss','connect_error'}:
            with pytest.raises(httpx.TransportError):
                client.post('https://generativelanguage.googleapis.com/v1/interactions',content=b'fabricated')
        else:
            response=client.post('https://generativelanguage.googleapis.com/v1/interactions',content=b'fabricated')
            assert response.status_code==(int(mode) if mode.isdigit() else 200)
    assert len(connections)==1
    assert sum(x.startswith(b'POST ') for x in writes)==(0 if mode=='connect_error' else 1)
    assert d.ai_count==1 and CANARY not in d.path.read_text()


@pytest.mark.parametrize('failure_stage',['response','intake'])
def test_journal_failure_after_http_never_intakes_or_replays(tmp_path,monkeypatch,failure_stage):
    calls=[]
    def handler(request):
        calls.append(1)
        return httpx.Response(200,json={'id':'synthetic','status':'completed','steps':[
            {'type':'model_output','content':[{'type':'text','text':json.dumps(GOOD)}]}]})
    d,ai,port,fence,review=build(tmp_path,monkeypatch,handler)
    event=d.event
    def fail(stage,*args,**kwargs):
        if stage==failure_stage:
            d.failed=True
            raise DiagnosticFailure()
        return event(stage,*args,**kwargs)
    monkeypatch.setattr(d,'event',fail)
    try:
        with pytest.raises(DiagnosticFailure):review.process_bytes(b'fake','image/png','synthetic-source-123')
        assert calls==[1] and not port.intake_calls
        with sqlite3.connect(fence.path) as db:assert db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==2
    finally:ai.client.close()
