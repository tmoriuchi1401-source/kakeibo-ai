from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

import pandas as pd

from .amazon_pipeline import date_ymd, money
from .reconciliation import ImportTransaction


REQUIRED_COLUMNS = {
    "ASIN", "Order Date", "Order ID", "Original Quantity", "Payment Method Type",
    "Product Name", "Total Amount", "Ship Date", "Carrier Name & Tracking Number",
    "Shipment Item Subtotal", "Shipment Item Subtotal Tax", "Shipment Status",
    "Shipping Charge", "Total Discounts", "Unit Price", "Unit Price Tax",
    "Order Status", "Currency",
}
MONEY_COLUMNS = (
    "Total Amount", "Shipment Item Subtotal", "Shipment Item Subtotal Tax",
    "Shipping Charge", "Total Discounts", "Unit Price", "Unit Price Tax",
)
METHODS = (
    "row_total_amount",
    "row_shipment_subtotal",
    "row_shipment_subtotal_plus_tax",
    "row_shipment_subtotal_plus_tax_plus_shipping",
    "row_unit_price_plus_tax_times_quantity",
    "order_total_amount",
    "order_unit_price_plus_tax_times_quantity",
    "shipment_total_amount",
    "shipment_subtotal",
    "shipment_subtotal_plus_tax",
    "shipment_subtotal_plus_tax_plus_shipping",
    "shipment_unit_price_plus_tax_times_quantity",
)
SHIPMENT_METHODS = {method for method in METHODS if method.startswith("shipment_")}
ALTERNATE_METHODS = set(METHODS) - {"row_total_amount", "order_total_amount"} - SHIPMENT_METHODS


def _match_state(count: int) -> str:
    if count == 1:
        return "unique"
    if count > 1:
        return "ambiguous"
    return "none"


def payment_method_class(values: list[str]) -> str:
    methods = [str(value).strip() for value in values if str(value).strip()]
    if not methods:
        return "unknown"
    upper = " ".join(methods).upper()
    if "GIFT" in upper or "ギフト" in upper:
        return "gift_or_mixed"
    if len(set(methods)) > 1 or any(re.search(r"[;,/+]", value) for value in methods):
        return "multiple_payment_methods"
    return "single_payment_method"


