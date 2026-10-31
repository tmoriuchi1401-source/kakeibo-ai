"""Pure aggregate projection of already classified Preview results; no I/O."""
from __future__ import annotations

GROUPS = ("new", "duplicate", "held", "excluded", "linked", "transfer", "supplement")
GAPS = ("parser_review_messages", "parser_review_lines", "rejected_transactions", "identity_collision_groups",
        "same_source_duplicates", "noncanonical_resends", "duplicate_gmail_messages",
        "money_notice_messages", "money_repeated_inputs")
REASONS = frozenset({None, "classification_amount_not_available", "resume_requires_intent_readback",
    "expense_allocation_not_in_preview", "overlapping_table_amounts_not_additive", "request_count_requires_write_readback"})


def metric(count=0, amount_yen=0, reason=None):
    if ((count is not None and (type(count) is not int or count < 0))
            or (amount_yen is not None and type(amount_yen) is not int)
            or reason not in REASONS or ((count is None or amount_yen is None) and reason is None)):
        raise RuntimeError("preview_aggregation_invalid")
    return {"count": count, "amount_yen": amount_yen, "reason": reason}


def add(*items):
    unknown = next((m["reason"] for m in items if m["count"] is None or m["amount_yen"] is None), None)
    return metric(None if any(m["count"] is None for m in items) else sum(m["count"] for m in items),
                  None if any(m["amount_yen"] is None for m in items) else sum(m["amount_yen"] for m in items), unknown)


def groups(): return {key: metric() for key in GROUPS}


def include(result, group, amount):
    result[group] = add(result[group], metric(1, amount))


def money_totals(decisions):
    """Use the SAME decisions as money Preview; never call a writer/reader."""
    result = groups()
    imports, expenses = metric(), metric()
    actions = {"post": "new", "duplicate": "duplicate", "review": "held", "resume": "held",
               "linked": "linked", "transfer": "transfer", "supplement": "supplement"}
    for decision in decisions:
        if decision.action not in actions:
            raise RuntimeError("preview_aggregation_unknown_classification")
        include(result, actions[decision.action], decision.record.amount)
        if decision.action == "post":
            imports = add(imports, metric(1, decision.record.amount))
            allocation = (metric(len(decision.expense_ids), decision.record.amount) if decision.expense_ids
                          else metric(None, None, "expense_allocation_not_in_preview"))
            expenses = add(expenses, allocation)
        elif decision.action == "resume":
            # A saved intent may be partially posted. Decision amount/IDs do
            # not identify the remaining rows; never invent a replay budget.
            imports = add(imports, metric(None, None, "resume_requires_intent_readback"))
            expenses = add(expenses, metric(None, None, "resume_requires_intent_readback"))
    return {"groups": result, "import_rows": imports, "expense_rows": expenses}


def build_totals(plan, reconciliation, statuses, collection, *, money=None,
                 money_input_count=0, money_notice_count=0):
    ordinary = groups()
    by_id = {item.canonical.identity: item.canonical for item in reconciliation["transactions"]}
    disposition = {"duplicate_existing_identity": "duplicate", "needs_review": "held",
        "withheld_cross_source_ambiguous": "held", "withheld_global_failure": "held",
        "withheld_return": "excluded", "invalid": "excluded"}
    for decision in plan.item_decisions:
        group = ("new" if decision.canonical_identity in statuses else "held") if decision.status == "eligible" else disposition.get(decision.status)
        if group is None or decision.canonical_identity not in by_id:
            raise RuntimeError("preview_aggregation_unknown_classification")
        include(ordinary, group, by_id[decision.canonical_identity].amount_yen)
    if ordinary["new"]["count"] != len(statuses):
        raise RuntimeError("preview_aggregation_inconsistent")
    money = money if money is not None else money_totals(())
    combined = {key: add(ordinary[key], money["groups"][key]) for key in GROUPS}
    gaps = dict(zip(GAPS, (collection.get("parser_review_mail_count"), collection.get("review_line_items"),
        reconciliation["summary"].get("rejected"), reconciliation["summary"].get("identity_collisions"),
        reconciliation["summary"].get("same_source_duplicates"),
        plan.noncanonical_resend_count, collection.get("duplicate_gmail_message"), money_notice_count,
        money_input_count - sum(m["count"] for m in money["groups"].values()))))
    unquantified = {name: metric(count, None, "classification_amount_not_available") if count is None or count != 0 else metric(count, 0)
                    for name, count in gaps.items()}
    imports = add(ordinary["new"], money["import_rows"])
    expenses = money["expense_rows"]
    # Transaction amount is counted once. The import and expense row amounts
    # overlap for money, so a cross-table amount sum would double-count money.
    planned = {"transactions": add(ordinary["new"], money["groups"]["new"]),
               "import_rows": imports, "expense_rows": expenses,
               "requests": metric(None, None, "request_count_requires_write_readback"),
               "all_rows": metric(None if imports["count"] is None or expenses["count"] is None
                                  else imports["count"] + expenses["count"],
                                  None, "overlapping_table_amounts_not_additive")}
    return {"schema_version": 1, "currency": "JPY", "amount_basis": "signed_integer_yen",
            "candidate_basis": "classified_canonical_transactions_and_unique_money_records",
            "candidates": add(*combined.values()), "by_classification": combined,
            "ordinary": ordinary, "money": money["groups"], "unquantified": unquantified,
            "planned_financial_writes": planned,
            "actual_financial_writes": {"request_count": 0, "row_count": 0, "amount_yen": 0}}


