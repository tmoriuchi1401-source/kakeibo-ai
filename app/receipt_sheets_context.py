"""Sheets-authority-only baseline decoding; legacy context unchanged. No storage IO."""
from copy import deepcopy
import re
from .category_rules import parse_rules, valid_rule
from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_item_correction_plan import preview_item_corrections
from .receipt_sheets_source import checked_source
from .receipt_sheets_decoder import decode_receipt_row
from .reconciliation import parse_import_rows

def _fail(code):
    raise StateError("receipt_sheets_" + code)


def expected_context(case_id, entry):
    if set(entry) != {"anchor", "import_row", "candidate_rows", "categories", "rules", "decoder"}:
        _fail("catalog_invalid")
    anchor, row, candidates = entry["anchor"], entry["import_row"], entry["candidate_rows"]
    categories, rules, decoder = entry["categories"], entry["rules"], entry["decoder"]
    if (set(anchor) != {"source", "candidate_digest", "import_digest", "policy_digest", "total_confirmation"}
            or type(row) is not list or len(row) != 12 or row[0] != case_id
            or type(candidates) is not list or not candidates
            or any(type(r) is not list or len(r) != 11 or r[1] != case_id for r in candidates)
            or type(categories) is not list or not categories
            or any(type(c) is not list or len(c) != 2 or any(type(x) is not str or not x for x in c) for c in categories)
            or len(set(map(tuple, categories))) != len(categories)
            or type(rules) is not list or any(type(r) is not list or len(r) != 18 for r in rules)
            or set(decoder) != {"machine", "kind_label"}):
        _fail("catalog_invalid")
    if len(candidates) > 3:
        _fail("capacity_exceeded")
    if (digest(row) != anchor["import_digest"] or digest(candidates) != anchor["candidate_digest"]
            or digest([categories, rules]) != anchor["policy_digest"]):
        _fail("catalog_evidence_mismatch")
    txs, parsed_rules = parse_import_rows([row]), parse_rules(rules)
    if (len(txs) != 1 or len(parsed_rules) != len(rules)
            or any(not valid_rule(r, set(map(tuple, categories))) for r in parsed_rules)):
        _fail("catalog_invalid")
    confirmation = anchor["total_confirmation"]
    if (type(confirmation) is not dict or set(confirmation) != {"source", "receipt_total", "actor_id"}
            or confirmation["source"] != anchor["source"]
            or type(confirmation["receipt_total"]) is not int or confirmation["receipt_total"] != txs[0].amount
            or type(confirmation["actor_id"]) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", confirmation["actor_id"])):
        _fail("catalog_confirmation_required")
    checked_source(anchor["source"])
    if anchor["source"]["source_id"] != row[3]:
        _fail("source_mismatch")
    original = preview_item_corrections(txs[0], list(enumerate(candidates, 2)), [], categories,
                      total_confirmed=True, approved_product_rules=parsed_rules)["original_items"]
    expected = dict(case_id=case_id, source=anchor["source"], original_items=original,
                    categories=categories, context_digest=digest(anchor), receipt_total=txs[0].amount, **decoder)
    # Validate the derived decoder contract with ALL original slots, never trim.
    probe = [case_id, decoder["kind_label"], None, txs[0].amount,
             sum(i["amount"] for i in original), txs[0].amount-sum(i["amount"] for i in original), None, None, None]
    labels = {"product": "商品", "tax": "外税", "discount": "値引き"}
    slots = {i["item_id"]: i for i in original}
    for key in decoder["machine"][4:7]:
        item = slots.get(key)
        probe.extend([item["name"], item["quantity"], item["amount"], *item["category"],
                      labels[item["kind"]], "", "採用", False] if item else [None]*9)
    probe.extend(decoder["machine"])
    decode_receipt_row(probe, expected=expected)
    return deepcopy(expected)
