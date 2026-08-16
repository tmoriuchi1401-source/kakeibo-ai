from app.reconciliation import parse_import_rows, reconcile_transactions


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


def test_card_charge_and_amazon_states_are_excluded():
    result = decisions([
        row("receipt:r1", "receipt", "2026-08-16", "Amazon.co.jp", 522, "解析済"),
        row("card:1", "au PAYカード", "2026-08-16", "Amazon.co.jp", 522, "matched_amazon"),
        row("card:2", "au PAYカード", "2026-08-16", "au PAY 残高オートチャージ", 522, "transfer_aupay_charge"),
    ])
    assert result == []
