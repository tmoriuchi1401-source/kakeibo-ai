from datetime import datetime

from app.reconciliation import (
    merchants_match,
    parse_import_rows,
    parse_store_aliases,
    reconciliation_scope,
    reconcile_transactions,
)


def row(import_id, source, date, merchant, amount, status):
    return [import_id, "", source, import_id, date, merchant, amount, "", status, "", "", ""]


def decisions(rows):
    return reconcile_transactions(parse_import_rows(rows))


def test_unique_receipt_aupay_match():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "ダイソー 大宮台店", 550, "解析済"),
        row("aupay:1", "au PAY", "2026-08-16", "ダイソー大宮台店", 550, "unclassified_aupay"),
    ])
    assert len(result) == 1
    assert result[0].status == "matched_receipt"
    assert result[0].target_id == "receipt:r1"


def test_amount_mismatch_is_not_candidate():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 500, "解析済"),
        row("aupay:1", "au PAY", "2026-08-16", "テスト商店", 550, "unclassified_aupay"),
    ])
    assert result == []


def test_multiple_receipts_need_review():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("receipt:r2", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("aupay:1", "au PAY", "2026-08-16", "テスト商店", 550, "unclassified_aupay"),
    ])
    assert result[0].status == "needs_review_duplicate"
    assert set(result[0].candidate_ids) == {"receipt:r1", "receipt:r2"}


def test_two_payments_cannot_claim_one_receipt():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("aupay:1", "au PAY", "2026-08-16", "テスト商店", 550, "unclassified_aupay"),
        row("aupay:2", "au PAY", "2026-08-16", "テスト商店", 550, "unclassified_aupay"),
    ])
    assert len(result) == 2
    assert all(x.status == "needs_review_duplicate" for x in result)


def test_paypay_matches_receipt_within_one_day():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店 本店", 550, "解析済"),
        row("paypay:1", "PayPay", "2026-08-17", "テスト商店", 550,
            "unclassified_paypay"),
    ])
    assert len(result) == 1
    assert result[0].status == "matched_receipt"
    assert result[0].target_id == "receipt:r1"


def test_paypay_multiple_receipt_candidates_need_review():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("receipt:r2", "receipt", "2026-08-17", "テスト商店", 550, "解析済"),
        row("paypay:1", "PayPay", "2026-08-17", "テスト商店", 550,
            "unclassified_paypay"),
    ])
    assert result[0].status == "needs_review_duplicate"
    assert set(result[0].candidate_ids) == {"receipt:r1", "receipt:r2"}


def test_paypay_does_not_match_receipt_more_than_one_day_apart():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-15", "テスト商店", 550, "解析済"),
        row("paypay:1", "PayPay", "2026-08-17", "テスト商店", 550,
            "unclassified_paypay"),
    ])
    assert result == []


def test_auto_expense_matches_later_receipt():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("paypay:1", "PayPay", "2026-08-16", "テスト商店", 550, "auto_expense"),
    ])
    assert len(result) == 1
    assert result[0].status == "matched_receipt"


def test_auto_expense_with_multiple_receipts_needs_review():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("receipt:r2", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("paypay:1", "PayPay", "2026-08-16", "テスト商店", 550, "auto_expense"),
    ])
    assert result[0].status == "needs_review_duplicate"


class ApplyDB:
    def __init__(self, rows):
        self.rows = rows
        self.updated = {}

    def get(self, rng):
        if rng == "取込データ!A2:L":
            return self.rows
        if rng == "店舗!A2:C":
            return []
        raise AssertionError(rng)

    def ensure_expense_status_column(self): pass
    def update_rows(self, sheet, rows): self.updated.setdefault(sheet, []).extend(rows)
    def expense_rows_for_import(self, import_id):
        if import_id == "paypay:1":
            return [(2, ["M-1", "2026-08-16", "テスト商店", "自動計上", 550,
                         "その他", "未分類", "PayPay", "PayPay", "", import_id, "", "active"])]
        return []


def test_apply_excludes_auto_expense_after_receipt_match():
    from app.reconciliation import ReconciliationPipeline
    db = ApplyDB([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("paypay:1", "PayPay", "2026-08-16", "テスト商店", 550, "auto_expense"),
    ])
    result = ReconciliationPipeline(db).apply()
    assert result["expenses_excluded"] == 1
    assert db.updated["支出明細"][0][1][12] == "duplicate_excluded"
    assert db.updated["取込データ"][0][1][8] == "matched_receipt"


