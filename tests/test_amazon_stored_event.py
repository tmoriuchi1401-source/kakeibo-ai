from app.amazon_stored_event import (
    AMAZON_EVENT_TYPES,
    APPLY_STATUSES,
    MATCH_STATUSES,
    PARSE_STATUSES,
    AmazonStoredEvent,
)
from app.sheets import HEADERS, SheetsDB


EXPECTED_HEADERS = [
    "イベントID", "Gmail Message ID", "RFC Message-ID", "Thread ID", "Source Hash",
    "Event Type", "Order ID", "Event Date", "Charged Amount", "Order Amount",
    "Refund Amount", "Shipment Amount", "Gift Card Amount", "Points Amount",
    "Coupon Amount", "Discount Amount", "Payment Method", "Item Count", "Parse Status",
    "Match Status", "Apply Status", "Parser Version", "Imported At", "Last Parsed At",
]


def _stored_event() -> AmazonStoredEvent:
    return AmazonStoredEvent(
        event_id="evt-1",
        gmail_message_id="gmail-1",
        rfc_message_id="<mail@example.com>",
        thread_id="thread-1",
        source_hash="hash-1",
        event_type="shipment",
        order_id="123-1234567-1234567",
        event_date="2026-08-22",
        charged_amount=1200,
        order_amount=1300,
        refund_amount=None,
        shipment_amount=1200,
        gift_card_amount=100,
        points_amount=0,
        coupon_amount=None,
        discount_amount=100,
        payment_method="Visa",
        item_count=2,
        parse_status="parsed",
        match_status="unmatched",
        apply_status="pending",
        parser_version="phase-a-v1",
        imported_at="2026-08-22T10:00:00+09:00",
        last_parsed_at="2026-08-22T10:00:00+09:00",
    )


def test_amazon_event_headers_are_the_specified_24_columns():
    assert HEADERS["Amazonイベント"] == EXPECTED_HEADERS
    assert len(HEADERS["Amazonイベント"]) == 24


class _Request:
    def __init__(self, result=None):
        self.result = result or {}

    def execute(self):
        return self.result


class _Values:
    def __init__(self, existing, updates):
        self.existing = existing
        self.updates = updates

    def get(self, spreadsheetId, range):
        title = range.split("!", 1)[0]
        values = [self.existing[title]] if title in self.existing else []
        return _Request({"values": values})

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return _Request()


class _Spreadsheets:
    def __init__(self, existing):
        self.existing = existing
        self.batch_updates = []
        self.value_updates = []
        self.values_api = _Values(existing, self.value_updates)

    def get(self, spreadsheetId):
        sheets = [{"properties": {"title": title}} for title in self.existing]
        return _Request({"sheets": sheets})

    def batchUpdate(self, **kwargs):
        self.batch_updates.append(kwargs)
        return _Request()

    def values(self):
        return self.values_api


class _Service:
    def __init__(self, existing):
        self.api = _Spreadsheets(existing)

    def spreadsheets(self):
        return self.api


def test_ensure_schema_adds_only_amazon_event_to_an_existing_schema():
    existing = {title: header for title, header in HEADERS.items() if title != "Amazonイベント"}
    service = _Service(existing)

    SheetsDB("sheet-id", service=service).ensure_schema()

    requests = service.api.batch_updates[0]["body"]["requests"]
    assert requests == [{"addSheet": {"properties": {"title": "Amazonイベント"}}}]
    assert len(service.api.value_updates) == 1
    assert service.api.value_updates[0]["range"] == "Amazonイベント!A1"
    assert service.api.value_updates[0]["body"]["values"] == [EXPECTED_HEADERS]


def test_stored_event_row_has_24_columns():
    assert len(_stored_event().to_row()) == 24


def test_stored_event_row_order_matches_headers():
    row = _stored_event().to_row()
    mapped = dict(zip(HEADERS["Amazonイベント"], row, strict=True))

    assert mapped["イベントID"] == "evt-1"
    assert mapped["Gmail Message ID"] == "gmail-1"
    assert mapped["RFC Message-ID"] == "<mail@example.com>"
    assert mapped["Shipment Amount"] == 1200
    assert mapped["Refund Amount"] == ""
    assert mapped["Parse Status"] == "parsed"
    assert mapped["Last Parsed At"] == "2026-08-22T10:00:00+09:00"


def test_event_type_and_status_values_match_phase_b1_specification():
    assert AMAZON_EVENT_TYPES == (
        "order", "payment", "shipment", "delivery", "cancellation", "return", "refund",
        "unknown",
    )
    assert PARSE_STATUSES == ("parsed", "needs_review", "unusable")
    assert MATCH_STATUSES == (
        "unmatched", "matched", "order_not_found", "missing_order_id", "item_unresolved",
    )
    assert APPLY_STATUSES == ("pending", "no_action", "review")
