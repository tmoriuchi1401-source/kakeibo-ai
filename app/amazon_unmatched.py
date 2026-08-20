from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime
from itertools import combinations

from .amazon_installment import (
    INSTALLMENT_STATUS,
    _has_refund_text,
    find_installment_matches,
    parse_baseline_orders,
)
from .aupay_card_pipeline import AuPayCardPipeline, is_amazon
from .auto_expense import _amazon_installment
from .reconciliation import ImportTransaction, parse_import_rows
from .sheets import SheetsDB


DIAGNOSIS_STATUSES = ("amazon_unmatched", "amazon_needs_review")
DIAGNOSES = (
    "exact_match",
    "date_outside_window",
    "amount_near_match",
    "multiple_exact_candidates",
    "installment_candidate",
    "refund_or_cancel_candidate",
    "no_order_candidate",
    "other",
)
AMAZON_COLUMNS = (
    "Amazonキー", "Order ID", "ASIN", "注文日", "商品名", "数量", "商品金額",
    "支払方法", "大カテゴリ", "小カテゴリ", "備考", "データハッシュ", "最終取込日時",
)
PIPELINE_REQUIRED_CSV_COLUMNS = (
    "Order ID", "ASIN", "Order Date", "Product Name", "Original Quantity",
    "Total Amount", "Payment Method Type",
)
ADJUSTMENT_KEYWORDS = ("ポイント", "値引", "割引", "PROMOTION", "COUPON", "クーポン")
DATE_WINDOWS = (10, 14, 21, 30)
MAX_SEARCH_ELEMENTS = 50


def _money(value) -> int | None:
    try:
        return int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _signed_days(order_date: str, card_date: str) -> int | None:
    try:
        order = datetime.strptime(order_date, "%Y-%m-%d")
        card = datetime.strptime(card_date, "%Y-%m-%d")
        return (card - order).days
    except ValueError:
        return None


def _match_state(count: int) -> str:
    if count == 1:
        return "unique"
    if count > 1:
        return "ambiguous"
    return "none"


def _sum_match_count(amounts: list[int], size: int, target: int) -> int:
    """Return 0, 1, or 2; two is enough to establish ambiguity."""
    matches = 0
    for values in combinations(amounts[:MAX_SEARCH_ELEMENTS], size):
        if sum(values) == target:
            matches += 1
            if matches == 2:
                break
    return matches


