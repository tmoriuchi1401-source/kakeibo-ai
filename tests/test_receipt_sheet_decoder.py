"""Synthetic row decoding only. No Google ports or financial construction."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess

import pytest

from app.page_receipt_model import digest
from app.receipt_sheet_cells import operations_from_cells
from app.receipt_sheet_decoder import decode_receipt_row, decode_receipt_rows
from app.receipt_sheet_validation import execute_accepted_request
from test_receipt_sheet_validation import World, RID


@pytest.fixture(autouse=True)
def no_external_or_financial_io(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        raise AssertionError("decoder must not perform external IO")
    monkeypatch.setattr(socket, "create_connection", denied)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", denied)
    yield
    assert calls == []


def fixture():
    w = World()
    originals = w.reader(w.case_id)["preview"]["original_items"]
    # Same vector layout as Gate B fixture; final hash is opaque, NOT context.
    machine = [w.case_id, w.source["source_id"], w.source["version"], w.source["sha256"],
               originals[0]["item_id"], originals[1]["item_id"], "new:row00001", RID, "GATE-B-TEST", "b"*64]
    expected = dict(case_id=w.case_id, source=deepcopy(w.source), original_items=originals,
                    categories=deepcopy(w.sheet["categories"]), context_digest=digest(w.anchor),
                    machine=machine, receipt_total=1368, kind_label="一般")
    row = [w.case_id, "一般", "synthetic-original-link", 1368, 1230, 138,
           "イベント観測のみ・登録未受付", "保持するメモ", "ready:"+RID]
    for old in originals:
        row.extend([old["name"], 1.0, old["amount"], *old["category"], "商品", "指定なし", "採用", False])
    row.extend(["", "", "", "食費", "食品", "商品", "指定なし", "採用", False])
    row.extend(machine)
    return w, row, expected


def corrected():
    w, row, expected = fixture()
    row[20], row[26], row[4], row[5] = 868, True, 1368, 0
    return w, row, expected


def check_languages(w, row, expected):
    before = deepcopy((row, expected))
    candidate = decode_receipt_row(row, expected=expected)
    _, tag = w.journal.load()
    completed = subprocess.run(["node", str(Path("apps-script/receipt-validation/Code.test.cjs").resolve()), "--prepare"],
        input=json.dumps(dict(candidate=candidate, tag=tag), ensure_ascii=True, allow_nan=False),
        text=True, encoding="utf-8", capture_output=True, check=True)
    captured = json.loads(completed.stdout)
    assert captured["error"] is None and captured["writes"] == 1
    assert captured["trace"][:2] == ["etag", "input"]
    w.row["draft"] = candidate["draft"]
    fresh = w.reader(w.case_id)
    assert captured["request"]["snapshot"] == fresh["request_snapshot"]
    assert captured["request"]["snapshot"]["digest"] == fresh["request_snapshot"]["digest"]
    assert "保持するメモ" not in json.dumps(captured["request"], ensure_ascii=False)
    w.journal.prepare(captured["request"], **captured["options"])
    w.journal.accept_prepared(RID, fresh["request_snapshot"]["digest"])
    result = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    assert result["result"]["financial_write_count"] == 0
    assert result["result"]["posting_authority"] is False
    assert (row, expected) == before
    return candidate, result


def test_two_original_products_empty_addition_and_730_to_868():
    w, row, expected = fixture()
    candidate, result = check_languages(w, row, expected)
    assert candidate["corrected_total"] == 1230 and candidate["difference"] == 138
    assert result["state"] == "修正が必要"
    w, row, expected = corrected()
    candidate, result = check_languages(w, row, expected)
    assert candidate["corrected_total"] == 1368 and candidate["difference"] == 0
    assert result["state"] == "検証済み・未記帳"
    assert candidate["context_digest"] != row[45]


@pytest.mark.parametrize("quantity", [2, 0.125, 0.001, 9999999.999])
def test_quantity_is_not_multiplied_into_line_amount(quantity):
    w, row, expected = corrected()
    row[19] = quantity
    candidate, result = check_languages(w, row, expected)
    assert candidate["corrected_total"] == 1368 and result["state"] == "検証済み・未記帳"


@pytest.mark.parametrize("kind,amount,target,remove", [
    ("商品", 138, "指定なし", False), ("商品", 868, "指定なし", True),
    ("外税", 138, "商品1", False), ("値引き", -28, "商品1", False),
])
def test_add_remove_tax_discount_and_target_identity(kind, amount, target, remove):
    w, row, expected = fixture()
    row[27:36] = ["追加の商品" if kind == "商品" else kind, 1, amount, "食費", "食品", kind, target, "採用", True]
    if remove:
        row[25], row[26] = "除外", True
    row[4] = 500 + (0 if remove else 730) + amount
    row[5] = 1368 - row[4]
    candidate, _ = check_languages(w, row, expected)
    ops = operations_from_cells(candidate["draft"], candidate["original_items"], candidate["categories"])
    assert any(op["action"] == "add" for op in ops)
    if remove:
        assert any(op["action"] == "remove" for op in ops)
    if target != "指定なし":
        assert ops[-1]["target_item_id"] == expected["original_items"][0]["item_id"]


@pytest.mark.parametrize("name", [" \t商品🍎 ", "\u3000日本語\u00a0", "👩‍💻", "e\u0301"])
def test_native_integer_float_yen_and_unicode(name):
    w, row, expected = corrected()
    row[18], row[20] = name, 868.0
    candidate, _ = check_languages(w, row, expected)
    assert type(candidate["draft"]["items"][1]["amount"]) is int


@pytest.mark.parametrize("column,value", [
    (20,"868"), (20,"1,368"), (20,868.5), (20,True), (20,float("nan")), (20,float("inf")),
    (20,2**53), (20,10**400), (19,"2"), (19,True), (19,0.00001), (19,0.0000001), (19,10**21),
    (19,0.1234), (19,None), (19,float("nan")), (18,"\u0085商品"), (18,"商品\ufeff"), (18,"商品\x00"),
    (21,"日用品"), (22,"洗剤"), (21,""), (21,"食費 "), (23,"tax"), (23,"不明"), (24,"商品4"), (24,1),
    (25,"不明"), (26,"TRUE"), (26,1), (26,None), (26,False),
    (3,1367), (4,1230), (5,138), (4,"=SUM(L2,U2)"), (0,"other-case"), (1,"医療"),
])
def test_bad_values_and_display_calculations_are_rejected_without_mutation(column, value):
    _, row, expected = corrected()
    row[column] = value
    before = deepcopy(row)
    with pytest.raises(ValueError):
        decode_receipt_row(row, expected=expected)
    # NaN is non-reflexive; compare serialized representations for this check.
    assert repr(row) == repr(before)


@pytest.mark.parametrize("column", range(36, 46))
@pytest.mark.parametrize("value", ["tampered", None])
def test_every_machine_column_is_pinned(column, value):
    _, row, expected = corrected()
    row[column] = value
    with pytest.raises(ValueError, match="binding"):
        decode_receipt_row(row, expected=expected)


@pytest.mark.parametrize("change", ["missing_column", "extra_column", "missing_id", "duplicate_id", "duplicate_original", "missing_original", "source", "context"])
def test_missing_or_ambiguous_identities_are_rejected(change):
    _, row, expected = corrected()
    if change == "missing_column": row.pop()
    elif change == "extra_column": row.append("")
    elif change == "missing_id": row[42] = ""
    elif change == "duplicate_id": expected["machine"][5] = row[41] = row[40]
    elif change == "duplicate_original": expected["original_items"].append(deepcopy(expected["original_items"][0]))
    elif change == "missing_original": del expected["original_items"][0]["item_id"]
    elif change == "source": expected["source"]["version"] = "changed"
    else: expected["context_digest"] = "unproven"
    with pytest.raises(ValueError):
        decode_receipt_row(row, expected=expected)


def second_case(row, expected):
    other, binding = deepcopy(row), deepcopy(expected)
    source = binding["source"]
    source["source_id"] = "g"*33
    binding["case_id"] = "receipt:"+source["source_id"]
    for i, original in enumerate(binding["original_items"]):
        original["item_id"] = binding["case_id"]+f":{i+1:03d}"
        binding["machine"][4+i] = original["item_id"]
    binding["machine"][:4] = [binding["case_id"], source["source_id"], source["version"], source["sha256"]]
    other[0], other[36:46] = binding["case_id"], binding["machine"][:]
    return other, binding


def test_full_row_sort_is_by_case_and_partial_identity_copy_is_rejected():
    _, row, expected = corrected()
    other, binding = second_case(row, expected)
    catalog = {expected["case_id"]:expected, binding["case_id"]:binding}
    before = deepcopy((row, other, catalog))
    assert decode_receipt_rows([row, other], catalog=catalog) == decode_receipt_rows([other, row], catalog=catalog)
    for rows in ([row, row], [row, other[:36]+row[36:]], [row, [row[0]]+other[1:]]):
        with pytest.raises(ValueError):
            decode_receipt_rows(rows, catalog=catalog)
    alias = deepcopy(binding)
    alias["source"]["source_id"] = expected["source"]["source_id"]
    alias["machine"][1] = alias["source"]["source_id"]
    copied = other[:36] + alias["machine"]
    with pytest.raises(ValueError, match="source_duplicate"):
        decode_receipt_rows([row, copied], catalog={row[0]:expected, copied[0]:alias})
    assert (row, other, catalog) == before


def test_blank_slot_variants_and_invalid_targets():
    _, row, expected = corrected()
    for blank in ("", None):
        row[27:36] = [blank]*9
        candidate = decode_receipt_row(row, expected=expected)
        assert candidate["draft"]["items"][2]["name"] == ""
        assert candidate["corrected_total"] == 1368
    row[35] = "FALSE"
    with pytest.raises(ValueError, match="empty_addition"):
        decode_receipt_row(row, expected=expected)
    for target in ("商品2", "商品3"):
        row[27:36] = ["外税",1,138,"食費","食品","外税",target,"採用",True]
        row[25], row[26] = "除外", True
        with pytest.raises(ValueError, match="target"):
            decode_receipt_row(row, expected=expected)


def test_value_only_copy_with_unchanged_identity_is_not_sort_proof_or_authority():
    _, row, expected = corrected()
    row[9], row[18], row[17], row[26] = "別案件の商品A", "別案件の商品B", True, True
    candidate = decode_receipt_row(row, expected=expected)
    assert operations_from_cells(candidate["draft"], candidate["original_items"], candidate["categories"])
    assert "posting_authority" not in candidate and "request_id" not in candidate
    # Toggling observational registration data cannot create an acceptance.
    row[8] = True
    assert decode_receipt_row(row, expected=expected)["draft"] == candidate["draft"]
    # Even another pinned observation UUID/run is not the validation context.
    row[43], row[44] = "22222222-2222-4222-8222-222222222222", "GATE-B-OTHER"
    expected["machine"][7:9] = row[43:45]
    assert decode_receipt_row(row, expected=expected)["draft"] == candidate["draft"]
