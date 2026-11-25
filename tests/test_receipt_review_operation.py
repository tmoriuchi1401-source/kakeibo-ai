"""Operation-critical regressions; no live data, credentials, network or finance."""
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
import pytest
from pypdf import PdfWriter

from test_receipt_pr_b import (case, ready, JsPort, ReceiptPipeline, ReceiptResult, ReceiptItem,
    ReviewPipeline, AttemptFence, registered, execute_review, deny_finance_and_network)
from app.page_receipt_model import digest
from app.receipt_review_cases import make_case, make_row
from app.receipt_review_snapshot import candidate_rows
from app.receipt_sheets_source import checked_source
from app.receipt_review_history import ReceiptHistory
from app.receipt_sheets_authority import UNKNOWN


def pdf(count=1):
    writer = PdfWriter()
    for _ in range(count): writer.add_blank_page(width=100, height=100)
    out = BytesIO(); writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize('count', [0, 2])
def test_pdf_structure_hold_precedes_ai_metadata_and_fence(count, tmp_path):
    calls = []
    port = SimpleNamespace(lookup_source=lambda *_: None)
    review = ReviewPipeline(SimpleNamespace(_analyze=lambda *_: calls.append('AI')), port,
        metadata=lambda *_: calls.append('metadata'), fence=AttemptFence(tmp_path/'attempts.sqlite'))
    assert review.process_bytes(pdf(count), 'application/pdf', 'synthetic-pdf-operation')['status'] == 'held'
    assert calls == []


@pytest.mark.parametrize('text,expected', [
    ('レシート 商品 小計1000円 現金1000円', 'review_created'),
    ('診療 領収書 患者負担額 3250円', 'privacy_blocked'),
    ('レシート 商品 小計1000円 診療 患者負担額3250円', 'privacy_blocked')])
def test_complete_single_page_pdf_uses_existing_privacy_and_one_check(text, expected, monkeypatch, tmp_path):
    from app import receipt_privacy_gate as gate
    from app.receipt_text_extraction import _ReceiptTextExtraction
    monkeypatch.setattr(gate, '_extract_receipt_text', lambda *_: _ReceiptTextExtraction('extracted','pdf_text',text))
    calls = []
    result = ReceiptResult(merchant='合成商店', date='2026-11-22', total=1000,
        items=[ReceiptItem(name='合成商品', amount=1000, major_category='食費', minor_category='食品')])
    class AI:
        def analyze_receipt(self, *args, **kwargs): calls.append('AI'); return result
    p = JsPort(); raw = pdf(); p.source_bytes = raw; sid = 'synthetic-pdf-operation'
    pipeline = ReceiptPipeline(SimpleNamespace(categories=lambda:[['食費','食品']]), AI())
    review = ReviewPipeline(pipeline, p, metadata=lambda _:dict(id=sid, version='1', mimeType='application/pdf', trashed=False),
        fence=AttemptFence(tmp_path/'attempts.sqlite'))
    outcome = review.process_bytes(raw, 'application/pdf', sid)
    assert outcome['status'] == expected
    assert calls == (['AI'] if expected == 'review_created' else [])
    if expected == 'review_created':
        entry = p.lookup_source(sid, '')
        with pytest.raises(ValueError): checked_source(entry['source'])  # PR-A stays image-only.
        p.state['ui'][0] = ready(entry, p.state['ui'][0])
        p.invoke(dict(action='register', request_id=entry['request_id']))
        assert execute_review(entry['request_id'], port=p, fence=AttemptFence(tmp_path/'backend.sqlite'))['state'] == '検証済み・未記帳'


def test_production_import_id_skips_before_ai_or_local_attempt(tmp_path):
    def denied(*args): pytest.fail('AI/metadata/lookup must not run')
    p = SimpleNamespace(lookup_source=denied)
    review = ReviewPipeline(SimpleNamespace(_analyze=denied), p, metadata=denied,
        fence=AttemptFence(tmp_path/'attempts.sqlite'), already_imported=lambda _: True)
    assert review.process_bytes(b'unused', 'image/png', 'synthetic-already-imported') == {'status':'skipped','reason':'already_imported'}


