import pytest

from app.medical_receipt_privacy import classify_receipt_text, ClassificationDecision
from app.receipt_text_extraction import _ReceiptTextExtraction
from app import receipt_privacy_gate as gate
from app import receipt_classification_ocr as reread

RETAIL = '小計 2点 ¥600\n10%税額 ¥60\nau PAY ¥660'
DINING = '2026/01/02\nピザセット ¥600\nハンバーガー ¥400\n10%内消費税 ¥91\nコード決済 ¥1000'
PARKING = '領 収 書\n中央駐車場\n駐車料金 1,200\n現金 1,200'


@pytest.mark.parametrize('text', [RETAIL, DINING, PARKING,
    '入庫時刻 10:00\n第一パーキング\n受領金額 500円'])
def test_document_structure_recognizes_shop_dining_and_parking_without_merchant_lists(text):
    assert classify_receipt_text(text).classification == 'normal'


def test_dispensing_insurance_statement_is_medical_without_pharmacy_name():
    text = '調 剤 日 令和8年8月1日\n保 険 情 報\n負 担 割 合 30%\n請 求 額 2,040円'
    decision = classify_receipt_text(text)
    assert decision.classification == 'medical'
    assert decision.reason_code == 'medical_multiple_signals'


def test_itemized_tax_and_loyalty_slip_without_title_is_retail():
    text = ('2026年8月1日\n衣料品 ¥5,000\n外税対象額 ¥5,000\n外税 ¥500\n'
            '電子マネー ¥5,500\n獲得予定ポイント 100P')
    assert classify_receipt_text(text).classification == 'normal'
    assert classify_receipt_text(text + '\n診療点数 120').classification == 'medical'
    assert classify_receipt_text(text + '\n保険').classification == 'sensitive_unknown'
    for omitted in ('外税', '電子マネー', '獲得予定ポイント', '2026年8月1日'):
        assert classify_receipt_text(text.replace(omitted, '')).classification == 'sensitive_unknown'


@pytest.mark.parametrize('label,expected', [
    ('<< 買取 >>', True), ('買 取', True), ('高価買取キャンペーン', False),
])
def test_local_gate_exposes_only_explicit_buyback_heading(monkeypatch, label, expected):
    text = f'商品\n{label}\n中古品 ¥600\n合計 ¥600\n現金 ¥600'
    monkeypatch.setattr(gate, '_extract_receipt_text', lambda *_: extracted(text))
    result = gate.evaluate_receipt_privacy(b'synthetic', 'image/png')
    assert result.classification == 'normal'
    assert result.buyback_evidence is expected


def test_buyback_heading_is_normal_receipt_without_purchase_anchor():
    text = '2026/08/21\n<< 買取 >>\n中古品 ¥5\n小計 ¥5\n合計 ¥5\n現金 ¥5'
    assert classify_receipt_text(text).classification == 'normal'


@pytest.mark.parametrize('text', ['領収書 1000円', '登録番号 T1234567890123',
    '合計 ¥1000 消費税10%', '駐車場の案内 現金500円',
    '2026/01/02\nピザ ¥600\nハンバーガー ¥400',
    '10%税額 ¥60\nau PAY ¥660', '合計点数1000\n現金300円'])
def test_isolated_title_merchant_money_or_tax_is_not_sufficient(text):
    assert classify_receipt_text(text).classification == 'sensitive_unknown'


@pytest.mark.parametrize('suffix,kind', [
    ('患 者 番 号', 'medical'), ('患\n者\n番\n号', 'medical'),
    ('診 療 費\n再 診', 'medical'), ('給 与 明 細', 'payroll'),
    ('医 療', 'sensitive_unknown'), ('患 者 番 号 給 与 明 細', 'sensitive_unknown'),
])
def test_sensitive_evidence_wins_even_when_ocr_splits_characters(suffix, kind):
    assert classify_receipt_text(RETAIL + '\n' + suffix).classification == kind


