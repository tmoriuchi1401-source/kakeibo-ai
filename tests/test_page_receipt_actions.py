from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app import page_receipt_actions as actions,pdf_unit_readonly_analysis as runner
from app.page_receipt_ai import GeminiPageReceipts
from app.drive_run_state import StateError
from test_page_receipts import page,reading,CATEGORIES
from test_private_state_bindings import key
from test_receipt_pdf_units import local_ocr,synthetic_pdf
from test_pdf_unit_readonly_analysis import environment

def run_model(key,monkeypatch,mode='page_p2',proof=None):
    kinds=['medical','normal','normal','unknown','normal','normal']
    pages={n:page(kind,n,kinds)[0] for n,kind in enumerate(kinds,1)}
    source=Mock(return_value=synthetic_pdf(kinds))
    store=Mock(payload=b'protected',tag='"strong"');store.load.return_value={'protected':'same'}
    expected=dict(source_file_id='synthetic-source-id',source_content_hash=pages[2].source.source_content_hash,
        proposal_digest='d'*64,confirmation_digest='c'*64,grouping_revision=2,
        unit_ids={n:'legacy-unit-'+str(n) for n in range(2,7)})
    monkeypatch.setattr(actions,'load_page',lambda _,expected,sid,n:pages[n])
    calls=Mock(return_value=SimpleNamespace(output_text=reading(2).model_dump_json()))
    factory=Mock(side_effect=lambda key,model,permission:GeminiPageReceipts(
        SimpleNamespace(interactions=SimpleNamespace(create=calls)),model,permission))
    env={**environment(),'GOOGLE_SERVICE_ACCOUNT_JSON':json.dumps({'private_key':key}),
        'PDF_READONLY_MODE':mode,'PDF_READONLY_P2_RUN_ID':'99','PDF_READONLY_PAGE_BATCH':'p3-p6'}
    def prior(env,pem,expected):actions.check_page_p2(proof,env,expected);return proof
    artifact,summary=actions.execute_pages(env,'a'*40,store,source,CATEGORIES,expected,key,prior=prior,factory=factory)
    return runner.decrypted(artifact,key),summary,env,expected,calls,factory

def test_new_model_p2_encrypted_independent_schema_cannot_authorize_accounting(local_ocr,key,monkeypatch):
    value,summary,env,_,calls,_=run_model(key,monkeypatch)
    assert summary['status']=='analysis_complete' and summary['gemini_calls']==2
    assert value['schema']==actions.SCHEMA and value['results'][0]['page_number']==2
    assert len(value['results'][0]['units'])==2 and not value['accounting_authority']
    assert all(value[k]==0 for k in ('cloud_writes','medical_calls','source_moves','p1_rendered','p1_submitted'))
    assert value['authority_unchanged'] and value['budgets']['peak_live_pages']==1
    text=json.dumps(value,ensure_ascii=False)
    assert key not in text and env['GEMINI_API_KEY'] not in text
    assert all(word not in text for word in ('PRIVATE_MEDICAL','raw_response','structured_tokens','base64','OCR fails'))

def test_new_model_requires_successful_exact_head_p2_and_keeps_unknown_held(local_ocr,key,monkeypatch):
    first,*_=run_model(key,monkeypatch);first['run_id']='99'
    rest,summary,_,_,calls,_=run_model(key,monkeypatch,'page_remaining',first)
    assert summary['status']=='analysis_complete' and summary['gemini_calls']==6
    assert [r['page_number'] for r in rest['results']]==[3,4,5,6]
    assert rest['results'][1]['status']=='human_general_authority_required'

def test_new_model_replay_uses_same_frozen_receipt_ids_with_no_accounting(local_ocr,key,monkeypatch):
    first,*_=run_model(key,monkeypatch);first['run_id']='99'
    replay,*_=run_model(key,monkeypatch,'page_replay',first)
    assert [u['receipt_unit_id'] for u in first['results'][0]['units']]==[
        u['receipt_unit_id'] for u in replay['results'][0]['units']]
    assert replay['cloud_writes']==0

@pytest.mark.parametrize('field,value',[('code_sha','f'*40),('confirmation_digest','f'*64),
    ('source_content_hash','f'*64),('authority_unchanged',False),('mode','p2'),('p1_submitted',1)])
def test_stale_prior_never_constructs_gemini(local_ocr,key,monkeypatch,field,value):
    proof,*_=run_model(key,monkeypatch);proof['run_id']='99';proof[field]=value
    with pytest.raises(StateError,match='proof_stale'):run_model(key,monkeypatch,'page_remaining',proof)

def test_page_mode_guard_rejects_unbounded_unknown_batch_before_credentials():
    env={**environment(),'PDF_READONLY_MODE':'page_remaining','PDF_READONLY_P2_RUN_ID':'99',
        'PDF_READONLY_PAGE_BATCH':'all'}
    opener=Mock()
    with pytest.raises(StateError,match='batch_required'):runner.execute(env,'a'*40,opener=opener,approve=Mock())
    opener.assert_not_called()
