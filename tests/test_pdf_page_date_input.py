"""Calendar UI storage compatibility, not accounting or picker emulation."""
from copy import deepcopy
from datetime import date
import pytest
from app.drive_run_state import StateError
from app.pdf_page_review import PageReviewSheet, sheet_date_value
from app.receipt_reimport import _date
from test_pdf_page_review import medical_context, CATEGORIES
from test_pdf_grouping_transport_ui import FakeSheets
from test_receipt_pdf_units import local_ocr


@pytest.mark.parametrize('day',['2026-10-01','2026-10-31','2026-12-31',
                               '2027-01-01','2024-02-29','2000-02-29'])
@pytest.mark.parametrize('zone',['UTC','Asia/Tokyo','America/Los_Angeles'])
def test_existing_calendar_forms_roundtrip_same_day_without_timezone_conversion(day,zone,monkeypatch):
    monkeypatch.setenv('TZ',zone)
    expected=date.fromisoformat(day)
    serial=(expected-date(1899,12,30)).days
    for value in (day,day.replace('-','/'),serial,float(serial),expected):
        assert sheet_date_value(value)==serial
        assert _date(sheet_date_value(value))==day


@pytest.mark.parametrize('value',['2026/02/30','2026/13/01','2026/04/31',
                                 '1900/02/29','abc','2026-10/04',True,1.5,float('nan')])
def test_invalid_values_are_rejected_before_publishing(value):
    with pytest.raises(StateError,match='pdf_date_input_invalid'):
        sheet_date_value(value)


def test_blank_and_previous_unpadded_ui_date():
    assert sheet_date_value('')==sheet_date_value(None)==''
    assert _date(sheet_date_value('2026/9/28'))=='2026-09-28'


def test_ui_republication_keeps_native_date_storage_and_no_accounting(local_ocr):
    kinds,live,t,owner,store,db,factory=medical_context()
    value,p,answers=kinds.current('drive-source-id')
    from app.pdf_page_review import cards
    card=cards(kinds.grouping.display('drive-source-id'),answers,CATEGORIES)[0]
    native=FakeSheets();sheet=PageReviewSheet(native,'management-sheet',CATEGORIES)
    old=[[label,'2024/02/29' if field=='date' else default,'pdf-page-review-v1',card['token'],
          'identity',field] for field,label,default in card['rows']]
    sheet._rows=lambda:deepcopy(old)
    before=(deepcopy(store.value),deepcopy(t.payload))
    sheet.publish_cards([card])
    saved=native.writes[-1][0]['values']
    day=next(r for r in saved if len(r)>5 and r[5]=='date')[1]
    assert isinstance(day,int) and _date(day)=='2024-02-29'
    formats=[x['repeatCell'] for x in native.formats if 'repeatCell' in x]
    assert any(x['cell'].get('userEnteredFormat',{}).get('numberFormat')==
               {'type':'DATE','pattern':'yyyy/mm/dd'} for x in formats)
    assert db.writes==0 and before==(store.value,t.payload)