def extracted(text=RETAIL, complete=True):
    return _ReceiptTextExtraction('extracted', 'image_ocr', text, (), complete)


@pytest.mark.parametrize('decision,expected', [
    (None, 'sensitive_unknown'),
    (ClassificationDecision(classification='medical', reason_code='medical_strong_signal'), 'medical'),
    (ClassificationDecision(classification='payroll', reason_code='payroll_strong_signal'), 'payroll'),
    (ClassificationDecision(classification='normal', reason_code='normal_receipt_evidence'), 'normal'),
])
def test_new_retail_evidence_requires_complete_rereading_and_preserves_sensitive_veto(monkeypatch, decision, expected):
    monkeypatch.setattr(gate, '_extract_receipt_text', lambda *_: extracted())
    monkeypatch.setattr(reread, 'reread_classification', lambda *_: decision)
    result = gate.evaluate_receipt_privacy(b'synthetic', 'image/png')
    assert result.classification == expected
    assert result.gemini_allowed == (expected == 'normal')
    if expected == 'medical':
        assert result.status == 'needs_review' and result.medical_payment_amount is None


def test_incomplete_initial_observation_cannot_use_structural_normal(monkeypatch):
    monkeypatch.setattr(gate, '_extract_receipt_text', lambda *_: extracted(complete=False))
    monkeypatch.setattr(reread, 'reread_classification', lambda *_: pytest.fail('must not reread incomplete source'))
    assert not gate.evaluate_receipt_privacy(b'synthetic','image/png').gemini_allowed


def test_known_sensitive_provenance_still_blocks_normal_rereading(monkeypatch):
    monkeypatch.setattr(gate, '_extract_receipt_text', lambda *_: extracted())
    monkeypatch.setattr(reread, 'reread_classification', lambda *_: classify_receipt_text(RETAIL))
    assert not gate.evaluate_receipt_privacy(b'synthetic','image/png',known_source_classification='medical').gemini_allowed


def test_rereading_keeps_original_sensitive_evidence_and_does_not_retain_text(monkeypatch):
    from PIL import Image
    from io import BytesIO
    out=BytesIO();Image.new('RGB',(50,50),'white').save(out,format='PNG')
    monkeypatch.setattr(reread,'_read_page',lambda _: (RETAIL, RETAIL))
    decision=reread.reread_classification(out.getvalue(),'image/png','患者番号 synthetic-private-marker')
    assert decision.classification == 'medical'
    assert 'synthetic-private-marker' not in str(decision.model_dump())


@pytest.mark.parametrize('observations', [('', RETAIL), (RETAIL, '')])
def test_empty_additional_pass_never_promotes_partial_observation(monkeypatch, observations):
    from PIL import Image
    from io import BytesIO
    out=BytesIO();Image.new('RGB',(50,50),'white').save(out,format='PNG')
    monkeypatch.setattr(reread,'_read_page',lambda _: observations)
    assert reread.reread_classification(out.getvalue(),'image/png',RETAIL) is None


def test_corrupt_document_is_not_promoted_or_exposed():
    assert reread.reread_classification(b'synthetic-private-marker','image/png',RETAIL) is None


def test_multiple_passes_cannot_manufacture_sale_structure(monkeypatch):
    from PIL import Image
    from io import BytesIO
    out=BytesIO();Image.new('RGB',(50,50),'white').save(out,format='PNG')
    weak = '2026/01/02\nピザセット ¥600\n10%内消費税 ¥55\nコード決済 ¥600'
    assert classify_receipt_text(weak).classification == 'sensitive_unknown'
    monkeypatch.setattr(reread,'_read_page',lambda _: (weak,weak))
    assert reread.reread_classification(out.getvalue(),'image/png',weak).classification == 'sensitive_unknown'


def test_low_resolution_reread_scales_whole_image_without_cropping(monkeypatch):
    from PIL import Image
    import pytesseract

    sizes=[]
    def observe(image, **kwargs):
        sizes.append(image.size)
        return RETAIL
    monkeypatch.setattr(pytesseract, 'image_to_string', observe)
    assert reread._read_page(Image.new('RGB', (80, 120), 'white')) == (RETAIL, RETAIL)
    assert sizes == [(160, 240), (160, 240)]


