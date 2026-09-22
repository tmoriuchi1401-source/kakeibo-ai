"""Synthetic local OCR/authority regressions; no private images or API keys."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PIL import Image

from app import medical_local_reading as local
from app.medical_local_date import receipt_date
from app.medical_anonymization import AnonymizationHold
from app.models import ReceiptResult,ReceiptItem
from app.receipt_confirmation import TITLE
from app.drive_run_state import StateError
from test_receipt_confirmation import medical

def token(text,box=(0,0,100,20),confidence=99,line=1):
    return dict(text=text,box=box,confidence=confidence,line=(1,line,1))

def observations():
    return [token('領収書',(0,0,80,20)),token('領収金額',(0,50,100,70)),token('1,234円',(120,50,220,70))]

def fields():return {'date':'2026-09-01','issuer':'Synthetic clinic','category':'医療・保険｜病院'}
def proof():return {'date_evidence_verified':True,'date_candidates':1,'issuer_status':'SELECTED_ISSUER','document_binding':{'source_sha256':'a'*64}}
def parsed():return ReceiptResult(date='2026-09-01',merchant='Synthetic clinic',total=1234,payment_method='',items=[ReceiptItem(name='医療費',amount=1234,major_category='医療・保険',minor_category='病院')])

@pytest.mark.parametrize('text,expected',[('￥１，２３４－',1234),('3，780円',3780),('300',300),('1,234',1234),('-123',None),('1.234',None),('1,23',None),('12O円',None),('123円注',None),('Y123',None)])
def test_numeric_grammar_never_repairs_digits(text,expected):assert local.amount_text(text)==expected


@pytest.mark.parametrize('readings,expected,accepted,calls',[
    (['1,234'],1234,True,1),
    (['1,235','1,234'],1234,False,1),
    (['1.234','1,234'],1234,True,2),
    (['','1,235'],1234,False,2),
    (['.','1.234'],1234,False,2),
    (['123円注','123円注'],123,False,2),
    (['-123','123'],123,False,1),
    (['△123','123'],123,False,1),
])
def test_digit_reread_is_bounded_and_never_retries_a_valid_disagreement(monkeypatch,readings,expected,accepted,calls):
    import pytesseract
    reader=Mock(side_effect=readings)
    monkeypatch.setattr(pytesseract,'image_to_string',reader)
    assert local.confirm_digits(Image.new('RGB',(100,50),'white'),(0,0,100,50),expected) is accepted
    assert reader.call_count==calls
    assert all(c.kwargs['timeout']==20 and c.kwargs['config']=='--psm 7' for c in reader.call_args_list)

def test_label_pair_uses_actual_payment_not_larger_insurance_total(monkeypatch):
    obs=observations()+[token('保険総額',(0,100,100,120)),token('9,999円',(120,100,220,120))]
    verify=Mock(return_value=True);monkeypatch.setattr(local,'confirm_digits',verify)
    result,reason=local.read_local_payment(Image.new('RGB',(300,200)),obs,fields(),proof())
    assert reason=='' and result.total==1234
    assert result.payment_method=='' and result.items[0].minor_category=='病院'
    assert verify.call_args.args[1:]==((120,50,220,70),1234)

@pytest.mark.parametrize('extra,reason',[
    ([token('支払金額',(0,150,100,170)),token('500円',(120,150,220,170))],'payment_amount_conflict'),
    ([token('777',(40,75,100,95))],'payment_pair_ambiguous'),
    ([token('取消済')],'payment_qualifier_present'),
    ([token('分割支払')],'payment_qualifier_present'),
])
def test_competing_amounts_and_qualifiers_hold_before_second_ocr(monkeypatch,extra,reason):
    verify=Mock();monkeypatch.setattr(local,'confirm_digits',verify)
    result,actual=local.read_local_payment(Image.new('RGB',(300,200)),observations()+extra,fields(),proof())
    assert result is None and actual==reason and not verify.called

def test_disagreement_missing_or_low_confidence_never_autopost(monkeypatch):
    monkeypatch.setattr(local,'confirm_digits',lambda *a:False)
    assert local.read_local_payment(None,observations(),fields(),proof())[1]=='local_digits_disagree'
    obs=observations();obs[-1]['confidence']=89
    assert local.candidate_pairs(obs)[1]=='payment_digits_uncertain'
    obs[-1]['confidence']=99;obs[1]['confidence']=84
    assert local.candidate_pairs(obs)[1]=='payment_label_uncertain'
    f=fields();f['date']=''
    assert local.read_local_payment(None,observations(),f,proof())[1]=='local_fields_incomplete'


@pytest.mark.parametrize('heading', ['医療費請求（領収）書', '診療費請求(領収)書', '医療费請求(領収)書'])
def test_combined_invoice_receipt_requires_verified_actual_payment(monkeypatch, heading):
    obs=observations();obs[0]['text']=heading
    monkeypatch.setattr(local,'confirm_digits',lambda *a:True)
    result,reason=local.read_local_payment(None,obs,fields(),proof())
    assert reason=='' and result.total==1234
    obs[1]['text']='請求金額'
    assert local.read_local_payment(None,obs,fields(),proof())[1]=='payment_label_missing'


@pytest.mark.parametrize('heading', ['医療費請求書', '領収書は再発行しません', '領収書見本', '診療明細書'])
def test_invoice_footer_or_sample_does_not_prove_receipt(monkeypatch, heading):
    obs=observations();obs[0]['text']=heading
    verify=Mock();monkeypatch.setattr(local,'confirm_digits',verify)
    assert local.read_local_payment(None,obs,fields(),proof())[1]=='paid_receipt_missing'
    verify.assert_not_called()


def test_multiple_combined_receipts_remain_held(monkeypatch):
    obs=observations()+[token('医療費請求（領収）書',(0,150,200,170))]
    verify=Mock();monkeypatch.setattr(local,'confirm_digits',verify)
    assert local.read_local_payment(None,obs,fields(),proof())[1]=='paid_receipt_missing'
    verify.assert_not_called()

@pytest.mark.parametrize('label,expected',[('発行日','2026-09-01'),('支払日','2026-09-01'),('生年月日',''),('診療日','')])
def test_date_below_its_own_role(label,expected):
    obs=[token(label,(50,0,100,20)),token('令和8年9月1日',(20,25,130,45),line=2)]
    assert receipt_date(obs)[0]==expected
    obs.insert(1,token('注記',(60,21,80,24),line=3))
    assert receipt_date(obs)[0]==''

def test_two_vertical_date_labels_or_invalid_date_remain_unknown():
    obs=[token('発行日',(50,0,100,20)),token('生年月日',(40,0,110,20),line=2),token('令和8年9月1日',(20,25,130,45),line=3)]
    assert receipt_date(obs)[0]==''
    obs=[token('発行日',(50,0,100,20)),token('令和8年2月30日',(20,25,130,45),line=2)]
    assert receipt_date(obs)[0]==''

def test_local_write_intent_readback_and_idempotency_without_owner_choice():
    review,store,db,verify,source=medical()
    assert local.apply_local(review,source,'synthetic-folder',parsed(),proof())
    item=next(iter(review.items.values()))
    assert item['status']=='applied' and item['local_decision']['external_requests']==0
    assert item['decision_origin']=='automatic' and 'confirmation_hash' not in item
    assert db.rows[TITLE][0][7:15]==['']*8
    assert len(db.rows['支出明細'])==1 and db.rows['支出明細'][0][4]==1234
    assert not local.apply_local(review,source,'synthetic-folder',parsed(),proof())
    assert len(db.rows['支出明細'])==1

@pytest.mark.parametrize('change',['owner','duplicate','source','binding'])
def test_local_authority_vetoes_do_not_write(change):
    review,store,db,verify,source=medical();p=proof()
    if change=='owner':db.rows[TITLE][0][12]='保留'
    if change=='duplicate':db.rows['取込データ']=[['card-id','','card','synthetic','2026-09-20','Card payment',1234,'','auto_expense']]
    if change=='source':verify.side_effect=[None,StateError('confirmation_source_changed')]
    if change=='binding':p['document_binding']['source_sha256']='b'*64
    assert not local.apply_local(review,source,'synthetic-folder',parsed(),p)
    assert not db.rows['支出明細'] and next(iter(review.items.values()))['status']=='waiting'

def test_owner_change_after_durable_intent_aborts_before_write():
    review,store,db,verify,source=medical()
    def change(*a):
        if verify.call_count==2:db.rows[TITLE][0][14]='Owner typing'
    verify.side_effect=change
    assert not local.apply_local(review,source,'synthetic-folder',parsed(),proof())
    assert not db.rows['支出明細'] and db.rows[TITLE][0][14]=='Owner typing'

def test_partial_write_remains_pending_and_cannot_be_replayed():
    review,store,db,verify,source=medical();db.before_write=True
    with pytest.raises(StateError,match='confirmation_write_unknown'):
        local.apply_local(review,source,'synthetic-folder',parsed(),proof())
    assert next(iter(review.items.values()))['status']=='pending'
    db.before_write=False
    assert not local.apply_local(review,source,'synthetic-folder',parsed(),proof())
    with pytest.raises(StateError,match='confirmation_write_reconciliation_required'):review.apply_confirmations()

def test_local_ocr_missing_models_fails_before_engine_or_download(tmp_path,monkeypatch):
    from app import receipt_local_ocr as ocr
    monkeypatch.setenv('KAKEIBO_OCR_MODEL_DIR',str(tmp_path))
    with pytest.raises(AnonymizationHold,match='local_ocr_unavailable'):ocr.read_tokens(Image.new('RGB',(100,100)))

def test_local_ocr_geometry_and_complete_output(monkeypatch):
    import numpy as np
    from app import receipt_local_ocr as ocr
    monkeypatch.setattr(ocr,'model_directory',lambda:'synthetic')
    result=SimpleNamespace(boxes=np.array([[[1,2],[20,2],[20,12],[1,12]]]),txts=['領収額'],scores=[.99])
    engine=Mock(return_value=result);monkeypatch.setattr(ocr,'_engine',lambda p:engine)
    assert ocr.read_tokens(Image.new('RGB',(100,100)))[0]['box']==(1,2,20,12)
    result.scores=[]
    with pytest.raises(AnonymizationHold):ocr.read_tokens(Image.new('RGB',(100,100)))
    result.scores=[.99];result.boxes[0,0,0]=-1
    with pytest.raises(AnonymizationHold):ocr.read_tokens(Image.new('RGB',(100,100)))

def test_local_prepare_consumes_no_cloud_crop_or_network(monkeypatch):
    from hashlib import sha256
    from app import medical_candidate_preparation as prep,receipt_local_ocr as ocr
    monkeypatch.delenv('GEMINI_API_KEY',raising=False);monkeypatch.setenv('KAKEIBO_LOCAL_OCR_ENABLED','true')
    monkeypatch.setattr(prep,'render_single_page',lambda *a:Image.new('RGB',(300,200)))
    monkeypatch.setattr(ocr,'read_tokens',lambda im:observations())
    monkeypatch.setattr(prep,'local_fields',lambda *a:(fields(),proof()))
    monkeypatch.setattr(local,'confirm_digits',lambda *a:True)
    crop=Mock(side_effect=AssertionError('No cloud crop'));monkeypatch.setattr(prep,'prepare_payment_crop',crop)
    source={'sha256':sha256(b'synthetic').hexdigest(),'mime_type':'image/png'}
    packet,pixels=prep.prepare(source,b'synthetic',b'synthetic-key',automatic=True)
    assert packet['status']=='local_ready' and packet['local_parsed']['total']==1234 and pixels is None
    crop.assert_not_called()

def test_intake_posts_one_local_result_and_never_exports_it_to_cloud(monkeypatch,tmp_path):
    import base64,json
    from hashlib import sha256
    from app import receipt_confirmation_production as runtime,google_clients,medical_candidate_preparation as prep
    from test_receipt_confirmation import Store,DB
    store=Store();db=DB();verify=Mock()
    options=SimpleNamespace(receipt_drive_folder_id='synthetic-inbox')
    monkeypatch.setattr(runtime,'open_context',lambda *a:(options,store,db,verify))
    monkeypatch.setattr('app.settings.service_account_source',lambda:('',{'private_key':'synthetic-private-key'}))
    api=Mock();api.files.return_value.list.return_value.execute.return_value={'files':[
        {'id':str(i),'version':'1','mimeType':'application/pdf'} for i in range(2)]}
    monkeypatch.setattr(google_clients,'read_only_drive_service',lambda:api)
    monkeypatch.setattr(google_clients,'download_drive_file',lambda sid,*a:sid.encode())
    monkeypatch.setattr(runtime,'evaluate_receipt_privacy',lambda *a,**k:SimpleNamespace(classification='medical'))
    monkeypatch.setattr(runtime,'configure_ui',lambda *a,**k:None)
    monkeypatch.setattr(runtime,'sync_review_visibility',lambda *a:None)
    monkeypatch.setattr('app.expense_view.ExpenseViewPipeline.refresh',lambda *a:{})
    def prepare(source,*a,**kw):
        p=proof();p['document_binding']['source_sha256']=source['sha256']
        return dict(source=source,status='local_ready',fields=fields(),local_provenance=p,local_parsed=parsed().model_dump()),None
    monkeypatch.setattr(prep,'prepare',prepare)
    env={'MEDICAL_PREPARE_DIR':str(tmp_path),'MEDICAL_CROP_ATTESTATION_KEY':base64.b64encode(b'synthetic').decode(),'MEDICAL_DERIVED_AI_POLICY':'auto-v1:free'}
    result=runtime.execute(env,True)
    assert result['written']==result['medical_local_written']==1
    assert len(db.rows['支出明細'])==1 and result['medical_pending']==1
    plans=json.loads((tmp_path/'medical-plan.json').read_bytes())
    assert len(plans)==1 and plans[0]['status']=='held' and 'local_parsed' not in plans[0]
    assert not list(tmp_path.glob('*.png'))
    assert all(row[7:15]==['']*8 for row in db.rows[TITLE])
    # Repeated scans cannot duplicate the applied payment.
    assert runtime.execute(env,True)['written']==0
    assert len(db.rows['支出明細'])==1


def test_local_archive_requires_complete_current_original_and_keeps_properties():
    review,store,db,verify,source=medical();drive=Mock()
    local.archive_local(review,source,'synthetic-folder','synthetic-processed',drive)
    drive.files.assert_not_called()
    local.apply_local(review,source,'synthetic-folder',parsed(),proof())
    meta={'parents':['synthetic-folder'],'version':'1','mimeType':'application/pdf','appProperties':{'other':'preserved'}}
    drive.files.return_value.get.return_value.execute.return_value=meta
    local.archive_local(review,source,'synthetic-folder','synthetic-processed',drive)
    args=drive.files.return_value.update.call_args.kwargs
    assert args['addParents']=='synthetic-processed' and args['removeParents']=='synthetic-folder'
    assert args['body']['appProperties']['other']=='preserved' and args['body']['appProperties']['kakeiboReceiptClass']=='medical'
    assert drive.files.return_value.update.return_value.execute.call_args.kwargs=={'num_retries':0}
    drive.files.return_value.update.reset_mock();meta['version']='2'
    with pytest.raises(StateError,match='confirmation_source_changed'):
        local.archive_local(review,source,'synthetic-folder','synthetic-processed',drive)
    drive.files.return_value.update.assert_not_called()
