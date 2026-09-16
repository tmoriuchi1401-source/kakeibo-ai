import base64
from copy import deepcopy
from hashlib import sha256
import hmac
import json
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from app.drive_run_state import StateError
from app.medical_anonymization import png,VERSION
from app.medical_image_candidate import PaymentAnswer,seal_crop
from app.medical_candidate_state import MedicalCandidateState
from app.medical_candidate_runtime import process_plans,send_derived
from app.receipt_confirmation import ReceiptConfirmation,review_id


class Store:
    def __init__(self):self.value={'confirmation_items':{}};self.fail=False
    def save(self,value):
        if self.fail:raise StateError('save_failed')
        self.value=deepcopy(value)


def fixture(count=1):
    key=b'x'*32;payload=png(Image.new('RGB',(120,40),'white'));plans=[];store=Store()
    review=ReceiptConfirmation(store,None,Mock())
    for i in range(count):
        source={'source_id':f'synthetic-{i}','version':'1','mime_type':'image/png','sha256':sha256(str(i).encode()).hexdigest()}
        review.observe_medical(source,'synthetic-folder')
        mapping={'source_sha256':source['sha256'],'source_image_sha256':'b'*64,'unit':1,'page':1,
            'crop_sha256':sha256(payload).hexdigest(),'crop_coordinates_original':[20,30,140,70],
            'rotation_clockwise_degrees':0,'preprocessor':VERSION}
        packet={'source':source,'fields':{'date':'2026-09-01','issuer':'Synthetic clinic','category':'医療・保険｜病院'},
            'local_provenance':{'origin':'LOCAL_OCR'},'status':'prepared','mapping':mapping,'proof':seal_crop(payload,'領収額',key)}
        encoded=json.dumps(packet,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
        packet['preparation_tag']=hmac.new(key,b'medical-preparation\0'+encoded,'sha256').hexdigest()
        packet['review_id']=review_id('medical',source);packet['crop_file']=packet['review_id']+'.png'
        plans.append(packet)
    answer=PaymentAnswer.model_validate({'status':'readable','candidates':[{'amount_yen':432,'label':'領収額','region':[0,0,1000,1000]}],'reason':''})
    send=Mock(return_value=answer)
    args=dict(state=MedicalCandidateState(store),verify_source=Mock(),load_crop=lambda p:payload,send=send,
        model='synthetic-model',key=key,identity_key=b'z'*32)
    return store,plans,args,send


def test_max_three_new_requests_and_successful_replay_is_not_resent():
    store,plans,args,send=fixture(4)
    counts=process_plans(plans,**args)
    assert counts['medical_ai_requests']==3 and counts['medical_ai_held']==1 and send.call_count==3
    assert all(item['inputs']==['']*8 and item['status']=='waiting' for item in store.value['confirmation_items'].values())
    replay=process_plans(plans,**args)
    assert replay['medical_ai_reused']==3 and replay['medical_ai_requests']==1 and send.call_count==4
    assert process_plans(plans,**args)['medical_ai_requests']==0


def test_unknown_response_and_save_failure_never_blindly_resend():
    store,plans,args,send=fixture();send.side_effect=TimeoutError()
    with pytest.raises(TimeoutError):process_plans(plans,**args)
    send.side_effect=None
    with pytest.raises(StateError,match='reconciliation_required'):process_plans(plans,**args)
    assert send.call_count==1
    store,plans,args,send=fixture();store.fail=True
    with pytest.raises(StateError,match='save_failed'):process_plans(plans,**args)
    assert send.call_count==0


def test_unknown_result_persistence_does_not_retry_the_completed_request():
    store,plans,args,send=fixture();saved=store.save
    def fail_after_intent(value):
        if any(r['phase']=='complete' for r in value.get('medical_image_analyses',{}).values()):raise StateError('write_unknown')
        saved(value)
    store.save=fail_after_intent
    with pytest.raises(StateError,match='write_unknown'):process_plans(plans,**args)
    store.save=saved
    with pytest.raises(StateError,match='reconciliation_required'):process_plans(plans,**args)
    assert send.call_count==1


def test_altered_crop_or_cross_source_preparation_cannot_send():
    store,plans,args,send=fixture();args['load_crop']=lambda p:png(Image.new('RGB',(120,40),'black'))
    with pytest.raises(ValueError):process_plans(plans,**args)
    assert send.call_count==0 and not store.value.get('medical_image_analyses')
    store,plans,args,send=fixture();plans[0]['fields']['issuer']='Changed after preparation'
    with pytest.raises(ValueError):process_plans(plans,**args)
    assert send.call_count==0


def test_privacy_hold_is_displayed_without_network_or_amount():
    store,plans,args,send=fixture()
    plans[0]={'source':plans[0]['source'],'review_id':plans[0]['review_id'],'fields':{},'status':'held','reason':'unaccounted_cell_ink'}
    result=process_plans(plans,**args)
    assert result['medical_ai_held']==1 and not send.called
    candidate=next(iter(store.value['confirmation_items'].values()))['medical_candidates']
    assert 'amount_yen' not in candidate and '匿名化確認' in candidate['review_message']


def test_plan_unverified_runs_preparation_without_request_intent_or_network():
    store,plans,args,send=fixture()
    counts=process_plans(plans,**args,allow_send=False)
    assert counts['medical_ai_requests']==0 and not send.called
    assert not store.value.get('medical_image_analyses')
    candidate=next(iter(store.value['confirmation_items'].values()))['medical_candidates']
    assert candidate['provenance']['status']=='prepared_not_sent' and 'amount_yen' not in candidate


def test_network_process_gets_no_original_credentials_or_source_metadata(monkeypatch):
    store,plans,args,send=fixture();seen=[]
    def run(command,**kw):
        seen.append(kw);return SimpleNamespace(returncode=0,stdout=send.return_value.model_dump_json())
    monkeypatch.setattr('app.medical_candidate_runtime.subprocess.run',run)
    env={'GEMINI_API_KEY':'synthetic','GEMINI_MODEL':'synthetic-model','MEDICAL_DERIVED_AI_POLICY':'reviewed-v1:paid',
        'GOOGLE_SERVICE_ACCOUNT_JSON':'must-not-pass','GOOGLE_SERVICE_ACCOUNT_FILE':'must-not-pass',
        'GOOGLE_GMAIL_TOKEN_JSON':'must-not-pass','SPREADSHEET_ID':'must-not-pass',
        'MEDICAL_CROP_ATTESTATION_KEY':base64.b64encode(args['key']).decode()}
    send_derived(plans[0],args['load_crop'](plans[0]),env)
    assert all('must-not-pass'!=v for v in seen[0]['env'].values())
    assert set(json.loads(seen[0]['input']))=={'png','proof'}
    assert 'source_id' not in seen[0]['input'] and 'issuer' not in seen[0]['input']
