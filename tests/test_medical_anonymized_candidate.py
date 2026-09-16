"""Synthetic only: no fixed production amount, original, ID or OCR fixture."""
import base64
from hashlib import sha256
from io import BytesIO
import json
from types import SimpleNamespace

from PIL import Image, ImageDraw, PngImagePlugin
import pytest

from app.medical_anonymization import (
    AnonymizationHold, VERSION, anchors, enclosure, png, render_single_page,
    validate_png, verify_cell_pixels, prepare_payment_crop,
)
from app.medical_image_candidate import (
    PaymentAnswer, admit_answer, analysis_key, request_payment, seal_crop,
)


def glyph_fixture(text='領収額123円'):
    image=Image.new('RGB',(len(text)*12+8,24),'white');draw=ImageDraw.Draw(image)
    glyphs=[]
    for i,char in enumerate(text):
        box=(4+i*12,5,12+i*12,19);draw.rectangle(box,outline='black')
        glyphs.append((char,(box[0],box[1],box[2]+1,box[3]+1)))
    observations=[{'text':text,'box':(4,5,image.width-4,20),'confidence':90,'line':(1,1,1)}]
    return image,observations,glyphs


def test_positive_grammar_and_unobserved_ink_are_both_required():
    image,observations,glyphs=glyph_fixture()
    assert verify_cell_pixels(image,observations=observations,glyphs=glyphs)=='領収額'
    ImageDraw.Draw(image).point((1,1),fill='black')
    with pytest.raises(AnonymizationHold,match='unaccounted_cell_ink'):
        verify_cell_pixels(image,observations=observations,glyphs=glyphs)


@pytest.mark.parametrize('text',['領収額123円患者12345','領収額123円山田','領収額123円2026/1/1',
    '領収額123円電話09012345678','領収額123円薬剤','領収額123円45円','請求額123円','保険負担123円','預り金123円'])
def test_identifying_or_non_payment_content_in_amount_cell_is_held(text):
    image,observations,glyphs=glyph_fixture(text)
    with pytest.raises(AnonymizationHold):verify_cell_pixels(image,observations=observations,glyphs=glyphs)


def test_digit_recognition_does_not_need_to_agree_on_correct_amount():
    image,observations,glyphs=glyph_fixture('領収額123円')
    observations[0]['text']='領収額128円'
    assert verify_cell_pixels(image,observations=observations,glyphs=glyphs)=='領収額'


def test_current_payment_label_is_supported_without_accepting_other_balances():
    from app.medical_anonymization import LABELS
    from app.medical_image_candidate import PaymentEvidence
    image,observations,glyphs=glyph_fixture('今回入金額123円')
    assert verify_cell_pixels(image,observations=observations,glyphs=glyphs)=='今回入金額'
    assert anchors([dict(observations[0],text='今回入金額')])
    candidate=PaymentEvidence(amount_yen=123,label='今回入金額',region=[0,0,1000,1000])
    assert candidate.label=='今回入金額'
    assert set(PaymentEvidence.model_json_schema()['properties']['label']['enum'])==set(LABELS)
    for label in ('前回入金額','累計入金額','今回請求額','今回未収額'):
        wrong,obs,chars=glyph_fixture(label+'123円')
        with pytest.raises(AnonymizationHold):verify_cell_pixels(wrong,observations=obs,glyphs=chars)


def test_two_numeric_fields_cannot_be_joined_by_removing_whitespace():
    image,observations,glyphs=glyph_fixture('領収額123456円')
    observations=[dict(observations[0],text='領収額123'),dict(observations[0],text='456円')]
    with pytest.raises(AnonymizationHold,match='multiple_numeric_regions'):
        verify_cell_pixels(image,observations=observations,glyphs=glyphs)


def test_unbounded_or_multiple_row_crop_is_not_approved():
    image=Image.new('RGB',(300,130),'white')
    with pytest.raises(AnonymizationHold):enclosure(image,(20,40,65,60))
    ImageDraw.Draw(image).rectangle((10,30,240,80),outline='black',width=2)
    assert enclosure(image,(20,40,65,60))==(13,33,238,78)


def test_anchor_is_not_an_amount_or_a_fixed_coordinate():
    observations=[{'text':'領収','box':(12,22,42,44),'confidence':90,'line':(1,1,1)},
        {'text':'額','box':(44,22,54,44),'confidence':90,'line':(1,1,1)}]
    assert anchors(observations)==[('領収額',(12,22,54,44))]


