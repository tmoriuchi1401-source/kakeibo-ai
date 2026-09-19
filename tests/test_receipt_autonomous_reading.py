import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.gemini_ai import GeminiAI
from app.models import ReceiptResult
from app.receipt_validation import validate_receipt_result
from app import gemini_ai,medical_candidate_preparation as medical

CATS=[('食費','食料品')]
def result(amounts=(825,1265,-200),total=1890,**changes):
    return dict(merchant='試験商店',date='2026-01-02',total=total,
        items=[dict(name='商品' if n>=0 else '値引き',amount=n,major_category='食費',minor_category='食料品') for n in amounts],**changes)

def ai_with(monkeypatch,values):
    monkeypatch.setattr(gemini_ai,'require_receipt_ai_permission',lambda *a,**kw:None)
    api=Mock(side_effect=[SimpleNamespace(output_text=json.dumps(v,ensure_ascii=False)) if isinstance(v,dict) else v for v in values])
    ai=object.__new__(GeminiAI);ai.model='synthetic';ai.client=SimpleNamespace(interactions=SimpleNamespace(create=api))
    return ai,api

@pytest.mark.parametrize('first,fixed',[(result((825,1265)),result()),
    (result((330,),660),result((330,330),660)),(result((200,300),540),result((200,300,40),540))])
def test_reread_recovers_discount_missing_item_and_printed_external_tax(monkeypatch,first,fixed):
    ai,api=ai_with(monkeypatch,[first,fixed])
    parsed=ai.analyze_receipt(b'synthetic original','image/png',CATS)
    assert validate_receipt_result(parsed,CATS)[0] and [x.amount for x in parsed.items]==[x['amount'] for x in fixed['items']]
    assert api.call_count==2
    assert api.call_args_list[0].kwargs['input'][1]==api.call_args_list[1].kwargs['input'][1]
    assert '明細合計' in api.call_args.kwargs['input'][0]['text']

def test_exact_result_does_not_trigger_extra_requests(monkeypatch):
    ai,api=ai_with(monkeypatch,[result()]);ai.analyze_receipt(b'original','image/png',CATS)
    assert api.call_count==1

def test_missing_information_remains_held_after_bounded_rereads(monkeypatch):
    value=result((999,),999);value.update(date='',merchant='')
    ai,api=ai_with(monkeypatch,[value]*3);parsed=ai.analyze_receipt(b'cropped','image/png',CATS)
    assert not validate_receipt_result(parsed,CATS)[0] and api.call_count==3
    assert parsed.date==parsed.merchant==''

def test_transport_failure_is_not_replayed(monkeypatch):
    ai,api=ai_with(monkeypatch,[TimeoutError()])
    with pytest.raises(TimeoutError):ai.analyze_receipt(b'original','image/png',CATS)
    assert api.call_count==1

def test_schema_error_can_be_corrected_without_echoing_unvalidated_text(monkeypatch):
    ai,api=ai_with(monkeypatch,[SimpleNamespace(output_text='UNTRUSTED INVALID TEXT'),result()])
    assert ai.analyze_receipt(b'original','image/png',CATS).total==1890
    assert 'UNTRUSTED INVALID TEXT' not in api.call_args.kwargs['input'][0]['text']


def test_changed_total_cannot_hide_an_omitted_item_without_corroboration(monkeypatch):
    ai,api=ai_with(monkeypatch,[result((330,),660),result((330,),330),result((660,),660)])
    parsed=ai.analyze_receipt(b'original','image/png',CATS)
    assert not validate_receipt_result(parsed,CATS)[0] and parsed.total==660
    assert api.call_count==3


def test_corroborated_changed_total_can_recover_a_bad_initial_reading(monkeypatch):
    ai,api=ai_with(monkeypatch,[result((330,),660),result((330,),330),result((330,),330)])
    parsed=ai.analyze_receipt(b'original','image/png',CATS)
    assert validate_receipt_result(parsed,CATS)[0] and parsed.total==330 and api.call_count==3

@pytest.mark.parametrize('patch',[{'total':1891},{'date':'2026-02-30'},{'date':'yesterday'},
    {'merchant':' '},{'items':[]},{'total':0}])
def test_posting_rejects_small_imbalance_invalid_fields_and_empty_items(patch):
    value=result();value.update(patch)
    assert not validate_receipt_result(ReceiptResult.model_validate(value),CATS)[0]

def fields(day='2026-01-02',issuer='試験医院'):
    return dict(date=day,issuer=issuer,category='医療・保険｜病院' if issuer else ''),dict(
        date_candidates=bool(day),date_evidence_verified=bool(day),date_basis='payment',
        issuer_status='SELECTED_ISSUER' if issuer else 'missing',paid_receipt_evidence=True)

def test_medical_different_layout_recovers_fields_without_ai(monkeypatch):
    monkeypatch.setattr(medical,'local_fields',Mock(side_effect=[fields('', ''),fields(),fields()]))
    monkeypatch.setattr(medical,'tokens',Mock(return_value=[{'transient':'private OCR'}]))
    found,p=medical.reread_local_fields(SimpleNamespace(size=(100,100)),[],object())
    assert found['date']=='2026-01-02' and found['issuer']=='試験医院' and p['field_readings']==3
    assert 'private OCR' not in json.dumps([found,p])

@pytest.mark.parametrize('conflict',[fields(day='2026-01-03'),fields(issuer='別医院')])
def test_medical_conflicting_fields_do_not_use_majority_vote(monkeypatch,conflict):
    monkeypatch.setattr(medical,'local_fields',Mock(side_effect=[fields(),fields(),conflict]))
    monkeypatch.setattr(medical,'tokens',Mock(return_value=[{}]))
    found,p=medical.reread_local_fields(SimpleNamespace(size=(100,100)),[],object())
    assert not found['date'] or not found['issuer']

def test_partial_medical_reread_cannot_promote_missing_fields(monkeypatch):
    original=fields('', '')
    monkeypatch.setattr(medical,'local_fields',Mock(side_effect=[original,fields()]))
    monkeypatch.setattr(medical,'tokens',Mock(side_effect=[[{}],TimeoutError()]))
    assert medical.reread_local_fields(SimpleNamespace(size=(100,100)),[],object())==original
