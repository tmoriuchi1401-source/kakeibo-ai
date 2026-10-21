"""Invented cells only; no live book reads or Google credentials in CI."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from app.drive_run_state import StateError
from staging_shadow.sheets_a_validation import (
    BOOK, TABS, TARGETS, cell, footer_formula, plan_repairs, sheets, verify_repairs,
)


def fixture():
    raw = {'spreadsheetId': BOOK, 'sheets': []}
    for title, sid in TABS.items():
        sh = {'properties': {'sheetId': sid, 'title': title},
              'data': [{'rowData': [{'values': [{}, {}]} for _ in range(66)]}]}
        raw['sheets'].append(sh)
        for row, cid, field in TARGETS.get(title, []):
            c = cell(sh, row, 1)
            c.update(userEnteredValue={'stringValue': '入力中'}, note=json.dumps({
                'marker': 'KAKEIBO_SYNTHETIC_UI_V1', 'caseId': cid,
                'role': 'input', 'field': field}), dataValidation={
                    'condition': {'type': 'ONE_OF_LIST', 'values': [
                        {'userEnteredValue': '入力中'}, {'userEnteredValue': '保留'}]},
                    'showCustomUi': True})
        if title == 'PDF':
            for cid, row in [('SYN-P201', 13), ('SYN-P202', 25)]:
                cell(sh, row, 0).update(userEnteredValue={'stringValue': cid+'｜下書きのみ・未送信'},
                    note=json.dumps({'marker': 'KAKEIBO_SYNTHETIC_UI_V1', 'caseId': cid, 'role': 'end'}))
        # Representative originals, notes and held/unfinished inputs must survive.
        cell(sh, 1, 0)['userEnteredValue'] = {'stringValue': '完全合成'}
        cell(sh, 40, 0)['userEnteredValue'] = {'stringValue': 'G103だけ保留'}
        cell(sh, 41, 0)['userEnteredValue'] = {'stringValue': 'LEGACY_UNMAPPED_HOLD'}
        cell(sh, 42, 0)['userEnteredValue'] = {'formulaValue': '=HYPERLINK("https://example.invalid","SYN")'}
    return raw


def apply_in_memory(raw):
    result = deepcopy(raw)
    by_id = {s['properties']['sheetId']: s for s in result['sheets']}
    for r in plan_repairs(raw):
        op = next(iter(r.values())); target = op['range']
        c = cell(by_id[target['sheetId']], target['startRowIndex']+1, target['startColumnIndex'])
        if 'setDataValidation' in r:
            c['dataValidation'] = deepcopy(op['rule'])
        else:
            c.update(deepcopy(op['rows'][0]['values'][0]))
    return result


def test_only_sixteen_rules_and_two_footer_values_change_and_no_authority():
    before = fixture(); untouched = deepcopy(before); requests = plan_repairs(before)
    assert len(requests) == 18
    assert sum('setDataValidation' in r for r in requests) == 16
    assert all(r['updateCells']['fields'] == 'userEnteredValue' for r in requests if 'updateCells' in r)
    after = apply_in_memory(before)
    assert verify_repairs(before, after) == {
        'status': 'verified_synthetic_only', 'authorized': False, 'operation_targets': []}
    assert before == untouched
    assert plan_repairs(after) == []
    for title, sh in sheets(before).items():
        other = sheets(after)[title]
        for row in (1, 40, 41, 42):
            assert cell(sh, row, 0) == cell(other, row, 0)
        for row, _, _ in TARGETS.get(title, []):
            assert cell(sh, row, 1)['userEnteredValue'] == cell(other, row, 1)['userEnteredValue']
            assert cell(sh, row, 1)['note'] == cell(other, row, 1)['note']
            assert cell(other, row, 1)['dataValidation'] == dict(cell(sh, row, 1)['dataValidation'], strict=True)


@pytest.mark.parametrize('attack', ['book', 'sheet', 'duplicate_sheet', 'origin', 'moved',
                                   'case', 'field', 'marker', 'role', 'rule', 'unknown_input', 'formula', 'footer'])
def test_unknown_or_moved_repair_targets_stop_without_rewriting(attack):
    raw = fixture(); c = cell(raw['sheets'][1], 12, 1)
    if attack == 'book': raw['spreadsheetId'] = 'production-or-adopted-one-tab'
    elif attack == 'sheet': raw['sheets'][1]['properties']['sheetId'] += 1
    elif attack == 'duplicate_sheet': raw['sheets'].append(deepcopy(raw['sheets'][0]))
    elif attack == 'origin': raw['sheets'][1]['data'][0]['startRow'] = 1
    elif attack == 'moved': raw['sheets'][1]['data'][0]['rowData'].insert(8, {'values': [{}, {}]})
    elif attack in {'case', 'field', 'marker', 'role'}:
        n = json.loads(c['note']); n[{'case': 'caseId'}.get(attack, attack)] = 'foreign'; c['note'] = json.dumps(n)
    elif attack == 'rule': c['dataValidation']['condition']['type'] = 'CUSTOM_FORMULA'
    elif attack == 'unknown_input': c['userEnteredValue'] = {'stringValue': '勝手に確定'}
    elif attack == 'formula': c['userEnteredValue'] = {'formulaValue': '=1'}
    else: cell(raw['sheets'][3], 13, 0)['userEnteredValue'] = {'stringValue': '未知のメモを消さない'}
    preserved = deepcopy(raw)
    with pytest.raises(StateError): plan_repairs(raw)
    assert raw == preserved


@pytest.mark.parametrize('row,col,key,value', [(40, 0, 'userEnteredValue', {'stringValue': '保留消失'}),
    (12, 1, 'note', '別案件'), (12, 1, 'userEnteredValue', {'stringValue': '競合入力'}),
    (12, 1, 'dataValidation', {}), (42, 0, 'userEnteredValue', {'formulaValue': '=2'})])
def test_readback_requires_exact_inputs_holds_originals_notes_and_rules(row, col, key, value):
    before = fixture(); after = apply_in_memory(before)
    cell(after['sheets'][1], row, col)[key] = value
    with pytest.raises(StateError): verify_repairs(before, after)


def test_partial_repairs_do_not_pass_readback_and_do_not_overwrite_saved_inputs():
    before = fixture(); after = apply_in_memory(before)
    cell(after['sheets'][1], 12, 1)['dataValidation'].pop('strict')
    with pytest.raises(StateError): verify_repairs(before, after)
    assert len(plan_repairs(after)) == 1


@pytest.mark.parametrize('cid,j,s', [('SYN-P201', 9, 10), ('SYN-P202', 21, 22)])
def test_existing_pdf_footer_never_claims_confirmation_or_execution(cid, j, s):
    f = footer_formula(cid, j, s)
    assert f.startswith('="'+cid+'｜下書きのみ・未送信"&CHAR(10)&IF(')
    assert '要再確認：結合・分割が矛盾' in f
    assert '選択保留・未確定' in f and '明示的な矛盾なし・未確定' in f
    assert 'HYPERLINK' not in f and 'IMPORT' not in f


def test_sheets_a_ci_is_exactly_credential_free():
    root = Path(__file__).resolve().parents[1]
    w = yaml.load((root/'.github/workflows/iphone-sheets-a-checks.yml').read_text('utf8'), Loader=yaml.BaseLoader)
    assert w == {
        'name': 'iPhone Sheets A synthetic completion',
        'on': {'pull_request': {'branches': ['codex/iphone-r1-confirmation-ui']}},
        'permissions': {'contents': 'read'}, 'env': {'UNIFIED_RECEIPT_WRITE_ENABLED': 'false'},
        'concurrency': {'group': 'kakeibo-sheets-a-${{ github.ref }}', 'cancel-in-progress': 'true'},
        'jobs': {'synthetic-ui': {'runs-on': 'ubuntu-24.04', 'timeout-minutes': '20', 'steps': [
            {'uses': 'actions/checkout@v4', 'with': {'ref': '${{ github.event.pull_request.head.sha }}',
                'fetch-depth': '0', 'persist-credentials': 'false'}},
            {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.12'}},
            {'name': 'Install test dependencies', 'run': 'pip install -r requirements-test.txt playwright==1.64.0'},
            {'name': 'Install disposable Chromium', 'run': 'python -m playwright install --with-deps chromium'},
            {'name': 'Sheets repair and existing identity retention checks', 'run':
                'python -m pytest -q tests/test_sheets_a_validation.py tests/test_iphone_sheet_reader.py tests/test_iphone_review_drafts.py tests/test_iphone_draft_journal.py tests/test_production_workflows.py --junitxml=.private/sheets-a-contracts.xml'},
            {'name': 'Existing isolated PC and 390px confirmation regression', 'run':
                'python -m pytest -q tests/test_iphone_r1_adapter.py tests/test_iphone_r1_confirmation.py tests/test_iphone_r1_browser.py --junitxml=.private/sheets-a-browser.xml'},
            {'uses': 'actions/upload-artifact@v4', 'if': 'always()', 'with': {
                'name': 'sheets-a-synthetic-evidence', 'path': '.private/r1-browser/\n.private/sheets-a-*.xml\n',
                'include-hidden-files': 'true'}},
            {'name': 'Compile and whitespace', 'run':
                'python -m compileall -q staging_shadow/sheets_a_validation.py tests/test_sheets_a_validation.py\ngit diff --check\n'},
        ]}},
    }
