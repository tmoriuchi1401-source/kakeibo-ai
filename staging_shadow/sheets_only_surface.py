"""Offline projection of the adopted one-tab fixture. No Google transport.

Insert two columns after case ID: native dropdown and comparison/result. The
17 original columns, links, notes, values and validations round-trip unchanged.
Projection is presentation only, never approval evidence. No Sheet CAS assumed.
"""
from copy import deepcopy

from app.drive_run_state import StateError
from app.iphone_sheet_reader import raw_value
from app.unified_receipt_review import digest

IDLE = '入力中'
CORE_HEADERS = ['案件ID','種類','原本','下書き状態','日付','店舗・施設','金額','カテゴリ・明細',
                '支払方法','PDFページ','PDF結合判断','PDF分割判断','途中メモ','保留理由','処理結果','原本hash','原本ID']


def fail(code):
    raise StateError('sheets_only_'+code)


def cell(value):
    return {'userEnteredValue': {'stringValue': value}}


def rule(options):
    return dict(strict=True, showCustomUi=True, condition=dict(type='ONE_OF_LIST',
                values=[dict(userEnteredValue=v) for v in options]))


def column(index):
    return chr(65+index)  # bounded 19-column fixture


def physical(index):
    return 0 if index == 0 else index+2


def literal(value):
    # All data is quoted, including formula-looking strings. Not an executor.
    return '&CHAR(10)&'.join('"'+part.replace('"', '""')+'"' for part in str(value).split('\n'))


