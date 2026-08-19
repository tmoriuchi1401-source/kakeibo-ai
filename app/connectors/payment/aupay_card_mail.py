from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from ...events import PaymentEvent
from ..base import RawMessage


CONNECTOR_NAME = "aupay_card_mail"
CONNECTOR_VERSION = "aupay_card_mail_v1"
SOURCE = "aupay_card"

_KNOWN_SENDER_DOMAINS = ("kddi-fs.com", "aupay-card.com")
_CARD_MARKER_RE = re.compile(r"au\s*PAY\s*カード", re.I)
_DETAIL_RE = re.compile(r"ご利用詳細|利用詳細|ご利用明細|利用明細")
_AUTHORIZATION_RE = re.compile(r"ご利用速報|利用速報")
_REFUND_RE = re.compile(r"返品|返金")
_REVERSAL_RE = re.compile(r"ご利用取消|利用取消|取消のお知らせ|利用を取り消し|キャンセル")
_BLOCK_RE = re.compile(r"(?im)^\s*No\.\s*(\d+)\s*-*\s*$")
_MEMBER_SECTION_RE = re.compile(r"(本会員|家族会員)さま\s*ご利用分")


def _normalize(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    value = re.sub(r"(?i)</?(?:p|div|tr|li|h[1-6]|table|section)[^>]*>", "\n", value)
    return re.sub(r"<[^>]+>", "", value)


def _message_text(message: RawMessage) -> str:
    parts = (
        str(message.get("body_text") or ""),
        _html_to_text(str(message.get("body_html") or "")),
    )
    return _normalize("\n".join(part for part in parts if part))


def _field(text: str, labels: tuple[str, ...], pattern: str = r"[^\n]+") -> str | None:
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{names})\s*[：:]?\s*({pattern})", text, re.I)
    return match.group(1).strip() if match else None


def _money(text: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    value = _normalize(text)
    match = re.search(
        r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?"
        r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        value,
    )
    if match:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4) or 0),
            int(match.group(5) or 0),
            int(match.group(6) or 0),
            tzinfo=ZoneInfo("Asia/Tokyo"),
        )
    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _message_kind(subject: str, text: str) -> tuple[str, str, str] | None:
    if _REFUND_RE.search(subject):
        return "refund", "confirmed", "credit"
    if _REVERSAL_RE.search(subject):
        return "reversal", "confirmed", "credit"
    if _AUTHORIZATION_RE.search(subject):
        return "authorization", "pending", "debit"
    if _DETAIL_RE.search(subject):
        return "payment_confirmed", "confirmed", "debit"
    value = text[:1200]
    if _REFUND_RE.search(value):
        return "refund", "confirmed", "credit"
    if _REVERSAL_RE.search(value):
        return "reversal", "confirmed", "credit"
    if _AUTHORIZATION_RE.search(value):
        return "authorization", "pending", "debit"
    if _DETAIL_RE.search(value):
        return "payment_confirmed", "confirmed", "debit"
    return None


def _block_kind(default: tuple[str, str, str], block: str) -> tuple[str, str, str]:
    if _REFUND_RE.search(block):
        return "refund", "confirmed", "credit"
    if _REVERSAL_RE.search(block):
        return "reversal", "confirmed", "credit"
    return default


def _blocks(text: str) -> list[tuple[str | None, int, str, str | None]]:
    markers = list(_BLOCK_RE.finditer(text))
    if not markers:
        return [(None, 1, text, None)]

    blocks = []
    for index, marker in enumerate(markers):
        next_marker_start = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        block_end = next_marker_start
        next_member = _MEMBER_SECTION_RE.search(text, marker.end(), next_marker_start)
        if next_member:
            block_end = next_member.start()

        preceding_members = list(_MEMBER_SECTION_RE.finditer(text, 0, marker.start()))
        member = preceding_members[-1].group(1) if preceding_members else None
        blocks.append((
            f"No.{int(marker.group(1)):03d}",
            index + 1,
            text[marker.end():block_end].strip(),
            member,
        ))
    return blocks


def _member(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"(本会員|家族会員)さま\s*ご利用分", text)
    if not match:
        match = re.search(r"(?:ご利用者|会員種別)\s*[：:]?\s*(本会員|家族会員)", text)
    if not match:
        return None, None
    member = match.group(1)
    return ("primary" if member == "本会員" else "family"), member


