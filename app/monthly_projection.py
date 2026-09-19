"""Rebuildable value projections of the existing expense ledger.

The index is a disposable read accelerator, not accounting authority. Bootstrap
streams bounded ledger pages once. Normal refreshes read only indexed rows for
dirty months; daily history reads the resulting recent purchase projection.
Writers must persist dirty months/index updates before acknowledging success.
No Google writes, AI requests, or accounting mutations occur in this module.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Callable, Iterable, Mapping, Sequence
from uuid import uuid4


class ProjectionError(ValueError):
    """Safe error code; never includes a private ledger value."""


def month_key(value: str) -> str:
    try:
        if len(value) != 7 or date.fromisoformat(value + "-01").strftime("%Y-%m") != value:
            raise ValueError
    except (ValueError, TypeError):
        raise ProjectionError("invalid_month") from None
    return value


def shift_month(month: str, delta: int) -> str:
    month_key(month)
    year, number = map(int, month.split("-"))
    year, number = divmod(year * 12 + number - 1 + delta, 12)
    return f"{year:04d}-{number + 1:02d}"


def _date(value: object) -> str:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = date(1899, 12, 30) + timedelta(days=int(value))
        else:
            parsed = datetime.strptime(str(value).replace("/", "-"), "%Y-%m-%d").date()
        return parsed.isoformat()
    except (ValueError, TypeError, OverflowError):
        raise ProjectionError("invalid_ledger_date") from None


def _yen(value: object) -> int:
    try:
        number = Decimal(str(value).replace(",", "").replace("¥", "").replace("￥", "").replace("円", "").strip())
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (ValueError, InvalidOperation):
        raise ProjectionError("invalid_ledger_amount") from None


@dataclass(frozen=True)
class Category:
    category_id: str
    major: str
    minor: str
    aliases: tuple[tuple[str, str], ...] = ()
    active: bool = True

    @property
    def label(self) -> str:
        return f"{self.major}｜{self.minor}"


class CategoryCatalog:
    """Persist assigned IDs. Renames keep IDs; splits/merges create new IDs.

    Historic aliases resolve to the same identity without editing ledger F:G.
    Retiring a category does not remap historical accounting to a new category.
    """

    def __init__(self, categories: Iterable[Category]):
        self.categories = tuple(categories)
        self.by_name: dict[tuple[str, str], str] = {}
        ids = set()
        for item in self.categories:
            if not item.category_id or item.category_id in ids or not item.major or not item.minor:
                raise ProjectionError("invalid_category_catalog")
            ids.add(item.category_id)
            for name in ((item.major, item.minor), *item.aliases):
                if name in self.by_name and self.by_name[name] != item.category_id:
                    raise ProjectionError("ambiguous_category_alias")
                self.by_name[name] = item.category_id

    @classmethod
    def bootstrap(cls, pairs: Iterable[tuple[str, str]]) -> CategoryCatalog:
        return cls(Category("CAT-" + uuid4().hex, major, minor)
                   for major, minor in dict.fromkeys(pairs))

    def resolve(self, major: str, minor: str) -> str:
        try:
            return self.by_name[(major, minor)]
        except KeyError:
            raise ProjectionError("category_requires_mapping") from None

    def rename(self, category_id: str, major: str, minor: str) -> CategoryCatalog:
        if category_id not in {x.category_id for x in self.categories}:
            raise ProjectionError("category_not_found")
        return CategoryCatalog(replace(x, major=major, minor=minor,
                                       aliases=tuple(dict.fromkeys((*x.aliases, (x.major, x.minor)))))
                               if x.category_id == category_id else x for x in self.categories)


@dataclass(frozen=True)
class LedgerLine:
    expense_id: str
    day: str
    amount: int
    category_id: str
    purchase_id: str
    merchant: str
    item: str
    payment: str
    source: str
    original_ref: str

    @property
    def month(self) -> str:
        return self.day[:7]


def parse_line(raw: Sequence, catalog: CategoryCatalog) -> LedgerLine | None:
    row = list(raw) + [""] * max(0, 13 - len(raw))
    if not row[0] or row[12] not in ("", "active"):
        return None
    # Existing receipt and Amazon writers use one import ID across item lines.
    # Never add receipt/header totals to these authoritative active lines.
    identity = ("import:" + str(row[10]) if row[10] else
                "receipt:" + str(row[9]) if row[9] else "expense:" + str(row[0]))
    return LedgerLine(str(row[0]), _date(row[1]), _yen(row[4]),
                      catalog.resolve(str(row[5]), str(row[6])), identity,
                      str(row[2]), str(row[3]), str(row[7]), str(row[8]),
                      str(row[9] or row[10] or row[0]))


@dataclass(frozen=True)
class IndexEntry:
    row: int
    month: str
    fingerprint: str


def fingerprint(raw: Sequence) -> str:
    row = list(raw) + [""] * max(0, 13 - len(raw))
    return hashlib.sha256(json.dumps(row[:13], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class LedgerIndex:
    def __init__(self):
        self.by_id: dict[str, IndexEntry] = {}
        self.by_month: dict[str, set[str]] = defaultdict(set)
        self.by_row: dict[int, str] = {}
        self.identity_rows: dict[str, int] = {}

    def observe(self, row_number: int, raw: Sequence, catalog: CategoryCatalog) -> set[str]:
        """Update a known row after ledger save; return both old and new months.

        Row insertion/reordering requires a fresh index. The fixed expense ID
        is the identity; a physical address is only a verified read hint.
        """
        if row_number < 2:
            raise ProjectionError("invalid_ledger_row")
        expense_id = str(raw[0]) if raw else ""
        line = parse_line(raw, catalog)
        previous_id = self.by_row.get(row_number)
        if previous_id and previous_id != expense_id:
            raise ProjectionError("ledger_index_rebuild_required")
        old = self.by_id.get(expense_id)
        if expense_id in self.identity_rows and self.identity_rows[expense_id] != row_number:
            raise ProjectionError("duplicate_expense_id")
        if old and line and old.month == line.month and old.fingerprint == fingerprint(raw):
            return set()
        dirty = set()
        if old:
            dirty.add(old.month)
            self.by_month[old.month].discard(expense_id)
            del self.by_id[expense_id]
        if expense_id:
            self.by_row[row_number] = expense_id
            self.identity_rows[expense_id] = row_number
        if line:
            self.by_id[expense_id] = IndexEntry(row_number, line.month, fingerprint(raw))
            self.by_month[line.month].add(expense_id)
            dirty.add(line.month)
        return dirty

    @classmethod
    def bootstrap(cls, pages: Iterable[tuple[int, Sequence[Sequence]]], catalog: CategoryCatalog) -> LedgerIndex:
        index = cls()
        for first_row, rows in pages:
            for offset, raw in enumerate(rows):
                index.observe(first_row + offset, raw, catalog)
        return index

    def ranges(self, month: str, page_size: int = 2000) -> list[tuple[int, int]]:
        month_key(month)
        if page_size < 1:
            raise ProjectionError("invalid_page_size")
        rows = sorted(self.by_id[key].row for key in self.by_month.get(month, ()))
        ranges: list[tuple[int, int]] = []
        for row in rows:
            if ranges and row == ranges[-1][1] + 1 and row - ranges[-1][0] < page_size:
                ranges[-1] = (ranges[-1][0], row)
            else:
                ranges.append((row, row))
        return ranges


@dataclass(frozen=True)
class Purchase:
    purchase_id: str
    day: str
    merchant: str
    amount: int
    categories: tuple[str, ...]
    item_count: int
    search_text: str
    original_ref: str


@dataclass(frozen=True)
class MonthProjection:
    month: str
    amount: int
    purchase_count: int
    refund_count: int
    category_amounts: tuple[tuple[str, int], ...]
    purchases: tuple[Purchase, ...]


def rebuild_month(month: str, index: LedgerIndex, catalog: CategoryCatalog,
                  read_rows: Callable[[int, int], Sequence[Sequence]]) -> MonthProjection:
    """Read only this month's verified rows, without any fixed accounting cap."""
    groups: dict[str, list[LedgerLine]] = defaultdict(list)
    categories: dict[str, int] = defaultdict(int)
    ranges = index.ranges(month, page_size=getattr(read_rows, "page_size", 2000))
    blocks = (read_rows.read_ranges(ranges) if hasattr(read_rows, "read_ranges")
              else (read_rows(first, last) for first, last in ranges))
    for (first, last), rows in zip(ranges, blocks, strict=True):
        if len(rows) != last - first + 1:
            raise ProjectionError("ledger_index_stale")
        for position, raw in enumerate(rows, first):
            key = str(raw[0]) if raw else ""
            entry = index.by_id.get(key)
            if (entry is None or entry.row != position or entry.month != month
                    or entry.fingerprint != fingerprint(raw)):
                raise ProjectionError("ledger_index_stale")
            line = parse_line(raw, catalog)
            if line is None:
                raise ProjectionError("ledger_index_stale")
            groups[line.purchase_id].append(line)
            categories[line.category_id] += line.amount
    purchases = []
    for key, lines in groups.items():
        if len({(x.day, x.merchant, x.payment, x.source) for x in lines}) != 1:
            raise ProjectionError("purchase_identity_conflict")
        purchases.append(Purchase(key, lines[0].day, lines[0].merchant,
                                  sum(x.amount for x in lines),
                                  tuple(sorted({x.category_id for x in lines})), len(lines),
                                  " ".join(dict.fromkeys([lines[0].merchant, *(x.item for x in lines)])).casefold(),
                                  lines[0].original_ref))
    purchases.sort(key=lambda x: (x.day, x.purchase_id), reverse=True)
    return MonthProjection(month, sum(categories.values()), sum(x.amount > 0 for x in purchases),
                           sum(x.amount < 0 for x in purchases), tuple(sorted(categories.items())), tuple(purchases))


