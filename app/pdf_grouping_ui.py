"""Management Spreadsheet projection and captured review requests only.

No accounting sheets are accepted by this adapter. Drive saves must complete
before displaying confirmation. Immutable row tokens bind captured user intent.
"""
import json
import re

from .drive_run_state import StateError
from .pdf_grouping_authority import UUID
from .receipt_pdf_units import _digest

TITLE = 'PDFページ確認'
QUEUE = '_PDF確認受付'
SHEET_ID, QUEUE_ID = 261001091, 261001092
HEADERS = ['確認ID', '状態', 'ページ数', '現在の候補・ページ範囲', 'privacy分類', '判定理由',
           '原本リンク', '操作', '対象group', '処理結果', 'source_file_id', 'source_content_hash',
           'proposal_digest', 'grouping_revision', 'row_token', 'confirmation_digest']
QUEUE_HEADERS = ['request_id', 'state', 'snapshot', 'submitted_at', 'result', 'finished_at']
REASONS = {'insufficient_continuation_evidence': '継続根拠が不足・単独候補',
           'matching_transaction_and_page_sequence': '取引識別情報とページ連番が一致',
           'matching_transaction_and_continuation': '取引識別情報と継続表示が一致',
           'human_partition': '人間による分割・結合'}
KINDS = {'normal': '一般候補', 'medical': '医療・接続保留', 'payroll': '給与・停止',
         'sensitive_unknown': '機微不明・停止'}


def row_token(proposal):
    return _digest([proposal['source_file_id'], proposal['source_content_hash'],
                    proposal['proposal_digest'], proposal['grouping_version']])


def project(view):
    proposal = view['proposal']
    if proposal is None:
        raise StateError('grouping_proposal_unavailable')
    token = row_token(proposal)
    groups = proposal['groups']
    describe = lambda g: 'p' + '-p'.join(map(str, g['page_range'])) if len(g['page_numbers']) > 1 else 'p' + str(g['page_numbers'][0])
    state = {'grouping_required': '未確定', 'grouping_confirmed': '確定済み・処理保留',
             'rejected': '拒否', 'held': '保留'}[view['status']]
    authority = view['confirmation']
    return ['pdf-review-' + token, state, proposal['page_count'],
            '\n'.join(f"Group {i}: {describe(g)}" for i, g in enumerate(groups, 1)),
            '\n'.join(f"Group {i}: {KINDS[g['proposed_group_type']]}" for i, g in enumerate(groups, 1)),
            '\n'.join(dict.fromkeys(REASONS[g['reason']] for g in groups)),
            'https://drive.google.com/file/d/' + proposal['source_file_id'] + '/view', '', '',
            view.get('result', '会計・AI・Medical・移動は未実行'), proposal['source_file_id'], proposal['source_content_hash'],
            proposal['proposal_digest'], proposal['grouping_version'], token,
            authority['confirmation_digest'] if authority else '']


def choices(proposal):
    count = len(proposal['groups'])
    return ['確定', '分割', '結合', '拒否', '保留'], [str(i) for i in range(1, count + 1)] + [
        f'{i}+{i+1}' for i in range(1, count)]


def captured_request(snapshot, request_id, proposal):
    if (not isinstance(snapshot, list) or len(snapshot) != len(HEADERS) or
            not re.fullmatch(UUID, request_id)):
        raise StateError('grouping_request_invalid')
    expected = project({'proposal': proposal, 'status': 'grouping_required', 'confirmation': None})
    # Cells are intent only. Every identity field and row token is reconstructed
    # from the Drive proposal, including the displayed page count/partition/type.
    for i in (0, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14):
        if str(snapshot[i]) != str(expected[i]):
            raise StateError('stale_proposal')
    operation = {'確定': 'confirm', '分割': 'edit', '結合': 'edit', '拒否': 'reject', '保留': 'hold'}.get(snapshot[7])
    if operation is None:
        raise StateError('grouping_request_invalid')
    partition = None
    if operation == 'edit':
        partition = [list(g['page_numbers']) for g in proposal['groups']]
        target = str(snapshot[8])
        if snapshot[7] == '分割' and re.fullmatch(r'[1-9]\d*', target):
            index = int(target) - 1
            if index >= len(partition):
                raise StateError('grouping_request_invalid')
            partition[index:index + 1] = [[n] for n in partition[index]]
        elif snapshot[7] == '結合' and re.fullmatch(r'[1-9]\d*\+[1-9]\d*', target):
            a, b = map(int, target.split('+'))
            if b != a + 1 or b > len(partition):
                raise StateError('grouping_request_invalid')
            partition[a - 1:b] = [partition[a - 1] + partition[b - 1]]
        else:
            raise StateError('grouping_request_invalid')
    return {'request_id': request_id, 'source_file_id': proposal['source_file_id'],
            'source_content_hash': proposal['source_content_hash'], 'proposal_digest': proposal['proposal_digest'],
            'grouping_revision': proposal['grouping_version'], 'operation': operation, 'partition': partition}


