from __future__ import annotations

from collections import Counter
from dataclasses import replace

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
        installment_ids = self._installment_ids(targets, amazon_rows)

        counts = Counter()
        samples: dict[str, list[dict]] = {}
        for tx in targets:
            diagnosis, candidates = self._diagnose(
                tx, amazon, installment_ids, card_pipeline,
            )
            counts[diagnosis] += 1
            if len(samples.setdefault(diagnosis, [])) < self.sample_limit:
                samples[diagnosis].append(self._sample(tx, candidates))

        result = {
            "amazon_unmatched": sum(tx.status == "amazon_unmatched" for tx in targets),
            "amazon_needs_review": sum(tx.status == "amazon_needs_review" for tx in targets),
            "diagnosed": len(targets),
        }
        result.update({name: counts[name] for name in DIAGNOSES})
        result["samples"] = {name: rows for name, rows in samples.items() if rows}
        return result
