"""Offline fixture definition/generation; no client, upload or live pin.

build_fixture() can bind the existing PNG privately. Public tests use a wholly
fictional source. Mock confirmation exists only inside one test, never in the
generated fixture or a reusable live catalog/pin.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import socket

import pytest

from app.category_rules import parse_rules
from app.drive_run_state import StateError
from app.models import ReceiptItem, ReceiptResult
from app.page_receipt_model import digest
from app.receipt_item_correction_review import review, registration_plan
from app.receipt_private_adapter import _expected
from app.receipt_review_snapshot import candidate_rows, verified_candidate_range
from app.receipt_sheet_cells import operations_from_cells
from app.receipt_sheet_decoder import decode_receipt_row
from app.reconciliation import parse_import_rows

ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "tests/fixtures/receipt_gate_c_candidate_v1.json"


def build_fixture(source):
    """Build local candidate DATA only. Missing identity evidence stays None."""
    definition = json.loads(DEFINITION.read_text(encoding="utf-8"))
    printed, policy = definition["printed"], definition["policy"]
    categories, rules = policy["categories"], policy["rules"]
    wrong = definition["intentional_wrong_read"]
    result = ReceiptResult(merchant=printed["merchant"], date=printed["date"],
        total=printed["total"], note="OFFLINE_SYNTHETIC_DEFINITION_NOT_OCR_OUTPUT",
        items=[ReceiptItem(name=p["name"], quantity=1,
            amount=wrong["amount"] if i == wrong["item_index"] else p["amount"],
            major_category=categories[0][0], minor_category=categories[0][1],
            confidence=0, note="synthetic quantity/category; policy not approved")
            for i, p in enumerate(printed["items"])])
    payload = result.model_dump(mode="json")
    analysis_hash = digest(payload)
    case_id = "receipt:" + source["source_id"]
    candidates = candidate_rows(case_id, analysis_hash, result)
    item_sum = sum(r[6] for r in candidates)
    import_row = [case_id, definition["synthetic_imported_at"], "receipt", source["source_id"],
                  result.date, result.merchant, result.total, "", "要確認", "", analysis_hash,
                  f"明細合計{item_sum}≠レシート合計{result.total}"]
    tx = parse_import_rows([import_row])[0]
    anchor = dict(source=deepcopy(source), candidate_digest=digest(candidates),
        import_digest=digest(import_row), policy_digest=digest([categories, rules]),
        total_confirmation=None)
    original = review(tx, list(enumerate(candidates, 2)), source, [], categories,
                      approved_product_rules=parse_rules(rules))["preview"]["original_items"]
    # Fixed observation values only; not a new registration UUID or ticket.
    machine = [case_id, source["source_id"], source["version"], source["sha256"],
               original[0]["item_id"], original[1]["item_id"], "new:offline001",
               "OFFLINE-NO-TICKET", definition["fixture_version"]]
    machine.append(sha256(json.dumps(machine, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
    entry = dict(anchor=anchor, import_row=import_row, candidate_rows=candidates,
                 categories=categories, rules=rules,
                 decoder=dict(machine=machine, kind_label="一般（完全合成）"))
    expected = dict(case_id=case_id, source=deepcopy(source), original_items=original,
                    categories=categories, context_digest=digest(anchor), receipt_total=result.total,
                    **entry["decoder"])
    row = [case_id, expected["kind_label"], "offline-original-reference", result.total,
           item_sum, result.total-item_sum, "オフライン・本人未確認・未記帳", "保持する合成メモ", None]
    for item in original:
        row.extend([item["name"], item["quantity"], item["amount"], *item["category"],
                    "商品", "指定なし", "採用", False])
    row.extend([None]*9)
    row.extend(machine)
    corrected = deepcopy(row)
    corrected[20], corrected[26], corrected[4], corrected[5] = printed["items"][1]["amount"], True, result.total, 0
    return dict(fixture_version=definition["fixture_version"], scope=definition["scope"],
        policy=deepcopy(policy), analysis_payload=payload, analysis_hash=analysis_hash,
        catalog_entry_draft=entry, decoder_expected=expected,
        initial_row=row, corrected_row=corrected,
        correction_confirmation_scope="OFFLINE_SIMULATED_ITEM_CHECK_NOT_OWNER_CONFIRMATION",
        verified_actor=None, live_catalog_complete=False, live_catalog_pin=None)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append(True)
        raise AssertionError("live/financial IO forbidden")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", denied)
    yield
    assert calls == []


@pytest.fixture
def data():
    return build_fixture(dict(source_id="offline-source-0001", sha256=sha256(b"fictional PNG binding").hexdigest(),
        version="4", mime_type="image/png", classification="normal", transaction_kind="purchase"))


def test_generated_rows_reuse_existing_parser_hash_and_identity(data):
    e = data["catalog_entry_draft"]
    tx = parse_import_rows([e["import_row"]])[0]
    result = ReceiptResult.model_validate(data["analysis_payload"])
    assert candidate_rows(tx.import_id, data["analysis_hash"], result) == e["candidate_rows"]
    assert data["analysis_hash"] == digest(data["analysis_payload"]) == tx.row[10]
    assert data["analysis_hash"] != e["anchor"]["source"]["sha256"]
    assert len(e["import_row"]) == 12 and len(e["candidate_rows"]) == 2
    assert all(len(r) == 11 for r in e["candidate_rows"])
    assert verified_candidate_range(tx, list(enumerate(e["candidate_rows"], 2))) == (2, 3, 1230)
    assert [r[6] for r in e["candidate_rows"]] == [500, 730] and tx.amount == 1368


def test_minimal_policy_is_existing_pair_but_not_approved(data):
    pairs = [line.split("\t") for line in (ROOT/"config/categories.tsv").read_text(encoding="utf-8").splitlines()[1:]]
    assert data["policy"]["categories"] == [["その他", "その他"]]
    assert all(pair in pairs for pair in data["policy"]["categories"])
    assert data["policy"]["status"] == "PROPOSED_NOT_APPROVED"
    assert data["policy"]["rules_status"] == "EMPTY_RULE_SET_PROPOSED_NOT_APPROVED"
    assert parse_rules(data["policy"]["rules"]) == []


def test_decoder_and_existing_review_keep_missing_owner_confirmation(data):
    e, expected = data["catalog_entry_draft"], data["decoder_expected"]
    before = deepcopy(data)
    initial = decode_receipt_row(data["initial_row"], expected=expected)
    corrected = decode_receipt_row(data["corrected_row"], expected=expected)
    assert (initial["corrected_total"], initial["difference"]) == (1230, 138)
    assert (corrected["corrected_total"], corrected["difference"]) == (1368, 0)
    ops = operations_from_cells(corrected["draft"], corrected["original_items"], corrected["categories"])
    assert len(ops) == 1 and ops[0]["item_id"] == e["candidate_rows"][1][0]
    tx = parse_import_rows([e["import_row"]])[0]
    fresh = review(tx, list(enumerate(e["candidate_rows"], 2)), e["anchor"]["source"], ops,
                   e["categories"], total_confirmation=None, approved_product_rules=parse_rules(e["rules"]))
    assert fresh["total_confirmation_required"] is True
    plan = registration_plan(fresh["snapshot"], fresh)
    assert plan["state"] == "修正が必要"
    assert "receipt_total_owner_confirmation_required" in plan["issues"]
    assert plan["posting_authority"] is False and plan["financial_write_count"] == 0
    assert data == before


def test_strict_catalog_rejects_actual_draft_without_identity_evidence(data):
    e = data["catalog_entry_draft"]
    with pytest.raises(StateError, match="catalog_confirmation_required"):
        _expected(e["import_row"][0], e)
    assert data["verified_actor"] is None and data["live_catalog_pin"] is None
    assert data["live_catalog_complete"] is False and e["anchor"]["total_confirmation"] is None


def test_strict_positive_structure_is_mock_only_and_never_exported(data):
    # Wholly fictional source + ephemeral mock actor, not the private PNG fixture.
    e = deepcopy(data["catalog_entry_draft"])
    assert e["anchor"]["source"]["source_id"] == "offline-source-0001"
    e["anchor"]["total_confirmation"] = dict(source=deepcopy(e["anchor"]["source"]),
        receipt_total=1368, actor_id=digest("OFFLINE_MOCK_ACTOR_NO_REAL_IDENTITY"))
    expected = _expected(e["import_row"][0], e)
    assert decode_receipt_row(data["corrected_row"], expected=expected)["difference"] == 0
    assert data["catalog_entry_draft"]["anchor"]["total_confirmation"] is None
    assert data["live_catalog_pin"] is None


@pytest.mark.parametrize("damage", ["analysis_hash", "order", "candidate_id", "source_id"])
def test_existing_candidate_verifier_rejects_stale_or_wrong_identity(data, damage):
    e = data["catalog_entry_draft"]
    rows = deepcopy(e["candidate_rows"])
    if damage == "analysis_hash":
        rows[0][2] = "0"*64
    elif damage == "order":
        rows.reverse()
    elif damage == "candidate_id":
        rows[0][0] = "product-a"
    else:
        e["import_row"][3] = "another-offline-source"
    assert verified_candidate_range(parse_import_rows([e["import_row"]])[0], list(enumerate(rows, 2))) is None


def test_edited_amount_without_item_check_still_rejected(data):
    row = deepcopy(data["corrected_row"])
    row[26] = False
    with pytest.raises(ValueError, match="original_check_required"):
        decode_receipt_row(row, expected=data["decoder_expected"])