def test_partial_multipage_pdf_cannot_become_normal(monkeypatch):
    from io import BytesIO
    from PIL import Image
    out=BytesIO();im=Image.new('RGB',(50,50),'white');im.save(out,format='PDF',save_all=True,append_images=[im])
    calls=[]
    def page(_):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('synthetic-private-marker')
        return RETAIL,RETAIL
    monkeypatch.setattr(reread,'_read_page',page)
    assert reread.reread_classification(out.getvalue(),'application/pdf',RETAIL) is None
    assert len(calls)==2


@pytest.mark.parametrize('owner_input',[False,True])
def test_resolved_kind_retires_only_same_content_untouched_question_without_accounting(owner_input):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from copy import deepcopy
    from tests.test_receipt_confirmation import Store,DB
    from app.receipt_confirmation import ReceiptConfirmation,TITLE,review_id
    store=Store();db=DB();verify=Mock();review=ReceiptConfirmation(store,db,verify)
    source={'source_id':'synthetic','version':'1','sha256':'a'*64,'mime_type':'image/png'}
    review.observe_intake_hold(source,'synthetic-inbox',SimpleNamespace(classification='sensitive_unknown'))
    review.render();before=deepcopy(db.rows)
    if owner_input:db.rows[TITLE][0][14]='保留してください'
    normal=SimpleNamespace(classification='normal',reason_code='normal_receipt_evidence',extraction_status='extracted')
    assert not review.resolve_intake_kind(dict(source,sha256='b'*64),'synthetic-inbox',normal)
    assert review.resolve_intake_kind(dict(source,version='2'),'synthetic-inbox',normal) is (not owner_input)
    assert review.needs_attention(review_id('intake',source)) is owner_input
    assert {k:v for k,v in db.rows.items() if k!=TITLE}=={k:v for k,v in before.items() if k!=TITLE}


def test_intake_metadata_version_change_reuses_same_review_row():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from tests.test_receipt_confirmation import Store,DB
    from app.receipt_confirmation import ReceiptConfirmation,TITLE,review_id

    store=Store();db=DB();review=ReceiptConfirmation(store,db,Mock())
    source={'source_id':'same-file','version':'2','sha256':'a'*64,'mime_type':'image/png'}
    gate=SimpleNamespace(classification='sensitive_unknown',reason_code='insufficient_evidence',extraction_status='extracted')
    assert review.observe_intake_hold(source,'folder',gate)
    review.render()
    newer=dict(source,version='3')
    assert not review.observe_intake_hold(newer,'folder',gate)
    review.render()
    assert list(review.items)==[review_id('intake',source)]
    assert len(db.rows[TITLE])==1
    assert not review.observe_intake_hold(newer,'folder',gate)
    assert len(db.rows[TITLE])==1


def test_confident_payroll_document_closes_as_other_without_accounting():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from tests.test_receipt_confirmation import Store,DB
    from app.receipt_confirmation import ReceiptConfirmation,TITLE,review_id

    source={'source_id':'payroll-file','version':'1','sha256':'c'*64,'mime_type':'application/pdf'}
    store,db=Store(),DB()
    review=ReceiptConfirmation(store,db,Mock())
    unknown=SimpleNamespace(classification='sensitive_unknown',reason_code='insufficient_evidence',extraction_status='extracted')
    review.observe_intake_hold(source,'inbox',unknown)
    review.render()
    payroll=SimpleNamespace(classification='payroll',reason_code='payroll_strong_signal',extraction_status='extracted')
    assert review.resolve_intake_kind(source,'inbox',payroll)
    review.render()
    assert review.items[review_id('intake',source)]['status']=='closed_machine'
    assert '対象外' in db.rows[TITLE][0][4]
    assert all(not rows for title,rows in db.rows.items() if title != TITLE)
