from datetime import datetime

import pytest

from app.connectors.payment.aupay_card_mail import AuPayCardMailConnector


def message(subject="【ご利用詳細】au PAY カード", body="", **overrides):
    values = {
        "subject": subject,
        "sender": "au PAY カード <info@kddi-fs.com>",
        "body_text": body,
        "body_html": "",
        "source_provider_id": "gmail-card-1",
        "source_message_id": "<card-test@example.invalid>",
        "date": "Thu, 20 Aug 2026 09:00:00 +0900",
    }
    values.update(overrides)
    return values


def detail_block(number="001", merchant="テスト給油所", amount="3,549円", date="2026年8月8日"):
    return f"""No.{number}--------
▼ご利用日
{date}
▼ご利用金額
{amount}
▼ご利用先
{merchant}
支払い区分: 1回払い
摘要: テスト明細
"""


def detail_body(member="本会員", blocks=None):
    return f"""▼カード情報
au PAY カード（Mastercard）
{member}さま ご利用分

{blocks or detail_block()}
"""


def parse(subject="【ご利用詳細】au PAY カード", body=None, **overrides):
    connector = AuPayCardMailConnector()
    return connector.parse(message(subject, body or detail_body(), **overrides))


def test_non_aupay_card_mail_is_not_supported():
    raw = message(subject="通常のお知らせ", sender="shop@example.com", body="商品情報")
    assert not AuPayCardMailConnector().supports(raw)


def test_aupay_card_detail_is_supported():
    assert AuPayCardMailConnector().supports(message(body=detail_body()))


def test_authorization_event_mapping():
    event = parse(subject="【ご利用速報】au PAY カード")[0]
    assert (event.event_type, event.status, event.direction) == ("authorization", "pending", "debit")


def test_payment_confirmed_event_mapping():
    event = parse()[0]
    assert (event.event_type, event.status, event.direction) == ("payment_confirmed", "confirmed", "debit")


def test_refund_event_mapping_and_positive_amount():
    body = detail_body(blocks=detail_block(amount="1,478円（返品）"))
    event = parse(subject="【返金のお知らせ】au PAY カード", body=body)[0]
    assert (event.event_type, event.status, event.direction) == ("refund", "confirmed", "credit")
    assert event.amount == 1478


def test_reversal_event_mapping():
    event = parse(subject="【ご利用取消のお知らせ】au PAY カード")[0]
    assert (event.event_type, event.status, event.direction) == ("reversal", "confirmed", "credit")


def test_normal_payment_direction_is_debit():
    assert parse()[0].direction == "debit"


def test_primary_member_is_preserved():
    event = parse(body=detail_body("本会員"))[0]
    assert event.account_type == "primary"
    assert event.metadata["member"] == "本会員"


def test_family_member_is_preserved():
    event = parse(body=detail_body("家族会員"))[0]
    assert event.account_type == "family"
    assert event.metadata["member"] == "家族会員"


def test_merchant_is_nfkc_normalized_and_trimmed():
    event = parse(body=detail_body(blocks=detail_block(merchant="  ＡＢＣ ストア  ")))[0]
    assert event.merchant == "ABC ストア"


def test_amount_is_extracted():
    assert parse()[0].amount == 3549


def test_occurred_at_is_extracted_in_japan_timezone():
    event = parse()[0]
    assert event.occurred_at == datetime.fromisoformat("2026-08-08T00:00:00+09:00")


def test_multiple_details_create_multiple_events():
    body = detail_body(blocks=detail_block("001") + detail_block("002", "テスト書店", "1,050円"))
    events = parse(body=body)
    assert len(events) == 2
    assert [event.external_transaction_id for event in events] == ["No.001", "No.002"]


def test_multiple_details_have_distinct_event_ids():
    body = detail_body(blocks=detail_block("001") + detail_block("002"))
    events = parse(body=body)
    assert events[0].event_id != events[1].event_id


