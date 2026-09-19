from copy import deepcopy
import pytest

from app.sheets import SheetsDB
from app.sheets_ui import EXPENSE_CATEGORY_HELPER_ID, EXPENSE_CATEGORY_HELPER_MARKER, VERSION, build_plan
from app.sheets_ui_actions import expense_helper_growth_requests
from test_sheets_ui import Call, metadata


def helper(rows=1001):
    return {'properties': {'sheetId': EXPENSE_CATEGORY_HELPER_ID, 'title': '_支出明細カテゴリ候補',
        'index': 30, 'gridProperties': {'rowCount': rows, 'columnCount': 1000}},
        'developerMetadata': [{'metadataKey': EXPENSE_CATEGORY_HELPER_MARKER, 'metadataValue': VERSION}]}


def test_reinstall_keeps_compact_helper_and_uses_actual_ledger_rows():
    meta = metadata()
    ledger = next(s for s in meta['sheets'] if s['properties']['title'] == '支出明細')
    ledger['properties']['gridProperties']['rowCount'] = 5078
    ledger['usedRowCount'] = 554
    meta['sheets'].append(helper())
    before = deepcopy(meta)
    plan = build_plan(meta)
    assert meta == before
    helper_updates = [r['updateSheetProperties'] for r in plan['requests'] if 'updateSheetProperties' in r
        and r['updateSheetProperties']['properties']['sheetId'] == EXPENSE_CATEGORY_HELPER_ID]
    assert not any('gridProperties' in q['properties'] for q in helper_updates)
    content = next(r['updateCells'] for r in plan['requests'] if r.get('updateCells', {}).get('range', {}).get('sheetId') == EXPENSE_CATEGORY_HELPER_ID)
    assert content['range']['endRowIndex'] == 1001
    ledger['usedRowCount'] = 1500
    grown = build_plan(meta)
    assert any(q.get('updateSheetProperties', {}).get('properties', {}).get('gridProperties', {}).get('rowCount') == 2001
        for q in grown['requests'])


@pytest.mark.parametrize('raw', [False, True])
def test_crossing_capacity_adds_only_missing_helper_rows_and_restores_new_dropdowns(raw):
    class Service:
        def __init__(self):self.requests=[];self.mode=None
        def spreadsheets(self):return self
        def values(self):return self
        def append(self, **kw):
            self.mode=kw['valueInputOption']
            return Call({'updates': {'updatedRange': "'支出明細'!A1002:M1003"}})
        def get(self, **kw):return Call({'sheets': [helper()]})
        def batchUpdate(self, **kw):self.requests.extend(kw['body']['requests']);return Call({})
    service=Service();db=SheetsDB('synthetic',service=service)
    (db.append_raw if raw else db.append)('支出明細', [['new1'], ['new2']])
    assert service.mode == ('RAW' if raw else 'USER_ENTERED')
    assert service.requests[0]['updateSheetProperties']['properties']['gridProperties']['rowCount'] == 2001
    fill=service.requests[1]['updateCells']
    assert fill['range']['startRowIndex']==1001 and fill['range']['endRowIndex']==2001
    assert fill['range']['sheetId']==EXPENSE_CATEGORY_HELPER_ID and fill['range']['startColumnIndex']==1
    assert len(fill['rows'])==1000
    assert 'INDEX(' in fill['rows'][0]['values'][0]['userEnteredValue']['formulaValue']
    assert [q['setDataValidation']['range']['startRowIndex'] for q in service.requests[2:]]==[1001,1002]
    assert db._sheet_metadata_cache is None


def test_capacity_noop_and_growth_never_shrink_existing_helper():
    assert expense_helper_growth_requests(helper(2001),1002)==[]
    requests=expense_helper_growth_requests(helper(1001),2002)
    assert requests[0]['updateSheetProperties']['properties']['gridProperties']['rowCount']==3001
    assert requests[1]['updateCells']['range']['startRowIndex']==1001
