from email.message import EmailMessage
import sys

import pytest

from app.amazon_email import (
    diagnose_amazon_email_money_context,
    diagnose_amazon_email_structure,
    parse_amazon_email,
)


ORDER_ID = "123-1234567-1234567"


def mail(subject: str, body: str, *, message_id="<fixture@example.invalid>") -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Amazon.co.jp <no-reply@amazon.co.jp>"
    message["To"] = "customer@example.invalid"
    message["Date"] = "Wed, 19 Aug 2026 10:00:00 +0900"
    if message_id:
        message["Message-ID"] = message_id
    message.set_content(body)
    return message.as_bytes()


def test_order_email_extracts_order_information():
    event = parse_amazon_email(mail("Amazon.co.jp ご注文の確認", f"""
注文番号: {ORDER_ID}
注文日: 2026年8月18日
注文合計: 1,500円
支払い方法: Visa
商品点数: 2
"""))
    assert event.event_type == "order"
    assert event.order_id == ORDER_ID
    assert event.event_date == "2026-08-18"
    assert event.order_amount == 1500
    assert event.item_count == 2


def test_shipment_email_extracts_shipment_amount():
    event = parse_amazon_email(mail("Amazon.co.jp 発送のお知らせ", f"""
注文番号: {ORDER_ID}
発送日: 2026/08/19
発送分合計: 980円
"""))
    assert event.event_type == "shipment"
    assert event.shipment_amount == 980


def test_delivery_email_is_recognized():
    event = parse_amazon_email(mail("配達しました", f"注文番号: {ORDER_ID}"))
    assert event.event_type == "delivery"


def test_cancellation_email_is_recognized():
    event = parse_amazon_email(mail("商品が正常にキャンセルされました", f"注文番号: {ORDER_ID}"))
    assert event.event_type == "cancellation"


def test_return_email_is_recognized():
    event = parse_amazon_email(mail("返品リクエストを受け付けました", f"注文番号: {ORDER_ID}"))
    assert event.event_type == "return"


def test_refund_email_extracts_refund_not_charge():
    event = parse_amazon_email(mail("返金を処理しました", f"""
注文番号: {ORDER_ID}
返金額: 853円
返金方法: Visa
"""))
    assert event.event_type == "refund"
    assert event.refund_amount == 853
    assert event.charged_amount is None


def test_charged_amount_and_order_amount_are_distinct():
    event = parse_amazon_email(mail("Amazon.co.jp ご請求のお知らせ", f"""
注文番号: {ORDER_ID}
注文合計: 1,500円
カードへのご請求額: 1,120円
ギフトカード利用額: 300円
Amazonポイント利用額: 80円
支払い方法: Visa
請求確定日: 2026年8月20日
"""))
    assert event.event_type == "payment"
    assert event.charged_amount == 1120
    assert event.order_amount == 1500
    assert event.gift_card_amount == 300
    assert event.points_amount == 80
    assert event.gift_card_used and event.points_used


def test_usage_without_amount_sets_flags_but_does_not_guess_amounts():
    event = parse_amazon_email(mail("Amazon.co.jp ご注文の確認", f"""
注文番号: {ORDER_ID}
ギフトカードを使用
Amazonポイントを使用
"""))
    assert event.gift_card_used and event.points_used
    assert event.gift_card_amount is None
    assert event.points_amount is None


def test_email_without_amounts_keeps_amounts_none():
    event = parse_amazon_email(mail("Amazon.co.jp 発送のお知らせ", f"注文番号: {ORDER_ID}"))
    assert event.charged_amount is None
    assert event.order_amount is None
    assert event.shipment_amount is None


def test_message_id_and_source_hash_are_stable():
    raw = mail("Amazon.co.jp ご注文の確認", f"注文番号: {ORDER_ID}")
    first = parse_amazon_email(raw)
    second = parse_amazon_email(raw)
    assert first.message_id == "<fixture@example.invalid>"
    assert first.source_hash == second.source_hash


