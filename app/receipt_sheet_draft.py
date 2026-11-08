"""Synthetic one-cell draft format. Not installed in any workbook.

Header identity travels WITH edits to detect column-only sorts. Amount is the
line total, never unit price. Empty edit fields retain the existing value.
"""
import re
from .page_receipt_model import digest
from .receipt_sheet_cells import NAME_SPACE, receipt_name, receipt_quantity


def parse_draft(text):
    if not isinstance(text, str) or len(text) > 40000:
        raise ValueError("receipt_draft_invalid")
    lines = [line.split("｜") for line in text.split("\n") if line.strip(NAME_SPACE)]
    lines = [[cell.strip(NAME_SPACE) for cell in line] for line in lines]
    if (len(lines) < 3 or len(lines) > 103 or len(lines[0]) != 2 or lines[0][0] != "案件"
            or len(lines[1]) != 5 or lines[1][0] != "原本"
            or len(lines[2]) != 2 or lines[2][0] != "基準"):
        raise ValueError("receipt_draft_header_invalid")
    _, sid, content_hash, version, mime = lines[1]
    if (not lines[0][1] or not re.fullmatch("[A-Za-z0-9_-]{10,128}", sid)
            or not re.fullmatch("[a-f0-9]{64}", content_hash) or not version
            or mime not in {"image/png", "image/jpeg", "image/heic", "image/heif"}
            or not re.fullmatch("[a-f0-9]{64}", lines[2][1])):
        raise ValueError("receipt_draft_identity_invalid")
    ops = []
    for row in lines[3:]:
        if len(row) != 10:
            raise ValueError("receipt_draft_columns_invalid")
        action, key, name, quantity, amount, kind, major, minor, target, evidence = row
        if action not in {"変更", "追加", "除外"} or not key or len(evidence) < 4:
            raise ValueError("receipt_draft_operation_invalid")
        op = dict(action={"変更": "edit", "追加": "add", "除外": "remove"}[action],
                  item_id=key, evidence=evidence)
        if action == "除外":
            if any(row[2:9]):
                raise ValueError("receipt_draft_remove_invalid")
        else:
            fields = {}
            if name:
                fields["name"] = receipt_name(name)
            if quantity:
                if not re.fullmatch(r"[0-9]{1,7}(\.[0-9]{1,3})?", quantity) or float(quantity) <= 0:
                    raise ValueError("receipt_draft_quantity_invalid")
                fields["quantity"] = receipt_quantity(float(quantity))
            if amount:
                if not re.fullmatch(r"-?[0-9]{1,8}", amount):
                    raise ValueError("receipt_draft_amount_invalid")
                fields["amount"] = int(amount)
            if bool(major) != bool(minor):
                raise ValueError("receipt_draft_category_invalid")
            if major:
                fields["category"] = [major, minor]
            if target:
                fields["target_item_id"] = target
            if action == "変更":
                if kind or not fields:
                    raise ValueError("receipt_draft_edit_invalid")
                op["changes"] = fields
            else:
                if not {"name", "quantity", "amount", "category"} <= fields.keys() or kind not in {"product", "tax", "discount"}:
                    raise ValueError("receipt_draft_add_invalid")
                op.update(fields, kind=kind)
        ops.append(op)
    snapshot = dict(case_id=lines[0][1], source=dict(source_id=sid, sha256=content_hash,
        version=version, mime_type=mime, classification="normal", transaction_kind="purchase"),
        context_digest=lines[2][1], operations=ops)
    snapshot["digest"] = digest(snapshot)
    return snapshot
