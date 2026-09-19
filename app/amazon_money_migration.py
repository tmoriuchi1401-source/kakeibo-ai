"""One-time, private binding of old canonical expenses to the money writer.

Call under the existing production writer lock, after a native backup and
writer freeze. This module never writes a Sheet or infers a payment identity
from a date/amount match. Supplied records come from the confirmed adapters.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import date
import hashlib
import re

from .amazon_money import MoneyError, digest, empty_book, source_alias, validate_book
from .aupay_card_contract import is_amazon_merchant
from .monthly_projection import _date, _yen
from .projection_store import replace_document


class MigrationReader:
    """Fresh extents, bounded reads; no Medical, Payroll, or event-body access."""
    def __init__(self, db):
        self.db = db

    def __call__(self):
        meta = self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid, fields="sheets(properties(title,gridProperties(rowCount)))"))
        result = {"spreadsheet_id": self.db.sid}
        for title, width, key in [("支出明細", 13, "expenses"), ("取込データ", 12, "imports")]:
            sheets = [s["properties"] for s in meta["sheets"] if s["properties"]["title"] == title]
            if len(sheets) != 1:
                raise MoneyError("money_migration_sheet_missing")
            end = sheets[0]["gridProperties"]["rowCount"]
            rows = []
            for first in range(2, end + 1, 2000):
                last = min(first + 1999, end)
                values = self.db.get_raw(f"'{title}'!A{first}:{chr(64 + width)}{last}")
                if len(values) > last - first + 1:
                    raise MoneyError("money_migration_read_invalid")
                rows.extend(values)
            result[key] = rows
        return result


def _rows(values, width):
    result = {}
    for value in values:
        if not any(v not in ("", None) for v in value):
            continue
        if len(value) > width or not isinstance(value[0], str) or not value[0] or value[0] in result:
            raise MoneyError("money_migration_identity_invalid")
        result[value[0]] = list(value) + [""] * (width - len(value))
    return result


def _snapshot(raw):
    if not isinstance(raw.get("spreadsheet_id"), str) or not raw["spreadsheet_id"]:
        raise MoneyError("money_migration_source_binding_missing")
    return {"source_binding": digest(raw["spreadsheet_id"]),
        "expenses": _rows(raw["expenses"], 13), "imports": _rows(raw["imports"], 12)}


def _amazon(row):
    return (row[8] in {"Amazon", "Amazon金銭"} or str(row[10]).startswith("amazon:")
            or str(row[2]).strip().casefold() == "amazon" or is_amazon_merchant(str(row[2])))


def _group(row):
    return row[10][7:] if str(row[10]).startswith("amazon:") and row[10][7:] else "expense:" + row[0]


def _anchor(imported, rows, record):
    """Use an existing fixed link only, never the old matching algorithm."""
    if (_yen(imported[6]) != record.amount
            or imported[2] != ("au PAYカード" if record.source == "au_pay_card" else "Amazon")
            or (record.kind == "purchase" and _date(imported[4]) != record.day)):
        raise MoneyError("money_migration_source_changed")
    if all(row[10] == record.source_id for row in rows):
        return
    if imported[8] not in {"matched_amazon", "matched_amazon_installment", "canonical_amazon"}:
        raise MoneyError("money_migration_binding_missing")
    ids = {row[0] for row in rows}
    if imported[9] and imported[9] in ids:
        return
    keys = re.findall(r"(?:^|;\s*)Amazonキー=([^;]+)", str(imported[11]))
    if len(keys) == 1 and "A-" + hashlib.sha256(keys[0].strip().encode()).hexdigest()[:24] in ids:
        return
    raise MoneyError("money_migration_binding_missing")


def build_manifest(raw, *, cutover_day, bindings=()):
    """Bindings: record, complete expense_ids, and refund related_id if needed.

    Unbound purchases remain open. Every active negative Amazon row must have
    a confirmed source binding and explicit original purchase before activation.
    The private result contains IDs/amounts; callers must not print or commit it.
    """
    if date.fromisoformat(cutover_day).isoformat() != cutover_day:
        raise MoneyError("money_migration_cutover_invalid")
    snapshot = _snapshot(raw)
    expenses, imports = snapshot["expenses"], snapshot["imports"]
    selected = {k: r for k, r in expenses.items() if _amazon(r)}
    if (any(str(r[10]).startswith("AM-") for r in expenses.values())
            or any(str(r[0]).startswith("AM-") or r[8] == "canonical_amazon_money" for r in imports.values())):
        raise MoneyError("money_migration_already_posted")
    active = {k: r for k, r in selected.items() if r[12] in {"", "active"}}
    legacy, negatives, totals = {}, set(), {}
    for key, row in expenses.items():
        if row[12] not in {"", "active"}:
            continue
        amount, day = _yen(row[4]), _date(row[1])
        kind = "refund" if amount < 0 else "purchase" if amount > 0 else "zero"
        total = totals.setdefault(day[:7] + ":" + kind, {"count": 0, "amount": 0})
        total["count"] += 1
        total["amount"] += amount
        if key not in active:
            continue
        if not amount:
            continue
        if amount < 0:
            negatives.add(key)
            continue
        group = legacy.setdefault(_group(row), {"state": "open", "amount": 0,
            "expense_ids": [], "expense_amounts": {}, "expense_fingerprints": {}})
        group["amount"] += amount
        group["expense_ids"].append(key)
        group["expense_amounts"][key] = row[4]
        group["expense_fingerprints"][key] = digest(row)
    for group in legacy.values():
        group["expense_ids"].sort()
    book = empty_book(cutover_day=cutover_day, legacy=legacy)
    claims, refunded, covered_negative = {}, {}, set()
    for binding in bindings:
        record = binding["record"]
        record.validate()
        if (not record.confirmed or record.kind not in {"purchase", "refund"}
                or record.payment in {"mixed", "unknown"}
                or (record.source == "amazon" and record.payment == "card")
                or (record.source == "au_pay_card" and record.payment != "card")):
            raise MoneyError("money_migration_authority_invalid")
        ids = sorted(binding["expense_ids"])
        if not ids or len(set(ids)) != len(ids) or any(k not in active for k in ids):
            raise MoneyError("money_migration_expense_missing")
        rows = [active[k] for k in ids]
        imported = imports.get(record.source_id)
        if imported is None:
            raise MoneyError("money_migration_source_missing")
        _anchor(imported, rows, record)
        alias_key = source_alias(record)
        if alias_key in book["aliases"] or record.money_id in book["records"]:
            raise MoneyError("money_migration_duplicate_binding")
        related = binding.get("related_id", "")
        if record.kind == "purchase":
            groups = [k for k, g in legacy.items() if g["expense_ids"] == ids]
            if len(groups) != 1 or related:
                raise MoneyError("money_migration_group_incomplete")
            group_key = groups[0]
            if record.order_id and record.order_id != group_key:
                raise MoneyError("money_migration_order_conflict")
            claims[group_key] = claims.get(group_key, 0) + record.amount
            if claims[group_key] > legacy[group_key]["amount"]:
                raise MoneyError("money_migration_claim_exceeds_purchase")
        else:
            origin = legacy.get(related[7:]) if related.startswith("legacy:") else None
            if (not origin or not set(ids) <= negatives or covered_negative & set(ids)
                    or sum(_yen(r[4]) for r in rows) != record.amount
                    or (record.order_id and record.order_id != related[7:])):
                raise MoneyError("money_migration_refund_binding_invalid")
            refunded[related] = refunded.get(related, 0) - record.amount
            if refunded[related] > origin["amount"]:
                raise MoneyError("money_migration_refund_exceeds_purchase")
            covered_negative.update(ids)
        book["aliases"][alias_key] = {"fingerprint": record.fingerprint, "expense_ids": ids,
            "expense_amounts": {k: active[k][4] for k in ids}, "amount": record.amount,
            "expense_fingerprints": {k: digest(active[k]) for k in ids}, "related_id": related}
        entry = {k: v for k, v in asdict(record).items() if k != "items"}
        entry.update(fingerprint=record.fingerprint, state="linked", reason="migration_existing_identity",
            expense_ids=ids, related_id=related)
        book["records"][record.money_id] = entry
    if covered_negative != negatives:
        raise MoneyError("money_migration_refund_identity_required")
    for key, amount in claims.items():
        if amount == legacy[key]["amount"]:
            legacy[key]["state"] = "settled"
    validate_book(book)
    return {"schema": 1, "source_binding": snapshot["source_binding"],
        "snapshot": digest(snapshot), "book": book, "ledger_totals": totals,
        "legacy_expenses": {k: {"fingerprint": digest(r), "amount": _yen(r[4]),
            "day": _date(r[1]), "status": r[12], "import_id": r[10]} for k, r in selected.items()},
        "legacy_imports": {k: {"fingerprint": digest(r), "amount": _yen(r[6]),
            "day": _date(r[4]), "status": r[8], "target_id": r[9]} for k, r in imports.items()
            if r[2] == "Amazon" or is_amazon_merchant(str(r[5])) or r[9] in selected},
        "counts": {"expense_rows": len(expenses), "import_rows": len(imports),
            "legacy_rows": len(selected), "open_groups": sum(g["state"] == "open" for g in legacy.values()),
            "bound_records": len(book["records"])}}


def initialize(store, reader, manifest, *, cutover_day, bindings=()):
    """Re-read frozen canonical rows; persist intent before create/readback.

    The caller holds the existing writer lock. No write retries; a lost response
    can be resumed with exactly the same manifest. A live book is never reset.
    """
    fresh = build_manifest(reader(), cutover_day=cutover_day, bindings=bindings)
    if fresh != manifest:
        raise MoneyError("money_migration_snapshot_changed")
    before = store.read("money-migration")
    current = store.read("money")
    if before is not None and before != manifest:
        raise MoneyError("money_migration_manifest_conflict")
    if current is not None and (before is None or current != manifest["book"]):
        raise MoneyError("money_migration_book_exists")
    replace_document(store, "money-migration", before, deepcopy(manifest))
    # Catch input edits or a concurrent writer between the snapshot and intent.
    if digest(_snapshot(reader())) != manifest["snapshot"]:
        raise MoneyError("money_migration_snapshot_changed")
    replace_document(store, "money", current, deepcopy(manifest["book"]))
    return {"money_initialized": int(current is None), "expense_rows_written": 0, "import_rows_written": 0}
