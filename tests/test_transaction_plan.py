import pytest

from app.transaction_plan import (
    build_write_plan,
    normalize_card_transaction,
    reconcile_transactions,
)


def row(identity, merchant="店", amount=1000, date="2026-08-08", memo="メール明細No.001"):
    return {"import_id": identity, "date": date, "merchant": merchant,
            "amount": amount, "payment_type": "メール通知", "member": "本会員",
            "memo": memo}


def test_normalized_schema_is_stable_and_materializable():
    tx = normalize_card_transaction(row("aupaycard-mail:abc:001"))
    assert tx.identity == "aupaycard-mail:abc:001"
    assert tx.source == "au PAYカード"
    assert tx.amount_yen == 1000
    assert len(tx.to_import_row()) == 12
    assert tx.to_import_row()[10] == tx.business_fingerprint


def test_existing_identity_is_duplicate_and_reprocessing_input_is_duplicate():
    result = build_write_plan(
        [row("same"), row("same")],
        [["same", "", "au PAYカード"]],
    )
    assert result["summary"]["duplicate"] == 2
    assert result["unique_transactions"] == 0
    assert result["identity_collisions"] == 0


def test_same_day_same_amount_different_identities_are_distinct():
    result = build_write_plan([
        row("one", merchant="コンビニ"), row("two", merchant="書店"),
    ])
    assert result["unique_transactions"] == 2
    assert result["summary"]["duplicate"] == 0
    group = result["same_day_same_amount_groups"][0]
    assert group["count"] == 2
    assert group["distinct"] is True


def test_invalid_core_fields_are_rejected_fail_closed():
    result = build_write_plan([row("bad", amount=0), row("bad-date", date="2026/08/08")])
    assert result["summary"]["rejected"] == 2
    assert result["summary"]["needs_review"] == 0


def test_identity_collision_is_reviewed_when_same_id_maps_to_different_data():
    result = build_write_plan([row("same", merchant="A"), row("same", merchant="B")])
    assert result["identity_collisions"] == 1
    assert result["summary"]["needs_review"] == 2
    assert any(item.reason == "identity_collision" for item in result["items"])


def mail_id(char, number=1):
    return f"aupaycard-mail:{char * 24}:{number:03d}"


def csv_row(identity="aupaycard:csv1", merchant="TEST STORE", amount=1000,
            date="2026-08-08"):
    return [identity, "", "au PAYカード", "", date, merchant, amount, "一括"]


def test_different_message_same_fingerprint_is_probable_resend_with_one_canonical():
    rows = [row(mail_id("b")), row(mail_id("a"))]
    result = reconcile_transactions(rows)
    assert result["summary"]["probable_resend_groups"] == 1
    assert result["summary"]["canonical_transactions"] == 1
    item = result["transactions"][0]
    assert item.state == "probable_resend"
    assert item.canonical.identity == mail_id("a")
    assert item.source_identities == (mail_id("a"), mail_id("b"))


def test_same_day_amount_merchant_but_different_detail_number_stays_distinct():
    result = reconcile_transactions([
        row(mail_id("a", 1), memo="メール明細No.001"),
        row(mail_id("b", 2), memo="メール明細No.002"),
    ])
    assert result["summary"]["probable_resend_groups"] == 0
    assert result["summary"]["canonical_transactions"] == 2


def test_identity_collision_is_not_probable_resend():
    result = reconcile_transactions([
        row(mail_id("a"), merchant="A"), row(mail_id("a"), merchant="B"),
    ])
    assert result["summary"]["identity_collisions"] == 1
    assert result["summary"]["probable_resend_groups"] == 0


def test_cross_source_strong_ambiguous_and_merchant_formatting():
    raw = row(mail_id("a"), merchant="Test Store")
    strong = reconcile_transactions([raw], [csv_row(merchant="ＴＥＳＴ－ＳＴＯＲＥ")])
    assert strong["transactions"][0].cross_source.state == "cross_source_strong_match"
    ambiguous = reconcile_transactions([raw], [
        csv_row("aupaycard:1", merchant="TEST STORE"),
        csv_row("aupaycard:2", merchant="TEST-STORE"),
    ])
    assert ambiguous["transactions"][0].cross_source.state == "cross_source_ambiguous"
    mismatch = reconcile_transactions([raw], [csv_row(merchant="OTHER STORE")])
    assert mismatch["transactions"][0].cross_source.state == "cross_source_ambiguous"


def test_reconciliation_is_deterministic_across_input_order():
    rows = [row(mail_id("b")), row(mail_id("a"))]
    forward = reconcile_transactions(rows)
    reverse = reconcile_transactions(list(reversed(rows)))
    assert forward["transactions"] == reverse["transactions"]
