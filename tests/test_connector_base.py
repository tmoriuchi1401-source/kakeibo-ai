from app.connectors.base import CommerceConnector, PaymentConnector
from app.events import PaymentEvent, PurchaseEvent


def purchase(**overrides):
    values = {
        "event_id": "purchase:connector",
        "source": "test",
        "connector": "test_commerce",
        "connector_version": "v1",
        "event_type": "ordered",
        "status": "ordered",
        "direction": "debit",
        "source_hash": "purchase-hash",
    }
    values.update(overrides)
    return PurchaseEvent(**values)


def payment(**overrides):
    values = {
        "event_id": "payment:connector",
        "source": "test",
        "connector": "test_payment",
        "connector_version": "v1",
        "event_type": "payment",
        "status": "pending",
        "direction": "debit",
        "amount": 0,
        "source_hash": "payment-hash",
    }
    values.update(overrides)
    return PaymentEvent(**values)


class FakeCommerceConnector:
    name = "test_commerce"

    def supports(self, message):
        return message.get("kind") == "commerce"

    def parse(self, message):
        return [purchase(product_name=message["product_name"])]


class FakePaymentConnector:
    name = "test_payment"

    def supports(self, message):
        return message.get("kind") == "payment"

    def parse(self, message):
        return [payment(amount=message["amount"])]


def test_commerce_connector_supports_and_parse_contract():
    connector: CommerceConnector = FakeCommerceConnector()
    assert connector.supports({"kind": "commerce", "product_name": "商品"})
    assert connector.parse({"kind": "commerce", "product_name": "商品"})[0].product_name == "商品"


def test_payment_connector_supports_and_parse_contract():
    connector: PaymentConnector = FakePaymentConnector()
    assert connector.supports({"kind": "payment", "amount": 500})
    assert connector.parse({"kind": "payment", "amount": 500})[0].amount == 500
