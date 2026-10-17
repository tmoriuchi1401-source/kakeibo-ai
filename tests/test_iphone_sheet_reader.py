"""Generated fictitious raw API payloads, never live file IDs or credentials."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.iphone_sheet_reader import (CASE_IDS, INPUT_COLUMNS, HELD, GET_FIELDS,
    GoogleRawTransport, RawReader, issue_contract, validate_snapshot, raw_value,
    dry_run, regeneration_preview)
from app.unified_receipt_review import digest


def text(value):
    return {'userEnteredValue': {'stringValue': value}}


def fixture():
    sid = 1701
    cases = ['SYN-G101', 'SYN-G102', 'SYN-G103', 'SYN-H301', 'SYN-M101', 'SYN-M102', 'SYN-P201', 'SYN-P202']
    rows = [{'values': [text('header'+str(c)) for c in range(17)]}]
    sources = []; opened = []
    for n, cid in enumerate(cases, 1):
        kind = '保留' if cid == 'SYN-H301' else {'G': '一般', 'M': '医療', 'P': 'PDF'}[cid[4]]
        cells = [text('対象外') for _ in range(17)]
        cells[0:2] = [text(cid), text(kind)]
        cells[3] = text('保留' if cid in HELD else '入力中')
        cells[12] = text(cid+'途中メモ')
        if cid != 'SYN-H301':
            source = dict(case_id=cid, source_id='fictitious-'+cid, sha256=digest(['fictitious-bytes', cid]),
                          source_version='fictitious-rev-1', version_scheme='drive_revision_id')
            sources.append(source)
            cells[2] = {'userEnteredValue': {'formulaValue': '=HYPERLINK("https://example.invalid/'+cid+'","原本")'}}
            cells[15:17] = [text(source['sha256']), text(source['source_id'])]
        else:
            cells[2] = {}; cells[15:17] = [{}, {}]
            cells[13] = text('LEGACY_UNMAPPED_HOLD')
        for c in (() if cid in HELD else INPUT_COLUMNS[kind]):
            opened.append(dict(sheetId=sid, startRowIndex=n, endRowIndex=n+1, startColumnIndex=c, endColumnIndex=c+1))
            cells[c].update(note='immutable input metadata '+cid+str(c))
            if c == 3:
                cells[c]['dataValidation'] = dict(strict=True, condition=dict(type='ONE_OF_LIST', values=[{'userEnteredValue': v} for v in ['未確認', '入力中', '保留']]))
            if c == 6:
                cells[c]['userEnteredValue'] = {'numberValue': 100+n}
        rows.append({'values': cells})
    snapshot = dict(spreadsheetId='fictitious-book', sheets=[dict(
        properties=dict(sheetId=sid, title='確認', gridProperties=dict(rowCount=1000, columnCount=26, frozenRowCount=1, frozenColumnCount=1)),
        protectedRanges=[dict(protectedRangeId=5, range=dict(sheetId=sid), editors=dict(users=['owner@example.invalid']), unprotectedRanges=opened)],
        tables=[dict(tableId='7', range=dict(sheetId=sid, startRowIndex=0, endRowIndex=9, startColumnIndex=0, endColumnIndex=17))],
        data=[dict(rowData=rows)])])
    return snapshot, sources


def setup():
    raw, sources = fixture()
    return raw, issue_contract(raw, sources), {s['source_id']: s for s in sources}


def test_exact_eight_cases_identity_bound_and_drafts_never_authorize():
    raw, contract, sources = setup()
    batch = RawReader(contract, lambda: deepcopy(raw), lambda: deepcopy(sources)).read()
    assert {d['case_id'] for d in batch.drafts.values()} == CASE_IDS
    assert sum(len(c['editable']) for c in contract['cases'].values()) == 34
    assert all(not d['authorized'] and not d['operation_targets'] for d in batch.drafts.values())
    assert [d['cells'][12] for d in batch.drafts.values()] == [r['values'][12] for r in raw['sheets'][0]['data'][0]['rowData'][1:]]


@pytest.mark.parametrize('attack', ['sort','insert','delete','duplicate','id','kind','hash','source_id','source_formula','held','legacy','note','validation','protection','open_held','acl','table','header','column','extra_row','grid_shape','sheet','workbook','input_formula','input_bool','dropdown','negative','fraction','huge','nan'])
def test_raw_mutations_stop_without_adoption_or_input_transfer(attack):
    raw, contract, sources = setup(); old = deepcopy(contract)
    sheet = raw['sheets'][0]; rows = sheet['data'][0]['rowData']; cells = rows[1]['values']
    if attack == 'sort': rows[1:] = list(reversed(rows[1:]))
    elif attack == 'insert': rows.insert(2, {'values': []})
    elif attack == 'delete': rows.pop(2)
    elif attack == 'duplicate': rows[2] = deepcopy(rows[1])
    elif attack in {'id','kind','hash','source_id','source_formula'}:
        cells[{'id':0,'kind':1,'hash':15,'source_id':16,'source_formula':2}[attack]] = text('foreign')
    elif attack == 'held': rows[3]['values'][12] = text('overwritten')
    elif attack == 'legacy': rows[4]['values'][16] = text('invented mapping')
    elif attack == 'note': cells[12]['note'] = 'changed'
    elif attack == 'validation': cells[3]['dataValidation'] = {}
    elif attack == 'protection': sheet['protectedRanges'] = []
    elif attack == 'open_held': sheet['protectedRanges'][0]['unprotectedRanges'].append(dict(sheetId=1701,startRowIndex=3,endRowIndex=4,startColumnIndex=12,endColumnIndex=13))
    elif attack == 'acl': sheet['protectedRanges'][0]['editors']['users'].append('foreign@example.invalid')
    elif attack == 'table': sheet['tables'][0]['range']['endRowIndex'] += 1
    elif attack == 'header': rows[0]['values'][0] = text('foreign')
    elif attack == 'column': cells.append(text('out of scope'))
    elif attack == 'extra_row': rows.append({'values': [text('extra')]})
    elif attack == 'grid_shape': sheet['properties']['gridProperties']['rowCount'] += 1
    elif attack == 'sheet': raw['sheets'].append(deepcopy(sheet))
    elif attack == 'workbook': raw['spreadsheetId'] = 'foreign'
    elif attack == 'input_formula': cells[12]['userEnteredValue'] = {'formulaValue': '=1+1'}
    elif attack == 'input_bool': cells[12]['userEnteredValue'] = {'boolValue': True}
    elif attack == 'dropdown': cells[3]['userEnteredValue'] = {'stringValue': '確定'}
    else: cells[6]['userEnteredValue'] = {'numberValue': dict(negative=-1,fraction=1.5,huge=100000000,nan=float('nan'))[attack]}
    with pytest.raises(StateError): validate_snapshot(contract, raw, sources)
    assert contract == old


@pytest.mark.parametrize('field', ['source_id','sha256','source_version','version_scheme'])
def test_independent_original_observer_mismatch_stops(field):
    raw, contract, sources = setup()
    sources[next(iter(sources))][field] = 'changed'
    with pytest.raises(StateError): validate_snapshot(contract, raw, sources)


@pytest.mark.parametrize('race', ['sheet', 'original'])
def test_equal_read_gate_detects_observed_concurrency(race):
    raw, contract, sources = setup(); count = dict(raw=0, sources=0)
    def read_raw():
        count['raw'] += 1; result = deepcopy(raw)
        if race == 'sheet' and count['raw'] == 2:
            result['sheets'][0]['data'][0]['rowData'][1]['values'][12] = text('racing memo')
        return result
    def read_sources():
        count['sources'] += 1; result = deepcopy(sources)
        if race == 'original' and count['sources'] == 2:
            result[next(iter(result))]['source_version'] = 'new'
        return result
    with pytest.raises(StateError, match='concurrent_or_unknown_read'):
        RawReader(contract, read_raw, read_sources).read()


@pytest.mark.parametrize('bad', ['missing','duplicate','extra','foreign_case','open_hold'])
def test_contract_issuance_requires_independent_seven_sources_and_exact_policy(bad):
    raw, sources = fixture()
    if bad == 'missing': sources.pop()
    elif bad == 'duplicate': sources[-1] = deepcopy(sources[0])
    elif bad == 'extra': sources.append(deepcopy(sources[0]))
    elif bad == 'foreign_case': raw['sheets'][0]['data'][0]['rowData'][1]['values'][0] = text('foreign')
    else: raw['sheets'][0]['protectedRanges'][0]['unprotectedRanges'].pop()
    with pytest.raises(StateError): issue_contract(raw, sources)


def test_protection_range_order_and_derived_owner_edit_flag_are_not_identity():
    raw, contract, sources = setup()
    raw['sheets'][0]['protectedRanges'][0]['unprotectedRanges'].reverse()
    raw['sheets'][0]['protectedRanges'][0]['requestingUserCanEdit'] = True
    assert validate_snapshot(contract, raw, sources)


def test_input_survives_reload_and_regeneration_is_only_preview():
    raw, contract, sources = setup()
    raw['sheets'][0]['data'][0]['rowData'][1]['values'][12]['userEnteredValue'] = {'stringValue':'途中入力を消さない'}
    batch = validate_snapshot(contract, raw, sources)
    assert regeneration_preview(batch, asdict(batch))['drafts'] == batch.drafts
    old = validate_snapshot(contract, fixture()[0], sources)
    with pytest.raises(StateError, match='unsent_inputs_preserved'):
        regeneration_preview(batch, asdict(old))


def test_transport_uses_existing_client_get_only_and_bounded_raw_field_mask():
    raw, contract, _ = setup(); calls = []
    class Service:
        def spreadsheets(self): return self
        def get(self, **kwargs):
            calls.append(kwargs); return SimpleNamespace(execute=lambda: deepcopy(raw))
    assert GoogleRawTransport(Service(), contract).read() == raw
    assert calls == [dict(spreadsheetId='fictitious-book',ranges=["'確認'!A1:Z12"],fields=GET_FIELDS)]
    assert 'effectiveValue' not in GET_FIELDS and 'protectedRanges' in GET_FIELDS


def test_unavailable_native_context_holds_all_cases_and_never_executes():
    raw, contract, sources = setup(); batch = validate_snapshot(contract, raw, sources)
    result = dry_run(batch, {})
    assert [v['validation']['status'] for v in result.values()].count('held') == 2
    assert all(v['operation_targets'] == [] and v['authorized'] is False for v in result.values())
