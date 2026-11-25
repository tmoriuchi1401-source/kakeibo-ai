"""Fresh read-only production receipt history; never construct a financial DB."""
import re
from urllib.parse import quote

from .utils import normalize_store
from .sheets import HEADERS
from .receipt_reimport import _date, _money


class ReceiptHistory:
    def __init__(self, session, spreadsheet_id, review_spreadsheet_id):
        if (not re.fullmatch(r'[A-Za-z0-9_-]{10,150}', spreadsheet_id)
                or spreadsheet_id == review_spreadsheet_id):
            raise ValueError('receipt_history_binding_invalid')
        self.session, self.spreadsheet_id = session, spreadsheet_id

    def rows(self, title, headers):
        url = 'https://sheets.googleapis.com/v4/spreadsheets/' + self.spreadsheet_id
        meta = self.session.get(url, params={'fields': 'spreadsheetId,sheets(properties(title,gridProperties(rowCount,columnCount)))'},
            timeout=30, allow_redirects=False)
        if meta.status_code != 200:
            raise RuntimeError('receipt_history_read_unknown')
        body = meta.json()
        candidates = [s['properties'] for s in body.get('sheets', []) if s['properties']['title'] == title]
        if body.get('spreadsheetId') != self.spreadsheet_id or len(candidates) != 1:
            raise ValueError('receipt_history_tab_invalid')
        grid = candidates[0]['gridProperties']
        if not 1 <= grid['rowCount'] <= 10000 or grid['columnCount'] < len(headers):
            raise ValueError('receipt_history_bounds_invalid')
        end = chr(ord('A') + len(headers) - 1)
        ran = quote("'" + title + "'!A1:" + end + str(grid['rowCount']), safe='')
        reply = self.session.get(url + '/values/' + ran,
            params={'valueRenderOption': 'UNFORMATTED_VALUE'}, timeout=30, allow_redirects=False)
        if reply.status_code != 200:
            raise RuntimeError('receipt_history_read_unknown')
        rows = reply.json().get('values', [])
        if not rows or rows[0] != headers:
            raise ValueError('receipt_history_header_invalid')
        return rows[1:]

    def imports(self):
        headers = HEADERS['取込データ']
        rows = self.rows('取込データ', headers)
        receipts = []
        seen = set()
        for row in rows:
            row = row + [''] * (12 - len(row))
            if row[2] != 'receipt':
                continue
            if not row[3] or row[0] != 'receipt:' + row[3] or row[0] in seen:
                raise ValueError('receipt_history_identity_invalid')
            row[4] = _date(row[4])
            amount = _money(row[6])
            row[6] = int(amount) if amount is not None else None
            seen.add(row[0]); receipts.append(row)
        return receipts

    def already_imported(self, source_id):
        return any(row[3] == source_id for row in self.imports())

    def duplicate(self, entry, snapshot):
        rows = self.imports()
        if any(row[3] == entry['source']['source_id'] for row in rows):
            return True
        day, merchant = entry['import_row'][4:6]
        total = snapshot['inputs']['receipt_total']
        # Different Drive IDs can still be copies. Ambiguity is a hold, never
        # another eligible transaction or an inferred link to existing finance.
        for row in rows:
            comparisons = [(row[4], day), (row[6], total),
                (normalize_store(row[5]) if row[5] else None, normalize_store(merchant))]
            known = [(left, right) for left, right in comparisons if left is not None]
            if len(known) >= 2 and all(left == right for left, right in known):
                return True
        return False

    def categories(self):
        rows = self.rows('カテゴリ', HEADERS['カテゴリ'])
        return [list(pair) for pair in dict.fromkeys(tuple(row) for row in rows
            if len(row) == 2 and all(type(v) is str and v for v in row))]