def test_result_does_not_retain_personal_information():
    event = parse_amazon_email(mail("Amazon.co.jp ご注文の確認", f"""
注文番号: {ORDER_ID}
氏名: テスト太郎
住所: 東京都テスト区1-2-3
電話番号: 000-0000-0000
追跡番号: TRACK-SECRET
"""))
    serialized = repr(event)
    assert "テスト太郎" not in serialized
    assert "東京都" not in serialized
    assert "TRACK-SECRET" not in serialized


def test_unknown_amazon_email_is_safe():
    event = parse_amazon_email(mail("Amazon.co.jpからのお知らせ", "一般的なお知らせです"))
    assert event.event_type == "unknown"
    assert event.order_id is None
    assert event.charged_amount is None


def test_html_email_is_supported():
    message = EmailMessage()
    message["Subject"] = "Amazon.co.jp ご請求のお知らせ"
    message["Message-ID"] = "<html@example.invalid>"
    message.set_content("HTML版をご覧ください")
    message.add_alternative(
        f"<div>注文番号: {ORDER_ID}</div><div>カード請求額: 500円</div>",
        subtype="html",
    )
    assert parse_amazon_email(message).charged_amount == 500


def test_cli_preview_is_anonymized(tmp_path, monkeypatch, capsys):
    path = tmp_path / "amazon.eml"
    path.write_bytes(mail("Amazon.co.jp ご請求のお知らせ", f"""
注文番号: {ORDER_ID}
カード請求額: 500円
支払い方法: Visa 末尾1234
"""))
    import app.cli as cli
    monkeypatch.setattr(sys, "argv", ["app.cli", "amazon-email-preview", str(path)])
    cli.main()
    output = capsys.readouterr().out
    assert "'charged_amount': 500" in output
    assert "'order_id_present': True" in output
    assert ORDER_ID not in output
    assert "Visa" not in output
    assert "1234" not in output


def test_invalid_input_type_is_rejected():
    with pytest.raises(TypeError):
        parse_amazon_email("not bytes")


def test_structure_diagnostic_for_plain_email_and_order_id_only():
    result = diagnose_amazon_email_structure(mail(
        "Amazon.co.jpのお知らせ", f"注文番号: {ORDER_ID}",
    ))
    assert result["has_text_plain"] is True
    assert result["has_text_html"] is False
    assert result["money_candidate_count"] == 0
    assert result["keywords"]["order_number"] is True
    assert result["order_id_present"] is True
    assert result["parser_failure_reason"] == "order_id_only"


def test_structure_diagnostic_for_html_table_and_json_ld():
    message = EmailMessage()
    message["Subject"] = "Amazon.co.jpのお知らせ"
    message.set_content("", subtype="plain")
    message.add_alternative("""
<html><body><table><tr><td>特別価格 1,234円</td></tr></table>
<script type="application/ld+json">{"kind": "test"}</script></body></html>
""", subtype="html")
    result = diagnose_amazon_email_structure(message)
    assert result["has_text_html"] is True
    assert result["multipart"] is True
    assert result["money_candidate_count"] == 1
    assert result["html_structure"]["table_present"] is True
    assert result["html_structure"]["json_ld_present"] is True
    assert result["html_structure"]["script_json_present"] is True
    assert result["html_structure"]["visible_money_candidate_count"] == 1
    assert result["parser_failure_reason"] == "money_present_but_labels_unknown"


def test_structure_diagnostic_for_html_only_email():
    message = EmailMessage()
    message["Subject"] = "Amazon.co.jp"
    message.set_content("<div>注文合計: ￥500</div>", subtype="html")
    result = diagnose_amazon_email_structure(message)
    assert result["has_text_plain"] is False
    assert result["has_text_html"] is True
    assert result["html_visible_length_band"] != "0"
    assert result["keywords"]["order_total"] is True
    assert result["money_candidate_count"] == 1