def test_fresh_canvas_removes_metadata_and_sender_refuses_it():
    image=Image.new('RGB',(60,20),'white');metadata=PngImagePlugin.PngInfo();metadata.add_text('patient','SYNTHETIC')
    stream=BytesIO();image.save(stream,format='PNG',pnginfo=metadata)
    with pytest.raises(AnonymizationHold,match='metadata'):validate_png(stream.getvalue())
    with Image.open(BytesIO(stream.getvalue())) as opened:clean=png(opened)
    assert not validate_png(clean).info
    assert b'SYNTHETIC' not in clean


def test_rotated_and_multi_page_sources_require_mapping_review():
    image=Image.new('RGB',(60,20),'white');exif=Image.Exif();exif[274]=6
    stream=BytesIO();image.save(stream,format='JPEG',exif=exif)
    with pytest.raises(AnonymizationHold,match='rotation'):render_single_page(stream.getvalue(),'image/jpeg')
    from pypdf import PdfWriter
    pdf=PdfWriter();pdf.add_blank_page(width=100,height=100);pdf.add_blank_page(width=100,height=100)
    stream=BytesIO();pdf.write(stream)
    with pytest.raises(AnonymizationHold,match='page_mapping'):render_single_page(stream.getvalue(),'application/pdf')


def test_large_single_pdf_uses_bounded_adaptive_render_without_raising_pixel_limit():
    from pypdf import PdfWriter
    from app.medical_anonymization import MAX_PIXELS
    pdf=PdfWriter();pdf.add_blank_page(width=2000,height=3000)
    stream=BytesIO();pdf.write(stream)
    image=render_single_page(stream.getvalue(),'application/pdf')
    assert image.width*image.height<=MAX_PIXELS
    assert abs(image.width/image.height-2/3)<.001


def answer(amount=321):
    return PaymentAnswer.model_validate({'status':'readable','candidates':[
        {'amount_yen':amount,'label':'領収額','region':[0,0,1000,1000]}],'reason':''})


def test_only_exact_attested_pixels_and_constant_prompt_reach_existing_client():
    image,_,_=glyph_fixture();payload=png(image);key=b'x'*32;proof=seal_crop(payload,'領収額',key)
    calls=[]
    def create(**kw):calls.append(kw);return SimpleNamespace(output_text=answer().model_dump_json())
    client=SimpleNamespace(interactions=SimpleNamespace(create=create))
    result=request_payment(client,'synthetic-model',payload,proof,key)
    assert result.candidates[0].amount_yen==321
    assert set(calls[0])=={'model','input','response_format','store'} and calls[0]['store'] is False
    assert base64.b64decode(calls[0]['input'][1]['data'])==payload
    assert set(calls[0]['input'][1])=={'type','mime_type','data'}
    tampered=png(Image.new('RGB',image.size,'black'))
    with pytest.raises(ValueError):request_payment(client,'synthetic-model',tampered,proof,key)
    with pytest.raises(ValueError):request_payment(client,'synthetic-model',payload,proof,b'y'*32)
    assert len(calls)==1


def test_result_admission_remains_separate_from_human_and_write_authority():
    image,_,_=glyph_fixture();payload=png(image)
    mapping={'source_sha256':'a'*64,'source_image_sha256':'b'*64,'unit':1,'page':1,
        'crop_sha256':sha256(payload).hexdigest(),'crop_coordinates_original':[10,20,200,60],
        'rotation_clockwise_degrees':0,'preprocessor':VERSION}
    result,signals,policy,outcome=admit_answer(answer(),payload=payload,mapping=mapping,
        model='synthetic-model',identity_key=b'z'*32)
    assert outcome.verdict=='AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION'
    assert result.production_authorized is False and result.write_authorized is False
    assert result.provenance.input_mode=='gemini_validated_payment_crop'
    other=admit_answer(answer(),payload=b'changed',mapping=mapping,model='synthetic-model',identity_key=b'z'*32)[3]
    assert other.verdict=='REQUIRES_HUMAN_REVIEW'
    conflict=admit_answer(answer(),payload=payload,mapping=mapping,model='synthetic-model',identity_key=b'z'*32,manual_conflict=True)[3]
    assert conflict.verdict=='REQUIRES_HUMAN_REVIEW'
    assert analysis_key({'version':'1'},mapping,'synthetic-model')!=analysis_key({'version':'2'},mapping,'synthetic-model')