def test_apply_does_not_exclude_auto_expense_for_ambiguous_receipts():
    from app.reconciliation import ReconciliationPipeline
    db = ApplyDB([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("receipt:r2", "receipt", "2026-08-16", "テスト商店", 550, "解析済"),
        row("paypay:1", "PayPay", "2026-08-16", "テスト商店", 550, "auto_expense"),
    ])
    result = ReconciliationPipeline(db).apply()
    assert result["needs_review"] == 1
    assert result["expenses_excluded"] == 0
    assert "支出明細" not in db.updated
    assert db.updated["取込データ"][0][1][8] == "needs_review_duplicate"


def test_card_charge_and_amazon_states_are_excluded():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "Amazon.co.jp", 522, "解析済"),
        row("card:1", "au PAYカード", "2026-08-16", "Amazon.co.jp", 522, "matched_amazon"),
        row("card:2", "au PAYカード", "2026-08-16", "au PAY 残高オートチャージ", 522, "transfer_aupay_charge"),
    ])
    assert result == []


def test_amazon_order_is_canonical_over_receipt():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "Amazon.co.jp", 1200, "解析済"),
        row("amazon:o1", "Amazon", "2026-08-14", "Amazon.co.jp", 1200, "canonical_amazon"),
    ])
    assert len(result) == 1
    assert result[0].transaction.import_id == "receipt:r1"
    assert result[0].status == "matched_amazon"
    assert result[0].target_id == "amazon:o1"


def test_non_amazon_receipt_does_not_match_amazon_by_amount_only():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "テスト商店", 1200, "解析済"),
        row("amazon:o1", "Amazon", "2026-08-14", "Amazon.co.jp", 1200, "canonical_amazon"),
    ])
    assert result == []


def test_store_master_alias_enables_match():
    aliases = parse_store_aliases([
        ["store:1", "（株）テストストア", "テストストア"],
    ])
    transactions = parse_import_rows([
        row("receipt:r1", "receipt", "2026-08-16", "テストストア", 550, "解析済"),
        row("aupay:1", "au PAY", "2026-08-16", "（株）テストストア", 550, "unclassified_aupay"),
    ])

    result = reconcile_transactions(transactions, aliases)

    assert result[0].status == "matched_receipt"


def test_store_master_ignores_incomplete_rows():
    assert parse_store_aliases([
        ["store:1", "別名のみ", ""],
        ["store:2", "", "標準名のみ"],
    ]) == {}


def test_store_master_alias_chain_and_cycle_are_safe():
    aliases = {
        "別名a": "別名b",
        "別名b": "標準店",
        "循環a": "循環b",
        "循環b": "循環a",
    }
    assert merchants_match("別名A", "標準店", aliases)
    assert not merchants_match("循環A", "標準店", aliases)


def test_scope_keeps_old_unresolved_and_its_historical_candidate():
    transactions = parse_import_rows([
        row("receipt:r1", "receipt", "2025-01-10", "テスト店", 550, "解析済"),
        row("aupay:1", "au PAY", "2025-01-10", "テスト店", 550, "unclassified_aupay"),
        row("receipt:unrelated", "receipt", "2025-01-10", "別店舗", 999, "解析済"),
    ])

    scoped = reconciliation_scope(
        transactions, months=6, as_of=datetime(2026, 8, 18),
    )

    assert {tx.import_id for tx in scoped} == {"receipt:r1", "aupay:1"}


def test_scope_keeps_old_unclassified_paypay_and_its_historical_candidate():
    transactions = parse_import_rows([
        row("receipt:r1", "receipt", "2025-01-10", "テスト店", 550, "解析済"),
        row("paypay:1", "PayPay", "2025-01-11", "テスト店", 550,
            "unclassified_paypay"),
    ])

    scoped = reconciliation_scope(
        transactions, months=6, as_of=datetime(2026, 8, 18),
    )

    assert {tx.import_id for tx in scoped} == {"receipt:r1", "paypay:1"}


def test_scope_does_not_keep_old_resolved_paypay_without_recent_activity():
    transactions = parse_import_rows([
        row("paypay:1", "PayPay", "2025-01-10", "テスト店", 550,
            "matched_receipt"),
    ])

    scoped = reconciliation_scope(
        transactions, months=6, as_of=datetime(2026, 8, 18),
    )

    assert scoped == []


def test_scope_keeps_recently_imported_old_dated_receipt():
    receipt = row("receipt:r1", "receipt", "2020-01-10", "テスト店", 550, "解析済")
    receipt[1] = "2026-08-17 12:00:00"

    scoped = reconciliation_scope(
        parse_import_rows([receipt]), months=6, as_of=datetime(2026, 8, 18),
    )

    assert [tx.import_id for tx in scoped] == ["receipt:r1"]
