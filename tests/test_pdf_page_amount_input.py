"""Whole-yen UI refresh preserves owner values without accounting authority."""
from copy import deepcopy
import pytest
from app.drive_run_state import StateError
from app.pdf_page_review import PageReviewSheet, sheet_amount_value, cards
from app.pdf_page_medical import manual_values, validate_manual_values
from test_pdf_page_review import medical_context, CATEGORIES
from test_pdf_grouping_transport_ui import FakeSheets
from test_receipt_pdf_units import local_ocr


@pytest.mark.parametrize('value',[1234,1234.0,'1,234','1234','1,234.','1234.0'])
def test_integer_owner_amount_survives_old_display_format(value):
    assert sheet_amount_value(value)==1234


@pytest.mark.parametrize('value',[0,-1,1.5,'1234.5','abc',True,float('inf')])
def test_invalid_or_fractional_amount_never_gets_rounded(value):
    with pytest.raises(StateError,match='pdf_amount_input_invalid'):
        sheet_amount_value(value)


def test_blank_is_an_in_progress_input():
    assert sheet_amount_value(None)==sheet_amount_value('')==''


def test_republication_keeps_numeric_amount_and_existing_manual_validator(local_ocr):
    kinds,live,t,owner,store,db,factory=medical_context()
    value,p,answers=kinds.current('drive-source-id')
    card=cards(kinds.grouping.display('drive-source-id'),answers,CATEGORIES)[0]
    entered={'date':'2026/10/01','facility':'本人手入力施設','amount':'1,234.',
             'category':'医療費'}
    old=[[label,entered.get(field,default),'pdf-page-review-v1',card['token'],
          'identity',field] for field,label,default in card['rows']]
    native=FakeSheets();sheet=PageReviewSheet(native,'management-sheet',CATEGORIES)
    sheet._rows=lambda:deepcopy(old)
    before=(deepcopy(store.value),deepcopy(t.payload))
    sheet.publish_cards([card])
    saved=native.writes[-1][0]['values']
    fields={row[5]:row[1] for row in saved if len(row)>5 and row[5]}
    assert fields['amount']==1234 and isinstance(fields['amount'],int)
    parsed=validate_manual_values(manual_values(fields,CATEGORIES),CATEGORIES)
    assert parsed.total==1234 and fields['medical_action']==''
    formats=[x['repeatCell'] for x in native.formats if 'repeatCell' in x]
    assert any(x['cell'].get('userEnteredFormat',{}).get('numberFormat')==
               {'type':'NUMBER','pattern':'#,##0'} for x in formats)
    validators=[x['setDataValidation'] for x in native.formats if 'setDataValidation' in x]
    integer_rule=next(x for x in validators if x.get('rule',{}).get('condition',{}).get('type')=='CUSTOM_FORMULA')
    row=integer_rule['range']['startRowIndex']+1
    assert integer_rule['rule']['strict'] is True
    assert integer_rule['rule']['condition']['values'][0]['userEnteredValue']==\
        f'=OR(ISBLANK(B{row}),AND(ISNUMBER(B{row}),B{row}>0,B{row}=INT(B{row})))'
    assert db.writes==0 and before==(store.value,t.payload)