def test_duplicate_backend_is_reasoned_hold_and_history_failure_is_unknown(tmp_path):
    entry, p = registered()
    result = execute_review(entry['request_id'], port=p, fence=AttemptFence(tmp_path/'hold.sqlite'), duplicate_check=lambda *_: True)
    assert result['state'] == '保留' and result['issues'] == ['existing_receipt_duplicate']
    assert result['financial_write_count'] == 0 and result['posting_authority'] is False
    entry, p = registered()
    def unreadable(*args): raise ValueError('history mismatch')
    result = execute_review(entry['request_id'], port=p, fence=AttemptFence(tmp_path/'unknown.sqlite'), duplicate_check=unreadable)
    assert result['state'] == UNKNOWN and p.calls.count('finish') == 0


def test_readonly_history_preserves_dates_and_catches_copied_original(monkeypatch):
    from app.sheets import HEADERS
    history = ReceiptHistory(SimpleNamespace(), 'synthetic-production-book', 'synthetic-review-book')
    old = ['receipt:original','now','receipt','original',46348,'合成 商店','1000','現金','解析済','','hash','']
    monkeypatch.setattr(history, 'rows', lambda title, headers: [old])
    assert history.already_imported('original')
    entry, row = case()
    old[4] = entry['import_row'][4]; old[5] = entry['import_row'][5]
    snap = {'inputs': {'receipt_total':1000}}
    assert history.duplicate(entry, snap)
    old[6] = 999
    assert not history.duplicate(entry, snap)


def test_status_is_readonly_even_while_stopped():
    p = JsPort()
    p.state['properties'] = {'RECEIPT_REVIEW_STOPPED':'outcome_unconfirmed'}
    status = p.invoke({'action':'status'})
    assert status['stopped'] is True and status['financial_write_count'] == 0
    assert not p.state['requests'][1:] and not p.state['cases'][1:]


def test_continuous_auth_rejects_wrong_owner_after_one_exchange():
    from scripts.run_receipt_review_daily import access_token, REQUIRED_SCOPES
    CLIENT_ID = 'synthetic-owner.apps.googleusercontent.com'
    calls = []
    class Session:
        def post(self, url, **kwargs):
            calls.append('exchange')
            return SimpleNamespace(status_code=200, json=lambda:{'access_token':'SYNTHETIC','expires_in':3600})
        def get(self, url, **kwargs):
            calls.append('identity')
            return SimpleNamespace(status_code=200, json=lambda:{'email':'other@synthetic.invalid'})
    client = {'installed':{'client_id':CLIENT_ID,'project_id':'synthetic-project','client_secret':'SYNTHETIC'}}
    owner = {'client_id':CLIENT_ID,'project_id':'synthetic-project','owner_email':'owner@synthetic.invalid','scopes':list(REQUIRED_SCOPES),'refresh_token':'SYNTHETIC'}
    with pytest.raises(ValueError, match='owner_identity_mismatch'):
        access_token(client, owner, 'owner@synthetic.invalid', Session())
    assert calls == ['exchange','identity']


def test_continuous_task_stop_and_disabled_config_never_load_credentials(tmp_path, monkeypatch):
    import json, sys
    from scripts import run_receipt_review_daily as daily
    cfg = tmp_path/'config.json'; cfg.write_text(json.dumps({'enabled':False,'financial_write_enabled':False}))
    args = ['daily','--config',str(cfg),'--state-dir',str(tmp_path/'state'),'--expected-sha','a'*40]
    for name in ('attempt-db','fence-seed','owner-token','client','gemini-key'):
        args.extend(['--'+name,str(tmp_path/'missing')])
    monkeypatch.setattr(sys, 'argv', args)
    monkeypatch.setattr(daily, 'access_token', lambda *_:pytest.fail('no auth before activation'))
    assert daily.main() == 0
    (tmp_path/'state'/'STOP.json').write_text('{}')
    stopped=(tmp_path/'state'/'STOP.json').read_bytes()
    cfg.write_text(json.dumps({'enabled':True,'financial_write_enabled':False}))
    assert daily.main() == 3
    assert (tmp_path/'state'/'STOP.json').read_bytes()==stopped


