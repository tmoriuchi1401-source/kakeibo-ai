from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from ...events import PurchaseEvent
from ..base import RawMessage


CONNECTOR_NAME = "amazon_mail"
CONNECTOR_VERSION = "amazon_mail_v1"
SOURCE = "amazon"

_AMAZON_SENDERS = {
    "auto-confirm@amazon.co.jp",
    "shipment-tracking@amazon.co.jp",
    "order-update@amazon.co.jp",
    "return@amazon.co.jp",
}
_ORDER_ID_RE = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|\b)", re.I)
_AD_MARKERS = (
    "あなたにイチオシ",
    "おすすめ商品",
    "最近見た商品",
    "タイムセール",
    "関連商品",
)


class _MailHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hrefs: list[str] = []
        self.anchor_hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                href = html.unescape(href)
                self.hrefs.append(href)
                self.anchor_hrefs.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.anchor_hrefs:
            self.parts.append(f"\n{self.anchor_hrefs.pop()}\n")
        if tag in {"p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _html_content(value: str) -> tuple[str, list[str]]:
    parser = _MailHTMLParser()
    parser.feed(value or "")
    return "".join(parser.parts), parser.hrefs


def _normalize(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _message_content(message: RawMessage) -> tuple[str, list[str]]:
    html_text, hrefs = _html_content(str(message.get("body_html") or ""))
    text = "\n".join(
        part for part in (str(message.get("body_text") or ""), html_text) if part
    )
    return _normalize(text), hrefs


def _without_advertising(text: str) -> str:
    positions = [text.find(marker) for marker in _AD_MARKERS if marker in text]
    return text[: min(positions)].rstrip() if positions else text


def _money(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else None


def _label(text: str, labels: tuple[str, ...], pattern: str = r"[^\n]+") -> str | None:
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{names})\s*[：:]?\s*({pattern})", text, re.I)
    return match.group(1).strip() if match else None


def _amount(text: str, labels: tuple[str, ...]) -> int | None:
    value = _label(text, labels, r"[¥￥]?\s*[0-9][0-9,]*\s*円?")
    return _money(value or "")


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    match = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", normalized)
    if match:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=ZoneInfo("Asia/Tokyo"),
        )
    try:
        parsed = parsedate_to_datetime(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _event_kind(subject: str, text: str) -> tuple[str, str] | None:
    value = f"{subject}\n{text[:1000]}".lower()
    rules = (
        (("返金を処理", "返金が完了", "返金のお知らせ", "refund"), "refund", "refund_confirmed"),
        (("返品手続", "返品リクエスト", "返品を受け付け", "return requested"), "return", "return_requested"),
        (("キャンセルされました", "キャンセルしました", "注文のキャンセル", "cancelled"), "cancellation", "cancelled"),
        (("配達しました", "お届け済み", "配達完了", "delivered"), "shipment_update", "delivered"),
        (("配達中", "配送中", "delivering", "out for delivery"), "shipment_update", "delivering"),
        (("発送しました", "発送のお知らせ", "shipped"), "shipment_update", "shipped"),
        (("ご注文の確認", "注文を受け付け", "注文確認", "order confirmation"), "ordered", "ordered"),
    )
    for needles, event_type, status in rules:
        if any(needle in value for needle in needles):
            return event_type, status
    return None


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


def _order_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(_ORDER_ID_RE.finditer(text))
    blocks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(0), text[match.end():end].strip()))
    return blocks


def _item_blocks(order_block: str) -> list[str]:
    marker = re.compile(r"(?im)^\s*商品(?:名)?\s*[：:]\s*")
    matches = list(marker.finditer(order_block))
    if not matches:
        return [order_block]
    return [
        order_block[match.end():(matches[index + 1].start() if index + 1 < len(matches) else len(order_block))].strip()
        for index, match in enumerate(matches)
    ]


def _product_name(item_block: str) -> str | None:
    first = next((line.strip() for line in item_block.splitlines() if line.strip()), "")
    if not first or _ORDER_ID_RE.search(first):
        return None
    if re.match(r"^(数量|商品価格|価格|注文合計|お支払い額|支払額|ASIN|https?://)", first, re.I):
        return None
    return first


def _asin(text: str, hrefs: list[str]) -> str | None:
    match = _ASIN_RE.search(text)
    if match:
        return match.group(1).upper()
    for href in hrefs:
        match = _ASIN_RE.search(href)
        if match:
            return match.group(1).upper()
    return None


def _quantity(text: str) -> float | None:
    value = _label(text, ("数量",), r"[0-9]+(?:\.[0-9]+)?")
    return float(value) if value else None


