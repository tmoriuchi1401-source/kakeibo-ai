"""Atomic bank-only metadata writes with fresh master comparison/read-back.

No import, expense, income, category, or Drive write is exposed here. A network
error from batchUpdate is never retried: the shared request becomes terminal
and a subsequent explicit submission must reconcile the stable IDs first.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib

from .bank_meaning_rules import RULE_HEADERS, RULE_SHEET, parse_rows as parse_rules
from .bank_review_decisions import DECISION_HEADERS, DECISION_SHEET, parse_rows as parse_decisions
from .bank_review_groups import digest

HEADERS = {RULE_SHEET: RULE_HEADERS, DECISION_SHEET: DECISION_HEADERS}
PARSERS = {RULE_SHEET: parse_rules, DECISION_SHEET: parse_decisions}
MAX_METADATA_ROWS = 10000


@dataclass(frozen=True)
class BankMasterSnapshot:
    tables: dict
    sheets: dict
    fingerprint: str

    def records(self, title):
        rows = self.tables.get(title, [])
        return PARSERS[title](rows[1:] if rows else [])


class BankReviewStore:
    def __init__(self, db):
        self.db = db

    def read(self):
        self.db._invalidate_sheet_metadata()
        metadata = self.db._sheet_metadata()
        sheets = {sheet["properties"]["title"]: deepcopy(sheet["properties"])
                  for sheet in metadata["sheets"]}
        tables = {}
        for title, header in HEADERS.items():
            properties = sheets.get(title)
            if properties is None:
                tables[title] = []
                continue
            extent = properties["gridProperties"]["rowCount"]
            if extent > MAX_METADATA_ROWS or properties["gridProperties"]["columnCount"] < len(header):
                raise ValueError("bank_review_master_extent_invalid")
            right = "R" if title == RULE_SHEET else "M"
            rows = self.db.get_raw(f"'{title}'!A1:{right}{extent}")
            if not rows or rows[0] != header:
                raise ValueError("bank_review_master_header_invalid")
            # Empty cells omitted at row ends are canonical blanks, not a
            # permission to omit nonblank required fields from the parser.
            padded = [(list(row) + [""] * len(header))[:len(header)] for row in rows]
            PARSERS[title](padded[1:])
            tables[title] = padded
        relevant = {title: sheets.get(title) for title in HEADERS}
        return BankMasterSnapshot(tables, sheets, digest({"tables": tables, "sheets": relevant}))

    def commit(self, expected, edits):
        """edits: title -> [(one-based destination row, complete literal row)]."""
        if not any(edits.values()):
            return 0
        if set(edits) - HEADERS.keys():
            raise ValueError("bank_review_write_target_invalid")
        if self.read().fingerprint != expected.fingerprint:
            raise ValueError("bank_review_master_changed")
        from .compact_categories import _cells
        requests = []
        used_ids = {properties["sheetId"] for properties in expected.sheets.values()}
        predicted = deepcopy(expected.tables)
        for title in HEADERS:
            updates = edits.get(title, [])
            if not updates:
                continue
            header = HEADERS[title]
            if len({number for number, _ in updates}) != len(updates):
                raise ValueError("bank_review_write_row_collision")
            for number, row in updates:
                if type(number) is not int or not 2 <= number <= MAX_METADATA_ROWS or len(row) != len(header):
                    raise ValueError("bank_review_write_shape_invalid")
                PARSERS[title]([row])
            properties = expected.sheets.get(title)
            final_row = max(number for number, _ in updates)
            if properties is None:
                sheet_id = int.from_bytes(hashlib.sha256(title.encode()).digest()[:4], "big") & 0x7fffffff
                if sheet_id in used_ids:
                    raise ValueError("bank_review_sheet_id_collision")
                used_ids.add(sheet_id)
                requests.append({"addSheet": {"properties": {"sheetId": sheet_id, "title": title,
                    "hidden": title == DECISION_SHEET, "gridProperties": {
                        "rowCount": max(1000, final_row), "columnCount": max(18, len(header)), "frozenRowCount": 1}}}})
                requests.append(_cells(sheet_id, 1, 0, [header], len(header)))
                predicted[title] = [list(header)]
            else:
                sheet_id = properties["sheetId"]
                extent = properties["gridProperties"]["rowCount"]
                if final_row > extent:
                    requests.append({"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": final_row - extent}})
            for number, row in updates:
                while len(predicted[title]) < number:
                    predicted[title].append([""] * len(header))
                predicted[title][number - 1] = list(row)
                requests.append(_cells(sheet_id, number, 0, [row], len(header)))
            PARSERS[title](predicted[title][1:])
        self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,
            body={"requests": requests}).execute(num_retries=0)
        # Never call commit again automatically after a response/read-back error.
        actual = self.read()
        if actual.tables != predicted:
            raise ValueError("bank_review_metadata_readback_failed")
        for title, updates in edits.items():
            for number, row in updates:
                if len(actual.tables[title]) < number or actual.tables[title][number - 1] != row:
                    raise ValueError("bank_review_metadata_readback_failed")
        return sum(len(updates) for updates in edits.values())
