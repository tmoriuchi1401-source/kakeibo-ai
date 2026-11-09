"""Offline 46-column row decoder. No IO, acceptance, authority or Sheet writes.

expected is independently supplied case data, NOT a catalog read from the row.
The Gate B machine vector is pinned in full; its final token stays opaque.
Value-only copies with unchanged identity cannot be distinguished from edits.
"""
from copy import deepcopy
import math
import re

from .page_receipt_model import digest
from .receipt_sheet_cells import operations_from_cells, receipt_name, receipt_quantity

KINDS = {"商品": "product", "外税": "tax", "値引き": "discount"}
TARGETS = {"商品1": 1, "商品2": 2, "商品3": 3}
SOURCE_FIELDS = {"source_id", "sha256", "version", "mime_type", "classification", "transaction_kind"}


def _blank(value):
    return value is None or type(value) is str and value == ""


def _yen(value):
    # Sheets native NUMBER may decode to int or float, but never parse text.
    if (type(value) not in (int, float) or abs(value) > 2**53 - 1
            or not math.isfinite(value) or int(value) != value):
        raise ValueError("receipt_row_integer_yen_required")
    return int(value)


def decode_receipt_row(row, *, expected):
    """Return an ordinary-cell candidate for operations_from_cells/JS snapshot.

    Gate B slots: A:I, three 9-cell products, then the exact AK:AT vector.
    AR/AS/AT are observation metadata; none becomes a request or context.
    Free text/formula captions in C/G/H and the checkbox I confer no authority.
    """
    if not isinstance(row, list) or len(row) != 46:
        raise ValueError("receipt_row_46_columns_required")
    if any(type(v) not in (str, int, float, bool, type(None))
           or type(v) is float and not math.isfinite(v) for v in row):
        raise ValueError("receipt_row_scalar_values_required")
    if any(type(row[i]) not in (str, type(None)) for i in (2, 6, 7)):
        raise ValueError("receipt_row_text_metadata_invalid")
    if type(row[8]) not in (str, bool, type(None)):
        raise ValueError("receipt_row_registration_display_invalid")
    required = {"case_id", "source", "original_items", "categories", "context_digest",
                "machine", "receipt_total", "kind_label"}
    if not isinstance(expected, dict) or set(expected) != required:
        raise ValueError("receipt_row_expected_binding_required")
    source, machine = expected["source"], expected["machine"]
    if (not isinstance(source, dict) or set(source) != SOURCE_FIELDS
            or source["classification"] != "normal" or source["transaction_kind"] != "purchase"
            or not isinstance(source["source_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", source["source_id"])
            or type(source["version"]) is not str or not source["version"]
            or source["mime_type"] not in {"image/png", "image/jpeg", "image/heic", "image/heif"}
            or type(source["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])):
        raise ValueError("receipt_row_source_binding_invalid")
    if (not isinstance(machine, list) or len(machine) != 10
            or any(type(v) is not str or not v for v in machine)
            or machine[:4] != [expected["case_id"], source["source_id"], source["version"], source["sha256"]]
            or not re.fullmatch(r"[a-f0-9]{64}", machine[9])
            or row[36:46] != machine or row[0] != expected["case_id"]
            or type(expected["kind_label"]) is not str or not expected["kind_label"]
            or row[1] != expected["kind_label"]
            or type(expected["context_digest"]) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", expected["context_digest"])):
        raise ValueError("receipt_row_machine_binding_mismatch")
    ids, originals = machine[4:7], expected["original_items"]
    if (not isinstance(originals, list) or not 1 <= len(originals) <= 3
            or any(not isinstance(i, dict) or type(i.get("item_id")) is not str or not i["item_id"] for i in originals)):
        raise ValueError("receipt_row_original_items_invalid")
    old_ids = [item["item_id"] for item in originals]
    if (len(set(ids)) != 3 or len(set(old_ids)) != len(old_ids) or not set(old_ids) <= set(ids)
            or any(key not in old_ids and not re.fullmatch(r"new:[A-Za-z0-9_-]{8,64}", key) for key in ids)):
        raise ValueError("receipt_row_item_identity_invalid")
    categories = expected["categories"]
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(c, (list, tuple)) or len(c) != 2
                   or any(type(x) is not str or not x for x in c) for c in categories)):
        raise ValueError("receipt_row_category_policy_invalid")
    categories = [list(c) for c in categories]
    total = _yen(row[3])
    if type(expected["receipt_total"]) is not int or total <= 0 or total != expected["receipt_total"]:
        raise ValueError("receipt_row_receipt_total_mismatch")
    binding, items = digest([expected["case_id"], source]), []
    for slot, key in enumerate(ids):
        name, qty, amount, major, minor, kind, target, mode, confirmed = row[9+slot*9:18+slot*9]
        no_target = _blank(target) or target == "指定なし"
        empty = key not in old_ids and _blank(name) and _blank(qty) and _blank(amount) and no_target
        category_empty = _blank(major) and _blank(minor)
        if not category_empty and [major, minor] not in categories:
            raise ValueError("receipt_row_category_mismatch")
        if empty:
            if (not (_blank(kind) or kind == "商品") or not (_blank(mode) or mode == "採用")
                    or not (_blank(confirmed) or type(confirmed) is bool and confirmed is False)):
                raise ValueError("receipt_row_empty_addition_invalid")
            # An entirely unused slot remains inert, preserving physical targets.
            items.append(dict(_item_id=key, _binding=binding, name="", quantity="", amount="",
                              category=None if category_empty else [major, minor], kind="product",
                              target="", mode="採用", confirmed=False))
            continue
        if (category_empty or kind not in KINDS or mode not in {"採用", "除外"}
                or type(confirmed) is not bool or not no_target and target not in TARGETS):
            raise ValueError("receipt_row_product_controls_invalid")
        items.append(dict(_item_id=key, _binding=binding, name=receipt_name(name),
                          quantity=receipt_quantity(qty), amount=_yen(amount), category=[major, minor],
                          kind=KINDS[kind], target="" if no_target else TARGETS[target],
                          mode=mode, confirmed=confirmed))
    for item in items:
        if item["target"]:
            target = items[item["target"]-1]
            if (item["kind"] == "product" or not target["name"]
                    or target["kind"] != "product" or target["mode"] != "採用"):
                raise ValueError("receipt_row_target_identity_invalid")
    draft = dict(case_id=expected["case_id"], source=deepcopy(source), items=items)
    operations_from_cells(draft, originals, categories)  # Existing identity/confirmation checks.
    computed = sum(i["amount"] for i in items if i["name"] and i["mode"] == "採用")
    difference = total - computed
    if (_yen(row[4]), _yen(row[5])) != (computed, difference):
        raise ValueError("receipt_row_display_calculation_mismatch")
    return dict(case_id=expected["case_id"], source=deepcopy(source), draft=draft,
                original_items=deepcopy(originals), categories=categories,
                context_digest=expected["context_digest"], receipt_total=total,
                corrected_total=computed, difference=difference,
                note=row[7], original_link=row[2])


def decode_receipt_rows(rows, *, catalog):
    """Resolve by case identity, never row number; reject duplicate cases/sources."""
    if not isinstance(rows, list) or not isinstance(catalog, dict):
        raise ValueError("receipt_rows_invalid")
    output, sources = {}, set()
    for row in rows:
        if (not isinstance(row, list) or len(row) != 46 or type(row[0]) is not str
                or row[0] in output or row[0] not in catalog):
            raise ValueError("receipt_rows_case_missing_or_duplicate")
        candidate = decode_receipt_row(row, expected=catalog[row[0]])
        sid = candidate["source"]["source_id"]
        if sid in sources:
            raise ValueError("receipt_rows_source_duplicate")
        sources.add(sid)
        output[row[0]] = candidate
    return output
