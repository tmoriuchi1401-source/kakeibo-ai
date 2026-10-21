"""Offline repair plan for the existing historical four-tab SYNTHETIC book.

No Google client, ingestion, confirmation, authority or writer. Coordinates
are repair targets for this fixed test layout, never durable case identity.
The caller must preserve a fresh snapshot, apply an explicitly reviewed plan
and verify exact read-back. This does not repair/adopt arbitrary workbooks.
"""
from copy import deepcopy
import json

from app.drive_run_state import StateError

BOOK = '1fx4MnXbKVjwSpUqAZV0rsdGeh7xfEl00WFiblDFpWbQ'
TABS = {'一覧': 2049352646, '一般': 659810360,
        '医療': 389093105, 'PDF': 256137339}
TARGETS = {
    '一般': [(12, 'SYN-G101', 'category'), (14, 'SYN-G101', 'status'),
             (26, 'SYN-G102', 'category'), (28, 'SYN-G102', 'status')],
    '医療': [(12, 'SYN-M101', 'category'), (13, 'SYN-M101', 'payment'),
             (15, 'SYN-M101', 'status'), (27, 'SYN-M102', 'category'),
             (28, 'SYN-M102', 'payment'), (30, 'SYN-M102', 'status')],
    'PDF': [(9, 'SYN-P201', 'join'), (10, 'SYN-P201', 'split'),
            (12, 'SYN-P201', 'status'), (21, 'SYN-P202', 'join'),
            (22, 'SYN-P202', 'split'), (24, 'SYN-P202', 'status')],
}


def deny():
    raise StateError('sheets_a_synthetic_repair_scope_changed')


def footer_formula(cid, join_row, split_row):
    """Reuse the adopted one-tab conflict wording in the existing footer."""
    if (cid, join_row, split_row) not in {('SYN-P201', 9, 10), ('SYN-P202', 21, 22)}:
        deny()
    j, s = f'B{join_row}', f'B{split_row}'
    return (f'="{cid}｜下書きのみ・未送信"&CHAR(10)&'
            f'IF(OR({j}="",{s}="",{j}="保留",{s}="保留"),"選択保留・未確定",'
            f'IF(OR(AND(OR({j}="p1＋p2を結合",{j}="p1～p3を結合"),{s}="p1の後で分割"),'
            f'AND(OR({j}="p2＋p3を結合",{j}="p1～p3を結合"),{s}="p2の後で分割")), '
            '"要再確認：結合・分割が矛盾","明示的な矛盾なし・未確定"))')


def sheets(snapshot):
    if snapshot.get('spreadsheetId') != BOOK:
        deny()
    result = {}
    for sh in snapshot.get('sheets', []):
        p = sh['properties']; title = p['title']
        if title in result or TABS.get(title) != p['sheetId']:
            deny()
        blocks = sh.get('data', [])
        if len(blocks) != 1 or blocks[0].get('startRow', 0) or blocks[0].get('startColumn', 0):
            deny()
        result[title] = sh
    if set(result) != set(TABS):
        deny()
    return result


def cell(sh, row, col):
    return sh['data'][0]['rowData'][row-1]['values'][col]


def metadata(c, cid, role, field=None):
    n = json.loads(c.get('note', '{}'))
    if (n.get('marker') != 'KAKEIBO_SYNTHETIC_UI_V1' or n.get('caseId') != cid
            or n.get('role') != role or field is not None and n.get('field') != field):
        deny()


def plan_repairs(snapshot):
    """Return only strict dropdown rules and two existing PDF footer values.

    Notes check repair scope only; they are NOT trusted native identity.
    Preserve every current input, even a contradictory PDF draft.
    """
    result = []; by_title = sheets(snapshot)
    for title, targets in TARGETS.items():
        sh = by_title[title]
        for row, cid, field in targets:
            c = cell(sh, row, 1); metadata(c, cid, 'input', field)
            rule = c.get('dataValidation', {})
            if rule.get('condition', {}).get('type') != 'ONE_OF_LIST':
                deny()
            value = c.get('userEnteredValue', {})
            allowed = [v['userEnteredValue'] for v in rule['condition']['values']]
            if value and (set(value) != {'stringValue'} or value['stringValue'] not in allowed):
                deny()
            if rule.get('strict') is True:
                continue
            result.append({'setDataValidation': {
                'range': dict(sheetId=TABS[title], startRowIndex=row-1, endRowIndex=row,
                              startColumnIndex=1, endColumnIndex=2),
                'rule': dict(deepcopy(rule), strict=True)}})
    sh = by_title['PDF']
    for cid, row, jr, sr in [('SYN-P201', 13, 9, 10), ('SYN-P202', 25, 21, 22)]:
        c = cell(sh, row, 0); metadata(c, cid, 'end')
        proposed = {'formulaValue': footer_formula(cid, jr, sr)}
        if c.get('userEnteredValue') == proposed:
            continue
        if c.get('userEnteredValue') != {'stringValue': cid + '｜下書きのみ・未送信'}:
            deny()
        result.append({'updateCells': {
            'range': dict(sheetId=TABS['PDF'], startRowIndex=row-1, endRowIndex=row,
                          startColumnIndex=0, endColumnIndex=1),
            'rows': [{'values': [{'userEnteredValue': proposed}]}], 'fields': 'userEnteredValue'}})
    return result


def verify_repairs(before, after):
    """Exact whole supplied snapshot comparison, including inputs and holds."""
    expected = deepcopy(before); by_title = sheets(expected)
    by_id = {sh['properties']['sheetId']: sh for sh in by_title.values()}
    for request in plan_repairs(before):
        operation = next(iter(request.values())); r = operation['range']
        c = cell(by_id[r['sheetId']], r['startRowIndex']+1, r['startColumnIndex'])
        if 'setDataValidation' in request:
            c['dataValidation'] = deepcopy(operation['rule'])
        else:
            c['userEnteredValue'] = deepcopy(operation['rows'][0]['values'][0]['userEnteredValue'])
    if expected != after:
        deny()
    return {'status': 'verified_synthetic_only', 'authorized': False, 'operation_targets': []}
