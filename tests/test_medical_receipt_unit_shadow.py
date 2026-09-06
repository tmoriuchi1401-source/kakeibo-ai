from io import BytesIO
import json
from dataclasses import replace
import pytest
from PIL import Image
from pypdf import PdfWriter
from app import medical_receipt_unit_shadow as unit
from app.medical_receipt_privacy import _StructuredOcrToken as Token, build_receipt_privacy_preview


def pdf(count=2):
    writer=PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=100,height=200)
    output=BytesIO(); writer.write(output); return output.getvalue()


def png():
    output=BytesIO()
    with Image.new('RGB',(300,600),'white') as image:
        image.save(output,format='PNG')
    return output.getvalue()


def install(monkeypatch,amounts=('1200','3500'),failure=None,empty=None):
    calls=[]
    def text(image):
        i=len(calls)
        if i==failure:
            calls.append((i,None))
            raise ValueError('PRIVATE_FAILURE')
        return ('病院 診療\n支払額 '+amounts[i]+'円') if i!=empty else ''
    def tokens(image,page):
        i=len(calls); calls.append((i,page))
        return (Token('支払額',page,10,20,60,20,96,(1,1,1,5)),
                Token(amounts[i]+'円',page,90,20,60,20,96,(1,1,1,5)))
    monkeypatch.setattr(unit.local.extraction,'_run_image_ocr',text)
    monkeypatch.setattr(unit.local.extraction,'_run_image_ocr_tokens',tokens)
    return calls


@pytest.mark.parametrize('amounts',[('1200','3500'),('1200','1200')])
def test_independent_receipts_never_conflict_or_collapse_across_pages(monkeypatch,amounts):
    calls=install(monkeypatch,amounts)
    result=unit.evaluate_receipt_units(pdf(),'application/pdf')
    assert result['complete'] and result['unit_count']==2
    assert [u['ordinal'] for u in result['units']]==[1,2]
    assert all(u['resolver_status']=='confirmed' and u['candidate_count']==1 for u in result['units'])
    assert calls==[(0,1),(1,1)]
    assert all(amount not in json.dumps(result) for amount in amounts)
    assert result['external_ai_allowed'] is False


def test_diagnostic_document_pooling_reproduction_is_not_unit_aggregation():
    a='病院 診療\n支払額 1200円'; b='病院 診療\n支払額 3500円'
    assert build_receipt_privacy_preview(a).status=='confirmed'
    assert build_receipt_privacy_preview(b).status=='confirmed'
    assert build_receipt_privacy_preview(a+'\n'+b).status=='needs_review'
    assert build_receipt_privacy_preview(a+'\n'+a).status=='confirmed'


@pytest.mark.parametrize('mode',['failure','empty'])
def test_failed_unit_is_explicit_and_never_repaired_by_sibling(monkeypatch,mode,capsys):
    install(monkeypatch,**{mode:1})
    result=unit.evaluate_receipt_units(pdf(),'application/pdf')
    assert not result['complete'] and len(result['units'])==2
    assert result['units'][0]['resolver_status']=='confirmed'
    assert result['units'][1]['resolver_status']=='needs_review'
    assert result['units'][1]['classification']=='sensitive_unknown'
    assert 'PRIVATE' not in json.dumps(result)+str(capsys.readouterr())


def test_classifier_runs_per_unit_not_on_a_medical_sibling(monkeypatch):
    install(monkeypatch)
    texts=iter(['病院 診療\n支払額 1200円','スーパー\n合計 3500円'])
    monkeypatch.setattr(unit.local.extraction,'_run_image_ocr',lambda image:next(texts))
    result=unit.evaluate_receipt_units(pdf(),'application/pdf')
    assert result['units'][0]['classification']=='medical'
    assert result['units'][1]['classification']!='medical'
    assert all(u['external_ai_allowed'] is False for u in result['units'])


def test_pdf_never_uses_embedded_text_or_whole_document_observer(monkeypatch):
    install(monkeypatch)
    def forbidden(*args,**kwargs):
        raise AssertionError('whole document observer used')
    monkeypatch.setattr(unit.local,'_pdf_input',forbidden)
    monkeypatch.setattr(unit.local.extraction,'_extract_pdf_embedded_text',forbidden)
    assert unit.evaluate_receipt_units(pdf(),'application/pdf')['complete']


def test_single_png_and_pdf_page_share_identical_observer(monkeypatch):
    install(monkeypatch,('1200',))
    first=unit.evaluate_receipt_units(png(),'image/png')['units'][0]
    install(monkeypatch,('1200',))
    second=unit.evaluate_receipt_units(pdf(1),'application/pdf')['units'][0]
    assert first==second


def test_mixed_page_tokens_rejected_before_evidence_collection():
    from app.medical_layout_shadow import PageFrame
    tokens=(Token('支払額',1,10,20,60,20,96,(1,1,1,5)),Token('1200円',2,90,20,60,20,96,(1,1,1,5)))
    with pytest.raises(ValueError):
        unit.summarize_unit_observation('病院 診療',tokens,PageFrame(1,300,600))


def test_low_confidence_malformed_competitor_survives_unit_boundary():
    from app.medical_layout_shadow import PageFrame
    tokens=(Token('支払額',1,10,20,60,20,96,(1,1,1,5)),Token('1200円',1,90,20,60,20,96,(1,1,1,5)),
            Token('3.500',1,170,20,60,20,69,(1,1,1,5)))
    r=unit.summarize_unit_observation('病院 診療\n支払額 1200円',tokens,PageFrame(1,300,600))
    assert r['resolver_status']=='needs_review'
    assert r['counters']['low_confidence_atoms']==r['counters']['malformed_atoms']==1
    assert r['counters']['competing_payment_region_views']>0


@pytest.mark.parametrize('content,mime',[(bytearray(b'private'),'image/png'),(memoryview(b'private'),'image/png'),(b'private','text/plain')])
def test_invalid_input_does_not_start_ocr(monkeypatch,content,mime):
    calls=install(monkeypatch)
    assert not unit.evaluate_receipt_units(content,mime)['complete']
    assert not calls


def test_page_limit_is_checked_before_ocr(monkeypatch):
    calls=install(monkeypatch)
    assert unit.evaluate_receipt_units(pdf(4),'application/pdf')['input_status']=='rejected'
    assert not calls


def test_render_failure_has_its_own_unit_and_does_not_borrow_evidence(monkeypatch):
    import pypdfium2 as pdfium
    install(monkeypatch,('1200',))
    render=pdfium.PdfPage.render
    calls=[]
    def partial(self,*args,**kwargs):
        calls.append(True)
        if len(calls)==2:
            raise RuntimeError('PRIVATE_RENDER_FAILURE')
        return render(self,*args,**kwargs)
    monkeypatch.setattr(pdfium.PdfPage,'render',partial)
    result=unit.evaluate_receipt_units(pdf(),'application/pdf')
    assert result['unit_count']==2 and not result['complete']
    assert result['units'][0]['resolver_status']=='confirmed'
    assert result['units'][1]['unit_status']=='render_failed'
    assert result['units'][1]['candidate_count']==0


def test_encrypted_pdf_is_rejected_before_render(monkeypatch):
    import pypdfium2 as pdfium
    writer=PdfWriter(); writer.add_blank_page(100,200); writer.encrypt('synthetic')
    output=BytesIO(); writer.write(output)
    def forbidden(*args,**kwargs):
        raise AssertionError('renderer reached')
    monkeypatch.setattr(pdfium,'PdfDocument',forbidden)
    assert unit.evaluate_receipt_units(output.getvalue(),'application/pdf')['input_status']=='rejected'
