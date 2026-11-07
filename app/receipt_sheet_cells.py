"""Proposed ordinary-cell editor, synthetic only. No Sheet mutations.

One case row contains repeated product column groups and empty addition slots.
_item_id/_binding are machine fields, never human input. Original evidence is
the existing saved candidate; 'confirmed' is only an input statement.
"""
from copy import deepcopy
import math
import re

from .page_receipt_model import digest


def operations_from_cells(draft, originals, categories):
    if set(draft) != {"case_id", "source", "items"} or not isinstance(draft["items"], list) or len(draft["items"]) > 100:
        raise ValueError("receipt_cells_shape_invalid")
    binding = digest([draft["case_id"], draft["source"]])
    baseline = {item["item_id"]: item for item in originals}
    seen, operations = set(), []
    fields = {"_item_id", "_binding", "name", "quantity", "amount", "category", "kind", "target", "mode", "confirmed"}
    for cell in draft["items"]:
        if (not isinstance(cell, dict) or set(cell) != fields or cell["_binding"] != binding
                or cell["_item_id"] in seen or cell["mode"] not in {"採用", "除外"}
                or type(cell["confirmed"]) is not bool):
            raise ValueError("receipt_cells_identity_invalid")
        key = cell["_item_id"]
        seen.add(key)
        old = baseline.get(key)
        if old is None and not re.fullmatch(r"new:[A-Za-z0-9_-]{8,64}", key):
            raise ValueError("receipt_cells_item_invalid")
        if (old is None and cell["name"] == "" and cell["mode"] == "採用"
                and all(cell[f] in (None, "") for f in ("quantity", "amount", "target"))):
            continue  # A still-empty addition group is not a fictitious item.
        evidence = "原本照合済み（入力者の申告・本人認証ではない）"
        if cell["mode"] == "除外":
            if old is None or not cell["confirmed"]:
                raise ValueError("receipt_cells_remove_unconfirmed")
            operations.append(dict(action="remove", item_id=key, evidence=evidence))
            continue
        quantity = cell["quantity"]
        if (not isinstance(cell["name"], str) or not cell["name"].strip()
                or type(cell["amount"]) is not int or type(quantity) not in (int, float)
                or not math.isfinite(quantity) or quantity <= 0
                or tuple(cell["category"] or ()) not in set(map(tuple, categories))):
            raise ValueError("receipt_cells_value_invalid")
        target = cell["target"]
        if target:
            # Dropdown shows 商品1/商品2; the adapter supplies a short slot number.
            if type(target) is not int or not 1 <= target <= len(draft["items"]):
                raise ValueError("receipt_cells_target_invalid")
            target = draft["items"][target - 1]["_item_id"]
        values = dict(name=cell["name"].strip(), quantity=int(quantity) if float(quantity).is_integer() else quantity,
                      amount=cell["amount"], category=list(cell["category"]), target_item_id=target)
        if old:
            if cell["kind"] != old["kind"]:
                raise ValueError("receipt_cells_original_kind_changed")
            changes = {k: v for k, v in values.items()
                       if v != (list(old[k]) if k == "category" and old[k] else old.get(k, ""))}
            if not changes:
                continue
            op = dict(action="edit", item_id=key, changes=changes, evidence=evidence)
        else:
            op = dict(action="add", item_id=key, kind=cell["kind"], **values, evidence=evidence)
        if not cell["confirmed"]:
            raise ValueError("receipt_cells_original_check_required")
        operations.append(op)
    if not set(baseline) <= seen:
        raise ValueError("receipt_cells_missing_original_not_explicitly_excluded")
    return deepcopy(operations)
