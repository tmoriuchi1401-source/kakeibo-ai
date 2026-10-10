"""Calendar UI storage compatibility, not accounting or picker emulation."""
from copy import deepcopy
from datetime import date
import pytest
from app.drive_run_state import StateError
from app.pdf_page_review import (PageReviewSheet, sheet_date_value, SCHEMA,
                                PAYMENT_DATE_HINT, payment_date_ui_requests, TITLE, SHEET_ID)
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


def date_block(token='synthetic-card'):
    return [['対象', 'p1', SCHEMA, token, 'synthetic identity', 'target'],
            ['支払日', 45200, SCHEMA, token, 'synthetic identity', 'date'],
            ['実支払額（円）', 100, SCHEMA, token, 'synthetic identity', 'amount']]


def configured_rows(requests):
    return [r['setDataValidation']['range']['startRowIndex'] for r in requests
            if 'setDataValidation' in r]


def test_payment_date_block_can_move_or_shift_after_inserting_and_deleting_rows():
    block = date_block()
    for leading_rows in (0, 3, 21, 74):
        rows = [['']] * leading_rows + deepcopy(block)
        assert configured_rows(payment_date_ui_requests(rows)) == [leading_rows+1]
        inserted = rows[:leading_rows+1] + [['挿入行', '']] + rows[leading_rows+1:]
        assert configured_rows(payment_date_ui_requests(inserted)) == [leading_rows+2]
        del inserted[leading_rows+1]
        assert configured_rows(payment_date_ui_requests(inserted)) == [leading_rows+1]
        assert rows[-3:] == block


def test_added_pdf_block_receives_same_date_policy_and_supports_range_offset():
    rows = date_block('first') + [['']] + date_block('new-card')
    requests = payment_date_ui_requests(rows, start_row_index=10)
    assert configured_rows(requests) == [11, 15]
    rules = [r['setDataValidation']['rule'] for r in requests if 'setDataValidation' in r]
    assert rules[0] == rules[1] == {
        'condition': {'type': 'DATE_IS_VALID'}, 'strict': True,
        'inputMessage': PAYMENT_DATE_HINT}


@pytest.mark.parametrize('label,schema,token,field', [
    ('発行日', SCHEMA, 'card', 'date'), ('生年月日', SCHEMA, 'card', 'date'),
    ('支払日', 'another-form', 'card', 'date'), ('支払日', SCHEMA, '', 'date'),
    ('支払日', SCHEMA, 'card', 'notice')])
def test_other_date_cells_and_non_input_labels_are_never_reformatted(label,schema,token,field):
    assert payment_date_ui_requests([[label, 45200, schema, token, '', field]]) == []


def test_metadata_only_refresh_finds_current_four_fields_without_changing_any_values():
    # These are expected live-layout results, not production targeting rules.
    rows = [['']] * 80
    for index, value in zip((6, 41, 55, 69), (45200, '2026/10/04', '', '2026-10-04')):
        rows[index] = ['支払日', value, SCHEMA, 'card-'+str(index), 'identity', 'date']
    rows[25] = ['発行日', 45200, SCHEMA, 'other-card', 'identity', 'date']
    before = deepcopy(rows)
    native = FakeSheets()
    native.metadata = [{'properties': {'title': TITLE, 'sheetId': SHEET_ID,
                                      'gridProperties': {'rowCount': len(rows)}}}]
    native.value_data[f"'{TITLE}'!A1:F{len(rows)}"] = deepcopy(rows)
    sheet = PageReviewSheet(native, 'management-sheet', CATEGORIES)
    assert sheet.configure_payment_dates() == 4
    assert configured_rows(native.formats) == [6, 41, 55, 69]
    for request in native.formats:
        if 'repeatCell' in request:
            change = request['repeatCell']
            assert change['fields'] == 'userEnteredFormat.numberFormat,note'
            assert change['cell'] == {
                'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy/mm/dd'}},
                'note': PAYMENT_DATE_HINT}
            assert 'userEnteredValue' not in change['cell']
    assert rows == before and native.writes == []


def test_no_matching_payment_date_inputs_does_not_call_sheet_update():
    native = FakeSheets()
    native.metadata = [{'properties': {'title': TITLE, 'sheetId': SHEET_ID,
                                      'gridProperties': {'rowCount': 10}}}]
    native.value_data[f"'{TITLE}'!A1:F10"] = [['発行日', '2026/10/04']]
    sheet = PageReviewSheet(native, 'management-sheet', CATEGORIES)
    assert sheet.configure_payment_dates() == 0
    assert native.formats == native.writes == []


def test_inserted_rows_beyond_original_grid_limit_are_read_in_bounded_chunks():
    native = FakeSheets()
    native.metadata = [{'properties': {'title': TITLE, 'sheetId': SHEET_ID,
                                      'gridProperties': {'rowCount': 2001}}}]
    rows = [['']] * 2001
    rows[1502] = date_block()[1]
    for start in range(0, len(rows), 1000):
        end = min(start+1000, len(rows))
        native.value_data[f"'{TITLE}'!A{start+1}:F{end}"] = deepcopy(rows[start:end])
    sheet = PageReviewSheet(native, 'management-sheet', CATEGORIES)
    assert sheet.configure_payment_dates() == 1
    assert configured_rows(native.formats) == [1502]
    assert native.writes == []


def test_date_settings_refuse_another_sheet_with_the_same_title():
    native = FakeSheets()
    native.metadata = [{'properties': {'title': TITLE, 'sheetId': 12,
                                      'gridProperties': {'rowCount': 10}}}]
    sheet = PageReviewSheet(native, 'management-sheet', CATEGORIES)
    with pytest.raises(StateError, match='pdf_review_existing_ui_required'):
        sheet.configure_payment_dates()
    assert native.formats == native.writes == []
