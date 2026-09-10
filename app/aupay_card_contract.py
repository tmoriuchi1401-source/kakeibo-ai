"""Shared materialization contract for au PAY card import rows."""
from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

from .utils import canonical_hash


SOURCE_HASH_FIELDS = (
    "date", "merchant", "amount", "payment_type", "member", "memo",
    "occurrence", "import_id",
)


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def normalize_card_payment_method(value: object) -> str:
    """Map transport provenance to the established card payment contract."""
    payment = _text(value)
    return "通常払い" if not payment or payment == "メール通知" else payment


def canonical_aupay_card_source_data(raw: dict) -> dict:
    """Return the exact normalized object historically hashed into column K."""
    source_memo = raw["source_memo"] if "source_memo" in raw else raw.get("memo", "")
    occurrence = raw.get("source_occurrence", raw.get("occurrence", 1))
    try:
        amount = int(raw.get("amount"))
        occurrence = int(occurrence)
    except (TypeError, ValueError) as exc:
        raise ValueError("card_source_hash_numeric_field_invalid") from exc
    data = {
        "date": _text(raw.get("date")),
        "merchant": _text(raw.get("merchant")),
        "amount": amount,
        "payment_type": normalize_card_payment_method(raw.get("payment_type")),
        "member": _text(raw.get("member")),
        "memo": _text(source_memo),
        "occurrence": occurrence,
        "import_id": _text(raw.get("import_id")),
    }
    if (
        not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["date"])
        or not data["merchant"]
        or not data["import_id"]
        or amount == 0
        or occurrence <= 0
    ):
        raise ValueError("card_source_hash_canonical_data_invalid")
    return data


def aupay_card_source_hash(raw: dict) -> str:
    """Hash canonical source data using the established 64-hex SHA-256 contract."""
    return canonical_hash(canonical_aupay_card_source_data(raw))


def format_import_timestamp(value: datetime) -> str:
    """Format an aware execution timestamp exactly like existing import rows."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("import_timestamp_timezone_required")
    return value.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")


def is_aupay_charge_merchant(merchant: object) -> bool:
    normalized = _text(merchant).upper()
    return (
        "AU PAY 残高オートチャージ" in normalized
        or "AU PAY 残高チャージ" in normalized
    )


def is_amazon_merchant(merchant: object) -> bool:
    normalized = _text(merchant).upper()
    return "AMAZON.CO.JP" in normalized or "アマゾン" in normalized
