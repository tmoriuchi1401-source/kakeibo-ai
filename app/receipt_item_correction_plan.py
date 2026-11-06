"""Read-only, source-bound preview of human item corrections for held receipts.

NOT a writer or a posting grant. No Sheets mutation, authority update, or
workflow dispatch occurs here. The authorized runner must independently
re-read original evidence and validate the full final accounting plan.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import math
import re

from .category_rules import match_transaction
from .models import ReceiptItem, ReceiptResult
from .receipt_review_snapshot import verified_candidate_range, yen
from .receipt_validation import validate_receipt_result

ADDED_ID = re.compile(r"^new:[a-zA-Z0-9_-]{8,64}$")
FORBIDDEN_ADJUSTMENT = re.compile(r"差額補正|差額調整|架空|帳尻|^調整$")
CATEGORY_SEPARATOR = "｜"
FALLBACK = ("その他", "未分類")


def _category(value):
    if isinstance(value, str):
        parts = value.split(CATEGORY_SEPARATOR)
    elif isinstance(value, (tuple, list)):
        parts = value
    else:
        return None
    if len(parts) != 2 or not all(isinstance(part, str) and part.strip() for part in parts):
        return None
    return tuple(part.strip() for part in parts)


def _amount(value):
    if type(value) is not int or not -99_999_999 <= value <= 99_999_999:
        raise ValueError("item_amount_invalid")
    return value


def _quantity(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("item_quantity_invalid")
    return value


def _valid_item(item):
    if not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > 200:
        raise ValueError("item_name_invalid")
    if FORBIDDEN_ADJUSTMENT.search(item["name"]):
        raise ValueError("fabricated_difference_adjustment_forbidden")
    _amount(item["amount"])
    _quantity(item["quantity"])
    if item["kind"] not in {"product", "tax", "discount"}:
        raise ValueError("item_kind_invalid")
    if (item["kind"] == "discount" and item["amount"] >= 0
            or item["kind"] != "discount" and item["amount"] <= 0):
        raise ValueError("item_sign_invalid")


def _original_rows(tx, indexed_rows):
    verified = verified_candidate_range(tx, indexed_rows)
    if verified is None:
        raise ValueError("receipt_candidate_snapshot_invalid")
    selected = [list(row) for _, row in indexed_rows
                if len(row) > 1 and row[1] == tx.import_id]
    original = []
    for row in selected:
        qty = float(str(row[5])) if row[5] not in ("", None) else 1
        amount = yen(row[6])
        if amount is None:
            raise ValueError("receipt_candidate_amount_invalid")
        name = str(row[4])
        kind = ("discount" if amount < 0 else
                "tax" if re.search("外税|消費税|税額", name) else "product")
        original.append({
            "item_id": str(row[0]), "name": name, "quantity": qty,
            "amount": amount, "category": _category([row[7], row[8]]),
            "kind": kind, "target_item_id": "",
        })
    return original, verified[2]


def preview_item_corrections(tx, indexed_rows, operations, categories, *,
                             approved_product_rules=(), total_confirmed=False):
    """Build a NON-POSTING item-level preview from the exact rejected snapshot.

    Operations: edit {item_id, changes, evidence}; remove {item_id,evidence};
    add {item_id:'new:<nonce>', name, quantity, amount, kind, category,evidence}.
    Evidence is an operator's statement, not proof of the printed original:
    independent original/HGA and owner confirmation remain mandatory.
    """
    if tx.source != "receipt" or not (tx.status == "要確認" or
                                      tx.status.startswith("needs_review")):
        raise ValueError("receipt_not_held_for_review")
    if not isinstance(operations, list) or len(operations) > 100:
        raise ValueError("correction_operations_invalid")
    valid_categories = set(map(tuple, categories))
    original, original_total = _original_rows(tx, indexed_rows)
    if tx.amount <= 0 or original_total == tx.amount:
        raise ValueError("receipt_not_mismatched")
    # Candidate rows are identity/hash checked by verified_candidate_range.
    items = {item["item_id"]: item for item in original}
    order = list(items)
    touched = set()
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("correction_operation_invalid")
        action = operation.get("action")
        key = operation.get("item_id")
        if (not isinstance(key, str) or not isinstance(operation.get("evidence"), str)
                or len(operation["evidence"].strip()) < 4 or key in touched):
            raise ValueError("correction_evidence_or_identity_invalid")
        touched.add(key)
        if action == "edit":
            if key not in items or not isinstance(operation.get("changes"), dict):
                raise ValueError("correction_edit_target_invalid")
            changes = operation["changes"]
            if not changes or set(changes) - {"name", "quantity", "amount", "category", "target_item_id"}:
                raise ValueError("correction_edit_fields_invalid")
            items[key].update(changes)
        elif action == "remove":
            if key not in items or set(operation) != {"action", "item_id", "evidence"}:
                raise ValueError("correction_remove_target_invalid")
            del items[key]
            order.remove(key)
        elif action == "add":
            if (not ADDED_ID.fullmatch(key) or key in items or
                    set(operation) != {"action", "item_id", "name", "quantity", "amount",
                                       "kind", "category", "evidence"}):
                raise ValueError("correction_add_fields_invalid")
            items[key] = {field: operation[field] for field in
                          ("item_id", "name", "quantity", "amount", "kind", "category")}
            items[key]["target_item_id"] = ""
            order.append(key)
        else:
            raise ValueError("correction_action_invalid")

    issues = []
    parsed_items = []
    resolved = []
    for key in order:
        item = deepcopy(items[key])
        _valid_item(item)
        supplied = _category(item["category"])
        if item["category"] not in (None, "") and supplied is None:
            raise ValueError("item_category_invalid")
        category = supplied if supplied in valid_categories else None
        if supplied and category is None:
            issues.append("category_not_in_master:" + key)
        if item["kind"] == "product" and approved_product_rules:
            # Store-total/service rules never classify individual products.
            tx_item = replace(tx, amount=item["amount"])
            match = match_transaction(
                [r for r in approved_product_rules if r.kind == "product"],
                tx_item, valid_categories, product_name=item["name"],
                aggregate_only=False,
            )
            if match.state == "conflict":
                issues.append("category_rule_conflict:" + key)
            elif match.state == "matched":
                if category and category != match.rule.category and key not in touched:
                    issues.append("candidate_rule_conflict:" + key)
                elif not category:
                    category = match.rule.category
        if category is None and supplied is None and FALLBACK in valid_categories and item["kind"] == "product":
            category = FALLBACK
        if category is None:
            issues.append("category_unresolved:" + key)
        item["category"] = category
        resolved.append(item)
        if category is not None:
            parsed_items.append(ReceiptItem(
                name=item["name"], quantity=item["quantity"], amount=item["amount"],
                major_category=category[0], minor_category=category[1],
                note="correction_preview_only",
            ))

    product_categories = {tuple(i["category"]) for i in resolved
                          if i["kind"] == "product" and i["category"] is not None}
    for item in resolved:
        if item["kind"] not in ("tax", "discount"):
            continue
        target = item.get("target_item_id", "")
        if target:
            product = items.get(target)
            if (not product or product["kind"] != "product" or
                    item["category"] != product["category"]):
                issues.append("tax_discount_target_conflict:" + item["item_id"])
        elif len(product_categories) > 1:
            issues.append("tax_discount_allocation_review:" + item["item_id"])

    item_sum = sum(item["amount"] for item in resolved)
    difference = tx.amount - item_sum
    if not resolved:
        issues.append("no_items")
    if difference:
        issues.append("item_total_mismatch")
    if len(parsed_items) == len(resolved) and resolved and difference == 0:
        try:
            parsed = ReceiptResult(merchant=tx.merchant, date=tx.date, total=tx.amount,
                                   items=parsed_items, payment_method=str(tx.row[7]))
            _, validation = validate_receipt_result(parsed, valid_categories)
            issues.extend(validation)
        except (TypeError, ValueError):
            issues.append("receipt_result_invalid")
    if not total_confirmed:
        issues.append("receipt_total_owner_confirmation_required")
    issues = list(dict.fromkeys(issues))
    canonical = json.dumps({
        "import_id": tx.import_id, "source_hash": tx.row[10],
        "receipt_total": tx.amount, "items": resolved, "operations": operations,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "status": "preview_balanced" if not issues else "needs_review",
        "receipt_total": tx.amount, "original_item_total": original_total,
        "item_total": item_sum, "difference": difference, "items": resolved,
        "issues": issues, "snapshot_digest": sha256(canonical.encode()).hexdigest(),
        "posting_authority": False, "financial_write_count": 0,
    }