def test_structure_diagnostic_detects_attachment_without_name_output():
    message = EmailMessage()
    message.set_content("本文")
    message.add_attachment(b"secret", maintype="application", subtype="octet-stream", filename="private.txt")
    result = diagnose_amazon_email_structure(message)
    assert result["attachment_present"] is True
    assert "private.txt" not in repr(result)


def test_empty_html_only_body_reports_extraction_failure():
    message = EmailMessage()
    message["Subject"] = "Amazon.co.jp"
    message.set_content("<div></div>", subtype="html")
    result = diagnose_amazon_email_structure(message)
    assert result["has_text_plain"] is False
    assert result["has_text_html"] is True
    assert result["parser_failure_reason"] == "html_only_not_extracted"


def html_mail(body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Amazon.co.jp"
    message.set_content("HTMLをご覧ください")
    message.add_alternative(body, subtype="html")
    return message


def test_billing_label_near_money_is_transaction_likely():
    result = diagnose_amazon_email_money_context(html_mail(
        "<table><tr><td>カードへのご請求額</td><td>¥987654</td></tr></table>",
    ))
    assert result["transaction_likely_count"] == 1
    assert result["message_classification"] == "transaction_amount_present"
    assert "summary_row" in result["table_structure_categories"]


def test_order_id_in_same_row_is_transaction_likely():
    result = diagnose_amazon_email_money_context(html_mail(
        f"<table><tr><td>注文番号 {ORDER_ID}</td><td>987654円</td></tr></table>",
    ))
    assert result["transaction_likely_count"] == 1
    assert "same_block" in result["order_id_proximity"]


def test_product_link_price_is_advertisement_likely():
    result = diagnose_amazon_email_money_context(html_mail(
        '<table><tr><td><img src="x"><a href="https://amazon.co.jp/dp/B000000000">'
        "おすすめ商品 詳細を見る</a></td><td>¥987654</td></tr></table>",
    ))
    assert result["advertisement_likely_count"] == 1
    assert result["message_classification"] == "advertisement_prices_only"
    assert "product_card_row" in result["table_structure_categories"]
    assert "amazon_product_page" in result["link_context_categories"]


def test_transaction_and_advertisement_mixed_context():
    result = diagnose_amazon_email_money_context(html_mail(f"""
<table><tr><td>注文番号 {ORDER_ID} ご請求額</td><td>¥987654</td></tr>
<tr><td><img src="x"><a href="https://amazon.co.jp/dp/B000000000">おすすめ商品</a></td>
<td>¥123456</td></tr></table>
"""))
    assert result["transaction_likely_count"] == 1
    assert result["advertisement_likely_count"] == 1
    assert result["message_classification"] == "mixed_context"


def test_no_money_candidates_and_order_distance():
    result = diagnose_amazon_email_money_context(mail("Amazon", f"注文番号: {ORDER_ID}"))
    assert result["money_candidate_count"] == 0
    assert result["message_classification"] == "no_money_candidates"
    assert result["order_id_proximity"] == ["absent"]


def test_plain_and_html_same_candidate_is_anonymously_correlated():
    message = EmailMessage()
    message.set_content("ご請求額 ¥987654")
    message.add_alternative("<div>ご請求額 ¥987654</div>", subtype="html")
    result = diagnose_amazon_email_money_context(message)
    assert result["money_candidate_count"] == 1
    assert result["source_presence_patterns"] == {"both": 1}


def test_html_only_candidate_does_not_output_value_or_url():
    message = EmailMessage()
    message.set_content(
        '<a href="https://amazon.co.jp/dp/B000000000?private=yes">おすすめ ¥987654</a>',
        subtype="html",
    )
    result = diagnose_amazon_email_money_context(message)
    serialized = repr(result)
    assert result["source_presence_patterns"] == {"html_only": 1}
    assert "987654" not in serialized
    assert "https://" not in serialized
    assert "private" not in serialized