class GroupingSheet:
    def __init__(self, service, spreadsheet_id):
        self.service, self.sid = service, spreadsheet_id

    def _get(self, title, region):
        if title not in {TITLE, QUEUE}:
            raise StateError('grouping_ui_range_forbidden')
        return self.service.spreadsheets().values().get(spreadsheetId=self.sid,
            range=f"'{title}'!{region}", valueRenderOption='UNFORMATTED_VALUE').execute(num_retries=0).get('values', [])

    def _write(self, data):
        if any(not entry['range'].startswith((f"'{TITLE}'!", f"'{QUEUE}'!")) for entry in data):
            raise StateError('grouping_ui_range_forbidden')
        self.service.spreadsheets().values().batchUpdate(spreadsheetId=self.sid,
            body={'valueInputOption': 'RAW', 'data': data}).execute(num_retries=0)

    def install(self):
        """Create owned review tabs only; no ensure_schema or ledger touching."""
        meta = self.service.spreadsheets().get(spreadsheetId=self.sid,
                                             fields='sheets(properties)').execute(num_retries=0)
        requests = []
        for title, sid, headers, hidden in ((TITLE, SHEET_ID, HEADERS, False), (QUEUE, QUEUE_ID, QUEUE_HEADERS, True)):
            found = next((s['properties'] for s in meta['sheets'] if s['properties']['title'] == title), None)
            if found is None:
                if any(s['properties']['sheetId'] == sid for s in meta['sheets']):
                    raise StateError('grouping_ui_sheet_collision')
                requests.append({'addSheet': {'properties': {'title': title, 'sheetId': sid, 'hidden': hidden,
                    'gridProperties': {'rowCount': 1001, 'columnCount': len(headers), 'frozenRowCount': 1}}}})
                requests.append({'updateCells': {'range': {'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1,
                    'startColumnIndex': 0, 'endColumnIndex': len(headers)},
                    'rows': [{'values': [{'userEnteredValue': {'stringValue': h}} for h in headers]}],
                    'fields': 'userEnteredValue'}})
            elif (found['sheetId'] != sid or self._get(title, 'A1:' + ('P' if title == TITLE else 'F') + '1') != [headers]):
                raise StateError('grouping_ui_schema_mismatch')
        requests += [{'updateDimensionProperties': {'range': {'sheetId': SHEET_ID, 'dimension': 'COLUMNS',
            'startIndex': 0, 'endIndex': 1}, 'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}},
            {'repeatCell': {'range': {'sheetId': SHEET_ID, 'startRowIndex': 0, 'endRowIndex': 1},
                'cell': {'userEnteredFormat': {'backgroundColor': {'red': .94, 'green': .94, 'blue': .94},
                                              'textFormat': {'bold': True}}},
                'fields': 'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold'}},
            {'updateDimensionProperties': {'range': {'sheetId': SHEET_ID, 'dimension': 'COLUMNS',
            'startIndex': 10, 'endIndex': 16}, 'properties': {'hiddenByUser': True}, 'fields': 'hiddenByUser'}},
            {'repeatCell': {'range': {'sheetId': SHEET_ID, 'startRowIndex': 0, 'endRowIndex': 1001},
                'cell': {'userEnteredFormat': {'wrapStrategy': 'WRAP', 'verticalAlignment': 'TOP'}},
                'fields': 'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment'}},
            {'repeatCell': {'range': {'sheetId': SHEET_ID, 'startRowIndex': 1, 'endRowIndex': 1001,
                'startColumnIndex': 7, 'endColumnIndex': 9}, 'cell': {'userEnteredFormat': {'backgroundColor':
                    {'red': 1, 'green': .98, 'blue': .88}}}, 'fields': 'userEnteredFormat.backgroundColor'}}]
        for a, b, width in ((0, 1, 65), (1, 3, 115), (3, 7, 240), (7, 9, 100), (9, 10, 240)):
            requests.append({'updateDimensionProperties': {'range': {'sheetId': SHEET_ID, 'dimension': 'COLUMNS',
                'startIndex': a, 'endIndex': b}, 'properties': {'pixelSize': width}, 'fields': 'pixelSize'}})
        self.service.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': requests}).execute(num_retries=0)

    def publish(self, view):
        row = project(view)
        existing = self._get(TITLE, 'A2:P1001')
        match = [(i + 2, r) for i, r in enumerate(existing) if len(r) > 10 and r[10] == row[10]]
        if len(match) > 1:
            raise StateError('grouping_ui_duplicate_source')
        n = match[0][0] if match else len(existing) + 2
        if n > 1001:
            raise StateError('grouping_ui_capacity')
        if match and len(match[0][1]) > 14 and match[0][1][14] == row[14]:
            # Preserve pending intent until the captured worker finishes.
            row[7:10] = (list(match[0][1]) + [''] * 16)[7:10]
        self._write([{'range': f"'{TITLE}'!A{n}:P{n}", 'values': [row]}])
        actions, targets = choices(view['proposal'])
        requests = [{'setDataValidation': {'range': {'sheetId': SHEET_ID, 'startRowIndex': n - 1,
            'endRowIndex': n, 'startColumnIndex': col, 'endColumnIndex': col + 1}, 'rule': {
                'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': x} for x in values]},
                'strict': True, 'showCustomUi': True}}} for col, values in ((7, actions), (8, targets))]
        self.service.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={'requests': requests}).execute(num_retries=0)

    def request(self, request_id):
        rows = self._get(QUEUE, 'A2:F1001')
        matches = [(i + 2, row) for i, row in enumerate(rows) if row and row[0] == request_id]
        if len(matches) != 1:
            raise StateError('grouping_request_missing_or_duplicate')
        n, row = matches[0]
        if len(row) < 4 or row[1] not in {'dispatching', 'accepted', 'complete', 'error'}:
            raise StateError('grouping_request_invalid')
        return n, json.loads(row[2])

    def finish(self, request_id, snapshot, result, now):
        n, current = self.request(request_id)
        if current != snapshot:
            raise StateError('grouping_request_replaced')
        self._write([{'range': f"'{QUEUE}'!B{n}", 'values': [['complete']]},
                     {'range': f"'{QUEUE}'!E{n}:F{n}", 'values': [[result, now]]}])
        rows = self._get(TITLE, 'A2:P1001')
        for i, row in enumerate(rows, 2):
            if len(row) > 14 and row[14] == snapshot[14]:
                self._write([{'range': f"'{TITLE}'!H{i}:J{i}", 'values': [['', '', result]]}])

    def mark_unverified(self, snapshot, result):
        for i, row in enumerate(self._get(TITLE, 'A2:P1001'), 2):
            if len(row) > 14 and row[14] == snapshot[14]:
                self._write([{'range': f"'{TITLE}'!B{i}", 'values': [['authority未確認・再表示必要']]},
                             {'range': f"'{TITLE}'!J{i}", 'values': [[result]]},
                             {'range': f"'{TITLE}'!P{i}", 'values': [['']]}])