def refresh_months(dirty: Iterable[str], index: LedgerIndex, catalog: CategoryCatalog,
                   read_rows: Callable, save_month: Callable[[MonthProjection], None]) -> None:
    """Replace complete month values. A failed save can replay without deltas.

    The durable caller keeps dirty markers until all saves/readbacks succeed.
    Display failures never invoke an accounting writer.
    """
    for month in sorted(set(dirty)):
        save_month(rebuild_month(month, index, catalog, read_rows))


@dataclass(frozen=True)
class HistoryPage:
    rows: tuple[Purchase, ...]
    total: int
    page: int
    pages: int


def history_page(read_month: Callable[[str], MonthProjection | None], *, current_month: str,
                 selected_month: str = "", category_id: str = "", search: str = "",
                 page: int = 1, page_size: int = 100) -> HistoryPage:
    if page < 1 or not 1 <= page_size <= 500:
        raise ProjectionError("invalid_history_page")
    months = [shift_month(current_month, -offset) for offset in range(13)]
    if selected_month:
        if selected_month not in months:
            raise ProjectionError("open_ledger_for_older_history")
        months = [selected_month]
    rows = []
    for month in months:
        projection = read_month(month)
        if projection is None:
            continue  # Coverage status, not a zero financial result.
        rows.extend(x for x in projection.purchases if
                    (not category_id or category_id in x.categories) and
                    (not search or search.casefold() in x.search_text))
    rows.sort(key=lambda x: (x.day, x.purchase_id), reverse=True)
    return HistoryPage(tuple(rows[(page - 1) * page_size:page * page_size]), len(rows), page,
                       max(1, (len(rows) + page_size - 1) // page_size))


@dataclass(frozen=True)
class YearComparison:
    months: tuple[int, ...]
    current_amount: int
    previous_amount: int
    current_average: float | None
    previous_average: float | None
    change_percent: float | None


def compare_years(year: int, current_month: str, amounts: Mapping[str, int],
                  coverage: Mapping[tuple[str, str], str], required_routes: Sequence[str]) -> YearComparison:
    """Compare completed common months only, keeping the current month apart.

    A route includes its account alias. Missing coverage is never completion;
    explicit complete coverage and a materialized zero total are both required.
    """
    month_key(current_month)
    months = []
    if required_routes:
        for number in range(1, 13):
            current, previous = f"{year:04d}-{number:02d}", f"{year-1:04d}-{number:02d}"
            if current >= current_month:
                continue
            if all(month in amounts and all(coverage.get((month, route)) == "complete"
                                           for route in required_routes) for month in (current, previous)):
                months.append(number)
    current_total = sum(amounts[f"{year:04d}-{n:02d}"] for n in months)
    previous_total = sum(amounts[f"{year-1:04d}-{n:02d}"] for n in months)
    return YearComparison(tuple(months), current_total, previous_total,
                          current_total / len(months) if months else None,
                          previous_total / len(months) if months else None,
                          (current_total - previous_total) / abs(previous_total) * 100 if previous_total else None)
