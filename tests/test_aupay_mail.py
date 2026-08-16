from email.message import EmailMessage

import pytest

from app.aupay_mail_pipeline import AuPayMailPipeline, parse_aupay_card_eml, parse_aupay_notice


NOTICE = """
au PAY ご利用のお知らせ
■利用店舗
テスト商店
■種別
支払
■利用日時
2026年8月15日(土) 12:34:56
■支払金額
1,234円
■伝票番号
123456789012
"""


class FakeDB:
    def __init__(self):
        self.rows = []

    def import_ids(self):
        return {row[0] for row in self.rows}

    def append(self, sheet, rows):
        assert sheet == "取込データ"
        self.rows.extend(rows)


def test_parse_aupay_notice():
    notice = parse_aupay_notice(NOTICE)
    assert notice.slip_number == "123456789012"
    assert notice.date == "2026-08-15"
    assert notice.merchant == "テスト商店"
    assert notice.amount == 1234
    assert notice.import_id == "aupay:123456789012"


def test_parser_fails_closed_without_slip_number():
    with pytest.raises(ValueError, match="伝票番号"):
        parse_aupay_notice(NOTICE.replace("■伝票番号\n123456789012", ""))


def test_parser_rejects_non_payment_notice():
    with pytest.raises(ValueError, match="種別が支払ではありません"):
        parse_aupay_notice(NOTICE.replace("■種別\n支払", "■種別\n取消"))


def test_slip_number_makes_import_idempotent():
    db = FakeDB()
    pipeline = AuPayMailPipeline(db)
    notice = parse_aupay_notice(NOTICE)
    assert pipeline.import_notice(notice) == "new"
    assert pipeline.import_notice(notice) == "unchanged"
    assert len(db.rows) == 1
    assert db.rows[0][2:5] == ["au PAY", "123456789012", "2026-08-15"]


def test_parse_multi_transaction_aupay_card_email(tmp_path):
    message = EmailMessage()
    message["Subject"] = "【ご利用詳細】au PAY カード"
    message["Message-ID"] = "<example-message@example.invalid>"
    message.set_content("""▼カード情報
au PAY カード（Mastercard）
家族会員さま ご利用分

No.001--------
▼ご利用日
2026年8月8日
▼ご利用金額
3,549円
▼ご利用先
テスト給油所

No.002--------
▼ご利用日
2026年8月13日
▼ご利用金額
1,050円
▼ご利用先
au PAY 残高オートチャージ（不足額）
""")
    path = tmp_path / "card.eml"
    path.write_bytes(message.as_bytes())
    rows = parse_aupay_card_eml(str(path))
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-08-08"
    assert rows[0]["amount"] == 3549
    assert rows[0]["merchant"] == "テスト給油所"
    assert rows[0]["member"] == "家族会員"
    assert rows[1]["import_id"].endswith(":002")
