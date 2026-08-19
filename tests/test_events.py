from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.events import PaymentEvent, PurchaseEvent


NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def purchase(**overrides):
    values = {
        "event_id": "purchase:1",
        "source": "amazon",
        "connector": "amazon_mail",
        "connector_version": "amazon_mail_v1",
        "event_type": "ordered",
        "status": "ordered",
        "external_order_id": "ORDER-1",
        "external_item_id": "ITEM-1",
        "merchant": "Amazon.co.jp",
        "ordered_at": NOW,
        "occurred_at": NOW,
        "product_name": "商品",
        "quantity": 1,
        "list_price": 1200,
        "paid_amount": 1000,
        "order_total": 2000,
        "category": "日用品/その他",
        "currency": "JPY",
        "direction": "debit",
        "payment_method": "カード",
        "source_message_id": "<message@example.invalid>",
        "source_provider_id": "gmail-message-1",
        "source_hash": "hash-1",
        "raw_reference": "gmail:gmail-message-1",
        "metadata": {"items": [{"asin": "ASIN-1"}], "retry": 0},
    }
    values.update(overrides)
    return PurchaseEvent(**values)


def payment(**overrides):
    values = {
        "event_id": "payment:1",
        "source": "au_pay_card",
        "connector": "au_pay_card_mail",
        "connector_version": "au_pay_card_mail_v1",
        "account_type": "credit_card",
        "payment_method": "au PAY カード",
        "merchant": "Amazon.co.jp",
        "occurred_at": NOW,
        "amount": 1000,
        "currency": "JPY",
        "status": "confirmed",
        "event_type": "payment_confirmed",
        "direction": "debit",
        "external_transaction_id": "TX-1",
        "order_reference": "ORDER-1",
        "source_message_id": "<card@example.invalid>",
        "source_provider_id": "gmail-message-2",
        "source_hash": "hash-2",
        "metadata": {"installments": 1, "labels": ["card", "confirmed"]},
    }
    values.update(overrides)
    return PaymentEvent(**values)


def test_purchase_event_normal_generation_and_versioned_source_ids():
    event = purchase()
    assert event.external_order_id == "ORDER-1"
    assert event.list_price == 1200
    assert event.paid_amount == 1000
    assert event.order_total == 2000
    assert event.connector_version == "amazon_mail_v1"
    assert event.source_provider_id == "gmail-message-1"
    assert event.source_message_id == "<message@example.invalid>"


def test_payment_event_normal_generation():
    event = payment()
    assert event.amount == 1000
    assert event.event_type == "payment_confirmed"
    assert event.order_reference == "ORDER-1"


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (purchase, "list_price"),
        (purchase, "paid_amount"),
        (purchase, "order_total"),
        (payment, "amount"),
    ],
)
def test_negative_amount_is_rejected(factory, field):
    with pytest.raises(ValidationError):
        factory(**{field: -1})


@pytest.mark.parametrize("direction", ["debit", "credit"])
def test_debit_and_credit_are_supported(direction):
    assert payment(direction=direction).direction == direction


def test_cancellation_and_refund_are_distinct_from_status():
    cancelled = purchase(event_type="cancellation", status="cancelled")
    refunded = purchase(event_type="refund", status="refund_confirmed", direction="credit")
    assert cancelled.event_type != refunded.event_type
    assert cancelled.status != refunded.status


def test_parent_event_relation_is_preserved():
    event = payment(
        event_type="refund",
        status="refund_confirmed",
        direction="credit",
        parent_event_id="payment:original",
    )
    assert event.parent_event_id == "payment:original"


def test_external_purchase_ids_can_be_null():
    event = purchase(external_order_id=None, external_item_id=None)
    assert event.external_order_id is None
    assert event.external_item_id is None


def test_metadata_accepts_json_compatible_nested_values_without_shared_default():
    first = purchase(metadata={"nested": {"ok": True}, "values": [1, None, "x"]})
    second = purchase(event_id="purchase:2", metadata={})
    first.metadata["new"] = 1
    assert first.metadata["nested"] == {"ok": True}
    assert "new" not in second.metadata


def test_event_type_supports_future_connector_values():
    event = purchase(event_type="shipment_update", status="delivering")
    assert (event.event_type, event.status) == ("shipment_update", "delivering")