@pytest.mark.parametrize('status,reason',[('ambiguous','multiple_payment_amounts'),('unreadable','illegible')])
def test_abstention_never_supplies_an_amount(status,reason):
    assert not PaymentAnswer(status=status,candidates=[],reason=reason).candidates
    with pytest.raises(ValueError):PaymentAnswer(status=status,candidates=answer().candidates,reason=reason)


def test_sender_has_no_source_credentials_and_disables_automatic_retry(monkeypatch):
    from app import medical_image_sender as sender
    calls=[]
    class AI:
        def __init__(self,key,model,**kwargs):
            calls.append(kwargs);self.model=model
            self.client=SimpleNamespace(interactions=SimpleNamespace(create=lambda **kw:SimpleNamespace(output_text=answer().model_dump_json())),close=lambda:None)
    monkeypatch.setattr(sender,'GeminiAI',AI)
    key=b'x'*32;payload=png(glyph_fixture()[0])
    packet={'png':base64.b64encode(payload).decode(),'proof':seal_crop(payload,'領収額',key)}
    env={'GEMINI_API_KEY':'synthetic-key','GEMINI_MODEL':'synthetic-model',
        'MEDICAL_CROP_ATTESTATION_KEY':base64.b64encode(key).decode(),'MEDICAL_DERIVED_AI_POLICY':'reviewed-v1:paid'}
    assert sender.execute(packet,env)['status']=='readable'
    assert calls==[{'request_attempts':1}]
    with pytest.raises(ValueError):sender.execute(dict(packet,source_id='synthetic-id'),env)
    with pytest.raises(ValueError):sender.execute(packet,dict(env,GOOGLE_SERVICE_ACCOUNT_FILE='private.json'))
    with pytest.raises(ValueError):sender.execute(packet,dict(env,MEDICAL_DERIVED_AI_POLICY='unknown'))
    assert len(calls)==1


def test_issuer_and_date_use_separate_non_ai_evidence():
    from app.medical_candidate_preparation import local_fields
    from app.medical_transaction_combined_shadow import MedicalDocumentBinding
    binding=MedicalDocumentBinding(source_sha256='a'*64,source_image_sha256='b'*64,unit=1,page=1)
    obs=[{'text':'架空病院','box':(10,10,100,30),'confidence':90,'line':(1,1,1)},
        {'text':'発行日 2026/1/2','box':(10,50,200,70),'confidence':90,'line':(2,1,1)},
        {'text':'生年月日 1999/2/3','box':(10,90,200,110),'confidence':90,'line':(3,1,1)}]
    fields,provenance=local_fields(obs,binding,(300,400))
    assert fields['date']=='2026-01-02' and fields['issuer']=='架空病院'
    assert fields['category']=='医療・保険｜病院' and provenance['date_candidates']==1
    obs[0]['text']='患者様 架空病院'
    assert local_fields(obs,binding,(300,400))[0]['issuer']==''


def test_real_local_ocr_isolates_synthetic_payment_cell_and_rejects_injected_ink():
    from pathlib import Path
    payload=(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').read_bytes()
    crop=prepare_payment_crop(payload,'image/png')
    derived=validate_png(crop.payload)
    assert crop.label=='領収金額'
    assert derived.width<700 and derived.height<120
    assert set(derived.getdata())<={(0,0,0),(255,255,255)}
    # Identifier-looking text inside the numeric cell must not be discarded as
    # "OCR did not identify PII". It is either positive forbidden text or ink.
    with Image.open(BytesIO(payload)) as original:
        modified=original.copy()
    ImageDraw.Draw(modified).text((230,240),'PATIENT 123456',fill='black')
    with pytest.raises(AnonymizationHold):prepare_payment_crop(png(modified),'image/png')
    # Two receipts within one raster have no unique unit-to-payment binding.
    with Image.open(BytesIO(payload)) as original:
        combined=Image.new('RGB',(700,800),'white');combined.paste(original,(0,0));combined.paste(original,(0,410))
    with pytest.raises(AnonymizationHold):prepare_payment_crop(png(combined),'image/png')


def test_pixels_ignored_by_ink_validation_cannot_survive_outbound_image():
    from pathlib import Path
    payload=(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').read_bytes()
    with Image.open(BytesIO(payload)) as original:modified=original.copy()
    ImageDraw.Draw(modified).text((230,240),'PRIVATE',fill=(225,225,225))
    derived=prepare_payment_crop(png(modified),'image/png')
    assert derived.payload==prepare_payment_crop(payload,'image/png').payload