def process_request(grouping, sheet, request_id):
    _, snapshot = sheet.request(request_id)
    if not isinstance(snapshot, list) or len(snapshot) != 16 or not isinstance(snapshot[10], str):
        raise StateError('grouping_request_invalid')
    # This loads current Drive state and fresh original observations first.
    view = grouping.display(snapshot[10])
    if view['proposal'] is None:
        result = view['reason'] or 'grouping_required'
        sheet.mark_unverified(snapshot, result)
    else:
        recovered = grouping.replayed(request_id, snapshot[10], _digest(snapshot))
        if recovered:
            view = recovered
            result = view['result']
        else:
            try:
                request = captured_request(snapshot, request_id, view['proposal'])
            except StateError as error:
                if str(error) != 'stale_proposal':
                    raise
                operation = {'確定': 'confirm', '分割': 'edit', '結合': 'edit', '拒否': 'reject', '保留': 'hold'}.get(snapshot[7])
                if operation is None:
                    raise StateError('grouping_request_invalid')
                view = grouping.note_stale(snapshot[10], request_id, operation, _digest(snapshot))
            else:
                view = grouping.review(request, intent_digest=_digest(snapshot))
            result = view['result']
        # Publish ONLY the read-back-validated Drive view. A projection failure
        # never rolls back authority or causes a second confirmation/revision.
        sheet.publish(view)
    sheet.finish(request_id, snapshot, result, grouping.clock())
    return {'result': result, 'accounting_writes': 0, 'gemini_calls': 0,
            'medical_calls': 0, 'archive_moves': 0}