@pytest.mark.parametrize('processing,completed,open_limit', [(0, 0, 50), (1, 0, 50), (0, 100, 50), (0, 100, 1)])
def test_cycle_owner_and_history_selection_or_unknown_stop(processing, completed, open_limit, tmp_path, monkeypatch):
    import json, sys
    from scripts import run_receipt_review as runner
    from app import receipt_review_worker as worker, receipt_review_cases as cases, receipt_review_history as history
    import googleapiclient.discovery
    import requests
    seen = []
    owner = 'owner@synthetic.invalid'
    files = [dict(id=sid, version='1', mimeType='image/png', owners=[{'emailAddress':email}])
        for sid,email in [('synthetic-other-owner','other@synthetic.invalid'),
                          ('synthetic-imported',owner),('synthetic-new-owner',owner)]]
    class Port:
        def __init__(self, *args, **kwargs): pass
        def status(self): return dict(enabled=True, stopped=False, processing_count=processing,
                                     case_count=completed+1, request_count=completed)
        def pending(self): return []
        def lookup_source(self, sid, hashed): return None
        def metadata(self, sid): return dict(id=sid, version='1', mimeType='image/png', trashed=False)
    class History:
        def __init__(self, *args): pass
        def imports(self): return [['receipt:synthetic-imported','','receipt','synthetic-imported']]
        def already_imported(self, sid): return sid == 'synthetic-imported'
        def duplicate(self, *args): return False
    class Review:
        def __init__(self, *args, metadata, **kwargs): self.metadata = metadata
        def process_bytes(self, raw, mime, sid, **kwargs): seen.append(sid); return dict(status='review_created',financial_write_count=0)
    class Session:
        def __init__(self): self.headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs): return SimpleNamespace(status_code=200, content=b'synthetic-image')
    service = SimpleNamespace(files=lambda:SimpleNamespace(list=lambda **_:SimpleNamespace(execute=lambda:{'files':files})), close=lambda:None)
    monkeypatch.setattr(worker, 'ScriptPort', Port)
    monkeypatch.setattr(history, 'ReceiptHistory', History)
    monkeypatch.setattr(cases, 'ReviewPipeline', Review)
    monkeypatch.setattr(requests, 'Session', Session)
    monkeypatch.setattr(googleapiclient.discovery, 'build', lambda *args, **kwargs:service)
    monkeypatch.setenv('RECEIPT_REVIEW_OWNER_ACCESS_TOKEN','SYNTHETIC')
    config = {'schema':'receipt-review-runner-b1','enabled':True,'financial_write_enabled':False,
        'script_id':'synthetic-deployment','folder_id':'synthetic-folder','owner_email':owner,
        'categories':[['食費','食品']], 'production_spreadsheet_id':'synthetic-production',
        'review_spreadsheet_id':'synthetic-review','model':'synthetic-model','max_open_case_count':open_limit}
    path = tmp_path/'config.json'; path.write_text(json.dumps(config),encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['runner','--config',str(path),'--attempt-db',str(tmp_path/'attempts.sqlite'),
        '--diagnostic',str(tmp_path/'cycle.jsonl'),'cycle'])
    if processing:
        with pytest.raises(RuntimeError, match='existing_claim_outcome_unknown'): runner.main()
        assert seen == []
    else:
        assert runner.main() == 0
        assert seen == ([] if open_limit == 1 else ['synthetic-new-owner'])