def public_totals(value):
    """Closed schema at the public boundary; reject details or arbitrary text."""
    def checked_metric(raw):
        if not isinstance(raw, dict) or set(raw) != {"count", "amount_yen", "reason"}:
            raise RuntimeError("preview_aggregation_invalid")
        return metric(**raw)
    expected = {"schema_version", "currency", "amount_basis", "candidate_basis", "candidates",
                "by_classification", "ordinary", "money", "unquantified",
                "planned_financial_writes", "actual_financial_writes"}
    if (not isinstance(value, dict) or set(value) != expected or type(value["schema_version"]) is not int
            or value["schema_version"] != 1 or value["currency"] != "JPY"
            or value["amount_basis"] != "signed_integer_yen"
            or value["candidate_basis"] != "classified_canonical_transactions_and_unique_money_records"):
        raise RuntimeError("preview_aggregation_invalid")
    result = {key: value[key] for key in ("schema_version", "currency", "amount_basis", "candidate_basis")}
    result["candidates"] = checked_metric(value["candidates"])
    for name, keys in (("by_classification", GROUPS), ("ordinary", GROUPS), ("money", GROUPS),
                       ("unquantified", GAPS), ("planned_financial_writes", ("transactions", "import_rows", "expense_rows", "all_rows", "requests"))):
        if not isinstance(value[name], dict) or set(value[name]) != set(keys):
            raise RuntimeError("preview_aggregation_invalid")
        result[name] = {key: checked_metric(value[name][key]) for key in keys}
    actual = value["actual_financial_writes"]
    if (not isinstance(actual, dict) or set(actual) != {"request_count", "row_count", "amount_yen"}
            or any(type(n) is not int or n != 0 for n in actual.values())):
        raise RuntimeError("preview_aggregation_invalid")
    result["actual_financial_writes"] = dict(actual)
    if (result["candidates"] != add(*result["by_classification"].values())
            or any(result["by_classification"][key] != add(result["ordinary"][key], result["money"][key]) for key in GROUPS)):
        raise RuntimeError("preview_aggregation_inconsistent")
    planned = result["planned_financial_writes"]
    if (planned["transactions"] != result["by_classification"]["new"]
            or planned["requests"] != metric(None, None, "request_count_requires_write_readback")
            or (planned["import_rows"]["reason"] != "resume_requires_intent_readback"
                and planned["import_rows"] != planned["transactions"])
            or (planned["expense_rows"]["amount_yen"] is not None
                and planned["expense_rows"]["amount_yen"] != result["money"]["new"]["amount_yen"])
            or planned["all_rows"] != metric(
                None if planned["import_rows"]["count"] is None or planned["expense_rows"]["count"] is None
                else planned["import_rows"]["count"] + planned["expense_rows"]["count"],
                None, "overlapping_table_amounts_not_additive")):
        raise RuntimeError("preview_aggregation_inconsistent")
    return result
