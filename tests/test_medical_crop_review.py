from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import Mock

from PIL import Image
import pytest

from app.medical_anonymization import AnonymizationHold, png
from app.medical_crop_review import make_review, reviewed_crop, selected_crop
from app.medical_crop_review_ui import ReviewSession, handler, PAGE
from app.medical_candidate_preparation import prepare, verify_preparation
from app.medical_candidate_state import MedicalCandidateState
from app.receipt_confirmation import ReceiptConfirmation,review_id


def fixture():
    image=Image.open(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').convert('RGB')
    source={'source_id':'synthetic','version':'1','sha256':sha256(png(image)).hexdigest(),'mime_type':'image/png'}
    return source,image,b'x'*32,[25,170,655,270]


def test_only_current_explicit_preview_can_save_and_never_overwrite(tmp_path):
    source,image,key,box=fixture();verify=Mock();out=tmp_path/'human.json'
    session=ReviewSession(source,image,key,out,verify)
    with pytest.raises(ValueError):session.confirm({'ticket':'guessed','confirmed':True})
    one=session.preview({'coordinates':box,'label':'領収金額'})
    two=session.preview({'coordinates':box,'label':'領収金額'})
    with pytest.raises(ValueError):session.confirm({'ticket':one['ticket'],'confirmed':True})
    with pytest.raises(ValueError):session.confirm({'ticket':two['ticket'],'confirmed':False})
    assert not out.exists() and not verify.called
    session.confirm({'ticket':two['ticket'],'confirmed':True})
    record=json.loads(out.read_text());assert verify.call_count==1
    assert reviewed_crop(source,image,record,key).payload==selected_crop(image,box)
    assert not any(k in record for k in ('png','payload','amount_yen','ocr','issuer'))
    with pytest.raises(ValueError):session.confirm({'ticket':two['ticket'],'confirmed':True})


def test_owner_can_choose_current_payment_label_and_actions_rebuilds_same_crop(tmp_path):
    source,image,key,box=fixture()
    session=ReviewSession(source,image,key,tmp_path/'human.json',Mock())
    preview=session.preview({'coordinates':box,'label':'今回入金額'})
    session.confirm({'ticket':preview['ticket'],'confirmed':True})
    record=json.loads(session.output.read_text())
    # This tests the selection/binding contract, not OCR correctness or a real
    # person's approval. Production approval must come from the owner UI.
    assert reviewed_crop(source,image,record,key).label=='今回入金額'


def test_source_changed_while_reviewing_does_not_save(tmp_path):
    source,image,key,box=fixture();verify=Mock(side_effect=ValueError('source_changed'))
    session=ReviewSession(source,image,key,tmp_path/'human.json',verify)
    result=session.preview({'coordinates':box,'label':'領収金額'})
    with pytest.raises(ValueError):session.confirm({'ticket':result['ticket'],'confirmed':True})
    assert not session.output.exists()


@pytest.mark.parametrize('change',['version','coordinates','label','statement','preprocessor','crop_sha256'])
def test_saved_review_cannot_approve_a_different_source_or_crop(change):
    source,image,key,box=fixture()
    record=make_review(source,image,box,'領収金額',confirmed=True,key=key)
    if change=='version':source=dict(source,version='2')
    else:record[change]='changed'
    with pytest.raises(AnonymizationHold):reviewed_crop(source,image,record,key)


def test_confirmed_pixels_rebuilt_in_key_free_preparation_and_invalid_review_held(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    source,image,key,box=fixture();record=make_review(source,image,box,'領収金額',confirmed=True,key=key)
    packet,crop=prepare(source,png(image),b'y'*32,crop_review=record,review_key=key)
    assert packet['status']=='prepared' and packet['mapping']['validation']=='exact_human_reviewed_payment_crop'
    assert crop==selected_crop(image,box)
    verify_preparation(packet,crop,b'y'*32)
    record['crop_sha256']='0'*64
    held,crop=prepare(source,png(image),b'y'*32,crop_review=record,review_key=key)
    assert held['status']=='held' and crop is None


def test_publish_review_does_not_touch_inputs_decision_or_candidate(tmp_path):
    class Store:
        value={'confirmation_items':{}}
        def save(self,value):self.value=deepcopy(value)
    source,image,key,box=fixture();store=Store();review=ReceiptConfirmation(store,None,Mock())
    review.observe_medical(source,'synthetic-folder');rid=review_id('medical',source)
    store.value['confirmation_items'][rid]['inputs']=['2026-01-02','','','','','保留','','メモ']
    before=deepcopy(store.value['confirmation_items'])
    record=make_review(source,image,box,'領収金額',confirmed=True,key=key)
    state=MedicalCandidateState(store)
    assert state.install_crop_review(record,image,key)
    assert not state.install_crop_review(record,image,key)
    assert store.value['confirmation_items']==before
    assert 'medical_image_analyses' not in store.value


def test_ui_denies_external_host_origin_and_does_not_enable_confirm_by_default():
    kind=handler(None,'random','http://127.0.0.1:1234')
    instance=object.__new__(kind);instance.headers={'Host':'attacker.example'}
    assert not instance.valid()
    instance.headers={'Host':'127.0.0.1:1234'};assert instance.valid()
    replies=[];instance.reply=lambda *x:replies.append(x)
    instance.headers={'Host':'127.0.0.1:1234','Origin':'https://attacker.example'}
    instance.do_POST();assert replies[0][0]==403
    assert 'id="save" disabled' in PAGE and 'type="checkbox" id="confirmed"' in PAGE
    assert 'https://' not in PAGE


def test_entire_original_cannot_be_selected_as_payment_crop(tmp_path):
    source,image,key,_=fixture();session=ReviewSession(source,image,key,tmp_path/'human.json',Mock())
    with pytest.raises(ValueError,match='minimal'):
        session.preview({'coordinates':[0,0,image.width,image.height],'label':'領収金額'})


@pytest.mark.parametrize('change',['metadata_only','bytes','during_read','parent'])
def test_ui_can_review_same_bytes_current_version_without_repinning_production(monkeypatch,tmp_path,change):
    from types import SimpleNamespace
    from app.medical_crop_review_ui import load_session
    monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    source,image,key,box=fixture()
    item={'source':source,'folder_id':'synthetic-folder','kind':'medical','status':'waiting'}
    store=SimpleNamespace(value={'confirmation_items':{'synthetic':item}},save=Mock())
    before={'version':'2','parents':['synthetic-folder'],'trashed':False,'mimeType':'image/png'}
    if change=='parent':before['parents']=['different-folder']
    after=dict(before,version='3') if change=='during_read' else before
    drive=Mock();drive.files().get().execute.side_effect=[before,after,dict(before,version='3')]
    monkeypatch.setattr('app.google_clients.read_only_drive_service',lambda:drive)
    monkeypatch.setattr('app.google_clients.download_drive_file',lambda *a:b'changed' if change=='bytes' else png(image))
    monkeypatch.setattr('app.receipt_reimport_production.ReimportStore',lambda *a:store)
    monkeypatch.setattr('app.settings.service_account_source',lambda:(None,{'private_key':'synthetic-only'}))
    binding=tmp_path/'binding.json';binding.write_text(json.dumps({'parent_folder_id':'synthetic-folder','file_id':'synthetic-file','manifest_digest':'synthetic'}))
    if change=='metadata_only':
        session=load_session(binding,tmp_path/'review.json')
        assert session.source['version']=='2' and source['version']=='1'
        assert not store.save.called and not session.output.exists()
        with pytest.raises(ValueError,match='review_source_changed'):session.verify()
    else:
        with pytest.raises(ValueError,match='review_source_changed'):load_session(binding,tmp_path/'review.json')
        assert not store.save.called
