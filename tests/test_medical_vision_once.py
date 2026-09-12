import hashlib
import pytest
from scripts.run_medical_vision_once import approved_image_binding, validate_answer, PROMPT

def test_uncertain_answer_cannot_keep_amount():
    with pytest.raises(ValueError):
        validate_answer(dict(amount_yen=123,label_quote='',status='ambiguous',reason='unclear'))

def test_literal_amount_only():
    value=dict(amount_yen=123,label_quote='領収額',status='readable',reason='')
    assert validate_answer(value) == value
    with pytest.raises(ValueError): validate_answer({**value,'amount_yen':True})
    with pytest.raises(ValueError): validate_answer({**value,'extra':1})

def test_fixed_prompt_has_no_reference_answer():
    assert 'review-session' not in PROMPT
    assert 'Do not call any tools' in PROMPT

def test_final_image_requires_all_human_review_checks(tmp_path):
    payload=b'approved-image'; image=tmp_path/'unit-01.png'; image.write_bytes(payload)
    record={'unit':1,'output_file':image.name,'output_sha256':hashlib.sha256(payload).hexdigest(),
            'human_send_review':'APPROVED_FOR_AI_EVAL',
            'review_checks':{'A_content':True,'B_privacy':True,'C_qr_barcode':True,'D_boundary':True}}
    manifest={'schema_version':'medical-final-images-local-v1','human_review_complete':True,
              'approved_for_ai_eval':[1]}
    assert approved_image_binding(tmp_path,manifest,record)==(image,record['output_sha256'])
    record['review_checks']['C_qr_barcode']=False
    with pytest.raises(ValueError,match='approval_binding_invalid'):
        approved_image_binding(tmp_path,manifest,record)

def test_comparison_preserves_human_value_when_action_would_be_confirm():
    from scripts.compare_medical_vision_local import comparison
    result = comparison(1200, 1300, None)
    assert result['existing_matches_human'] is False
    assert result['ai_matches_human'] is None
    assert comparison(None,1300,1300)['ai_matches_human'] is True