class AmazonUnmatchedPreview:
    """Read-only diagnostics for unresolved Amazon card transactions."""

    def __init__(self, db: SheetsDB, *, sample_limit: int = 3):
        self.db = db
        self.sample_limit = sample_limit

    @staticmethod
    def _installment_text(tx: ImportTransaction) -> str:
        payment_type = str(tx.row[7]) if len(tx.row) > 7 else ""
        return " ".join((tx.merchant, payment_type, tx.note))

    def _installment_ids(
        self, transactions: list[ImportTransaction], amazon_rows: list[list],
    ) -> set[str]:
        candidates = [
            replace(tx, status=INSTALLMENT_STATUS)
            for tx in transactions
            if _amazon_installment(self._installment_text(tx))
        ]
        matches, _, _ = find_installment_matches(candidates, parse_baseline_orders(amazon_rows))
        return {
            tx.import_id
            for match in matches
            for tx in match.installments
        }

    @staticmethod
    def _amazon_items(rows: list[list]) -> dict[str, dict]:
        orders: dict[str, dict] = {}
        for raw in rows:
            row = list(raw) + [""] * max(0, 13 - len(raw))
            amount = _money(row[6])
            order_id = str(row[1])
            if not order_id or amount is None:
                continue
            order = orders.setdefault(order_id, {"date": str(row[3]), "amounts": []})
            order["amounts"].append(amount)
        return orders

    @staticmethod
    def _combination_diagnosis(
        tx: ImportTransaction, orders: dict[str, dict],
    ) -> dict[str, str]:
        nearby = [
            order for order in orders.values()
            if AuPayCardPipeline._days(str(order["date"]), tx.date) <= 7
        ]
        nearby.sort(key=lambda order: AuPayCardPipeline._days(str(order["date"]), tx.date))

        single_count = sum(
            amount == tx.amount
            for order in nearby[:MAX_SEARCH_ELEMENTS]
            for amount in order["amounts"][:MAX_SEARCH_ELEMENTS]
        )
        same_order_two = 0
        same_order_three = 0
        for order in nearby[:MAX_SEARCH_ELEMENTS]:
            same_order_two = min(
                2, same_order_two + _sum_match_count(order["amounts"], 2, tx.amount),
            )
            same_order_three = min(
                2, same_order_three + _sum_match_count(order["amounts"], 3, tx.amount),
            )

        totals = [sum(order["amounts"]) for order in nearby[:MAX_SEARCH_ELEMENTS]]
        return {
            "single_item": _match_state(single_count),
            "same_order_2_items": _match_state(same_order_two),
            "same_order_3_items": _match_state(same_order_three),
            "nearby_2_orders": _match_state(_sum_match_count(totals, 2, tx.amount)),
            "nearby_3_orders": _match_state(_sum_match_count(totals, 3, tx.amount)),
        }

    @staticmethod
    def _date_simulation(transactions: list[ImportTransaction], amazon: list[dict]) -> dict:
        simulation = {}
        for window in DATE_WINDOWS:
            counts = Counter()
            for tx in transactions:
                candidates = [
                    order for order in amazon
                    if int(order["amount"]) == tx.amount
                    and AuPayCardPipeline._days(str(order["date"]), tx.date) <= window
                ]
                state = _match_state(len(candidates))
                counts["unmatched" if state == "none" else state] += 1
            simulation[f"plus_minus_{window}_days"] = {
                "unique": counts["unique"],
                "ambiguous": counts["ambiguous"],
                "unmatched": counts["unmatched"],
            }
        return simulation

    @staticmethod
    def _date_directions(transactions: list[ImportTransaction], amazon: list[dict]) -> dict:
        counts = Counter()
        for tx in transactions:
            same_amount = [order for order in amazon if int(order["amount"]) == tx.amount]
            if not same_amount:
                counts["unknown"] += 1
                continue
            nearest = min(
                same_amount,
                key=lambda order: AuPayCardPipeline._days(str(order["date"]), tx.date),
            )
            days = _signed_days(str(nearest["date"]), tx.date)
            if days is None:
                counts["unknown"] += 1
            elif days > 0:
                counts["amazon_order_before_card"] += 1
            elif days < 0:
                counts["card_before_amazon_order"] += 1
            else:
                counts["same_day"] += 1
        return {
            "basis": "nearest exact-amount Amazon order per date_outside_window transaction",
            "amazon_order_before_card": counts["amazon_order_before_card"],
            "card_before_amazon_order": counts["card_before_amazon_order"],
            "same_day": counts["same_day"],
            "unknown": counts["unknown"],
        }

    @staticmethod
    def _difference_analysis(
        transactions: list[ImportTransaction], amazon: list[dict], amazon_rows: list[list],
    ) -> dict:
        differences = Counter()
        rates = Counter()
        for tx in transactions:
            nearby = [
                order for order in amazon
                if AuPayCardPipeline._days(str(order["date"]), tx.date) <= 7
            ]
            if not nearby:
                continue
            nearest = min(
                nearby,
                key=lambda order: (
                    abs(int(order["amount"]) - tx.amount),
                    AuPayCardPipeline._days(str(order["date"]), tx.date),
                ),
            )
            order_amount = int(nearest["amount"])
            difference = order_amount - tx.amount
            differences[difference] += 1
            if order_amount:
                rates[round(difference / order_amount * 100, 2)] += 1

        note_hits = Counter()
        for raw in amazon_rows:
            row = list(raw) + [""] * max(0, 13 - len(raw))
            note = str(row[10]).upper()
            for keyword in ADJUSTMENT_KEYWORDS:
                if keyword in note:
                    note_hits[keyword] += 1
        return {
            "repeated_amount_differences": [
                {"difference": value, "count": count}
                for value, count in sorted(differences.items()) if count > 1
            ],
            "repeated_difference_rates_percent": [
                {"rate": value, "count": count}
                for value, count in sorted(rates.items()) if count > 1
            ],
            "amazon_csv_evidence": {
                "confirmed_pipeline_required_csv_columns": list(PIPELINE_REQUIRED_CSV_COLUMNS),
                "confirmed_sheets_columns": list(AMAZON_COLUMNS),
                "confirmed_dedicated_adjustment_columns": [],
                "confirmed_conclusion": (
                    "The current pipeline does not retain a dedicated point, discount, "
                    "coupon, or promotion column for diagnostics"
                ),
                "source_csv_extra_columns_observable": False,
                "inferred_note_keyword_hits": dict(note_hits),
            },
        }

    @staticmethod
    def _sample(tx: ImportTransaction, candidates: list[dict]) -> dict:
        sample = {
            "status": tx.status,
            "card_date": tx.date,
            "card_amount": tx.amount,
            "candidate_orders": len(candidates),
        }
        if candidates:
            nearest = min(
                candidates,
                key=lambda item: (
                    AuPayCardPipeline._days(str(item["date"]), tx.date),
                    abs(int(item["amount"]) - tx.amount),
                ),
            )
            sample.update({
                "nearest_order_amount": int(nearest["amount"]),
                "amount_difference": abs(int(nearest["amount"]) - tx.amount),
                "date_difference_days": AuPayCardPipeline._days(str(nearest["date"]), tx.date),
            })
        return sample

    @staticmethod
    def _diagnose(
        tx: ImportTransaction, amazon: list[dict], installment_ids: set[str],
        card_pipeline: AuPayCardPipeline,
    ) -> tuple[str, list[dict]]:
        if _has_refund_text(tx):
            return "refund_or_cancel_candidate", []
        if tx.import_id in installment_ids:
            return "installment_candidate", []

        match_status, exact = card_pipeline._classify_amazon(tx.date, tx.amount, amazon)
        if match_status == "amazon_needs_review":
            return "multiple_exact_candidates", exact
        if match_status == "matched_amazon":
            return "exact_match", exact

        same_amount = [order for order in amazon if int(order["amount"]) == tx.amount]
        if same_amount:
            return "date_outside_window", same_amount

        near_date = [
            order for order in amazon
            if AuPayCardPipeline._days(str(order["date"]), tx.date) <= 7
        ]
        if near_date:
            return "amount_near_match", near_date
        if amazon:
            return "no_order_candidate", []
        return "no_order_candidate", []

    def preview(self) -> dict:
        import_rows = self.db.get("取込データ!A2:L")
        amazon_rows = self.db.get("Amazon注文!A2:M")
        all_transactions = parse_import_rows(import_rows)
        targets = [
            tx for tx in all_transactions
            if tx.source == "au PAYカード"
            and tx.status in DIAGNOSIS_STATUSES
            and is_amazon(tx.merchant)
        ]
        card_pipeline = AuPayCardPipeline(self.db)
        amazon = card_pipeline._amazon_candidates()
        item_orders = self._amazon_items(amazon_rows)
        installment_ids = self._installment_ids(targets, amazon_rows)

        counts = Counter()
        samples: dict[str, list[dict]] = {}
        diagnosed: list[tuple[ImportTransaction, str]] = []
        for tx in targets:
            diagnosis, candidates = self._diagnose(
                tx, amazon, installment_ids, card_pipeline,
            )
            counts[diagnosis] += 1
            diagnosed.append((tx, diagnosis))
            if len(samples.setdefault(diagnosis, [])) < self.sample_limit:
                samples[diagnosis].append(self._sample(tx, candidates))

        result = {
            "amazon_unmatched": sum(tx.status == "amazon_unmatched" for tx in targets),
            "amazon_needs_review": sum(tx.status == "amazon_needs_review" for tx in targets),
            "diagnosed": len(targets),
        }
        result.update({name: counts[name] for name in DIAGNOSES})
        result["samples"] = {name: rows for name, rows in samples.items() if rows}
        amount_near = [tx for tx, diagnosis in diagnosed if diagnosis == "amount_near_match"]
        date_outside = [tx for tx, diagnosis in diagnosed if diagnosis == "date_outside_window"]
        methods = (
            "single_item", "same_order_2_items", "same_order_3_items",
            "nearby_2_orders", "nearby_3_orders",
        )
        structures = [self._combination_diagnosis(tx, item_orders) for tx in amount_near]
        result["amount_structure"] = {
            "transactions": len(amount_near),
            "limits": {"max_elements": MAX_SEARCH_ELEMENTS, "max_combination_size": 3},
            "summary": {
                method: {
                    state: Counter(row[method] for row in structures)[state]
                    for state in ("unique", "ambiguous", "none")
                }
                for method in methods
            },
            "samples": [
                {"card_date": tx.date, "card_amount": tx.amount, **structure}
                for tx, structure in list(zip(amount_near, structures))[:self.sample_limit]
            ],
        }
        result["amount_differences"] = self._difference_analysis(
            amount_near, amazon, amazon_rows,
        )
        result["date_window_simulation"] = self._date_simulation(date_outside, amazon)
        result["date_direction"] = self._date_directions(date_outside, amazon)
        return result
