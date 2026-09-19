"""Bounded, measured Google reads for the disposable ledger projection.

No accounting values or identifiers are logged. The caller uses the same
SheetsDB and production lock as existing writers. This reader is read-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .monthly_projection import ProjectionError


@dataclass
class ReadMetrics:
    requests: int = 0
    ranges: int = 0
    returned_rows: int = 0
    returned_cells: int = 0


class SheetsLedgerReader:
    def __init__(self, db, *, page_size: int = 2000, batch_ranges: int = 100):
        if not 1 <= page_size <= 2000 or not 1 <= batch_ranges <= 100:
            raise ProjectionError("invalid_read_bounds")
        self.db, self.page_size, self.batch_ranges = db, page_size, batch_ranges
        self.metrics = ReadMetrics()

    def _batch(self, ranges):
        self.metrics.requests += 1
        self.metrics.ranges += len(ranges)
        response = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().values().batchGet(
            spreadsheetId=self.db.sid,
            ranges=[f"'支出明細'!A{first}:M{last}" for first, last in ranges],
            valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="SERIAL_NUMBER"))
        blocks = response.get("valueRanges", [])
        if len(blocks) != len(ranges):
            raise ProjectionError("ledger_range_response_missing")
        result = []
        for (first, last), block in zip(ranges, blocks):
            values = block.get("values", [])
            if len(values) > last - first + 1 or any(len(row) > 13 for row in values):
                raise ProjectionError("ledger_range_response_invalid")
            self.metrics.returned_rows += len(values)
            self.metrics.returned_cells += sum(len(row) for row in values)
            # Sheets omits trailing blank rows; preserve physical addresses.
            result.append(values + [[] for _ in range(last - first + 1 - len(values))])
        return result

    def __call__(self, first: int, last: int):
        return list(self.read_ranges([(first, last)]))[0]

    def read_ranges(self, ranges: Iterable[tuple[int, int]]):
        batch = []
        cells = 0
        for first, last in ranges:
            if first < 2 or last < first or last - first + 1 > self.page_size:
                raise ProjectionError("invalid_ledger_read_range")
            size = (last - first + 1) * 13
            # Bound payload size as well as range count for scattered months.
            if batch and (len(batch) == self.batch_ranges or cells + size > self.page_size * 13):
                yield from self._batch(batch)
                batch, cells = [], 0
            batch.append((first, last))
            cells += size
        if batch:
            yield from self._batch(batch)

    def bootstrap_pages(self):
        # Always fetch fresh grid extent. There is no 5,000-row accounting cap.
        self.metrics.requests += 1
        meta = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid, fields="sheets(properties(title,gridProperties(rowCount)))"))
        sheets = [s["properties"] for s in meta.get("sheets", [])
                  if s["properties"]["title"] == "支出明細"]
        if len(sheets) != 1:
            raise ProjectionError("ledger_sheet_missing")
        end = sheets[0]["gridProperties"]["rowCount"]
        for first in range(2, end + 1, self.page_size):
            yield first, self(first, min(end, first + self.page_size - 1))