def _source_hash(message: RawMessage) -> str:
    stable = "\n".join(
        str(message.get(key) or "")
        for key in (
            "subject",
            "sender",
            "body_text",
            "body_html",
            "source_provider_id",
            "source_message_id",
        )
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


class AuPayCardMailConnector:
    name = CONNECTOR_NAME
    version = CONNECTOR_VERSION

    def supports(self, message: RawMessage) -> bool:
        subject = _normalize(str(message.get("subject") or ""))
        text = _message_text(message)
        if not _CARD_MARKER_RE.search(f"{subject}\n{text[:1000]}"):
            return False
        if _message_kind(subject, text) is None:
            return False

        sender = _normalize(str(message.get("sender") or "")).lower()
        if any(domain in sender for domain in _KNOWN_SENDER_DOMAINS):
            return True
        forwarded = re.findall(r"(?im)^\s*from\s*[：:]\s*([^\n]+)", text)
        return any(any(domain in value.lower() for domain in _KNOWN_SENDER_DOMAINS)
                   for value in forwarded)

    def parse(self, message: RawMessage) -> list[PaymentEvent]:
        if not self.supports(message):
            return []

        subject = _normalize(str(message.get("subject") or ""))
        text = _message_text(message)
        default_kind = _message_kind(subject, text)
        if default_kind is None:
            return []

        source_hash = _source_hash(message)
        provider_id = str(message.get("source_provider_id") or "") or None
        message_id = str(message.get("source_message_id") or "") or None
        source_identity = message_id or provider_id or source_hash
        message_date = _date(str(message.get("date") or ""))
        global_account_type, global_member = _member(text)
        card_match = re.search(r"au\s*PAY\s*カード(?:\s*[（(]([^）)]+)[）)])?", text, re.I)
        payment_method = "au PAY カード"
        if card_match and card_match.group(1):
            payment_method += f" ({card_match.group(1).strip()})"

        events: list[PaymentEvent] = []
        for detail_number, item_index, block, section_member in _blocks(text):
            amount_raw = _field(
                block,
                ("▼ご利用金額", "ご利用金額", "利用金額", "返金額", "取消金額"),
                r"[+\-−△]?\s*[¥￥]?\s*[0-9][0-9,]*\s*円?(?:\s*[（(][^）)]+[）)])?",
            )
            amount = _money(amount_raw or "")
            if amount is None:
                continue

            merchant = _field(
                block,
                ("▼ご利用先", "ご利用先", "利用先", "加盟店名"),
            )
            merchant = _normalize(merchant or "") or None
            occurred_raw = _field(
                block,
                ("▼ご利用日時", "ご利用日時", "利用日時", "▼ご利用日", "ご利用日", "利用日"),
                r"20\d{2}[年/-]\d{1,2}[月/-]\d{1,2}日?"
                r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
            )
            occurred_at = _date(occurred_raw) or message_date
            event_type, status, direction = _block_kind(default_kind, block)
            account_type, member = _member(block)
            if section_member:
                account_type = "primary" if section_member == "本会員" else "family"
                member = section_member
            account_type = account_type or global_account_type
            member = member or global_member
            payment_type = _field(block, ("支払い区分", "お支払い区分"))
            memo = _field(block, ("摘要", "備考"))
            transaction_id = _field(
                block,
                ("取引番号", "承認番号", "伝票番号"),
                r"[A-Za-z0-9-]{4,40}",
            ) or detail_number
            order_reference = _field(
                block,
                ("注文番号", "オーダー番号"),
                r"[A-Za-z0-9-]{4,40}",
            )

            metadata: dict[str, Any] = {"mail_item_index": item_index}
            if detail_number:
                metadata["detail_number"] = detail_number
            if member:
                metadata["member"] = member
            if payment_type:
                metadata["payment_type"] = payment_type
            if memo:
                metadata["memo"] = memo
            if amount_raw:
                metadata["amount_text"] = amount_raw

            event_key = "|".join((
                SOURCE,
                source_identity,
                event_type,
                transaction_id or "",
                merchant or "",
                occurred_at.isoformat() if occurred_at else "",
                str(amount),
                str(item_index),
            ))
            event_id = "aupay-card-mail:" + hashlib.sha256(
                event_key.encode("utf-8")
            ).hexdigest()[:24]
            events.append(PaymentEvent(
                event_id=event_id,
                source=SOURCE,
                connector=CONNECTOR_NAME,
                connector_version=CONNECTOR_VERSION,
                account_type=account_type,
                payment_method=payment_method,
                merchant=merchant,
                occurred_at=occurred_at,
                amount=amount,
                currency="JPY",
                status=status,
                event_type=event_type,
                direction=direction,
                external_transaction_id=transaction_id,
                order_reference=order_reference,
                source_message_id=message_id,
                source_provider_id=provider_id,
                source_hash=source_hash,
                raw_reference=f"gmail:{provider_id}" if provider_id else None,
                metadata=metadata,
            ))
        return events
