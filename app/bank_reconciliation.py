"""Reconciliation-first, read-only classification for bank PDF rows."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

from .bank_pdf_pipeline import (
    DEFAULT_ACCOUNT_ALIAS,
    BankPdfPipeline,
    BankPdfResult,
    NormalizedBankTransaction,
)
from .reconciliation import (
    ImportTransaction,
    match_exact_import_authority,
    parse_import_rows,
)
from .sheets import SheetsDB


CLASSIFICATIONS = (
    "card_settlement", "transfer", "income", "expense", "needs_review",
)
RECONCILIATION_STATUSES = (
    "matched", "identified_unlinked", "not_applicable", "unmatched",
)

# Existing purchase rows are not settlement authority.  These statuses are a
# deliberately narrow contract for a future statement-total or explicit
# funding record; no current importer creates them implicitly.
CARD_STATEMENT_AUTHORITY_STATUS = "card_statement_total"
PAYPAY_BANK_AUTHORITY_STATUS = "paypay_bank_transfer"


@dataclass(frozen=True)
class BankClassification:
    transaction: NormalizedBankTransaction
    classification: str
    reason: str


@dataclass(frozen=True)
class BankReconciliation:
    classification: BankClassification
    reconciliation_status: str
    matched_source: str = ""
    matched_identity: str = ""


@dataclass(frozen=True)
class BankShadowResult:
    parsed: BankPdfResult
    decisions: tuple[BankReconciliation, ...]

    def summary(self) -> dict:
        classification_counts = Counter(
            decision.classification.classification for decision in self.decisions
        )
        reconciliation_counts = Counter(
            decision.reconciliation_status for decision in self.decisions
        )
        review_reasons = Counter(
            decision.classification.reason for decision in self.decisions
            if decision.classification.classification == "needs_review"
        )
        return {
            "read_only": True,
            "source": self.parsed.summary()["source"],
            "total_rows": self.parsed.candidate_rows,
            "parsed": len(self.parsed.transactions),
            "parse_failure": len(self.parsed.issues),
            "duplicate": self.parsed.duplicate_candidates,
            "classification": {
                name: classification_counts[name] for name in CLASSIFICATIONS
            },
            "reconciliation": {
                name: reconciliation_counts[name]
                for name in RECONCILIATION_STATUSES
            },
            "needs_review_reasons": dict(sorted(review_reasons.items())),
        }


def _compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
    return re.sub(r"[^0-9A-Z\u3040-\u30ff\u3400-\u9fff]+", "", normalized)


def classify_bank_transaction(
    transaction: NormalizedBankTransaction,
    *,
    confirmed_internal_descriptions: frozenset[str] = frozenset(),
) -> BankClassification:
    """Classify only cases supported by explicit bank-row evidence."""
    description = _compact(transaction.description)
    confirmed = {_compact(value) for value in confirmed_internal_descriptions}

    if transaction.signed_amount < 0 and "AUPAYカード" in description:
        return BankClassification(transaction, "card_settlement", "au_pay_card_settlement")

    if description in confirmed:
        return BankClassification(transaction, "transfer", "confirmed_internal_transfer")

    if transaction.signed_amount > 0:
        if "賞与" in description:
            return BankClassification(transaction, "income", "bonus")
        if "給与" in description:
            return BankClassification(transaction, "income", "salary")
        if "利息" in description:
            return BankClassification(transaction, "income", "bank_interest")
        if "定額自動入金" in description:
            return BankClassification(transaction, "transfer", "automatic_own_account_deposit")
        if "振込" in description or "振替" in description:
            return BankClassification(
                transaction, "needs_review", "ambiguous_incoming_transfer",
            )
        return BankClassification(transaction, "needs_review", "unclassified_deposit")

    if "PAYPAY" in description:
        if "チャージ" in description:
            return BankClassification(transaction, "transfer", "paypay_charge")
        return BankClassification(transaction, "needs_review", "paypay_candidate")
    if "ATM" in description:
        return BankClassification(transaction, "needs_review", "cash_withdrawal")
    if "振込" in description or (
        "振替" in description and "口座振替" not in description
    ):
        return BankClassification(
            transaction, "needs_review", "ambiguous_outgoing_transfer",
        )
    if "約定返済" in description:
        return BankClassification(transaction, "needs_review", "loan_repayment")
    if "手数料" in description:
        return BankClassification(transaction, "expense", "bank_fee")
    if "口座振替" in description:
        return BankClassification(transaction, "expense", "direct_debit")
    return BankClassification(transaction, "needs_review", "unclassified_withdrawal")


def reconcile_bank_classification(
    classified: BankClassification,
    existing_transactions: list[ImportTransaction],
) -> BankReconciliation:
    transaction = classified.transaction.to_canonical()
    if classified.classification == "card_settlement":
        match = match_exact_import_authority(
            transaction,
            existing_transactions,
            source_priority=("au PAYカード", "PayPay"),
            authority_statuses={
                "au PAYカード": frozenset({CARD_STATEMENT_AUTHORITY_STATUS}),
            },
        )
        if match.state == "matched":
            return BankReconciliation(
                classified, "matched", match.matched_source, match.matched_identity,
            )
        return BankReconciliation(classified, "identified_unlinked")

    if classified.reason in {"paypay_charge", "paypay_candidate"}:
        match = match_exact_import_authority(
            transaction,
            existing_transactions,
            source_priority=("au PAYカード", "PayPay"),
            authority_statuses={
                "PayPay": frozenset({PAYPAY_BANK_AUTHORITY_STATUS}),
            },
        )
        if match.state == "matched":
            return BankReconciliation(
                classified, "matched", match.matched_source, match.matched_identity,
            )
        return BankReconciliation(classified, "unmatched")

    return BankReconciliation(classified, "not_applicable")


def build_bank_shadow_result(
    parsed: BankPdfResult,
    existing_transactions: list[ImportTransaction],
    *,
    confirmed_internal_descriptions: frozenset[str] = frozenset(),
) -> BankShadowResult:
    classified = [
        classify_bank_transaction(
            transaction,
            confirmed_internal_descriptions=confirmed_internal_descriptions,
        )
        for transaction in parsed.transactions
    ]
    return BankShadowResult(
        parsed,
        tuple(
            reconcile_bank_classification(item, existing_transactions)
            for item in classified
        ),
    )


class BankPdfShadowPipeline:
    """Read the PDF and existing canonical rows without exposing row content."""

    def __init__(self, db: SheetsDB):
        self.db = db

    def preview(
        self,
        path: str | Path,
        *,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_descriptions: frozenset[str] = frozenset(),
    ) -> dict:
        existing_rows = self.db.get("取込データ!A2:L")
        existing_transactions = parse_import_rows(existing_rows)
        parsed = BankPdfPipeline().parse(
            path,
            account_alias=account_alias,
            existing_identities={
                transaction.import_id for transaction in existing_transactions
            },
        )
        return build_bank_shadow_result(
            parsed,
            existing_transactions,
            confirmed_internal_descriptions=confirmed_internal_descriptions,
        ).summary()
