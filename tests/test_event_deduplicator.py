from copy import deepcopy
from datetime import datetime, timezone

from app.events import PaymentEvent, PurchaseEvent
from app.matching.event_deduplicator import EventDeduplicator, canonical_fingerprint


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def purchase(**changes):
    values = dict(
        event_id="purchase-1", source="amazon", connector="amazon_mail",
        connector_version="amazon_mail_v1", event_type="ordered", status="ordered",
        direction="debit", source_hash="hash-1", external_order_id="ORDER-1",
        external_item_id="ASIN-1", merchant="Amazon", ordered_at=NOW,
        occurred_at=NOW, list_price=1000, paid_amount=900, order_total=900,
    )
    values.update(changes)
    return PurchaseEvent(**values)


def payment(**changes):
    values = dict(
        event_id="payment-1", source="aupay_card", connector="aupay_card_mail",
        connector_version="aupay_card_mail_v1", event_type="payment_confirmed",
        status="confirmed", direction="debit", source_hash="hash-2",
        merchant="ABC Store", occurred_at=NOW, amount=1000, currency="JPY",
        payment_method="au PAY Card", account_type="primary",
        external_transaction_id="TX-1", metadata={"mail_item_index": 1},
    )
    values.update(changes)
    return PaymentEvent(**values)


DEDUP = EventDeduplicator()


def test_same_purchase_is_duplicate():
    assert DEDUP.compare(purchase(), purchase(event_id="other")).status == "duplicate"


def test_same_payment_is_duplicate():
    assert DEDUP.compare(payment(), payment(event_id="other")).status == "duplicate"


def test_same_identity_with_status_change_is_revision():
    result = DEDUP.compare(purchase(status="shipped"), purchase())
    assert result.status == "revision"
    assert result.changed_fields == ["status"]


def test_same_identity_with_amount_change_is_revision_and_reports_field():
    result = DEDUP.compare(payment(amount=1100), payment())
    assert result.status == "revision"
    assert "amount" in result.changed_fields


def test_different_purchase_order_is_new():
    assert DEDUP.compare(purchase(external_order_id="ORDER-2"), purchase()).status == "new"


def test_same_order_with_different_asin_is_new():
    assert DEDUP.compare(purchase(external_item_id="ASIN-2"), purchase()).status == "new"


def test_different_payment_transaction_is_new():
    assert DEDUP.compare(payment(external_transaction_id="TX-2"), payment()).status == "new"


def test_merchant_amount_date_only_is_possible_duplicate():
    incoming = payment(external_transaction_id=None, account_type="family")
    existing = payment(external_transaction_id=None, account_type="primary")
    assert DEDUP.compare(incoming, existing).status == "possible_duplicate"


def test_same_shop_amount_date_with_distinct_ids_is_not_duplicate():
    assert DEDUP.compare(payment(external_transaction_id="TX-2"), payment()).status == "new"


def test_purchase_without_item_id_can_use_message_identity():
    values = dict(external_item_id=None, source_message_id="<mail-1>")
    assert DEDUP.compare(purchase(**values), purchase(event_id="other", **values)).status == "duplicate"


def test_payment_without_transaction_id_can_use_fingerprint():
    assert DEDUP.compare(
        payment(external_transaction_id=None),
        payment(event_id="other", external_transaction_id=None),
    ).status == "duplicate"


def test_source_message_id_and_mail_index_are_strong_payment_identity():
    values = dict(external_transaction_id=None, source_message_id="<mail-1>")
    assert DEDUP.compare(payment(**values), payment(event_id="other", **values)).status == "duplicate"


def test_source_provider_id_is_purchase_fallback():
    values = dict(external_item_id=None, source_provider_id="provider-1")
    assert DEDUP.compare(purchase(**values), purchase(event_id="other", **values)).status == "duplicate"


def test_event_id_difference_does_not_prevent_duplicate():
    assert DEDUP.compare(payment(event_id="new-id"), payment(event_id="old-id")).status == "duplicate"


def test_observation_only_changes_are_ignored():
    incoming = payment(
        connector_version="v2", raw_reference="gmail:new", metadata={"new": True},
    )
    existing = payment(
        connector_version="v1", raw_reference="gmail:old", metadata={"old": True},
    )
    assert DEDUP.compare(incoming, existing).status == "duplicate"


def test_cross_connector_same_source_business_key_is_duplicate():
    incoming = purchase(connector="amazon_csv", connector_version="csv_v1")
    assert DEDUP.compare(incoming, purchase()).status == "duplicate"


def test_multiple_equally_strong_candidates_are_not_auto_duplicate():
    result = DEDUP.find_best_duplicate(
        payment(), [payment(event_id="old-1"), payment(event_id="old-2")]
    )
    assert result.status == "possible_duplicate"
    assert result.candidate_event_ids == ["old-1", "old-2"]


def test_refund_and_payment_are_not_duplicate():
    incoming = payment(event_type="refund", direction="credit")
    assert DEDUP.compare(incoming, payment()).status != "duplicate"


def test_authorization_and_confirmed_payment_are_not_duplicate():
    incoming = payment(event_type="authorization", status="pending")
    assert DEDUP.compare(incoming, payment()).status != "duplicate"


def test_reversal_and_original_payment_are_not_duplicate():
    incoming = payment(event_type="reversal", direction="credit")
    assert DEDUP.compare(incoming, payment()).status != "duplicate"


def test_same_transaction_with_incompatible_type_and_amount_is_conflict():
    incoming = payment(event_type="refund", direction="credit", amount=9000)
    result = DEDUP.compare(incoming, payment())
    assert result.status == "conflict"
    assert {"event_type", "amount"}.issubset(result.changed_fields)


def test_small_amount_difference_across_event_types_is_only_possible_duplicate():
    incoming = payment(event_type="refund", direction="credit", amount=1100)
    assert DEDUP.compare(incoming, payment()).status == "possible_duplicate"


def test_fingerprint_is_stable_and_ignores_parser_observation_fields():
    first = purchase()
    second = deepcopy(first).model_copy(update={
        "event_id": "other", "connector_version": "v2", "raw_reference": "other",
        "metadata": {"parse_warning": "changed"},
    })
    assert canonical_fingerprint(first) == canonical_fingerprint(first)
    assert canonical_fingerprint(first) == canonical_fingerprint(second)


def test_timezone_equivalent_instants_have_same_fingerprint():
    local = datetime.fromisoformat("2026-08-20T09:00:00+09:00")
    assert canonical_fingerprint(payment(occurred_at=local)) == canonical_fingerprint(payment())


def test_empty_candidate_list_is_new():
    assert DEDUP.find_best_duplicate(payment(), []).status == "new"
