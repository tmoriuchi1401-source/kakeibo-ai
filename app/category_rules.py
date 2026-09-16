"""Conservative, operator-approved category rules.

This module deliberately has no fuzzy matching.  A rule is a compact record in
one Sheet tab and is useful only after an operator has classified an expense.
The default callers leave it disabled; these helpers remain pure so that a
future UI can be enabled and audited independently of the import pipelines.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .reconciliation import ImportTransaction

RULE_SHEET = "カテゴリ自動分類ルール"
RULE_HEADERS = [
    "ルールID", "条件種別", "データ元", "口座別名", "請求名", "店舗名", "商品ID", "商品名", "金額",
    "大カテゴリ", "小カテゴリ", "承認元支出ID", "承認日時", "revision", "有効", "適用件数",
    "最終適用支出ID", "最終適用日時",
]
RULE_KINDS = frozenset({"service", "store_total", "product"})
RESERVED_ITEM_NAMES = frozenset({"", "自動計上", "手動計上", "Amazon注文", "Amazon注文合計", "注文合計"})
AGGREGATE_ITEM_NAMES = frozenset({"", "自動計上", "手動計上"})


def narrow_text(value: object) -> str:
    """Only NFKC and whitespace folding; deliberately no aliases or substrings."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip())


def bank_account_alias(import_id: str) -> str:
    parts = str(import_id).split(":", 3)
    return parts[2] if len(parts) == 4 and parts[0] == "bankpdf" else ""


def parse_timestamp(value: object) -> datetime | None:
    text = narrow_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Existing Sheets timestamps use now_jst_string() without an offset.
        # Treating them as UTC could admit a transaction nine hours before an
        # approval, so preserve that established local-time contract.
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CategoryRule:
    rule_id: str
    kind: str
    source: str
    account_alias: str
    billing_name: str
    merchant: str
    product_id: str
    product_name: str
    amount: int | None
    category: tuple[str, str]
    approved_expense_id: str
    approved_at: datetime
    revision: int
    active: bool
    row_num: int = 0

    def identity(self) -> tuple[str, ...]:
        return (
            self.kind, self.source, self.account_alias, self.billing_name,
            self.merchant, self.product_id, self.product_name,
            "" if self.amount is None else str(self.amount),
        )

    def to_row(self, *, applied_count: int = 0, last_expense_id: str = "", last_applied_at: str = "") -> list:
        return [
            self.rule_id, self.kind, self.source, self.account_alias, self.billing_name,
            self.merchant, self.product_id, self.product_name,
            "" if self.amount is None else self.amount, self.category[0], self.category[1],
            self.approved_expense_id, self.approved_at.isoformat(), self.revision,
            "TRUE" if self.active else "FALSE", applied_count, last_expense_id, last_applied_at,
        ]


def parse_rules(rows: list[list]) -> list[CategoryRule]:
    rules: list[CategoryRule] = []
    for row_num, raw in enumerate(rows, start=2):
        row = list(raw) + [""] * max(0, len(RULE_HEADERS) - len(raw))
        approved_at = parse_timestamp(row[12])
        try:
            revision = int(row[13])
            amount = None if narrow_text(row[8]) == "" else int(float(str(row[8]).replace(",", "")))
        except (TypeError, ValueError):
            continue
        if (narrow_text(row[0]) and narrow_text(row[1]) in RULE_KINDS and approved_at
                and revision > 0 and narrow_text(row[9]) and narrow_text(row[10])):
            rules.append(CategoryRule(
                narrow_text(row[0]), narrow_text(row[1]), narrow_text(row[2]), narrow_text(row[3]),
                narrow_text(row[4]), narrow_text(row[5]), narrow_text(row[6]), narrow_text(row[7]), amount,
                (narrow_text(row[9]), narrow_text(row[10])), narrow_text(row[11]), approved_at, revision,
                str(row[14]).strip().upper() in {"TRUE", "1", "YES", "ON"}, row_num,
            ))
    return rules


def valid_rule(rule: CategoryRule, categories: set[tuple[str, str]]) -> bool:
    if rule.kind not in RULE_KINDS or rule.category not in categories or not rule.source:
        return False
    if rule.kind == "service":
        return bool(rule.billing_name) and (not rule.source.startswith("bank") or bool(rule.account_alias))
    if rule.kind == "store_total":
        return bool(rule.merchant) and not rule.product_id and not rule.product_name
    return bool(rule.product_id or (rule.merchant and rule.product_name)) and rule.product_name not in RESERVED_ITEM_NAMES


def _product_subject(tx: ImportTransaction) -> tuple[str, str]:
    # Import provenance is the only product namespace currently available in
    # the shared ledger: Amazon item IDs use amazon:<order>:<ASIN> elsewhere.
    match = re.fullmatch(r"amazon:([^:]+):([^:]+)", tx.import_id)
    return (("amazon:" + match.group(2), narrow_text(tx.merchant)) if match else ("", ""))


def matches(rule: CategoryRule, tx: ImportTransaction, *, product_name: str = "") -> bool:
    if not rule.active or narrow_text(tx.source) != rule.source:
        return False
    if rule.amount is not None and abs(tx.amount) != rule.amount:
        return False
    account = bank_account_alias(tx.import_id)
    if rule.account_alias and account != rule.account_alias:
        return False
    merchant = narrow_text(tx.merchant)
    product_id, _ = _product_subject(tx)
    if rule.kind == "service":
        return merchant == rule.billing_name
    if rule.kind == "store_total":
        return merchant == rule.merchant and not product_id
    if rule.product_id:
        return product_id == rule.product_id
    # A caller with item provenance may opt in to this exact second form.  An
    # import row alone has no item name, so it remains ineligible rather than
    # degrading into a merchant-wide rule.
    return bool(product_name) and merchant == rule.merchant and narrow_text(product_name) == rule.product_name


@dataclass(frozen=True)
class RuleMatch:
    state: str  # no_match / matched / conflict / held
    rule: CategoryRule | None = None


def match_transaction(rules: list[CategoryRule], tx: ImportTransaction,
                      categories: set[tuple[str, str]], *, product_name: str = "") -> RuleMatch:
    imported_at = parse_timestamp(tx.imported_at)
    candidates = [rule for rule in rules if valid_rule(rule, categories) and matches(rule, tx, product_name=product_name)]
    if not candidates:
        return RuleMatch("no_match")
    if imported_at is None or any(imported_at <= rule.approved_at for rule in candidates):
        return RuleMatch("held")
    categories_found = {rule.category for rule in candidates}
    if len(categories_found) != 1:
        return RuleMatch("conflict")
    return RuleMatch("matched", max(candidates, key=lambda rule: (rule.revision, rule.rule_id)))


def rule_id_for(rule: CategoryRule) -> str:
    payload = "\x1f".join(rule.identity() + rule.category).encode("utf-8")
    return "CR-" + hashlib.sha256(payload).hexdigest()[:20]
