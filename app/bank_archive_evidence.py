"""Read-only proof of completed bank postings; never changes classification."""
from __future__ import annotations

from collections import defaultdict
import re

from .bank_income import BANK_NAMESPACES, income_id, validate_income_rows
from .auto_expense import expense_id
from .reconciliation import parse_import_rows

# Explicit operator holds, conflicting rules and ambiguous matching evidence
# are deliberately absent. Only absence of a current classification may defer
# to a previously completed, exact posting.
RECLASSIFICATION_REASONS = frozenset({
    "unclassified_withdrawal", "unclassified_deposit",
    "ambiguous_incoming_transfer", "ambiguous_outgoing_transfer",
})


def import_matches(tx, matches):
    if len(matches) != 1:
        return False
    row = matches[0]
    amount = str(row.row[6]).replace(",", "")
    namespace = BANK_NAMESPACES.get(tx.source)
    identity = rf"bankpdf:{re.escape(namespace or '')}:{re.escape(tx.account_alias)}:{re.escape(tx.source_row_hash[:24])}(?::[0-9]{{3}})?"
    return bool(namespace and re.fullmatch(r"[0-9a-f]{64}", tx.source_row_hash)
        and re.fullmatch(identity, tx.source_row_identity)
        and re.fullmatch(r"-?[0-9]+", amount) and int(amount) == tx.signed_amount
        and row.source == tx.source and row.row[3] == tx.source_row_identity
        and row.date == tx.transaction_date and row.merchant == tx.description
        and row.row[7] == "銀行口座" and row.row[10] == tx.source_row_hash)


def completed_bank_postings(db, transactions, import_rows, *, selected=None):
    """Return identity -> posting kind only after both source and ledger match.

    Import labels alone (including bank_income and bank_non_expense) never prove
    a posting. A missing, duplicate, excluded, conflicting or altered row stays
    unresolved. Income rows retain their validated historical category/reason.
    """
    by_id = defaultdict(list)
    for row in parse_import_rows(import_rows):
        by_id[row.import_id].append(row)
    candidates = []
    for tx in transactions:
        if selected is not None and tx.source_row_identity not in selected:
            continue
        matches = by_id[tx.source_row_identity]
        if import_matches(tx, matches):
            candidates.append((tx, matches[0]))
    income_candidates = [(tx, row) for tx, row in candidates
        if tx.signed_amount > 0 and row.status == "bank_income" and not row.target_id]
    expense_candidates = [(tx, row) for tx, row in candidates
        if tx.signed_amount < 0 and row.status == "auto_expense"
        and row.target_id == expense_id(tx.source_row_identity)]
    incomes = validate_income_rows(db.get("収入明細!A2:J")) if income_candidates else {}
    expenses = defaultdict(list)
    if expense_candidates:
        for raw in db.get("支出明細!A2:M"):
            if raw and raw[0]:
                expenses[str(raw[0])].append(list(raw) + [""] * max(0, 13 - len(raw)))
    completed = {}
    for tx, _ in income_candidates:
        saved = incomes.get(income_id(tx.source_row_identity))
        if saved and (saved[1], saved[2], saved[4], saved[5], saved[6], saved[7], saved[9]) == (
                tx.transaction_date, tx.signed_amount, tx.description, tx.account_alias,
                tx.source_row_identity, tx.source, tx.source_row_hash):
            completed[tx.source_row_identity] = "income"
    for tx, row in expense_candidates:
        matches = expenses[row.target_id]
        if len(matches) != 1:
            continue
        saved = matches[0]
        amount = str(saved[4]).replace(",", "")
        if (re.fullmatch(r"[0-9]+", amount) and int(amount) == -tx.signed_amount
                and saved[1] == tx.transaction_date and saved[2] == tx.description
                and saved[7] == "銀行口座" and saved[8] == tx.source
                and saved[10] == tx.source_row_identity and saved[12] == "active"):
            completed[tx.source_row_identity] = "expense"
    return completed


def approved_nonposting_holds(db, transactions, import_rows, meaning_resolver):
    """An approved transfer/refund/exclusion cannot hide a financial posting.

    Inspect linkage even when an import label is wrong or a ledger row is
    orphaned. Exact import values alone never resolve a prior review status.
    No non-bank transaction or existing value is changed by this check.
    """
    if meaning_resolver is None:
        return {}
    selected = []
    for tx in transactions:
        meaning = meaning_resolver(tx)
        if meaning["state"] == "matched" and meaning["classification"] in {
                "transfer", "reimbursement", "other_nonwrite"}:
            selected.append(tx)
    if not selected:
        return {}
    imports = defaultdict(list)
    for row in parse_import_rows(import_rows):
        imports[row.import_id].append(row)
    incomes = validate_income_rows(db.get("収入明細!A2:J"))
    income_links = {row[6] for row in incomes.values()}
    expenses = db.get("支出明細!A2:M")
    expense_ids = {str(row[0]) for row in expenses if row and row[0]}
    expense_links = {str(row[10]) for row in expenses if len(row) > 10 and row[10]}
    holds = {}
    for tx in selected:
        identity = tx.source_row_identity
        old = imports.get(identity, [])
        if (income_id(identity) in incomes or identity in income_links
                or expense_id(identity) in expense_ids or identity in expense_links):
            holds[identity] = "saved_financial_posting"
        elif old and not import_matches(tx, old):
            holds[identity] = "existing_import_mismatch"
        elif old and (old[0].target_id or old[0].status in {"bank_income", "auto_expense"}):
            holds[identity] = "posting_classification_conflict"
        elif old and old[0].status != "bank_non_expense":
            holds[identity] = "unresolved_import_status"
    return holds
