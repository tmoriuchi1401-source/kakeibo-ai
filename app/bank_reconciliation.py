"""Reconciliation-first, read-only classification for bank PDF rows."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
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
    CARD_STATEMENT_AUTHORITY_STATUS,
    ImportTransaction,
    match_exact_import_authority,
    parse_import_rows,
)
from .sheets import SheetsDB
from .transaction_plan import resolve_transaction_identities


CLASSIFICATIONS = (
    "card_settlement", "transfer", "income", "expense", "loan_repayment",
    "cash_withdrawal", "needs_review",
)
WRITE_ELIGIBLE_CLASSIFICATIONS = frozenset({
    "income", "expense", "loan_repayment",
})
ConfirmedInternalTransfers = frozenset[tuple[str, str, str]]
RECONCILIATION_STATUSES = (
    "matched", "identified_unlinked", "not_applicable", "unmatched",
)

# Existing purchase rows are not settlement authority.  These statuses are a
# deliberately narrow contract for a future statement-total or explicit
# funding record; no current importer creates them implicitly.
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
    write_eligibility: str = "withheld"


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
        write_eligibility = Counter(
            decision.write_eligibility for decision in self.decisions
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
            "write_eligibility": {
                "preview_candidate": write_eligibility["preview_candidate"],
                "withheld": write_eligibility["withheld"],
            },
            "needs_review_reasons": dict(sorted(review_reasons.items())),
        }


@dataclass(frozen=True)
class BankPreviewPlan:
    """Production-equivalent decision counts without a writer or side effects."""

    parsed: int
    eligible_before_dedupe: int
    existing_duplicate: int
    ambiguous_collision: int
    new_plan_candidates: int
    withheld_by_classification: int
    candidate_identities: tuple[str, ...]
    write_attempted: int = 0

    def summary(self) -> dict:
        return {
            "parsed": self.parsed,
            "eligible_before_dedupe": self.eligible_before_dedupe,
            "existing_duplicate": self.existing_duplicate,
            "ambiguous_collision": self.ambiguous_collision,
            "new_plan_candidates": self.new_plan_candidates,
            "withheld_by_classification": self.withheld_by_classification,
            "candidate_identity_count": len(self.candidate_identities),
            "write_attempted": self.write_attempted,
        }


@dataclass(frozen=True)
class TransferOwnershipCandidateGroup:
    """Local-console-only operator confirmation candidate."""

    normalized_description: str
    direction: str
    account_alias: str
    occurrence_count: int


def group_transfer_ownership_candidates(
    parsed: BankPdfResult,
) -> tuple[TransferOwnershipCandidateGroup, ...]:
    counts = Counter()
    for transaction in parsed.transactions:
        classified = classify_bank_transaction(transaction)
        if classified.reason not in {
            "ambiguous_incoming_transfer", "ambiguous_outgoing_transfer",
        }:
            continue
        counts[(
            normalize_bank_description(transaction.description),
            "incoming" if transaction.signed_amount > 0 else "outgoing",
            transaction.account_alias,
        )] += 1
    return tuple(
        TransferOwnershipCandidateGroup(description, direction, account_alias, count)
        for (description, direction, account_alias), count in sorted(counts.items())
    )


def normalize_bank_description(value: str) -> str:
    """Return the exact comparison form used by private ownership rules."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
    return re.sub(r"[^0-9A-Z\u3040-\u30ff\u3400-\u9fff]+", "", normalized)


# Kept private-name compatible for the local diagnostic used in earlier phases.
_compact = normalize_bank_description


def classify_bank_transaction(
    transaction: NormalizedBankTransaction,
    *,
    confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
) -> BankClassification:
    """Classify only cases supported by explicit bank-row evidence."""
    description = normalize_bank_description(transaction.description)
    direction = "incoming" if transaction.signed_amount > 0 else "outgoing"
    confirmed = {
        (value, configured_direction, account_alias)
        for value, configured_direction, account_alias
        in confirmed_internal_transfers
    }

    if transaction.signed_amount < 0 and "AUPAYカード" in description:
        return BankClassification(transaction, "card_settlement", "au_pay_card_settlement")

    if (description, direction, transaction.account_alias) in confirmed:
        return BankClassification(transaction, "transfer", "confirmed_internal_transfer")

    if transaction.signed_amount > 0:
        if "賞与" in description:
            return BankClassification(transaction, "income", "bonus")
        if "給与" in description:
            return BankClassification(transaction, "income", "salary")
        if "利息" in description:
            return BankClassification(transaction, "income", "bank_interest")
        if (
            "特典" in description or "トクテン" in description
            or "金利優遇" in description or "キンリユウグウ" in description
        ):
            return BankClassification(transaction, "income", "bank_reward")
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
        return BankClassification(transaction, "cash_withdrawal", "atm_cash_withdrawal")
    if "振込" in description or (
        "振替" in description and "口座振替" not in description
    ):
        return BankClassification(
            transaction, "needs_review", "ambiguous_outgoing_transfer",
        )
    if "約定返済" in description:
        return BankClassification(
            transaction, "loan_repayment", "contractual_loan_repayment",
        )
    if "手数料" in description:
        return BankClassification(transaction, "expense", "bank_fee")
    if "口座振替" in description:
        return BankClassification(transaction, "expense", "direct_debit")
    return BankClassification(transaction, "needs_review", "unclassified_withdrawal")


