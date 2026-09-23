"""Fill only unclassified history across all months from a submitted checkbox."""
from datetime import datetime, timezone
from .auto_expense import FALLBACK_CATEGORY
from .category_backfill import BackfillSpec, CategoryBackfillPipeline
from .category_rules import CategoryRule, bank_account_alias, narrow_text
from .category_rule_choices import checked
from .category_ui_order import proof
from .reconciliation import parse_import_rows

PAST_HEADER = "過去分にも反映（全月）"
LEGACY_PAST_HEADER = "過去分の候補に追加"


def identity(data):
    return tuple(narrow_text(data.get(k)) for k in ("kind", "source", "account_alias")) + (
        narrow_text(data.get("billing_name") or data.get("merchant")),)


def ledger_inputs(db):
    return (db.expense_records(),
            {tx.import_id:tx for tx in parse_import_rows(db.get("取込データ!A2:L"))})


def remaining(identity_key, records, transactions):
    for expense_id, (_, expense) in records.items():
        tx = transactions.get(str(expense[10]))
        if not tx or expense[12] != "active" or tx.status != "auto_expense" or tx.target_id != expense_id:
            continue
        current = ("service", narrow_text(tx.source), bank_account_alias(tx.import_id), narrow_text(tx.merchant))
        if current == identity_key and tuple(map(narrow_text, expense[5:7])) == FALLBACK_CATEGORY:
            return True
    return False


def resolved_conditions(db, records=None, transactions=None):
    """History is retained; only conditions with no remaining work disappear.

    A later unclassified import, partial apply, or restoration
    makes the condition visible again without needing a new future rule.
    """
    if not hasattr(db, "category_backfill_requests"):
        return set()
    completed = {}
    for row in db.category_backfill_requests():
        if len(row) < 4 or row[2] != "complete":
            continue
        data = proof(row[3])
        if data.get("ui_all_months") is True:
            completed[identity(data)] = tuple(data.get("category", []))
    if not completed:
        return set()
    if records is None or transactions is None:
        records, transactions = ledger_inputs(db)
    return {key for key, category in completed.items() if len(category) == 2
            and not remaining(key, records, transactions)}


def remove_resolved(rows, resolved):
    return [row for row in rows if identity(proof(row[11])) not in resolved
            or checked(row[4]) or checked(row[5]) or "held:" in str(row[1])
            or "再承認" in str(row[1])]


def apply_checked(db):
    rows = db.category_rule_ui_rows()
    selected = [r for r in rows if len(r) >= 12 and checked(r[5])]
    choices = {}
    for row in selected:
        choices.setdefault(identity(proof(row[11])), set()).add(tuple(row[2:4]))
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True)
    results = {}; cache = {}; updates = []
    for number, row in enumerate(rows, start=2):
        if len(row) < 12 or not checked(row[5]):
            continue
        data = proof(row[11]); key = identity(data); category = tuple(row[2:4])
        if key not in results:
            if len(choices[key]) != 1:
                result = {"state":"held", "reason":"conflicting_all_month_categories"}
            elif (data.get("kind") != "service" or list(category) != data.get("category")
                  or not all(category) or category == FALLBACK_CATEGORY or not key[1] or not key[3]):
                result = {"state":"held", "reason":"invalid_all_month_condition"}
            else:
                rule = CategoryRule("adhoc", "service", key[1], key[2], key[3], "", "", "", None,
                                    category, "", datetime.now(timezone.utc), 1, True)
                result = pipe.preview(BackfillSpec(rule, ui_all_months=True),
                                      display_read_cache=cache)
                if result.get("state") == "previewed":
                    request, count = result["request_id"], result["targets"]
                    result = pipe.confirm(request, expected_count=count)
                    if result.get("state") == "confirmed":
                        result = pipe.apply(request, expected_count=count)
                if result.get("state") == "complete":
                    records, transactions = ledger_inputs(db)
                    if remaining(key, records, transactions):
                        result = dict(result, state="held", reason="all_month_rows_remain")
            results[key] = result
        result = results[key]
        row[5] = False
        row[1] = "\n".join(line for line in str(row[1]).split("\n")
                           if not line.startswith("held: all_month:"))
        row[1] += "\n" + ("過去分: 全月反映済み" if result.get("state") == "complete"
                           else "held: all_month: " + result.get("reason", "partial"))
        updates.append((number, row))
    if updates:
        db.update_rows("カテゴリ自動分類", updates)
    return list(results.values())
