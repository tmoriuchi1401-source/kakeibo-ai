"""Optional product details from saved CSV rows; never a payment identity source."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from dataclasses import replace

from googleapiclient.errors import HttpError

from .amazon_money import MoneyItem
from .monthly_projection import ProjectionError, _yen


class SavedProducts:
    """One-run, bounded ID indexes; read only the selected product payloads."""

    def __init__(self, db, *, auto_apply=False):
        self.db = db
        self.sheets = None
        self.indexes = {}
        self.orders = {}
        self.categories = None
        self.disabled = False
        self.auto_apply = auto_apply
        self.rules = None
        self.product_ids = {}

    def _rows(self, title, width, first=2):
        sheet = self.sheets.get(title)
        if not sheet:
            return
        extent = sheet["gridProperties"]["rowCount"]
        for start in range(first, extent + 1, 1000):
            last = min(start + 999, extent)
            for offset, row in enumerate(self.db.get_raw(f"'{title}'!A{start}:{chr(64 + width)}{last}")):
                yield start + offset, (list(row) + [""] * width)[:width]

    def _index(self, title, column, width):
        if title not in self.indexes:
            found = {}
            for number, row in self._rows(title, width):
                if row[column]:
                    found.setdefault(str(row[column]), []).append((number, str(row[0])))
            self.indexes[title] = found
        return self.indexes[title]

    def _one(self, title, number, width):
        rows = self.db.get_raw(f"'{title}'!A{number}:{chr(64 + width)}{number}")
        return (list(rows[0]) + [""] * width)[:width] if len(rows) == 1 else None

    def _load(self, order):
        if self.sheets is None:
            meta = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
                spreadsheetId=self.db.sid, fields="sheets(properties)"))
            self.sheets = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}
        selected = self._index("Amazon注文", 1, 2).get(order, [])
        if not selected:
            return ()
        if self.categories is None:
            self.categories = {tuple(row) for _, row in self._rows("カテゴリ", 2) if all(row)}
        items = {}
        for number, key in selected:
            row = self._one("Amazon注文", number, 12)
            if (not row or row[0] != key or row[1] != order or not row[2]
                    or key != f"{order}|{row[2]}" or key in items or not str(row[4]).strip()):
                return ()
            # These are total amounts for the item row, already including its
            # quantity. Never multiply again, prorate, or add discounts/points.
            try:
                quantity = Decimal(str(row[5]))
                amount = _yen(row[6])
                if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value() or amount <= 0:
                    return ()
            except (InvalidOperation, ValueError, ProjectionError):
                return ()
            category = tuple(row[8:10])
            if category not in self.categories or category == ("その他", "未分類"):
                positions = self._index("商品マスタ", 0, 1).get(str(row[2]), [])
                master = self._one("商品マスタ", positions[0][0], 4) if len(positions) == 1 else None
                category = tuple(master[2:4]) if master and master[0] == row[2] else ()
            if category not in self.categories:
                category = ("その他", "未分類")
            items[key] = MoneyItem(str(row[4]).strip(), amount, *category)
        self.product_ids[order] = tuple(key.split("|", 1)[1] for key in sorted(items))
        return tuple(items[key] for key in sorted(items))

    def _classify(self, record, items):
        if not self.auto_apply or not any((i.major, i.minor) == ("その他", "未分類") for i in items):
            return items
        from .category_rules import RULE_SHEET, parse_rules, match_transaction
        from .reconciliation import ImportTransaction
        from .utils import now_jst_string
        if self.rules is None:
            self.rules = parse_rules([row for _, row in self._rows(RULE_SHEET, 18)])
        result = []
        for asin, item in zip(self.product_ids[record.order_id], items):
            if (item.major, item.minor) == ("その他", "未分類"):
                # Keep the existing product-rule source and exact predicates;
                # do not reinterpret an issuer's billing text as a product.
                tx = ImportTransaction(0, f"amazon:{record.order_id}:{asin}", "Amazon", record.day,
                    "Amazon.co.jp", item.amount, "unclassified_amazon", "", "", [], now_jst_string())
                matched = match_transaction(self.rules, tx, self.categories,
                    product_name=item.name, product_id="amazon:" + asin, aggregate_only=False)
                if matched.state == "matched" and matched.rule:
                    item = replace(item, major=matched.rule.category[0], minor=matched.rule.category[1])
            result.append(item)
        return tuple(result)

    def __call__(self, record):
        if self.disabled or record.kind != "purchase" or not record.order_id:
            return ()
        try:
            if record.order_id not in self.orders:
                self.orders[record.order_id] = self._load(record.order_id)
            items = self.orders[record.order_id]
            if sum(item.amount for item in items) != record.amount:
                return ()
            return self._classify(record, items)
        except (HttpError, OSError, TimeoutError):
            # Product retrieval is optional. The authoritative amount and its
            # normal identity/duplicate checks still decide whether to post.
            self.disabled = True
            return ()
