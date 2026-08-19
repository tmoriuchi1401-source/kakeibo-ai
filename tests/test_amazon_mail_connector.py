import pytest

from app.connectors.commerce.amazon_mail import AmazonMailConnector


ORDER_1 = "123-1234567-1234567"
ORDER_2 = "456-7654321-7654321"
ASIN_1 = "B012345678"
ASIN_2 = "B087654321"


def message(subject="Amazon.co.jp ご注文の確認", body="", **overrides):
    values = {
        "subject": subject,
        "sender": "Amazon.co.jp <auto-confirm@amazon.co.jp>",
        "body_text": body,
        "body_html": "",
        "source_provider_id": "gmail-provider-1",
        "source_message_id": "<amazon-test@example.invalid>",
        "date": "Wed, 19 Aug 2026 10:00:00 +0900",
    }
    values.update(overrides)
    return values


def ordered_block(order_id=ORDER_1, product="テスト商品", asin=ASIN_1):
    link = f"https://www.amazon.co.jp/dp/{asin}" if asin else ""
    return f"""注文番号: {order_id}
注文日: 2026年8月19日
商品: {product}
数量: 1
商品価格: 1,200円
{link}
注文合計: 853円
支払い方法: テストカード
"""


def parse(subject="Amazon.co.jp ご注文の確認", body=None, **overrides):
    connector = AmazonMailConnector()
    return connector.parse(message(subject, body or ordered_block(), **overrides))


def test_non_amazon_mail_is_not_supported():
    connector = AmazonMailConnector()
    assert not connector.supports(message(sender="shop@example.com", body="通常のお知らせ"))


def test_amazon_order_mail_is_supported():
    assert AmazonMailConnector().supports(message(body=ordered_block()))


def test_single_item_order():
    event = parse()[0]
    assert event.external_order_id == ORDER_1
    assert event.product_name == "テスト商品"
    assert event.quantity == 1
    assert event.event_type == "ordered"
    assert event.status == "ordered"


def test_multiple_orders_in_one_mail_are_separated():
    events = parse(body=ordered_block(ORDER_1, "商品1", ASIN_1) + ordered_block(ORDER_2, "商品2", ASIN_2))
    assert [event.external_order_id for event in events] == [ORDER_1, ORDER_2]


def test_multiple_items_in_one_order():
    body = f"""注文番号: {ORDER_1}
注文日: 2026年8月19日
商品: 商品1
数量: 1
商品価格: 500円
https://amazon.co.jp/dp/{ASIN_1}
商品: 商品2
数量: 2
商品価格: 700円
https://amazon.co.jp/dp/{ASIN_2}
注文合計: 1,900円
"""
    events = parse(body=body)
    assert [(event.product_name, event.quantity) for event in events] == [("商品1", 1), ("商品2", 2)]


def test_asin_is_extracted_from_product_url():
    assert parse()[0].external_item_id == ASIN_1


def test_missing_asin_is_not_inferred():
    assert parse(body=ordered_block(asin=None))[0].external_item_id is None


@pytest.mark.parametrize("marker", ["あなたにイチオシ", "おすすめ商品", "最近見た商品", "タイムセール", "関連商品"])
def test_advertising_sections_are_excluded(marker):
    body = ordered_block() + f"\n{marker}\n商品: 広告商品\nhttps://amazon.co.jp/dp/{ASIN_2}\n"
    events = parse(body=body)
    assert len(events) == 1
    assert events[0].external_item_id == ASIN_1


def test_advertising_asin_is_not_used_when_purchased_item_has_no_asin():
    body = ordered_block(asin=None) + f"\nおすすめ商品\nhttps://amazon.co.jp/dp/{ASIN_2}\n"
    assert parse(body=body)[0].external_item_id is None


def test_list_price_and_order_total_remain_distinct():
    event = parse()[0]
    assert event.list_price == 1200
    assert event.order_total == 853


