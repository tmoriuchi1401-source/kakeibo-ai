"""Finalize already-imported bank rows through existing household-ledger paths."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .auto_expense import FALLBACK_CATEGORY, expense_id
from .bank_canary import LOAN_EXPENSE_CATEGORY
from .bank_pdf_pipeline import CHIBA_BANK_SOURCE, DOCOMO_SMTB_SOURCE, SOURCE
from .bank_reconciliation import (
    CARD_STATEMENT_AUTHORITY_STATUS,
    ConfirmedInternalTransfers,
    is_ambiguous_financial_counterparty,
    is_known_card_settlement_description,
    normalize_bank_description,
)
from .reconciliation import ImportTransaction, merchants_match, parse_import_rows
from .sheets import SheetsDB


BANK_SOURCES = frozenset({SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE})
BANK_EXPENSE_STATUS = "bank_expense"
BANK_INCOME_STATUS = "bank_income"
BANK_LOAN_STATUS = "bank_loan_repayment"
BANK_NON_EXPENSE_STATUS = "bank_non_expense"
BANK_DUPLICATE_STATUS = "bank_duplicate_excluded"
BANK_REVIEW_STATUS = "needs_review_bank_finalization"
PENDING_BANK_STATUSES = frozenset({
    BANK_EXPENSE_STATUS, BANK_INCOME_STATUS, BANK_LOAN_STATUS,
})
MUTATING_ACTIONS = frozenset({"post", "link", "non_expense", "review", "repair"})
CANARY_MAX_ROWS = 1


@dataclass(frozen=True)
class ExistingExpense:
    row_num: int
    expense_id: str
    date: str
    merchant: str
    amount: int
    import_id: str
    status: str
    row: tuple


@dataclass(frozen=True)
class BankFinalizationDecision:
    transaction: ImportTransaction
    outcome: str
    action: str
    reason: str
    target_id: str = ""
    category: tuple[str, str] | None = None


def _money(value) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def parse_expense_rows(rows: list[list]) -> list[ExistingExpense]:
    parsed = []
    for row_num, raw in enumerate(rows, start=2):
        row = tuple((list(raw) + [""] * 13)[:13])
        if not row[0]:
            continue
        parsed.append(ExistingExpense(
            row_num=row_num,
            expense_id=str(row[0]),
            date=str(row[1]),
            merchant=str(row[2]),
            amount=_money(row[4]),
            import_id=str(row[10]),
            status=str(row[12]) or "active",
            row=row,
        ))
    return parsed


def _active_expenses(expenses: list[ExistingExpense]) -> list[ExistingExpense]:
    return [expense for expense in expenses if expense.status != "duplicate_excluded"]


def _same_purchase_candidates(
    transaction: ImportTransaction,
    expenses: list[ExistingExpense],
) -> list[ExistingExpense]:
    return [
        expense for expense in _active_expenses(expenses)
        if expense.import_id != transaction.import_id
        and expense.date == transaction.date
        and abs(expense.amount) == abs(transaction.amount)
        and merchants_match(expense.merchant, transaction.merchant)
    ]


def _statement_authority_candidates(
    transaction: ImportTransaction,
    imports: list[ImportTransaction],
) -> list[ImportTransaction]:
    return [
        candidate for candidate in imports
        if candidate.import_id != transaction.import_id
        and candidate.source == "au PAYカード"
        and candidate.status == CARD_STATEMENT_AUTHORITY_STATUS
        and candidate.date == transaction.date
        and abs(candidate.amount) == abs(transaction.amount)
    ]


def _bank_account_alias(import_id: str) -> str:
    """Read the exact non-sensitive account alias from a stable bank identity."""
    parts = str(import_id or "").split(":", 3)
    if len(parts) != 4 or parts[0] != "bankpdf":
        return ""
    return parts[2]


def _is_confirmed_internal_transfer(
    transaction: ImportTransaction,
    confirmed_internal_transfers: ConfirmedInternalTransfers,
) -> bool:
    direction = "incoming" if transaction.amount > 0 else "outgoing"
    return (
        normalize_bank_description(transaction.merchant),
        direction,
        _bank_account_alias(transaction.import_id),
    ) in confirmed_internal_transfers


def bank_finalization_decisions(
    imports: list[ImportTransaction],
    expenses: list[ExistingExpense],
    *,
    confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
) -> list[BankFinalizationDecision]:
    """Classify imported bank rows without broad description-only posting.

    The imported bank status remains the expense/income authority.  Description
    checks only reduce automation: exact card-settlement labels become
    non-expense and ambiguous financial counterparties go to review.
    """
    bank_rows = [transaction for transaction in imports if transaction.source in BANK_SOURCES]
    active = _active_expenses(expenses)
    expense_by_id = {expense.expense_id: expense for expense in active}
    decisions = []
    for transaction in bank_rows:
        stable_expense_id = expense_id(transaction.import_id)
        stable_expense = expense_by_id.get(stable_expense_id)

        if transaction.status == "auto_expense" or (
            transaction.target_id and transaction.target_id in expense_by_id
        ):
            decisions.append(BankFinalizationDecision(
                transaction, "duplicate", "skip", "already_finalized",
                transaction.target_id or stable_expense_id,
            ))
            continue
        if stable_expense and stable_expense.import_id == transaction.import_id:
            decisions.append(BankFinalizationDecision(
                transaction, "duplicate", "repair", "stable_expense_replay",
                stable_expense_id,
            ))
            continue
        if transaction.status == BANK_NON_EXPENSE_STATUS:
            decisions.append(BankFinalizationDecision(
                transaction, "non_expense", "skip", "already_non_expense",
            ))
            continue
        if transaction.status == BANK_DUPLICATE_STATUS:
            decisions.append(BankFinalizationDecision(
                transaction, "excluded_link", "skip", "already_linked",
                transaction.target_id,
            ))
            continue
        if transaction.status.startswith("needs_review"):
            decisions.append(BankFinalizationDecision(
                transaction, "review", "skip", "already_in_review",
            ))
            continue
        if transaction.status == BANK_INCOME_STATUS:
            if transaction.amount > 0:
                decisions.append(BankFinalizationDecision(
                    transaction, "new_income", "skip", "bank_income_preserved",
                ))
            else:
                decisions.append(BankFinalizationDecision(
                    transaction, "review", "review", "bank_income_sign_invalid",
                ))
            continue
        if transaction.status == BANK_LOAN_STATUS:
            if transaction.amount < 0:
                decisions.append(BankFinalizationDecision(
                    transaction, "new_expense", "post", "housing_loan_repayment",
                    category=LOAN_EXPENSE_CATEGORY,
                ))
            else:
                decisions.append(BankFinalizationDecision(
                    transaction, "review", "review", "bank_loan_sign_invalid",
                ))
            continue
        if transaction.status != BANK_EXPENSE_STATUS:
            decisions.append(BankFinalizationDecision(
                transaction, "review", "review", "unsupported_bank_status",
            ))
            continue
        if transaction.amount >= 0:
            decisions.append(BankFinalizationDecision(
                transaction, "review", "review", "bank_expense_sign_invalid",
            ))
            continue

        if is_known_card_settlement_description(transaction.merchant):
            authorities = _statement_authority_candidates(transaction, imports)
            if len(authorities) == 1:
                decisions.append(BankFinalizationDecision(
                    transaction, "excluded_link", "link",
                    "exact_card_statement_authority", authorities[0].import_id,
                ))
            elif authorities:
                decisions.append(BankFinalizationDecision(
                    transaction, "review", "review",
                    "ambiguous_card_statement_authority",
                ))
            else:
                decisions.append(BankFinalizationDecision(
                    transaction, "non_expense", "non_expense",
                    "known_card_settlement",
                ))
            continue

        if _is_confirmed_internal_transfer(
            transaction, confirmed_internal_transfers,
        ):
            decisions.append(BankFinalizationDecision(
                transaction, "non_expense", "non_expense",
                "confirmed_internal_transfer",
            ))
            continue

        if is_ambiguous_financial_counterparty(transaction.merchant):
            decisions.append(BankFinalizationDecision(
                transaction, "review", "review",
                "ambiguous_financial_counterparty",
            ))
            continue

        duplicates = _same_purchase_candidates(transaction, expenses)
        if len(duplicates) == 1:
            decisions.append(BankFinalizationDecision(
                transaction, "excluded_link", "link",
                "exact_existing_expense", duplicates[0].expense_id,
            ))
        elif duplicates:
            decisions.append(BankFinalizationDecision(
                transaction, "review", "review",
                "ambiguous_existing_expense",
            ))
        else:
            decisions.append(BankFinalizationDecision(
                transaction, "new_expense", "post", "bank_expense_authority",
                category=FALLBACK_CATEGORY,
            ))
    return decisions


def validate_bank_finalization_canary(
    selected_import_ids: tuple[str, ...],
    *,
    target_spreadsheet_id: str,
    approved_target: str,
    current_head: str,
    expected_head: str,
) -> None:
    """Bind a production canary to one identity, one Sheet, and one commit."""
    if len(selected_import_ids) != CANARY_MAX_ROWS:
        raise RuntimeError("bank_finalization_canary_requires_exactly_one_identity")
    if not target_spreadsheet_id or target_spreadsheet_id != approved_target:
        raise RuntimeError("bank_finalization_approved_target_mismatch")
    if not current_head or current_head != expected_head:
        raise RuntimeError("bank_finalization_expected_head_mismatch")


class BankFinalizationPipeline:
    """Preview by default; apply only an explicit stable-identity selection."""

    def __init__(
        self,
        db: SheetsDB,
        *,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    ):
        self.db = db
        self.confirmed_internal_transfers = confirmed_internal_transfers

    def _context(self):
        imports = parse_import_rows(self.db.get("取込データ!A2:L"))
        expenses = parse_expense_rows(self.db.get("支出明細!A2:M"))
        return imports, expenses, bank_finalization_decisions(
            imports,
            expenses,
            confirmed_internal_transfers=self.confirmed_internal_transfers,
        )

    def preview(self, selected_import_ids: tuple[str, ...] = ()) -> dict:
        _, _, decisions = self._context()
        if selected_import_ids:
            if len(set(selected_import_ids)) != len(selected_import_ids):
                raise RuntimeError("bank_finalization_selection_duplicate")
            by_id = {decision.transaction.import_id: decision for decision in decisions}
            if any(identity not in by_id for identity in selected_import_ids):
                raise RuntimeError("bank_finalization_identity_not_found")
            decisions = [by_id[identity] for identity in selected_import_ids]
        result = self._summary(decisions)
        result["selected"] = len(selected_import_ids)
        return result

    def apply(
        self,
        selected_import_ids: tuple[str, ...],
        *,
        max_rows: int = CANARY_MAX_ROWS,
    ) -> dict:
        if (
            max_rows < 1
            or max_rows > 100
            or not selected_import_ids
            or len(selected_import_ids) > max_rows
        ):
            raise RuntimeError("bank_finalization_selection_bound_invalid")
        if len(set(selected_import_ids)) != len(selected_import_ids):
            raise RuntimeError("bank_finalization_selection_duplicate")

        _, expenses, decisions = self._context()
        by_id = {decision.transaction.import_id: decision for decision in decisions}
        if any(identity not in by_id for identity in selected_import_ids):
            raise RuntimeError("bank_finalization_identity_not_found")
        selected = [by_id[identity] for identity in selected_import_ids]

        categories = set(self.db.categories())
        required_categories = {
            decision.category for decision in selected
            if decision.action == "post" and decision.category is not None
        }
        if not required_categories.issubset(categories):
            raise RuntimeError("bank_finalization_category_unavailable")

        expense_index = {expense.expense_id: expense.row_num for expense in expenses}
        expense_new = []
        expense_updates = []
        import_updates = []
        for decision in selected:
            transaction = decision.transaction
            if decision.action == "skip":
                continue
            updated = list(transaction.row)
            annotation = f"銀行最終判定={decision.reason}"
            updated[11] = "; ".join(x for x in (transaction.note, annotation) if x)
            if decision.action in {"post", "repair"}:
                target = expense_id(transaction.import_id)
                updated[8] = "auto_expense"
                updated[9] = target
                if decision.action == "post":
                    category = decision.category or FALLBACK_CATEGORY
                    expense = [
                        target, transaction.date, transaction.merchant, "自動計上",
                        abs(transaction.amount), category[0], category[1],
                        transaction.row[7], transaction.source, "",
                        transaction.import_id, decision.reason, "active",
                    ]
                    if target in expense_index:
                        expense_updates.append((expense_index[target], expense))
                    else:
                        expense_new.append(expense)
            elif decision.action == "link":
                updated[8] = BANK_DUPLICATE_STATUS
                updated[9] = decision.target_id
            elif decision.action == "non_expense":
                updated[8] = BANK_NON_EXPENSE_STATUS
                updated[9] = ""
            elif decision.action == "review":
                updated[8] = BANK_REVIEW_STATUS
                updated[9] = ""
            else:
                raise RuntimeError("bank_finalization_action_invalid")
            import_updates.append((transaction.row_num, updated))

        if expense_new or expense_updates:
            self.db.ensure_expense_status_column()
        self.db.append("支出明細", expense_new)
        self.db.update_rows("支出明細", expense_updates)
        self.db.update_rows("取込データ", import_updates)

        read_back_verified = self._verify(selected)

        result = self._summary(selected)
        result.update({
            "read_only": False,
            "selected": len(selected),
            "expenses_created": len(expense_new),
            "expenses_updated": len(expense_updates),
            "imports_updated": len(import_updates),
            "read_back_verified": read_back_verified,
        })
        return result

    def _verify(self, selected: list[BankFinalizationDecision]) -> bool:
        imports = parse_import_rows(self.db.get("取込データ!A2:L"))
        expenses = parse_expense_rows(self.db.get("支出明細!A2:M"))
        imports_by_id = {transaction.import_id: transaction for transaction in imports}
        expenses_by_id = {
            expense.expense_id: expense for expense in _active_expenses(expenses)
        }
        for decision in selected:
            transaction = imports_by_id.get(decision.transaction.import_id)
            if transaction is None:
                raise RuntimeError("bank_finalization_readback_import_missing")
            if decision.action in {"post", "repair"}:
                target = expense_id(transaction.import_id)
                expense = expenses_by_id.get(target)
                if (
                    transaction.status != "auto_expense"
                    or transaction.target_id != target
                    or expense is None
                    or expense.import_id != transaction.import_id
                ):
                    raise RuntimeError("bank_finalization_readback_expense_mismatch")
            elif decision.action == "link" and (
                transaction.status != BANK_DUPLICATE_STATUS
                or transaction.target_id != decision.target_id
            ):
                raise RuntimeError("bank_finalization_readback_link_mismatch")
            elif decision.action == "non_expense" and (
                transaction.status != BANK_NON_EXPENSE_STATUS
                or transaction.target_id
            ):
                raise RuntimeError("bank_finalization_readback_non_expense_mismatch")
            elif decision.action == "review" and (
                transaction.status != BANK_REVIEW_STATUS
                or transaction.target_id
            ):
                raise RuntimeError("bank_finalization_readback_review_mismatch")
        return True

    @staticmethod
    def _summary(decisions: list[BankFinalizationDecision]) -> dict:
        outcomes = Counter(decision.outcome for decision in decisions)
        reasons = Counter(decision.reason for decision in decisions)
        sources = {}
        for decision in decisions:
            source = sources.setdefault(decision.transaction.source, Counter())
            source[decision.outcome] += 1
        return {
            "read_only": True,
            "total_bank_rows": len(decisions),
            "new_expense": outcomes["new_expense"],
            "new_income": outcomes["new_income"],
            "excluded_link": outcomes["excluded_link"],
            "non_expense": outcomes["non_expense"],
            "review": outcomes["review"],
            "duplicate": outcomes["duplicate"],
            "reasons": dict(sorted(reasons.items())),
            "by_source": {
                source: dict(sorted(counts.items()))
                for source, counts in sorted(sources.items())
            },
            "planned_expense_writes": sum(
                decision.action == "post" for decision in decisions
            ),
            "planned_import_updates": sum(
                decision.action in MUTATING_ACTIONS for decision in decisions
            ),
            "new_expense_identities": [
                decision.transaction.import_id for decision in decisions
                if decision.outcome == "new_expense"
            ],
            "review_identities": [
                decision.transaction.import_id for decision in decisions
                if decision.outcome == "review"
            ],
        }
