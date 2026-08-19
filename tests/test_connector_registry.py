from app.connectors.registry import ConnectorRegistry
from app.events import PurchaseEvent


class FakeCommerceConnector:
    def __init__(self, name="test_commerce"):
        self.name = name

    def supports(self, message):
        return message.get("kind") == "commerce"

    def parse(self, message):
        return [PurchaseEvent(
            event_id="purchase:registry",
            source="test",
            connector=self.name,
            connector_version="v1",
            event_type="ordered",
            status="ordered",
            product_name=message["product_name"],
            direction="debit",
            source_hash="registry-hash",
        )]


def test_registry_selects_one_connector_and_parses():
    registry = ConnectorRegistry([FakeCommerceConnector()])
    result = registry.dispatch({"kind": "commerce", "product_name": "商品"})
    assert result.status == "processed"
    assert result.connector_name == "test_commerce"
    assert len(result.events) == 1


def test_registry_skips_unsupported_message():
    registry = ConnectorRegistry([FakeCommerceConnector()])
    result = registry.dispatch({"kind": "unknown"})
    assert result.status == "skipped"
    assert result.events == ()


def test_registry_reports_ambiguous_without_parsing():
    registry = ConnectorRegistry([
        FakeCommerceConnector(),
        FakeCommerceConnector("also_commerce"),
    ])
    result = registry.dispatch({"kind": "commerce", "product_name": "商品"})
    assert result.status == "ambiguous"
    assert result.events == ()
    assert result.matched_connectors == ("test_commerce", "also_commerce")