def test_unknown_item_paid_amount_is_not_allocated():
    event = parse()[0]
    assert event.paid_amount is None
    assert event.metadata["allocation_pending"] is True


@pytest.mark.parametrize(
    ("subject", "event_type", "status"),
    [
        ("Amazon.co.jp 発送しました", "shipment_update", "shipped"),
        ("Amazon.co.jp 配達中です", "shipment_update", "delivering"),
        ("Amazon.co.jp 配達しました", "shipment_update", "delivered"),
    ],
)
def test_shipment_status_events(subject, event_type, status):
    event = parse(subject=subject)[0]
    assert (event.event_type, event.status) == (event_type, status)
    assert event.external_order_id == ORDER_1


def test_cancelled_event():
    event = parse(subject="商品が正常にキャンセルされました")[0]
    assert (event.event_type, event.status) == ("cancellation", "cancelled")


def test_cancelled_without_charge_is_recorded_in_metadata():
    body = ordered_block() + "\nこの注文の請求は行われていません\n"
    event = parse(subject="商品が正常にキャンセルされました", body=body)[0]
    assert event.metadata["charged"] is False


def test_return_requested_is_not_treated_as_confirmed_refund():
    body = ordered_block() + "\n返金予定額: 853円\n返送期限: 2026年9月1日\nRMA ID: RMA-123\n"
    event = parse(subject="返品リクエストを受け付けました", body=body)[0]
    assert (event.event_type, event.status, event.direction) == ("return", "return_requested", "debit")
    assert event.paid_amount is None
    assert event.metadata["estimated_refund_amount"] == 853
    assert event.metadata["rma_id"] == "RMA-123"


def test_refund_confirmed_event_and_credit_direction():
    body = ordered_block() + "\n返金額: 853円\n返金方法: テストカード\n返金処理日: 2026年8月20日\n"
    event = parse(subject="返金を処理しました", body=body)[0]
    assert (event.event_type, event.status) == ("refund", "refund_confirmed")
    assert event.direction == "credit"
    assert event.paid_amount == 853
    assert event.metadata["refund_method"] == "テストカード"


def test_parse_is_idempotent_for_event_id():
    raw = message(body=ordered_block())
    connector = AmazonMailConnector()
    assert connector.parse(raw)[0].event_id == connector.parse(raw)[0].event_id


def test_forwarded_amazon_mail_is_supported():
    raw = message(
        sender="User <user@example.com>",
        body="From: auto-confirm@amazon.co.jp\n" + ordered_block(),
    )
    assert AmazonMailConnector().supports(raw)


def test_forwarded_amazon_display_name_is_supported():
    raw = message(sender="User <user@example.com>", body="From: Amazon.co.jp\n" + ordered_block())
    assert AmazonMailConnector().supports(raw)


def test_source_ids_and_connector_version_are_preserved():
    event = parse()[0]
    assert event.source_provider_id == "gmail-provider-1"
    assert event.source_message_id == "<amazon-test@example.invalid>"
    assert event.connector_version == "amazon_mail_v1"


def test_missing_message_ids_use_content_hash_fallback():
    raw = message(body=ordered_block(), source_provider_id=None, source_message_id=None)
    connector = AmazonMailConnector()
    assert connector.parse(raw)[0].event_id == connector.parse(raw)[0].event_id


def test_asin_can_be_extracted_from_html_href():
    raw = message(
        body_text=f"注文番号: {ORDER_1}\n注文日: 2026年8月19日\n商品: HTML商品\n数量: 1\n注文合計: 500円",
        body_html=f'<a href="https://amazon.co.jp/dp/{ASIN_1}">HTML商品</a>',
    )
    assert AmazonMailConnector().parse(raw)[0].external_item_id == ASIN_1


def test_unrecognized_amazon_template_returns_no_events():
    raw = message(subject="Amazon.co.jpからのお知らせ", body=f"注文番号: {ORDER_1}\n情報")
    assert AmazonMailConnector().parse(raw) == []
