"""Synthetic receipts only; preview must never write financial data."""
from copy import deepcopy

import pytest

from app.reconciliation import parse_import_rows
from app.receipt_item_correction_plan import preview_item_corrections
from app.receipt_review_snapshot import candidate_rows
from app.models import ReceiptItem, ReceiptResult

HASH = "a" * 64
SOURCE = "f" * 33
CATEGORIES = [("食費", "食品"), ("日用品", "洗剤"), ("その他", "未分類")]


def scenario(amounts, total, *, categories=None, names=None):
    result = ReceiptResult(
        merchant="合成商店", date="2026-11-06", total=total,
        items=[ReceiptItem(
            name=(names[i-1] if names else f"商品{i}"), amount=amount,
            major_category=(categories or CATEGORIES)[min(i-1, len(categories or CATEGORIES)-1)][0],
            minor_category=(categories or CATEGORIES)[min(i-1, len(categories or CATEGORIES)-1)][1],
        ) for i, amount in enumerate(amounts, 1)],
    )
    item_sum = sum(amounts)
    tx = parse_import_rows([[
        "receipt:" + SOURCE, "2026-11-06T10:00:00+09:00", "receipt", SOURCE,
        "2026-11-06", "合成商店", total, "", "要確認", "", HASH,
        f"明細合計{item_sum}≠レシート合計{total}",
    ]])[0]
    rows = list(enumerate(candidate_rows(tx.import_id, HASH, result), 2))
    return tx, rows


def edit(item_id, **changes):
    return {"action": "edit", "item_id": item_id,
            "changes": changes, "evidence": "原本の商品行を確認"}


def add(name, amount, nonce="item0001"):
    return {"action": "add", "item_id": "new:" + nonce, "name": name,
            "quantity": 1, "amount": amount, "kind": "product",
            "category": "食費｜食品", "evidence": "原本に商品行が存在"}


def test_1368_yen_correct_one_item_without_creating_difference_adjustment():
    tx, rows = scenario([500, 730], 1368, categories=[("食費", "食品")])
    key = rows[1][1][0]
    original = deepcopy(rows)
    result = preview_item_corrections(
        tx, rows, [edit(key, amount=868)], CATEGORIES, total_confirmed=True)
    assert result["status"] == "preview_balanced"
    assert result["item_total"] == result["receipt_total"] == 1368
    assert result["difference"] == 0 and len(result["items"]) == 2
    assert result["posting_authority"] is False
    assert result["financial_write_count"] == 0
    assert rows == original  # immutable source snapshot


def test_967_yen_add_a_real_missing_item_only_as_a_preview():
    tx, rows = scenario([500, 439], 967, categories=[("食費", "食品")])
    result = preview_item_corrections(tx, rows, [add("抜けていた商品", 28)],
                                      CATEGORIES, total_confirmed=True)
    assert result["status"] == "preview_balanced"
    assert result["original_item_total"] == 939
    assert result["item_total"] == 967
    assert len(result["items"]) == 3


def test_duplicate_extraction_can_be_excluded_with_explicit_reason():
    tx, rows = scenario([500, 28], 500, categories=[("食費", "食品")])
    result = preview_item_corrections(tx, rows, [{
        "action": "remove", "item_id": rows[1][1][0],
        "evidence": "原本に二重行は存在しない",
    }], CATEGORIES, total_confirmed=True)
    assert result["status"] == "preview_balanced" and len(result["items"]) == 1


def test_amount_gap_and_missing_total_confirmation_both_hold():
    tx, rows = scenario([500, 730], 1368, categories=[("食費", "食品")])
    result = preview_item_corrections(tx, rows, [], CATEGORIES)
    assert result["status"] == "needs_review"
    assert result["difference"] == 138
    assert "item_total_mismatch" in result["issues"]
    assert "receipt_total_owner_confirmation_required" in result["issues"]


@pytest.mark.parametrize("op", [
    add("差額補正", 138),
    {"action": "edit", "item_id": "no-such-item",
     "changes": {"amount": 868}, "evidence": "原本の金額を確認"},
    {"action": "add", "item_id": "new:item0001", "name": "商品",
     "quantity": 1, "amount": 138, "kind": "product", "category": "食費｜食品",
     "evidence": ""},
    {"action": "add", "item_id": "new:item0001", "name": "商品",
     "quantity": 1, "amount": 138.0, "kind": "product", "category": "食費｜食品",
     "evidence": "原本に存在を確認"},
])
def test_invalid_or_unsubstantiated_corrections_are_rejected(op):
    tx, rows = scenario([500, 730], 1368, categories=[("食費", "食品")])
    with pytest.raises(ValueError):
        preview_item_corrections(tx, rows, [op], CATEGORIES, total_confirmed=True)


def test_changed_candidate_hash_and_nonreceipt_source_are_rejected():
    tx, rows = scenario([500, 730], 1368, categories=[("食費", "食品")])
    bad = deepcopy(rows)
    bad[0][1][2] = "b" * 64
    with pytest.raises(ValueError, match="snapshot_invalid"):
        preview_item_corrections(tx, bad, [], CATEGORIES, total_confirmed=True)
    wrong_source = parse_import_rows([[tx.import_id, "", "au PAY", SOURCE, tx.date,
                                       tx.merchant, tx.amount, "", tx.status, "",
                                       HASH, tx.note]])[0]
    with pytest.raises(ValueError, match="not_held"):
        preview_item_corrections(wrong_source, rows, [], CATEGORIES, total_confirmed=True)


def test_mixed_categories_and_unallocated_tax_hold_even_if_amount_balances():
    tx, rows = scenario([300, 200, 20], 540, categories=[
        ("食費", "食品"), ("日用品", "洗剤"), ("食費", "食品")],
        names=["食料品", "洗剤", "外税"])
    res = preview_item_corrections(tx, rows, [add("商品4", 20)], CATEGORIES,
                                   total_confirmed=True)
    assert res["item_total"] == 540
    assert any(issue.startswith("tax_discount_allocation_review") for issue in res["issues"])
    assert res["status"] == "needs_review"
