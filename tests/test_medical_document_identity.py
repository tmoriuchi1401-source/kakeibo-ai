"""Synthetic identifiers only; local evidence never grants write/send authority."""
from copy import deepcopy
import json
from unittest.mock import Mock

from PIL import Image
import pytest

from app import medical_document_identity as identity
from test_medical_local_reading import parsed,proof,token


def evidence(monkeypatch,number='001234',*,source='a',key=b'x'*32,day='2026-09-01',merchant='Synthetic clinic'):
    monkeypatch.setattr(identity,'confirm_number',lambda *a:True)
    p=proof();p['document_binding'].update(source_sha256=source*64,source_image_sha256=source*64,page=1,unit=1)
    receipt=parsed();receipt.date=day;receipt.merchant=merchant
    obs=[token('請求書番号'+number)]
    return identity.read_identity(None,obs,receipt,p,key)


def test_same_document_different_bytes_and_distinct_family_receipts(monkeypatch):
    first=evidence(monkeypatch)
    same=evidence(monkeypatch,source='b')
    other=evidence(monkeypatch,'001235',source='c')
    assert identity.compare_identities(first,same)=='same'
    assert identity.compare_identities(first,other)=='different'
    assert identity.compare_identities(first,evidence(monkeypatch,'1234',source='d'))=='different'
    assert identity.compare_identities(first,evidence(monkeypatch,source='d',day='2026-09-02'))=='different'
    assert identity.compare_identities(first,evidence(monkeypatch,source='e',merchant='Another spelling'))=='unknown'


def test_evidence_does_not_persist_raw_identifiers_or_ocr(monkeypatch):
    out=evidence(monkeypatch,number='987650123456',merchant='Synthetic private issuer')
    serialized=json.dumps(out)
    assert '987650123456' not in serialized and 'Synthetic private issuer' not in serialized
    assert 'box' not in out and 'text' not in out


def test_only_typographic_middle_dot_variant_is_equivalent(monkeypatch):
    left=evidence(monkeypatch,merchant='Synthetic・clinic')
    right=evidence(monkeypatch,source='b',merchant='Synthetic·clinic')
    assert identity.compare_identities(left,right)=='same'
    changed=evidence(monkeypatch,source='c',merchant='Syntheticーclinic')
    assert identity.compare_identities(left,changed)=='unknown'


def test_different_key_conflicting_same_pixels_or_amount_is_unknown(monkeypatch):
    first=evidence(monkeypatch)
    assert identity.compare_identities(first,evidence(monkeypatch,source='b',key=b'y'*32))=='unknown'
    assert identity.compare_identities(first,evidence(monkeypatch,'555555'))=='unknown'
    changed=deepcopy(first);changed['amount']+=1
    assert identity.compare_identities(first,changed)=='unknown'
    assert identity.compare_identities(first,None)=='unknown'
    changed=deepcopy(first);changed['number_readers']=1
    assert identity.compare_identities(first,changed)=='unknown'


@pytest.mark.parametrize('text',['患者番号001234','保険番号001234','001234','請求書番号001234/001235','領収証No.A001234'])
def test_patient_unlabelled_and_ambiguous_identifiers_are_not_document_numbers(text):
    assert identity.number_region([token(text)]) is None


def test_adjacent_number_allows_detector_overlap_but_not_intervening_text():
    label=token('請求書番号',(0,0,100,25))
    number=token('001234',(97,0,160,25))
    assert identity.number_region([label,number])==('001234',(0,0,160,25))
    number['box']=(125,0,185,25)
    assert identity.number_region([label,token('注',(105,0,120,25)),number]) is None
    assert identity.number_region([label,number,token('999',(150,0,200,25))]) is None


def test_multiple_or_uncertain_document_numbers_do_not_resolve(monkeypatch):
    assert identity.number_region([token('請求書番号001234'),token('領収証No.001234')]) is None
    assert identity.number_region([token('請求書番号001234',confidence=89)]) is None
    reader=Mock(return_value=False);monkeypatch.setattr(identity,'confirm_number',reader)
    p=proof();p['document_binding'].update(source_image_sha256='b'*64,page=1,unit=1)
    assert identity.read_identity(None,[token('請求書番号001234')],parsed(),p,b'x'*32) is None
    p['date_evidence_verified']=False;reader.reset_mock()
    assert identity.read_identity(None,[token('請求書番号001234')],parsed(),p,b'x'*32) is None
    reader.assert_not_called()


@pytest.mark.parametrize('readings,accepted,calls',[
    (['請求書番号 001234'],True,1),
    (['請求書番号 001235','001234'],False,1),
    (['請求書番号 1234','001234'],False,1),
    (['','001234'],True,2),
    (['','0012 34'],False,2),
    (['-001234','001234'],False,1),
])
def test_independent_number_reader_preserves_zeros_and_disagreement(monkeypatch,readings,accepted,calls):
    import pytesseract
    reader=Mock(side_effect=readings);monkeypatch.setattr(pytesseract,'image_to_string',reader)
    assert identity.confirm_number(Image.new('RGB',(200,30),'white'),(0,0,200,30),'001234') is accepted
    assert reader.call_count==calls


def test_preparation_keeps_identity_opaque_and_cannot_create_cloud_crop(monkeypatch):
    from hashlib import sha256
    from app import medical_candidate_preparation as prep,receipt_local_ocr as ocr,medical_local_reading as local
    from test_medical_local_reading import observations,fields
    monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    monkeypatch.setenv('KAKEIBO_LOCAL_OCR_ENABLED','true')
    monkeypatch.setattr(prep,'render_single_page',lambda *a:Image.new('RGB',(300,200),'white'))
    obs=observations()+[token('請求書番号00987654',(0,100,200,120))]
    monkeypatch.setattr(ocr,'read_tokens',lambda *a:obs)
    def local_fields(obs,binding,size):
        p=proof();p['document_binding']=binding.model_dump()
        return fields(),p
    monkeypatch.setattr(prep,'local_fields',local_fields)
    monkeypatch.setattr(local,'confirm_digits',lambda *a:True)
    monkeypatch.setattr(identity,'confirm_number',lambda *a:True)
    crop=Mock(side_effect=AssertionError('No cloud crop'))
    monkeypatch.setattr(prep,'prepare_payment_crop',crop)
    source={'sha256':sha256(b'synthetic').hexdigest(),'mime_type':'image/png'}
    packet,pixels=prep.prepare(source,b'synthetic',b'ephemeral',automatic=True,document_key=b'x'*32)
    assert packet['status']=='local_ready' and pixels is None
    assert packet['local_provenance']['document_identity']['source_sha256']==source['sha256']
    assert '00987654' not in json.dumps(packet)
    crop.assert_not_called()