class SyntheticSheetSurface:
    synthetic_memory_only = True

    def __init__(self, trusted_core, contract, *, registration=False):
        self.registration = registration is True
        self.idle_text = '未登録・入力後に登録を依頼してください' if self.registration else '未確定・比較を依頼してください'
        self.changed_text = '入力変更・新しい登録を依頼してください' if self.registration else '入力変更・比較を依頼してください'
        self.trusted_core = deepcopy(trusted_core)
        self.contract = deepcopy(contract)
        self.sources = {c['identity']['source_id']:deepcopy(c['identity'])
                        for c in contract['cases'].values() if c['identity']}
        self.raw = deepcopy(trusted_core)
        sheet = self.raw['sheets'][0]
        sheet['tables'][0]['range']['endColumnIndex'] += 2
        opened = sheet['protectedRanges'][0]['unprotectedRanges']
        for r in opened:
            r['startColumnIndex'] = physical(r['startColumnIndex'])
            r['endColumnIndex'] += 2
        for n, row in enumerate(sheet['data'][0]['rowData']):
            values = row['values']
            row['values'] = values[:1]+[cell('操作依頼' if n == 0 else IDLE),
                cell(('登録・結果' if self.registration else '比較・結果') if n == 0 else self.idle_text)]+values[1:]
            if n == 0:
                row['values'] = [cell(v) for v in CORE_HEADERS[:1]+['操作依頼','登録・結果' if self.registration else '比較・結果']+CORE_HEADERS[1:]]
            if n:
                row['values'][2] = {'userEnteredValue': {'formulaValue':
                    '=IF(B'+str(n+1)+'='+literal(IDLE)+','+literal(self.idle_text)+','+
                    literal('受付待ち・自動処理を待ってください')+')'}}
                row['values'][1]['dataValidation'] = rule([IDLE, '登録 1' if self.registration else '比較 1'])
                opened.append(dict(sheetId=contract['sheet_id'], startRowIndex=n, endRowIndex=n+1,
                                   startColumnIndex=1, endColumnIndex=2))
        self.layout = deepcopy(self.raw)
        self.expected = {}
        self.writes = 0
        self.fault = None
        sid = contract['sheet_id']
        self.format_plan = [dict(updateDimensionProperties=dict(range=dict(sheetId=sid, dimension='COLUMNS',
            startIndex=n, endIndex=n+1), properties=dict(pixelSize=width), fields='pixelSize'))
            for n, width in enumerate((88, 132, 146))]
        self.format_plan.append(dict(repeatCell=dict(range=dict(sheetId=sid, startColumnIndex=0, endColumnIndex=19),
            cell=dict(userEnteredFormat=dict(wrapStrategy='WRAP', verticalAlignment='TOP')),
            fields='userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment')))

    def read(self):
        return deepcopy(self.raw)

    def observe_sources(self):
        # Offline independent source inventory. Actual selected image revision
        # bytes/hash/native identity are ALSO re-observed by #155 _capture.
        return deepcopy(self.sources)

    def core(self, value=None):
        raw = self.read() if value is None else deepcopy(value)
        try:
            if raw['spreadsheetId'] != self.layout['spreadsheetId'] or len(raw['sheets']) != 1:
                fail('workbook_or_tabs_changed')
            s, expected = raw['sheets'][0], self.layout['sheets'][0]
            if any(s[k] != expected[k] for k in ('properties', 'tables', 'protectedRanges')):
                fail('layout_changed')
            if len(s['data']) != 1 or any(s['data'][0].get(k, 0) != 0 for k in ('startRow', 'startColumn')):
                fail('coverage_changed')
            rows = s['data'][0]['rowData']
            if len(rows) != 9 or rows[0] != expected['data'][0]['rowData'][0]:
                fail('header_or_case_count_changed')
            result = deepcopy(self.trusted_core)
            for n, row in enumerate(rows[1:], 1):
                cells = row['values']
                if len(cells) != 19:
                    fail('column_count_changed')
                if set(cells[1])-{'userEnteredValue', 'dataValidation'} or set(cells[2])-{'userEnteredValue'}:
                    fail('control_shape_changed')
                if 'formulaValue' in cells[1].get('userEnteredValue', {}):
                    fail('command_formula_forbidden')
                raw_value(cells[1])  # untrusted hint; validation never authorizes
                result['sheets'][0]['data'][0]['rowData'][n]['values'] = cells[:1]+cells[3:]
            return result
        except (KeyError, TypeError, IndexError):
            fail('layout_changed')

    def row(self, cid):
        rows = self.raw['sheets'][0]['data'][0]['rowData'][1:]
        matches = [r['values'] for r in rows if raw_value(r['values'][0]) == cid]
        if len(matches) != 1:
            fail('case_identity_changed')
        return matches[0]

    def command(self, cid, raw=None):
        rows = (self.read() if raw is None else raw)['sheets'][0]['data'][0]['rowData'][1:]
        matches = [r['values'] for r in rows if raw_value(r['values'][0]) == cid]
        if len(matches) != 1:
            fail('case_identity_changed')
        return raw_value(matches[0][1])

    def formula(self, cid, command, text, batch):
        n = self.contract['cases'][cid]['row_index']+1
        if text.startswith('UNKNOWN'):
            return '='+literal(text)
        draft = next(d for d in batch.drafts.values() if d['case_id'] == cid)
        matches = []
        for index in self.contract['cases'][cid]['editable']:
            value = raw_value(draft['cells'][index])
            # Native typed numeric equality, exact strings. No digest authority
            # in formulas: the backend independently validates the whole batch.
            ref = column(physical(index))+str(n)
            matches.append(ref+'='+str(value) if type(value) in (int, float)
                           else 'EXACT('+ref+','+literal(value)+')')
        same = 'AND('+','.join(matches or ['TRUE'])+')' if text.startswith(('比較済み', '登録検証済み')) else 'TRUE'
        b = 'B'+str(n)
        return ('=IF('+b+'='+literal(command)+',IF('+same+','+literal(text)+','+
                literal(self.changed_text)+'),IF('+b+'='+literal(IDLE)+','+
                literal('入力中・未確定')+','+literal('受付待ち・自動処理を待ってください')+'))')

    def publish(self, cid, command, text, batch, options):
        # Only C output and B validation. Never rewrite B selection or inputs.
        # A real Google projection cannot offer atomic CAS; no such contract is
        # introduced here. Fresh backend confirmation rejects stale projection.
        if self.fault:
            self.fault('before_publish')
        if text.startswith(('比較済み', '登録検証済み')) and digest(self.core()) != batch.snapshot_digest:
            fail('projection_conflict')
        cells = self.row(cid)
        cells[2] = {'userEnteredValue': {'formulaValue': self.formula(cid, command, text, batch)}}
        cells[1]['dataValidation'] = rule(list(dict.fromkeys([IDLE, command]+options)))
        self.writes += 1
        self.expected[cid] = dict(command=command, text=text, batch=deepcopy(batch),
                                  formula=deepcopy(cells[2]), options=deepcopy(cells[1]['dataValidation']))
        if self.fault:
            self.fault('after_publish')
        self.check_published(cid)
        if text.startswith(('比較済み', '登録検証済み')) and digest(self.core()) != batch.snapshot_digest:
            fail('projection_conflict')

    def check_published(self, cid):
        e = self.expected[cid]; c = self.row(cid)
        if c[2] != e['formula'] or c[1].get('dataValidation') != e['options']:
            fail('projection_UNKNOWN')

    def view_text(self, cid):
        """Fixture display oracle; Google formula recalculation remains untested."""
        e = self.expected.get(cid)
        if not e:
            return self.idle_text if self.command(cid) == IDLE else '受付待ち・自動処理を待ってください'
        if e['text'].startswith('UNKNOWN'):
            return e['text']
        if self.command(cid) != e['command']:
            return '入力中・未確定' if self.command(cid) == IDLE else '受付待ち・自動処理を待ってください'
        current = self.core()
        cells = current['sheets'][0]['data'][0]['rowData'][self.contract['cases'][cid]['row_index']]['values']
        old = next(d for d in e['batch'].drafts.values() if d['case_id'] == cid)['cells']
        return e['text'] if all(raw_value(cells[i]) == raw_value(old[i]) for i in self.contract['cases'][cid]['editable']) else self.changed_text
