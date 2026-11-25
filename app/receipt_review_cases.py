"""PR-B ordinary receipt review. No accounting DB, posting, or source moves.

The hidden case catalog is immutable evidence, not editable UI. Eight ordinary
cell groups stay on the adopted single case row. Amounts are LINE amounts,
independent of quantity; included tax is never added a second time.
"""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import re
from uuid import uuid4

from .category_rules import parse_rules, valid_rule
from .page_receipt_model import digest
from .receipt_item_correction_plan import preview_item_corrections
from .receipt_review_snapshot import candidate_rows, verified_candidate_range
from .receipt_sheet_cells import operations_from_cells, receipt_quantity
from uuid import UUID

def request_id(value):
    if type(value) is not str or str(UUID(value, version=4)) != value:
        raise ValueError("request_id_invalid")
    return value

from .receipt_sheets_source import checked_source
from .reconciliation import parse_import_rows

MAX_ITEMS = 8
MACHINE_START = 9 + 9 * MAX_ITEMS
WIDTH = MACHINE_START + 4 + MAX_ITEMS + 3 + 2
SCHEMA = "receipt-review-b1"
APPROVAL_METHOD = "register_all_displayed_v1"
APPROVAL_EVIDENCE = "登録による表示内容の一括承認（原本の目視確認・本人認証を証明しない）"
LABELS = {"product": "商品", "tax": "外税", "discount": "値引き"}
VALIDATOR_REVISION = "receipt-review-amount-v2"


def source_identity(source):
    checked_source(source, allow_single_page_pdf=True)
    if not re.fullmatch(r"[1-9][0-9]*", source.get("version", "")):
        raise ValueError("source_version_required")
    return deepcopy(source)


def preview(entry, operations, *, total_confirmed=False):
    tx = replace(parse_import_rows([entry["import_row"]])[0], status="要確認")
    return preview_item_corrections(tx, list(enumerate(entry["candidate_rows"], 2)),
        operations, entry["categories"], approved_product_rules=parse_rules(entry["rules"]),
        total_confirmed=total_confirmed, general_review=True)


def check_case(entry):
    if (type(entry) is not dict or set(entry) != {"schema", "case_id", "source", "import_row",
            "candidate_rows", "categories", "rules", "request_id", "slots", "baseline_digest"}
            or entry["schema"] != SCHEMA):
        raise ValueError("case_invalid")
    s = source_identity(entry["source"])
    request_id(entry["request_id"])
    row = entry["import_row"]
    if (type(row) is not list or len(row) != 12 or entry["case_id"] != "receipt:" + s["source_id"]
            or row[:1] != [entry["case_id"]] or row[2:4] != ["receipt", s["source_id"]]
            or type(row[6]) is not int or not 0 < row[6] <= 99_999_999
            or not re.fullmatch(r"[a-f0-9]{64}", row[10])
            or type(entry["candidate_rows"]) is not list or not entry["candidate_rows"]
            or len(entry["candidate_rows"]) > 100
            or any(type(r) is not list or len(r) != 11 for r in entry["candidate_rows"])
            or type(entry["categories"]) is not list or not entry["categories"]
            or any(type(c) is not list or len(c) != 2 or any(type(x) is not str or not x for x in c)
                   for c in entry["categories"])
            or len(set(map(tuple, entry["categories"]))) != len(entry["categories"])
            or type(entry["rules"]) is not list or any(len(r) != 18 for r in entry["rules"])):
        raise ValueError("case_evidence_invalid")
    rules = parse_rules(entry["rules"])
    if len(rules) != len(entry["rules"]) or any(not valid_rule(r, set(map(tuple, entry["categories"]))) for r in rules):
        raise ValueError("case_policy_invalid")
    tx = parse_import_rows([row])[0]
    if verified_candidate_range(tx, list(enumerate(entry["candidate_rows"], 2))) is None:
        raise ValueError("case_candidate_mismatch")
    wanted = [r[0] for r in entry["candidate_rows"][:MAX_ITEMS]]
    wanted += ["new:" + digest([entry["case_id"], i])[:24] for i in range(len(wanted), MAX_ITEMS)]
    if entry["slots"] != wanted or entry["baseline_digest"] != digest({k:v for k,v in entry.items()
            if k not in {"request_id", "baseline_digest"}}):
        raise ValueError("case_binding_invalid")
    return entry


