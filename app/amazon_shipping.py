from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .amazon_pipeline import date_ymd
from .sheets import SheetsDB


@dataclass(frozen=True)
class ShippingBackfillPlan:
    summary: dict[str, int]
    updates: tuple[tuple[int, list], ...]


def _text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _load_shipping_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    required = {"Order ID", "ASIN", "Ship Date", "Original Quantity"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Amazon CSVに発送情報の必要列がありません: " + ", ".join(sorted(missing)))
    quantity = pd.to_numeric(df["Original Quantity"], errors="coerce").fillna(0)
    return df[quantity > 0].copy()


def _shipping_records(path: str):
    df = _load_shipping_csv(path)
    tracking_column = "Carrier Name & Tracking Number"
    per_order: dict[str, set[tuple[str, str]]] = {}
    per_key: dict[str, list[str]] = {}
    for _, raw in df.iterrows():
        order_id = _text(raw["Order ID"])
        asin = _text(raw["ASIN"])
        if not order_id or not asin:
            continue
        ship_date = date_ymd(raw["Ship Date"])
        tracking = _text(raw.get(tracking_column, ""))
        if ship_date or tracking:
            per_order.setdefault(order_id, set()).add((ship_date, tracking))
        per_key.setdefault(f"{order_id}|{asin}", []).append(ship_date)
    shipment_counts = {order_id: len(groups) for order_id, groups in per_order.items()}
    return df, per_key, shipment_counts


def plan_shipping_backfill(path: str, amazon_rows: list[list]) -> ShippingBackfillPlan:
    df, csv_by_key, shipment_counts = _shipping_records(path)
    sheet_by_key: dict[str, list[tuple[int, list]]] = {}
    for row_num, raw in enumerate(amazon_rows, start=2):
        row = list(raw) + [""] * max(0, 15 - len(raw))
        key = _text(row[0]) or (f"{_text(row[1])}|{_text(row[2])}" if row[1] and row[2] else "")
        if key:
            sheet_by_key.setdefault(key, []).append((row_num, row[:15]))

    updates = []
    matched = ambiguous = unmatched = 0
    update_dates = update_counts = 0
    for key, dates in csv_by_key.items():
        targets = sheet_by_key.get(key, [])
        unique_dates = {value for value in dates if value}
        if not targets:
            unmatched += 1
            continue
        if len(targets) != 1 or len(dates) != 1 or len(unique_dates) > 1:
            ambiguous += 1
            continue
        matched += 1
        row_num, row = targets[0]
        order_id = _text(row[1])
        ship_date = next(iter(unique_dates), "")
        shipment_count = shipment_counts.get(order_id, 0)
        changed = False
        if _text(row[13]) != ship_date:
            row[13] = ship_date
            update_dates += 1
            changed = True
        old_count = 0
        try:
            old_count = int(float(str(row[14]).strip())) if str(row[14]).strip() else 0
        except ValueError:
            old_count = 0
        if old_count != shipment_count:
            row[14] = shipment_count
            update_counts += 1
            changed = True
        if changed:
            updates.append((row_num, row))

    summary = {
        "csv_rows": len(df),
        "matched_amazon_rows": matched,
        "would_update_ship_date": update_dates,
        "would_update_shipment_count": update_counts,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
    }
    return ShippingBackfillPlan(summary, tuple(updates))


class AmazonShippingBackfillPipeline:
    def __init__(self, db: SheetsDB):
        self.db = db

    def preview(self, path: str) -> dict:
        return plan_shipping_backfill(path, self.db.get("Amazon注文!A2:O")).summary

    def apply(self, path: str) -> dict:
        plan = plan_shipping_backfill(path, self.db.get("Amazon注文!A2:O"))
        self.db.update_shipping_fields(list(plan.updates))
        return {**plan.summary, "updated_rows": len(plan.updates)}