def test_repeated_parse_has_stable_event_id():
    raw = message(body=detail_body())
    connector = AuPayCardMailConnector()
    assert connector.parse(raw)[0].event_id == connector.parse(raw)[0].event_id


def test_source_message_id_is_preserved():
    assert parse()[0].source_message_id == "<card-test@example.invalid>"


def test_source_provider_id_is_preserved():
    assert parse()[0].source_provider_id == "gmail-card-1"


def test_connector_version_is_preserved():
    event = parse()[0]
    assert (event.source, event.connector, event.connector_version) == (
        "aupay_card", "aupay_card_mail", "aupay_card_mail_v1"
    )


def test_missing_message_ids_use_content_hash_fallback():
    raw = message(body=detail_body(), source_message_id=None, source_provider_id=None)
    connector = AuPayCardMailConnector()
    assert connector.parse(raw)[0].event_id == connector.parse(raw)[0].event_id


def test_forwarded_aupay_card_mail_is_supported():
    raw = message(
        sender="User <user@example.com>",
        body="From: info@kddi-fs.com\n" + detail_body(),
    )
    assert AuPayCardMailConnector().supports(raw)


def test_amazon_mail_is_not_misidentified():
    raw = message(
        subject="Amazon.co.jp ご注文の確認",
        sender="auto-confirm@amazon.co.jp",
        body="支払い方法: au PAY カード",
    )
    assert not AuPayCardMailConnector().supports(raw)


def test_incomplete_detail_does_not_discard_valid_detail():
    broken = """No.001--------
▼ご利用日
2026年8月8日
▼ご利用先
壊れた明細
"""
    valid = detail_block("002", "正常店舗", "500円")
    events = parse(body=detail_body(blocks=broken + valid))
    assert len(events) == 1
    assert events[0].merchant == "正常店舗"


def test_html_only_mail_is_parsed():
    raw = message(
        body_text="",
        body_html="""
        <h1>au PAY カード ご利用詳細</h1>
        <div>本会員さま ご利用分</div>
        <div>No.001--------</div>
        <div>▼ご利用日<br>2026年8月8日</div>
        <div>▼ご利用金額<br>600円</div>
        <div>▼ご利用先<br>HTML店舗</div>
        """,
    )
    event = AuPayCardMailConnector().parse(raw)[0]
    assert (event.merchant, event.amount) == ("HTML店舗", 600)


def test_payment_type_and_memo_are_preserved():
    event = parse()[0]
    assert event.metadata["payment_type"] == "1回払い"
    assert event.metadata["memo"] == "テスト明細"


def test_detail_number_is_preserved():
    event = parse()[0]
    assert event.metadata["detail_number"] == "No.001"


def test_explicit_transaction_id_takes_priority_over_detail_number():
    body = detail_body(blocks=detail_block() + "取引番号: TX-12345\n")
    assert parse(body=body)[0].external_transaction_id == "TX-12345"


def test_order_reference_is_preserved_when_present():
    body = detail_body(blocks=detail_block() + "注文番号: ORDER-12345\n")
    assert parse(body=body)[0].order_reference == "ORDER-12345"


@pytest.mark.parametrize(
    ("amount_text", "expected"),
    [("-1,478円", 1478), ("△1,478円", 1478), ("￥1,478円", 1478)],
)
def test_amount_sign_and_symbols_do_not_make_amount_negative(amount_text, expected):
    body = detail_body(blocks=detail_block(amount=amount_text))
    assert parse(subject="【返金のお知らせ】au PAY カード", body=body)[0].amount == expected


def test_refund_marker_in_detail_overrides_confirmed_mail_default():
    normal = detail_block("001", "通常店舗", "500円")
    refunded = detail_block("002", "返品店舗", "700円（返品）")
    events = parse(body=detail_body(blocks=normal + refunded))
    assert [event.event_type for event in events] == ["payment_confirmed", "refund"]
    assert [event.direction for event in events] == ["debit", "credit"]