def make_case(source, import_row, candidates, categories, rules=()):
    """Use stored Drive/analysis candidates too; never infer source identity."""
    source_identity(source)
    candidates = deepcopy(candidates)
    # Sheets/JS have one number type. Normalize only integral quantities, never
    # round decimals or alter the recorded analysis hash / original evidence.
    for candidate in candidates:
        candidate[5] = receipt_quantity(candidate[5])
    cid = "receipt:" + source["source_id"]
    slots = [r[0] for r in candidates[:MAX_ITEMS]]
    slots += ["new:" + digest([cid, i])[:24] for i in range(len(slots), MAX_ITEMS)]
    entry = dict(schema=SCHEMA, case_id=cid, source=deepcopy(source), import_row=deepcopy(import_row),
        candidate_rows=deepcopy(candidates), categories=[list(c) for c in categories], rules=deepcopy(list(rules)),
        request_id=str(uuid4()), slots=slots)
    entry["baseline_digest"] = digest({k:v for k,v in entry.items() if k != "request_id"})
    check_case(entry)
    return entry


def machine(entry):
    s = entry["source"]
    return [entry["case_id"], s["source_id"], s["version"], s["sha256"], *entry["slots"],
            entry["request_id"], "1", entry["baseline_digest"]]


def make_row(entry):
    check_case(entry)
    # No silent truncation: over-capacity cases are visible but cannot register.
    originals = preview(entry, [])["original_items"]
    total = sum(i["amount"] for i in originals)
    state = "保留：商品数上限8件" if len(originals) > MAX_ITEMS else "編集中・未登録"
    row = [entry["case_id"], "一般", "https://drive.google.com/file/d/" + entry["source"]["source_id"] + "/view",
           entry["import_row"][6], total, entry["import_row"][6]-total, state, "", "ready:"+entry["request_id"]]
    for old in originals[:MAX_ITEMS]:
        row += [old["name"], receipt_quantity(old["quantity"]), old["amount"], *(old["category"] or ["", ""]),
                LABELS[old["kind"]], "指定なし", "採用", False]
    for _ in range(len(originals), MAX_ITEMS):
        row += ["", "", "", "", "", "商品", "指定なし", "採用", False]
    row += machine(entry) + ["不明", False]  # tax handling, source-total confirmation
    assert len(row) == WIDTH
    return row


def snapshot_from_row(row, entry, *, approval_method=APPROVAL_METHOD):
    """Capture registration approval; None only rebuilds pre-existing snapshots."""
    check_case(entry)
    if approval_method not in (None, APPROVAL_METHOD):
        raise ValueError("approval_method_invalid")
    if (type(row) is not list or len(row) != WIDTH or row[0] != entry["case_id"] or row[1] != "一般"
            or row[2] != "https://drive.google.com/file/d/"+entry["source"]["source_id"]+"/view"
            or row[MACHINE_START:-2] != machine(entry) or row[3] != entry["import_row"][6]
            or type(row[3]) is not int or type(row[7]) is not str or len(row[7]) > 2000
            or row[8] != "register:" + entry["request_id"]):
        raise ValueError("row_binding_invalid")
    originals = preview(entry, [])["original_items"]
    if len(originals) > MAX_ITEMS:
        raise ValueError("capacity_exceeded")
    binding = digest([entry["case_id"], entry["source"]])
    items = []
    for i, key in enumerate(entry["slots"]):
        name, qty, amount, major, minor, kind, target, mode, confirmed = row[9+i*9:18+i*9]
        old = next((x for x in originals if x["item_id"] == key), None)
        if not old and name in (None, "") and qty in (None, "") and amount in (None, ""):
            if (major not in (None, "") or minor not in (None, "") or kind not in (None, "", "商品")
                    or target not in (None, "", "指定なし") or mode not in (None, "", "採用")
                    or approval_method is None and confirmed not in (None, "", False)):
                raise ValueError("empty_addition_invalid")
            name, qty, amount, major, minor, kind, target, mode, confirmed = "", "", "", "", "", "商品", "", "採用", False
        elif approval_method == APPROVAL_METHOD:
            # The register event approves every used slot, including exclusions.
            # Hidden legacy cells are neither visual evidence nor prerequisites.
            confirmed = True
        elif type(confirmed) is not bool or not confirmed:
            raise ValueError("original_check_required")
        if kind not in LABELS.values() or target not in [None, "", "指定なし", *[f"商品{j}" for j in range(1,MAX_ITEMS+1)]]:
            raise ValueError("item_controls_invalid")
        items.append(dict(_item_id=key, _binding=binding, name=name, quantity=receipt_quantity(qty) if name else qty, amount=amount,
            category=[major,minor] if major or minor else None, kind={v:k for k,v in LABELS.items()}[kind],
            target=int(target[2:]) if target and target.startswith("商品") else "", mode=mode, confirmed=confirmed))
    # Freeze even a category outside the master, so backend validation can
    # report a correction on the same case instead of silently dropping input.
    capture_categories = deepcopy(entry["categories"])
    for item in items:
        cat = item["category"]
        if cat and len(cat) == 2 and all(type(c) is str and c for c in cat) and cat not in capture_categories:
            capture_categories.append(cat)
    operations = operations_from_cells(dict(case_id=entry["case_id"], source=entry["source"], items=items), originals, capture_categories)
    for operation in operations:
        if approval_method == APPROVAL_METHOD:
            operation["evidence"] = APPROVAL_EVIDENCE
        if operation["action"] == "edit" and "name" in operation["changes"]:
            operation["changes"]["category"] = next(i["category"] for i in items if i["_item_id"] == operation["item_id"])
    item_total = sum(i["amount"] for i in items if i["name"] and i["mode"] == "採用")
    if type(row[4]) is not int or type(row[5]) is not int or row[4:6] != [item_total,row[3]-item_total]:
        raise ValueError("display_total_mismatch")
    if row[-2] not in {"税込", "税別", "不明"} or (approval_method is None and type(row[-1]) is not bool):
        raise ValueError("total_controls_invalid")
    inputs = dict(items=items, receipt_total=row[3], tax_mode=row[-2],
        total_confirmed=True if approval_method == APPROVAL_METHOD else row[-1], note=row[7])
    if approval_method == APPROVAL_METHOD:
        inputs["approval_method"] = approval_method
    return dict(schema=SCHEMA, case_id=entry["case_id"], source=deepcopy(entry["source"]),
        baseline_digest=entry["baseline_digest"], input_version=digest(inputs), inputs=inputs, operations=operations)


