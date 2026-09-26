from copy import deepcopy

import pytest

from app.models import ReceiptResult, ReceiptItem
from app.receipt_reimport import compare_receipt
from app.utils import canonical_hash


def fixture():
    parsed = ReceiptResult(date="2026-01-02", merchant="Synthetic shop", total=120,
                           payment_method="現金", items=[ReceiptItem(name="Synthetic item", amount=120,
                           major_category="食費", minor_category="食品")])
    rows = dict(receipt_rows=[["R-s1", "2026-01-02", "Synthetic shop", 120, "現金", "", "解析済", "original-time", "manual-note"]],
                import_rows=[["receipt:s1", "original-time", "receipt", "s1", "2026-01-02", "Synthetic shop", 120, "現金", "解析済", "", canonical_hash(parsed.model_dump()), ""]],
                expense_rows=[["R-s1-01", "2026-01-02", "Synthetic shop", "Synthetic item", 120, "食費", "食品", "現金", "receipt", "R-s1", "receipt:s1", "manual-note", "active"]],
                review_rows=[], categories=[("食費", "食品")])
    return parsed, rows


def test_matching_reparse_is_noop_and_preserves_inputs():
    parsed, rows = fixture()
    original = deepcopy(rows)
    result = compare_receipt("s1", parsed, **rows)
    assert result.status == "unchanged"
    assert result.analysis_hash_matches
    assert compare_receipt("s1", parsed, **rows) == result
    assert rows == original


def test_import_marker_does_not_hide_missing_details():
    parsed, rows = fixture()
    rows["expense_rows"] = []
    result = compare_receipt("s1", parsed, **rows)
    assert result.missing_item_ids == ("R-s1-01",)
    assert result.status == "needs_review"
    assert result.analysis_hash_matches  # Still not correction authority.


@pytest.mark.parametrize("change,reason", [
    (lambda r: r["expense_rows"][0].__setitem__(4, 121), "detail_difference"),
    (lambda r: r["expense_rows"][0].__setitem__(12, "inactive"), "protected_expense_status"),
    (lambda r: r["import_rows"][0].__setitem__(8, "matched"), "existing_decision_requires_review"),
    (lambda r: r["expense_rows"].append(list(r["expense_rows"][0])), "duplicate_expense_identity"),
    (lambda r: r["review_rows"].append(["receipt:s1"] + [""]*9 + ["保留"]), "manual_review_protected"),
    (lambda r: r["import_rows"].append(["card:other"] + [""]*8 + ["R-s1-01"]), "linked_payment_requires_review"),
])
def test_differences_and_existing_decisions_are_held(change, reason):
    parsed, rows = fixture()
    change(rows)
    result = compare_receipt("s1", parsed, **rows)
    assert result.status == "needs_review"
    assert reason in result.reasons


def test_equal_date_store_amount_is_not_identity():
    parsed, rows = fixture()
    rows["receipt_rows"][0][0] = "R-other"
    assert "receipt_or_import_identity_missing_or_duplicate" in compare_receipt("s1", parsed, **rows).reasons


def test_ai_difference_cannot_authorize_manual_category_overwrite():
    parsed, rows = fixture()
    rows["expense_rows"][0][5:7] = ["日用品", "消耗品"]
    result = compare_receipt("s1", parsed, **rows)
    assert result.status == "needs_review"
    assert result.analysis_hash_matches


def test_invalid_total_or_category_remains_review():
    parsed, rows = fixture()
    parsed.total = 999
    parsed.items[0].minor_category = "Not allowed"
    assert "parsed_fields_need_review" in compare_receipt("s1", parsed, **rows).reasons