class AmazonMailConnector:
    name = CONNECTOR_NAME
    version = CONNECTOR_VERSION

    def supports(self, message: RawMessage) -> bool:
        sender = str(message.get("sender") or "").lower()
        if any(address in sender for address in _AMAZON_SENDERS):
            return True
        text, _ = _message_content(message)
        forwarded_from = re.findall(r"(?im)^\s*from\s*[：:]\s*([^\n]+)", text)
        return any(
            any(address in value.lower() for address in _AMAZON_SENDERS)
            or "amazon.co.jp" in value.lower()
            for value in forwarded_from
        )

    def parse(self, message: RawMessage) -> list[PurchaseEvent]:
        if not self.supports(message):
            return []

        subject = _normalize(str(message.get("subject") or ""))
        full_text, hrefs = _message_content(message)
        text = _without_advertising(full_text)
        kind = _event_kind(subject, text)
        if kind is None:
            return []
        event_type, status = kind

        source_hash = _source_hash(message)
        provider_id = str(message.get("source_provider_id") or "") or None
        message_id = str(message.get("source_message_id") or "") or None
        source_identity = message_id or provider_id or source_hash
        raw_reference = f"gmail:{provider_id}" if provider_id else None
        message_date = _date(str(message.get("date") or ""))

        blocks = _order_blocks(text)
        if not blocks:
            return []

        events: list[PurchaseEvent] = []
        for order_id, order_block in blocks:
            order_total = _amount(order_block, ("注文合計", "合計"))
            payment_method = _label(order_block, ("支払い方法", "お支払い方法"))
            ordered_at = _date(_label(order_block, ("注文日", "ご注文日")))
            occurred_at = _date(_label(
                order_block,
                ("返金処理日", "発送日", "配達日", "返品受付日", "キャンセル日"),
            )) or message_date
            rma_id = _label(order_block, ("RMA ID", "返品受付ID", "返品ID"), r"[A-Za-z0-9-]+")
            return_deadline = _label(order_block, ("返送期限",))
            refund_method = _label(order_block, ("返金方法", "返金予定方法"))
            expected_deposit = _label(order_block, ("入金予定日",))
            charged_false = bool(re.search(r"請求は行われていません|請求されません", order_block))
            refund_amount = _amount(order_block, ("返金額", "返金予定額"))

            item_blocks = _item_blocks(order_block)
            for item_index, item_block in enumerate(item_blocks, start=1):
                product_name = _product_name(item_block)
                item_asin = _asin(item_block, hrefs if len(blocks) == 1 and len(item_blocks) == 1 else [])
                list_price = _amount(item_block, ("商品価格", "価格"))
                explicit_paid = _amount(item_block, ("商品のお支払い額", "お支払い額", "支払額"))
                paid_amount = (
                    refund_amount
                    if event_type == "refund" and len(item_blocks) == 1
                    else explicit_paid
                )
                metadata: dict[str, Any] = {}
                if list_price is not None and explicit_paid is None and event_type == "ordered":
                    metadata["allocation_pending"] = True
                if charged_false:
                    metadata["charged"] = False
                if event_type == "return" and refund_amount is not None:
                    key = "estimated_refund_amount" if len(item_blocks) == 1 else "order_estimated_refund_amount"
                    metadata[key] = refund_amount
                    paid_amount = None
                if event_type == "refund" and refund_amount is not None and len(item_blocks) > 1:
                    metadata["order_refund_amount"] = refund_amount
                    metadata["allocation_pending"] = True
                if rma_id:
                    metadata["rma_id"] = rma_id
                if return_deadline:
                    metadata["return_deadline"] = return_deadline
                if refund_method:
                    metadata["refund_method"] = refund_method
                if expected_deposit:
                    metadata["expected_deposit_date"] = expected_deposit
                if product_name is None:
                    metadata.setdefault("parse_warnings", []).append("product_name_not_found")

                item_identity = item_asin or product_name or f"item-{item_index}"
                event_key = "|".join((
                    SOURCE,
                    source_identity,
                    event_type,
                    status,
                    order_id,
                    item_identity,
                    str(item_index),
                ))
                event_id = "amazon-mail:" + hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:24]
                events.append(PurchaseEvent(
                    event_id=event_id,
                    source=SOURCE,
                    connector=CONNECTOR_NAME,
                    connector_version=CONNECTOR_VERSION,
                    event_type=event_type,
                    status=status,
                    external_order_id=order_id,
                    external_item_id=item_asin,
                    merchant="Amazon",
                    ordered_at=ordered_at,
                    occurred_at=occurred_at,
                    product_name=product_name,
                    quantity=_quantity(item_block),
                    list_price=list_price,
                    paid_amount=paid_amount,
                    order_total=order_total,
                    currency="JPY",
                    direction="credit" if event_type == "refund" else "debit",
                    payment_method=payment_method,
                    source_message_id=message_id,
                    source_provider_id=provider_id,
                    source_hash=source_hash,
                    raw_reference=raw_reference,
                    metadata=metadata,
                ))
        return events