def validate_snapshot(snapshot, entry):
    """Rebuild from frozen ordinary cells; never trust receiver operations."""
    check_case(entry)
    if set(snapshot) != {"schema","case_id","source","baseline_digest","input_version","inputs","operations"}:
        raise ValueError("snapshot_invalid")
    inputs = snapshot["inputs"]
    # Preserve historical per-cell snapshots byte-for-byte. A present but unknown
    # method must never fall back to the legacy interpretation.
    approval_method = inputs.get("approval_method")
    if "approval_method" in inputs and approval_method != APPROVAL_METHOD:
        raise ValueError("approval_method_invalid")
    if approval_method == APPROVAL_METHOD and (
            inputs["total_confirmed"] is not True or len(inputs["items"]) != MAX_ITEMS
            or any(type(item["confirmed"]) is not bool for item in inputs["items"])):
        raise ValueError("approval_state_invalid")
    row = make_row(entry)
    row[8] = "register:"+entry["request_id"]
    row[3], row[7], row[-2], row[-1] = inputs["receipt_total"], inputs["note"], inputs["tax_mode"], inputs["total_confirmed"]
    for i, item in enumerate(inputs["items"]):
        if i >= MAX_ITEMS:
            raise ValueError("capacity_exceeded")
        target = item["target"]
        row[9+i*9:18+i*9] = [item["name"],item["quantity"],item["amount"],*(item["category"] or ["",""]),
            LABELS[item["kind"]],f"商品{target}" if target else "指定なし",item["mode"],item["confirmed"]]
    row[4] = sum(i["amount"] for i in inputs["items"] if i["name"] and i["mode"] == "採用")
    row[5] = row[3]-row[4]
    if snapshot_from_row(row, entry, approval_method=approval_method) != snapshot:
        raise ValueError("snapshot_input_version_mismatch")
    plan = preview(entry, snapshot["operations"], total_confirmed=inputs["total_confirmed"])
    closed = {"カテゴリ不正":"category_invalid", "日付不明":"receipt_date_required", "日付不正":"receipt_date_invalid",
              "店舗名不明":"merchant_required", "明細なし":"no_items", "合計金額不正":"receipt_total_invalid", "明細不正":"item_invalid"}
    issues = [closed.get(issue, issue if re.fullmatch(r"[a-z0-9_:.-]+",issue) else "receipt_validation_failed")
              for issue in plan["issues"]]
    warnings = ["tax_treatment_unknown"] if inputs["tax_mode"] == "不明" else []
    if inputs["tax_mode"] == "税込" and any(i["kind"] == "tax" for i in plan["items"]):
        issues.append("included_tax_not_additive")
    # A visibly separate external-tax candidate cannot disappear simply by
    # increasing a product amount to make the arithmetic look balanced.
    adopted = {i["item_id"] for i in plan["items"]}
    if any(i["kind"] == "tax" and "外税" in i["name"] and i["item_id"] not in adopted
           for i in plan["original_items"]):
        issues.append("external_tax_addition_missing")
    # An unknown tax label is auditable, but cannot excuse any amount issue.
    # A separate tax/discount allocation problem still blocks validation.
    hold = plan["difference"] != 0 or any(any(word in issue for word in ("tax_", "included_tax", "allocation")) for issue in issues)
    return dict(state="保留" if hold else "修正が必要" if issues else "検証済み・未記帳", issues=issues,
        warnings=warnings, validator_revision=VALIDATOR_REVISION,
        receipt_total=plan["receipt_total"], item_total=plan["item_total"], difference=plan["difference"],
        posting_authority=False, financial_write_count=0)
