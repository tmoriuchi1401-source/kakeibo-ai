"""Confirmed income ledger; Payroll never participates in this ledger.

The schema is intentionally separate from SheetsDB.ensure_schema so deploying
this module cannot create a production sheet. Recurring entry points are preview-only.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import hashlib
import re
import unicodedata

from .bank_pdf_pipeline import (
    SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE, NormalizedBankTransaction,
)
from .bank_reconciliation import classify_bank_transaction, normalize_bank_description
from .reconciliation import parse_import_rows
from .transaction_plan import resolve_transaction_identities

INCOME_SHEET = "収入明細"
INCOME_HEADERS = [
    "収入ID", "入金日", "金額", "分類", "入金元", "口座alias", "元取込ID",
    "データ元", "判定根拠", "元データハッシュ",
]
BANK_NAMESPACES = {SOURCE: "au-jibun", DOCOMO_SMTB_SOURCE: "docomo-smtb", CHIBA_BANK_SOURCE: "chiba"}
OUTCOMES = ("confirmed_income", "transfer", "reimbursement", "other_non_income", "needs_review")
INCOME_CATEGORIES = {"給与", "賞与", "利息", "その他確認済収入"}
INCOME_REASONS = {"bank_salary_type", "bank_bonus_type", "bank_interest_type", "operator_confirmed_income"}
RECEIPT_BUYBACK_SOURCE = "receipt_buyback"
RECEIPT_BUYBACK_REASON = "receipt_explicit_buyback"


def income_id(import_id: str) -> str:
    # Same stable source-ID hash convention as auto_expense.expense_id.
    return "I-" + hashlib.sha256(import_id.encode("utf-8")).hexdigest()[:24]


def _alias(identity: str, source: str) -> str:
    match = re.fullmatch(r"bankpdf:([^:]+):([a-z0-9][a-z0-9_-]{1,63}):([0-9a-f]{24})(?::[0-9]{3})?", identity)
    return match[2] if match and BANK_NAMESPACES.get(source) == match[1] else ""


def _valid_transaction(tx: NormalizedBankTransaction) -> bool:
    try:
        valid_date = date.fromisoformat(tx.transaction_date).isoformat() == tx.transaction_date
    except (ValueError, TypeError):
        return False
    return bool(
        valid_date and tx.signed_amount > 0
        and _alias(tx.source_row_identity, tx.source) == tx.account_alias
        and tx.account_alias and tx.description.strip()
        and re.fullmatch(r"[0-9a-f]{64}", tx.source_row_hash)
        and tx.source_row_identity.split(":")[3] == tx.source_row_hash[:24]
        and tx.review_status == "accepted"
        and tx.transaction_kind == "deposit"
    )


@dataclass(frozen=True)
class IncomeDecision:
    transaction: NormalizedBankTransaction
    outcome: str
    reason: str
    category: str = ""

    def row(self) -> list:
        if self.outcome != "confirmed_income" or not _valid_transaction(self.transaction):
            raise RuntimeError("bank_income_not_confirmed")
        tx = self.transaction
        return [income_id(tx.source_row_identity), tx.transaction_date, tx.signed_amount,
                self.category, tx.description, tx.account_alias, tx.source_row_identity,
                tx.source, self.reason, tx.source_row_hash]


def classify_deposit(tx, *, confirmed_internal_transfers=frozenset(),
                     confirmed_non_own_classifications=frozenset()) -> IncomeDecision:
    """Explicit private rules or bank transaction types, never an income label.

    Name-only transfers, broad reward keywords and positive direction do not
    establish household income. Existing exact ownership/non-own rules are reused.
    """
    if not _valid_transaction(tx):
        return IncomeDecision(tx, "needs_review", "invalid_bank_deposit")
    description = normalize_bank_description(tx.description)
    key = (description, "incoming", tx.account_alias)
    rules = {kind for text, direction, alias, kind in confirmed_non_own_classifications
             if (text, direction, alias) == key}
    if key in confirmed_internal_transfers:
        rules.add("transfer")
    if len(rules) > 1:
        return IncomeDecision(tx, "needs_review", "conflicting_confirmed_rules")
    if rules:
        kind = next(iter(rules))
        if kind == "transfer":
            return IncomeDecision(tx, "transfer", "confirmed_internal_transfer")
        # Reuse the established rule evaluator; no second private rule format.
        result = classify_bank_transaction(tx,
            confirmed_internal_transfers=confirmed_internal_transfers,
            confirmed_non_own_classifications=confirmed_non_own_classifications)
        outcome = {"income": "confirmed_income", "reimbursement": "reimbursement",
                   "other_nonwrite": "other_non_income"}.get(result.classification, "needs_review")
        category = "その他確認済収入" if outcome == "confirmed_income" else ""
        if outcome == "confirmed_income" and description in {"利息", "普通預金利息", "リソク"}:
            category = "利息"
        return IncomeDecision(tx, outcome, result.reason, category)
    if any(word in description for word in ("返金", "返品", "払戻", "立替", "精算", "REFUND")):
        return IncomeDecision(tx, "reimbursement", "explicit_refund_or_reimbursement")
    if any(word in description for word in ("借入", "融資", "ローン実行", "貸付")):
        return IncomeDecision(tx, "other_non_income", "explicit_borrowing")
    # Keep the bank's leading transaction type and delimiter; employer names
    # or a substring such as 給与振込特典 cannot masquerade as a salary.
    typed = unicodedata.normalize("NFKC", tx.description).strip()
    match = re.match(r"^(給与|賞与)(?:$|[\s*])", typed)
    if match:
        return IncomeDecision(tx, "confirmed_income", "bank_salary_type" if match[1] == "給与" else "bank_bonus_type", match[1])
    if description in {"利息", "普通預金利息", "リソク"}:
        return IncomeDecision(tx, "confirmed_income", "bank_interest_type", "利息")
    return IncomeDecision(tx, "needs_review", "deposit_purpose_unconfirmed")


def deposits_from_imports(rows: list[list]) -> list[NormalizedBankTransaction]:
    deposits = []
    # Connectors may represent empty cells as null; Sheets values.get uses "".
    for tx in parse_import_rows([["" if value is None else value for value in row] for row in rows]):
        if tx.source not in BANK_NAMESPACES or tx.amount <= 0:
            continue
        raw_amount = str(tx.row[6]).replace(",", "")
        valid = bool(re.fullmatch(r"[1-9][0-9]*", raw_amount))
        # A link to an expense or an excluded/review status cannot be silently
        # converted to income. The status only withholds; it never confirms.
        withheld = bool(tx.target_id) or tx.status != "bank_income"
        deposits.append(NormalizedBankTransaction(
            source=tx.source, account_alias=_alias(tx.import_id, tx.source),
            transaction_date=tx.date, description=tx.merchant, signed_amount=tx.amount,
            source_page=0, source_row=tx.row_num, source_row_identity=tx.import_id,
            source_row_hash=str(tx.row[10] or ""), transaction_kind="deposit",
            review_status="accepted" if valid and not withheld else "needs_review",
        ))
    return deposits


def deposit_decisions(transactions, **rules) -> tuple[list[IncomeDecision], int]:
    positive = [tx for tx in transactions if tx.source in BANK_NAMESPACES and tx.signed_amount > 0]
    resolution = resolve_transaction_identities(
        [tx.to_canonical() for tx in positive],
        signature=lambda tx: (tx.source, tx.transaction_date, tx.merchant, tx.amount_yen, tx.source_hash, tx.transaction_kind),
    )
    originals = {tx.source_row_identity: tx for tx in positive}
    # Include validation state in collision checks (not page/row: PDFs overlap).
    invalid_ids = {tx.source_row_identity for tx in positive if not _valid_transaction(tx)}
    decisions = [classify_deposit(originals[tx.identity], **rules) if tx.identity not in invalid_ids
                 else IncomeDecision(originals[tx.identity], "needs_review", "invalid_bank_deposit")
                 for tx in resolution.unique]
    decisions.extend(IncomeDecision(originals[group[0].identity], "needs_review", "bank_identity_collision")
                     for group in resolution.collision_groups)
    return sorted(decisions, key=lambda d: d.transaction.source_row_identity), resolution.duplicate_count


def income_summary(decisions: list[IncomeDecision], duplicates: int = 0) -> dict:
    counts = Counter(d.outcome for d in decisions)
    return {"read_only": True, "income_write_enabled": False, "write_attempted": 0,
            "deposit_count": len(decisions), "duplicate_import_rows": duplicates,
            "classification": {name: {"count": counts[name], "amount": sum(
                d.transaction.signed_amount for d in decisions if d.outcome == name)} for name in OUTCOMES}}


def validate_income_rows(rows: list[list]) -> dict[str, list]:
    """Validate bank deposits and explicit cash buybacks by source identity."""
    by_id = {}
    for raw in rows:
        if not any(raw):
            continue
        if len(raw) != len(INCOME_HEADERS):
            raise RuntimeError("bank_income_row_schema_mismatch")
        row = list(raw)
        if not re.fullmatch(r"[1-9][0-9]*", str(row[2])):
            raise RuntimeError("bank_income_amount_invalid")
        row[2] = int(row[2])
        if row[7] == RECEIPT_BUYBACK_SOURCE:
            try:
                valid_date = date.fromisoformat(row[1]).isoformat() == row[1]
            except (ValueError, TypeError):
                valid_date = False
            valid = (valid_date and isinstance(row[4], str) and bool(row[4].strip()) and row[5] == ""
                     and bool(re.fullmatch(r"receipt:[A-Za-z0-9_-]+", row[6]))
                     and bool(re.fullmatch(r"[0-9a-f]{64}", row[9]))
                     and row[3] == "その他確認済収入"
                     and row[8] == RECEIPT_BUYBACK_REASON)
        else:
            tx = NormalizedBankTransaction(row[7], row[5], row[1], row[4], row[2], 0, 0, row[6], row[9], "deposit")
            valid = (_valid_transaction(tx) and row[3] in INCOME_CATEGORIES
                     and row[8] in INCOME_REASONS)
        if (not valid or row[0] != income_id(row[6])):
            raise RuntimeError("bank_income_provenance_invalid")
        if row[0] in by_id:
            raise RuntimeError("bank_income_duplicate_existing_id")
        by_id[row[0]] = row
    return by_id


def monthly_income(rows: list[list]) -> dict[str, int]:
    totals = Counter()
    for row in validate_income_rows(rows).values():
        totals[row[1][:7]] += row[2]
    return dict(sorted(totals.items()))


class BankIncomePipeline:
    def __init__(self, db, *, confirmed_internal_transfers=frozenset(),
                 confirmed_non_own_classifications=frozenset()):
        self.db = db
        self.rules = dict(confirmed_internal_transfers=confirmed_internal_transfers,
                          confirmed_non_own_classifications=confirmed_non_own_classifications)

    def _existing(self, *, required=False):
        if INCOME_SHEET not in self.db.sheet_titles():
            if required:
                raise RuntimeError("bank_income_schema_not_installed")
            return {}
        if self.db.get(f"{INCOME_SHEET}!A1:J1") != [INCOME_HEADERS]:
            raise RuntimeError("bank_income_header_mismatch")
        return validate_income_rows(self.db.get(f"{INCOME_SHEET}!A2:J"))

    def preview(self, selected_import_ids: tuple[str, ...] = ()) -> dict:
        decisions, duplicates = deposit_decisions(deposits_from_imports(self.db.get("取込データ!A2:L")), **self.rules)
        if selected_import_ids:
            by_id = {d.transaction.source_row_identity: d for d in decisions}
            if len(set(selected_import_ids)) != len(selected_import_ids) or not set(selected_import_ids) <= by_id.keys():
                raise RuntimeError("bank_income_selection_invalid")
            decisions = [by_id[i] for i in selected_import_ids]
        existing = self._existing()
        planned, conflicts, already = [], [], 0
        for decision in decisions:
            if decision.outcome != "confirmed_income":
                continue
            row = decision.row()
            if row[0] not in existing:
                planned.append(row)
            elif existing[row[0]] == row:
                already += 1
            else:
                conflicts.append(row[6])
        groups = {}
        for d in decisions:
            if d.outcome != "needs_review":
                continue
            tx = d.transaction
            key = (normalize_bank_description(tx.description), tx.account_alias, d.reason)
            group = groups.setdefault(key, {"description": tx.description, "account_alias": tx.account_alias,
                                           "reason": d.reason, "count": 0, "amount": 0, "import_ids": []})
            group["count"] += 1
            group["amount"] += tx.signed_amount
            group["import_ids"].append(tx.source_row_identity)
        return {**income_summary(decisions, duplicates), "planned_rows": planned,
                "planned_income_writes": len(planned), "existing_income": already,
                "conflicting_import_ids": conflicts, "review_groups": list(groups.values()),
                "monthly_planned_income": monthly_income(planned),
                "decisions": [{"import_id": d.transaction.source_row_identity,
                               "source_row": d.transaction.source_row, "outcome": d.outcome,
                               "reason": d.reason, "amount": d.transaction.signed_amount} for d in decisions]}

    def apply(self, selected_import_ids: tuple[str, ...], *, income_write_enabled=False,
              approved_spreadsheet_id="", max_rows=100) -> dict:
        """Batch writer for a separately authorized, serialized caller.

        Only the fixed manual backfill calls this in production. Re-read before
        append; never update expense links, Payroll, import status or Drive.
        """
        if income_write_enabled is not True:
            raise RuntimeError("bank_income_write_disabled")
        if not approved_spreadsheet_id or self.db.sid != approved_spreadsheet_id:
            raise RuntimeError("bank_income_target_not_approved")
        if not 1 <= max_rows <= 100 or not selected_import_ids or len(selected_import_ids) > max_rows:
            raise RuntimeError("bank_income_batch_bound_invalid")
        self._existing(required=True)
        plan = self.preview(selected_import_ids)
        if plan["conflicting_import_ids"]:
            raise RuntimeError("bank_income_existing_content_conflict")
        rows = plan["planned_rows"]
        if rows:
            self.db.append_raw(INCOME_SHEET, rows)
        actual = self._existing(required=True)
        if any(actual.get(row[0]) != row for row in rows):
            raise RuntimeError("bank_income_readback_mismatch")
        return {"read_only": False, "incomes_created": len(rows), "read_back_verified": True}
