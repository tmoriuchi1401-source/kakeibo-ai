from app.amazon_order_header import (
    AMAZON_ORDER_SOURCES,
    ORDER_STATUSES,
    REFUND_STATUSES,
    AmazonOrderHeader,
)
from app.sheets import HEADERS


EXPECTED_HEADERS = [
    "Order ID", "Order Date", "Order Amount", "Payment Method", "Item Count",
    "Order Status", "Charged Amount", "Refund Status", "Refund Amount",
    "Shipment Amount", "Gift Card Amount", "Points Amount", "Discount Amount",
    "Source", "Last Updated At",
]
EXISTING_AMAZON_ORDER_HEADERS = [
    "Amazonキー", "Order ID", "ASIN", "注文日", "商品名", "数量", "商品金額", "支払方法",
    "大カテゴリ", "小カテゴリ", "備考", "データハッシュ", "最終取込日時", "発送日", "発送数",
]
EXISTING_AMAZON_EVENT_HEADERS = [
    "イベントID", "Gmail Message ID", "RFC Message-ID", "Thread ID", "Source Hash",
    "Event Type", "Order ID", "Event Date", "Charged Amount", "Order Amount",
    "Refund Amount", "Shipment Amount", "Gift Card Amount", "Points Amount",
    "Coupon Amount", "Discount Amount", "Payment Method", "Item Count", "Parse Status",
    "Match Status", "Apply Status", "Parser Version", "Imported At", "Last Parsed At",
]


def _order_header() -> AmazonOrderHeader:
    return AmazonOrderHeader(
        order_id="123-1234567-1234567",
        order_date="2026-08-22",
        order_amount=3000,
        payment_method=None,
        item_count=2,
        order_status="partially_shipped",
        charged_amount=2000,
        refund_status="partial",
        refund_amount=500,
        shipment_amount=None,
        gift_card_amount=1000,
        points_amount=100,
        discount_amount=None,
        source="mixed",
        last_updated_at="2026-08-23T10:00:00+09:00",
    )


def test_amazon_order_header_has_the_specified_15_columns():
    assert HEADERS["Amazon注文ヘッダ"] == EXPECTED_HEADERS
    assert len(HEADERS["Amazon注文ヘッダ"]) == 15


def test_amazon_order_header_row_has_15_columns_in_header_order():
    row = _order_header().to_row()
    assert len(row) == 15
    assert dict(zip(EXPECTED_HEADERS, row, strict=True)) == {
        "Order ID": "123-1234567-1234567",
        "Order Date": "2026-08-22",
        "Order Amount": 3000,
        "Payment Method": "",
        "Item Count": 2,
        "Order Status": "partially_shipped",
        "Charged Amount": 2000,
        "Refund Status": "partial",
        "Refund Amount": 500,
        "Shipment Amount": "",
        "Gift Card Amount": 1000,
        "Points Amount": 100,
        "Discount Amount": "",
        "Source": "mixed",
        "Last Updated At": "2026-08-23T10:00:00+09:00",
    }


def test_amazon_order_header_status_and_source_values_match_specification():
    assert ORDER_STATUSES == (
        "ordered", "partially_shipped", "shipped", "delivered", "cancelled",
    )
    assert REFUND_STATUSES == ("none", "partial", "full")
    assert AMAZON_ORDER_SOURCES == ("gmail", "csv", "mixed")


def test_existing_amazon_schemas_are_unchanged():
    assert HEADERS["Amazonイベント"] == EXISTING_AMAZON_EVENT_HEADERS
    assert HEADERS["Amazon注文"] == EXISTING_AMAZON_ORDER_HEADERS