def _load(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError("Amazon CSVに診断用の必要列がありません: " + ", ".join(sorted(missing)))
    quantity = pd.to_numeric(frame["Original Quantity"], errors="coerce").fillna(0)
    frame = frame[quantity > 0].copy()
    frame["_quantity"] = quantity[quantity > 0]
    frame["_order_date"] = frame["Order Date"].map(date_ymd)
    for column in MONEY_COLUMNS:
        frame[f"_{column}"] = frame[column].map(money)
    frame["_unit_formula"] = (
        (frame["_Unit Price"] + frame["_Unit Price Tax"]) * frame["_quantity"]
    ).round().astype(int)
    return frame


def _nearby(frame: pd.DataFrame, card_date: str) -> pd.DataFrame:
    card = pd.to_datetime(card_date, errors="coerce")
    dates = pd.to_datetime(frame["_order_date"], errors="coerce")
    if pd.isna(card):
        return frame.iloc[0:0]
    return frame[(dates - card).abs().dt.days <= 7].copy()


def _candidate_map(rows: pd.DataFrame, target: int) -> dict[str, set[tuple]]:
    found = {method: set() for method in METHODS}
    for index, row in rows.iterrows():
        row_id = ("row", int(index))
        row_values = {
            "row_total_amount": row["_Total Amount"],
            "row_shipment_subtotal": row["_Shipment Item Subtotal"],
            "row_shipment_subtotal_plus_tax": (
                row["_Shipment Item Subtotal"] + row["_Shipment Item Subtotal Tax"]
            ),
            "row_shipment_subtotal_plus_tax_plus_shipping": (
                row["_Shipment Item Subtotal"] + row["_Shipment Item Subtotal Tax"]
                + row["_Shipping Charge"]
            ),
            "row_unit_price_plus_tax_times_quantity": row["_unit_formula"],
        }
        for method, value in row_values.items():
            if value == target:
                found[method].add(row_id)

    for order_id, group in rows.groupby("Order ID", sort=False):
        identity = ("order", str(order_id))
        order_values = {
            "order_total_amount": int(group["_Total Amount"].sum()),
            "order_unit_price_plus_tax_times_quantity": int(group["_unit_formula"].sum()),
        }
        for method, value in order_values.items():
            if value == target:
                found[method].add(identity)

    prepared = rows.copy()
    prepared["_shipment_key"] = [
        (str(row["Order ID"]), str(row["Ship Date"]), str(row["Carrier Name & Tracking Number"]))
        if str(row["Ship Date"]).strip() and str(row["Carrier Name & Tracking Number"]).strip()
        else (str(row["Order ID"]), "missing", f"row:{index}")
        for index, row in prepared.iterrows()
    ]
    for shipment_key, group in prepared.groupby("_shipment_key", sort=False):
        first = group.iloc[0]
        shipment_values = {
            "shipment_total_amount": int(group["_Total Amount"].sum()),
            "shipment_subtotal": int(first["_Shipment Item Subtotal"]),
            "shipment_subtotal_plus_tax": int(
                first["_Shipment Item Subtotal"] + first["_Shipment Item Subtotal Tax"]
            ),
            "shipment_subtotal_plus_tax_plus_shipping": int(
                first["_Shipment Item Subtotal"] + first["_Shipment Item Subtotal Tax"]
                + first["_Shipping Charge"]
            ),
            "shipment_unit_price_plus_tax_times_quantity": int(group["_unit_formula"].sum()),
        }
        identity = ("shipment",) + tuple(shipment_key)
        for method, value in shipment_values.items():
            if value == target:
                found[method].add(identity)
    return found


def diagnose_amazon_csv_amounts(
    path: str | Path, transactions: list[ImportTransaction], *, sample_limit: int = 3,
) -> dict:
    frame = _load(path)
    method_counts = {method: Counter() for method in METHODS}
    payment_counts = Counter()
    final_counts = Counter()
    shipment_counts = Counter()
    alternate_counts = Counter()
    samples = []
    quantity_gt_one = frame[frame["_quantity"] > 1]

    for tx in transactions:
        rows = _nearby(frame, tx.date)
        payment_class = payment_method_class(rows["Payment Method Type"].tolist())
        payment_counts[payment_class] += 1
        candidates = _candidate_map(rows, tx.amount)
        states = {method: _match_state(len(values)) for method, values in candidates.items()}
        for method, state in states.items():
            method_counts[method][state] += 1

        shipment_ids = set().union(*(candidates[method] for method in SHIPMENT_METHODS))
        alternate_ids = set().union(*(candidates[method] for method in ALTERNATE_METHODS))
        shipment_state = _match_state(len(shipment_ids))
        alternate_state = _match_state(len(alternate_ids))
        shipment_counts[shipment_state] += 1
        alternate_counts[alternate_state] += 1

        matched_methods = [method for method, values in candidates.items() if values]
        order_totals = rows.groupby("Order ID")["_Total Amount"].sum().tolist() if len(rows) else []
        gift_residual_possible = (
            payment_class == "gift_or_mixed" and any(total > tx.amount for total in order_totals)
        )
        explanation_types = set()
        if gift_residual_possible:
            explanation_types.add("gift")
        if shipment_ids:
            explanation_types.add("shipment")
        if alternate_ids:
            explanation_types.add("alternate")
        any_ambiguous = any(state == "ambiguous" for state in states.values())
        if any_ambiguous or len(explanation_types) > 1 or len(matched_methods) > 1:
            final = "multiple_possible_explanations"
        elif explanation_types == {"gift"}:
            final = "gift_or_mixed_payment_candidate"
        elif explanation_types == {"shipment"} and shipment_state == "unique":
            final = "shipment_amount_match"
        elif explanation_types == {"alternate"} and alternate_state == "unique":
            final = "alternate_amount_formula_match"
        else:
            final = "csv_still_insufficient"
        final_counts[final] += 1

        if len(samples) < sample_limit:
            nearest_difference = None
            nearest_days = None
            if order_totals:
                nearest_difference = min((abs(total - tx.amount) for total in order_totals), default=None)
            if len(rows):
                dates = pd.to_datetime(rows["_order_date"], errors="coerce")
                card = pd.to_datetime(tx.date, errors="coerce")
                if not pd.isna(card):
                    nearest_days = int((dates - card).abs().dt.days.min())
            samples.append({
                "card_date": tx.date,
                "card_amount": tx.amount,
                "date_difference_days": nearest_days,
                "payment_method_class": payment_class,
                "matched_methods": matched_methods,
                "nearest_order_amount_difference": nearest_difference,
                "classification": final,
            })

    states = ("unique", "ambiguous", "none")
    return {
        "amount_mismatch_transactions": len(transactions),
        "csv_profile": {
            "active_rows": len(frame),
            "rows_quantity_gt_one": len(quantity_gt_one),
            "rows_nonzero_total_discounts": int((frame["_Total Discounts"] != 0).sum()),
            "rows_nonzero_shipping_charge": int((frame["_Shipping Charge"] != 0).sum()),
            "distinct_order_statuses": int(frame["Order Status"].nunique()),
            "distinct_shipment_statuses": int(frame["Shipment Status"].nunique()),
            "distinct_currencies": int(frame["Currency"].nunique()),
        },
        "payment_method_classification": {
            name: payment_counts[name] for name in (
                "single_payment_method", "gift_or_mixed", "multiple_payment_methods", "unknown",
            )
        },
        "shipment_amount_match": {state: shipment_counts[state] for state in states},
        "alternate_amount_formula_match": {state: alternate_counts[state] for state in states},
        "method_matches": {
            method: {state: method_counts[method][state] for state in states}
            for method in METHODS
        },
        "final_classification": dict(final_counts),
        "quantity_analysis": {
            "rows_quantity_gt_one": len(quantity_gt_one),
            "total_amount_equals_unit_formula": int(
                (quantity_gt_one["_Total Amount"] == quantity_gt_one["_unit_formula"]).sum()
            ),
            "definition_confirmed": False,
        },
        "samples": samples,
    }
