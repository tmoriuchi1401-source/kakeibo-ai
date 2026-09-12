from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import re

import pytest

from app.aupay_card_apply_plan import (
    build_canonical_apply_plan,
    production_import_status,
)
from app.aupay_card_pipeline import AuPayCardPipeline
from app.aupay_card_contract import (
    aupay_card_source_hash,
    canonical_aupay_card_source_data,
    format_import_timestamp,
)
from app.aupay_mail_pipeline import parse_aupay_card_raw
from app.utils import canonical_hash
from app.transaction_plan import reconcile_transactions


def detail(number, merchant, amount, date="2026年8月8日"):
    return f"""No.{number:03d}--------
▼ご利用日
{date}
▼ご利用金額
{amount:,}円
▼ご利用先
{merchant}
"""


def raw_message(*blocks, message_id="<unit-card-message@example.invalid>"):
    message = EmailMessage()
    message["Subject"] = "【ご利用詳細】au PAY カード"
    message["Message-ID"] = message_id
    message.set_content(
        "▼カード情報\nau PAY カード\n本会員さま ご利用分\n\n"
        + "\n".join(blocks)
    )
    return message.as_bytes()


def raw(identity="aupaycard-mail:" + "a" * 24 + ":001", **changes):
    value = {
        "import_id": identity,
        "date": "2026-08-08",
        "merchant": "テスト店",
        "amount": 1200,
        "transaction_kind": "purchase",
        "payment_type": "メール通知",
        "member": "本会員",
        "memo": "メール明細No.001",
        "source_memo": "",
        "source_occurrence": 1,
    }
    value.update(changes)
    return value


def candidate(**changes):
    source = raw(**changes)
    reconciliation = reconcile_transactions([source], [])
    plan = build_canonical_apply_plan({
        "collection_complete": True,
        "listing_complete": True,
        "collection_truncated": False,
        "gmail_list_failed": 0,
        "gmail_read_failed": 0,
        "needs_review": 0,
    }, reconciliation)
    return plan.candidates[0]


def test_existing_and_gmail_paths_share_canonical_source_hash_contract():
    source = raw(payment_type="通常払い")
    canonical = canonical_aupay_card_source_data(source)

    assert tuple(canonical) == (
        "date", "merchant", "amount", "payment_type", "member", "memo",
        "occurrence", "import_id",
    )
    assert aupay_card_source_hash(source) == canonical_hash(canonical)
    assert re.fullmatch(r"[0-9a-f]{64}", aupay_card_source_hash(source))


def test_existing_writer_and_canonical_candidate_hash_same_source_input_identically():
    source = raw(
        identity="aupaycard:" + "b" * 24,
        payment_type="通常払い",
        memo="",
        source_memo="",
    )

    class DB:
        def __init__(self):
            self.appended = []

        def import_ids(self):
            return set()

        def get(self, value):
            assert value == "Amazon注文!A2:M"
            return []

        def append(self, sheet, rows):
            assert sheet == "取込データ"
            self.appended.extend(rows)

    db = DB()
    AuPayCardPipeline(db).import_transactions([source])
    projected = candidate(**source)
    canonical_row = projected.to_import_row(
        imported_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        status="auto_expense",
    )

    assert db.appended[0][10] == canonical_row[10] == aupay_card_source_hash(source)


def test_gmail_parser_uses_normal_payment_and_full_source_hash_not_business_fingerprint():
    parsed = parse_aupay_card_raw(raw_message(detail(1, "テスト店", 1200)))[0]
    projected = candidate(**parsed)
    row = projected.to_import_row(
        imported_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        status="auto_expense",
    )

    assert parsed["payment_type"] == "通常払い"
    assert row[7] == "通常払い"
    assert re.fullmatch(r"[0-9a-f]{64}", row[10])
    assert row[10] == parsed["source_hash"]
    assert row[10] != projected.business_fingerprint


def test_gmail_source_occurrence_is_business_duplicate_count_not_mail_item_number():
    parsed = parse_aupay_card_raw(raw_message(
        detail(2, "同一店", 1200),
        detail(9, "同一店", 1200),
    ))

    assert [item["source_occurrence"] for item in parsed] == [1, 2]
    assert parsed[0]["source_hash"] != parsed[1]["source_hash"]


def test_import_timestamp_requires_timezone_and_formats_in_jst():
    instant = datetime(2026, 9, 10, 1, 2, 3, tzinfo=timezone.utc)
    assert format_import_timestamp(instant) == "2026-09-10 10:02:03"
    assert format_import_timestamp(instant.astimezone(timezone(timedelta(hours=-4)))) == (
        "2026-09-10 10:02:03"
    )
    with pytest.raises(ValueError, match="timezone_required"):
        format_import_timestamp(datetime(2026, 9, 10, 1, 2, 3))


@pytest.mark.parametrize(("merchant", "amazon_status", "expected"), [
    ("フィットネス会費(FIT365)", None, "auto_expense"),
    ("au PAY 残高オートチャージ(不足額)", None, "transfer_aupay_charge"),
    ("AMAZON.CO.JP", "matched_amazon", "matched_amazon"),
    ("AMAZON.CO.JP", "amazon_needs_review", "amazon_needs_review"),
    ("AMAZON.CO.JP", "amazon_unmatched", "amazon_unmatched"),
])
def test_production_status_preserves_existing_special_classification(
    merchant, amazon_status, expected,
):
    assert production_import_status(
        candidate(merchant=merchant), amazon_status=amazon_status,
    ) == expected


def test_amazon_is_never_promoted_without_existing_classification():
    with pytest.raises(ValueError, match="amazon_classification_required"):
        production_import_status(candidate(merchant="AMAZON.CO.JP"))


def test_source_specific_unclassified_status_is_not_materializable():
    with pytest.raises(ValueError, match="unclassified_status_forbidden"):
        candidate().to_import_row(
            imported_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            status="unclassified_card",
        )