def reconcile_bank_classification(
    classified: BankClassification,
    existing_transactions: list[ImportTransaction],
    *,
    card_statement_authorities: tuple[ImportTransaction, ...] = (),
) -> BankReconciliation:
    transaction = classified.transaction.to_canonical()
    if classified.classification == "card_settlement":
        match = match_exact_import_authority(
            transaction,
            existing_transactions + list(card_statement_authorities),
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

    eligibility = (
        "preview_candidate"
        if classified.classification in WRITE_ELIGIBLE_CLASSIFICATIONS
        else "withheld"
    )
    return BankReconciliation(
        classified, "not_applicable", write_eligibility=eligibility,
    )


def build_bank_shadow_result(
    parsed: BankPdfResult,
    existing_transactions: list[ImportTransaction],
    *,
    confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    card_statement_authorities: tuple[ImportTransaction, ...] = (),
) -> BankShadowResult:
    classified = [
        classify_bank_transaction(
            transaction,
            confirmed_internal_transfers=confirmed_internal_transfers,
        )
        for transaction in parsed.transactions
    ]
    statement_rows = tuple(
        transaction
        for transaction in (*existing_transactions, *card_statement_authorities)
        if transaction.source == "au PAYカード"
        and transaction.status == CARD_STATEMENT_AUTHORITY_STATUS
    )
    bank_card_keys = Counter(
        (
            item.transaction.transaction_date,
            abs(item.transaction.signed_amount),
        )
        for item in classified
        if item.classification == "card_settlement"
    )
    statement_keys = Counter(
        (transaction.date, abs(transaction.amount)) for transaction in statement_rows
    )
    non_statement_existing = [
        transaction for transaction in existing_transactions
        if not (
            transaction.source == "au PAYカード"
            and transaction.status == CARD_STATEMENT_AUTHORITY_STATUS
        )
    ]
    eligible_identities = {
        transaction.identity for transaction in parsed.canonical_transactions
    }
    decisions = []
    for item in classified:
        row_statement_authorities = card_statement_authorities
        row_existing = existing_transactions
        if item.classification == "card_settlement":
            key = (
                item.transaction.transaction_date,
                abs(item.transaction.signed_amount),
            )
            row_existing = non_statement_existing
            row_statement_authorities = (
                tuple(transaction for transaction in statement_rows if (
                    transaction.date, abs(transaction.amount),
                ) == key)
                if bank_card_keys[key] == 1 and statement_keys[key] == 1
                else ()
            )
        decision = reconcile_bank_classification(
            item,
            row_existing,
            card_statement_authorities=tuple(row_statement_authorities),
        )
        if (
            decision.write_eligibility == "preview_candidate"
            and item.transaction.source_row_identity not in eligible_identities
        ):
            decision = replace(decision, write_eligibility="withheld")
        decisions.append(decision)
    return BankShadowResult(parsed, tuple(decisions))


def build_bank_preview_plan(
    shadow: BankShadowResult,
    existing_transactions: list[ImportTransaction],
) -> BankPreviewPlan:
    """Build the final pre-writer plan using the shared identity resolver."""
    decisions = shadow.decisions
    canonical = [
        decision.classification.transaction.to_canonical()
        for decision in decisions
    ]
    resolution = resolve_transaction_identities(
        canonical,
        signature=lambda transaction: (
            transaction.source,
            transaction.source_record_id,
            transaction.transaction_date,
            transaction.merchant,
            transaction.amount_yen,
            transaction.transaction_kind,
            transaction.payment_method,
            transaction.business_fingerprint,
            transaction.source_hash,
        ),
    )
    existing_ids = {
        transaction.import_id for transaction in existing_transactions
    }
    eligible_before_dedupe = sum(
        decision.classification.classification in WRITE_ELIGIBLE_CLASSIFICATIONS
        for decision in decisions
    )
    eligible = {
        decision.classification.transaction.source_row_identity
        for decision in decisions
        if decision.write_eligibility == "preview_candidate"
    }
    collision_ids = {
        transaction.identity
        for group in resolution.collision_groups
        for transaction in group
    }
    existing_duplicate = sum(
        transaction.identity in existing_ids
        for transaction in resolution.unique
        if transaction.identity not in collision_ids
    )
    candidate_identities = tuple(sorted(
        transaction.identity
        for transaction in resolution.unique
        if transaction.identity in eligible
        and transaction.identity not in existing_ids
        and transaction.identity not in collision_ids
    ))
    return BankPreviewPlan(
        parsed=len(decisions),
        eligible_before_dedupe=eligible_before_dedupe,
        existing_duplicate=existing_duplicate,
        ambiguous_collision=len(resolution.collision_groups),
        new_plan_candidates=len(candidate_identities),
        withheld_by_classification=sum(
            decision.classification.classification not in WRITE_ELIGIBLE_CLASSIFICATIONS
            for decision in decisions
        ),
        candidate_identities=candidate_identities,
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
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
        card_statement_authorities: tuple[ImportTransaction, ...] = (),
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
            confirmed_internal_transfers=confirmed_internal_transfers,
            card_statement_authorities=card_statement_authorities,
        ).summary()

    def production_preview(
        self,
        path: str | Path,
        *,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
        card_statement_authorities: tuple[ImportTransaction, ...] = (),
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
        shadow = build_bank_shadow_result(
            parsed,
            existing_transactions,
            confirmed_internal_transfers=confirmed_internal_transfers,
            card_statement_authorities=card_statement_authorities,
        )
        result = shadow.summary()
        result.update(build_bank_preview_plan(
            shadow, existing_transactions,
        ).summary())
        return result
