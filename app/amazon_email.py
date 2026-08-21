from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import re
import unicodedata


ORDER_ID_RE = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")
AMOUNT = r"[￥¥]?\s*([0-9][0-9,]*)\s*円"


@dataclass(frozen=True)
class AmazonMailEvent:
    event_type: str
    order_id: str | None
    event_date: str | None
    charged_amount: int | None
    order_amount: int | None
    refund_amount: int | None
    gift_card_amount: int | None
    points_amount: int | None
    coupon_amount: int | None
    discount_amount: int | None
    payment_method: str | None
    shipment_amount: int | None
    item_count: int | None
    message_id: str | None
    source_hash: str
    gift_card_used: bool
    points_used: bool

    def anonymized(self) -> dict:
        values = asdict(self)
        return {
            "event_type": self.event_type,
            "event_date": self.event_date,
            "order_id_present": self.order_id is not None,
            "charged_amount": self.charged_amount,
            "order_amount": self.order_amount,
            "refund_amount": self.refund_amount,
            "gift_card_amount": self.gift_card_amount,
            "points_amount": self.points_amount,
            "coupon_amount": self.coupon_amount,
            "discount_amount": self.discount_amount,
            "shipment_amount": self.shipment_amount,
            "item_count": self.item_count,
            "payment_method_present": self.payment_method is not None,
            "message_id_present": self.message_id is not None,
            "gift_card_used": values["gift_card_used"],
            "points_used": values["points_used"],
            "source_hash": self.source_hash,
        }


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"br", "p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _message(value: bytes | Message) -> tuple[Message, bytes]:
    if isinstance(value, bytes):
        return BytesParser(policy=policy.default).parsebytes(value), value
    if isinstance(value, Message):
        raw = value.as_bytes(policy=policy.default)
        return value, raw
    raise TypeError("raw_email must be MIME bytes or email.message.Message")


def _body(message: Message) -> str:
    parts: list[str] = []
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/html":
            parser = _HTMLText()
            parser.feed(str(content))
            content = "".join(parser.parts)
        parts.append(str(content))
    return _normalize("\n".join(parts))


def _amount(text: str, labels: tuple[str, ...]) -> int | None:
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{names})\s*[：:]?\s*{AMOUNT}", text, re.I)
    return int(match.group(1).replace(",", "")) if match else None


def _label(text: str, labels: tuple[str, ...]) -> str | None:
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{names})\s*[：:]?\s*([^\n]+)", text, re.I)
    return match.group(1).strip() if match else None


def _event_type(subject: str, text: str) -> str:
    value = f"{subject}\n{text[:1500]}".lower()
    rules = (
        (("返金を処理", "返金が完了", "返金のお知らせ", "refund"), "refund"),
        (("返品手続", "返品リクエスト", "返品を受け付け", "return"), "return"),
        (("キャンセルされました", "注文のキャンセル", "cancelled"), "cancellation"),
        (("配達しました", "お届け済み", "配達完了", "delivered"), "delivery"),
        (("発送しました", "発送のお知らせ", "shipped"), "shipment"),
        (("請求額", "請求金額", "お支払いが確定", "charge"), "payment"),
        (("ご注文の確認", "注文を受け付け", "注文確認", "order confirmation"), "order"),
    )
    for needles, kind in rules:
        if any(needle in value for needle in needles):
            return kind
    return "unknown"


def _date(text: str, message: Message, event_type: str) -> str | None:
    labels = {
        "shipment": ("発送日",), "delivery": ("配達日",),
        "cancellation": ("キャンセル日",), "refund": ("返金処理日", "返金日"),
        "return": ("返品受付日",), "payment": ("請求確定日", "請求日"),
        "order": ("注文日", "ご注文日"),
    }.get(event_type, ())
    candidate = _label(text, labels) if labels else None
    if candidate:
        match = re.search(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", candidate)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    try:
        parsed = parsedate_to_datetime(message.get("Date", ""))
        return parsed.date().isoformat() if parsed else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_amazon_email(raw_email: bytes | Message) -> AmazonMailEvent:
    message, raw = _message(raw_email)
    subject = _normalize(str(message.get("Subject", "")))
    text = _body(message)
    event_type = _event_type(subject, text)
    order_match = ORDER_ID_RE.search(text)
    payment_method = _label(text, ("支払い方法", "お支払い方法", "返金方法"))
    item_count_value = _label(text, ("商品点数", "商品の数", "商品数"))
    item_count_match = re.search(r"\d+", item_count_value or "")
    gift_amount = _amount(text, ("ギフトカード利用額", "Amazonギフトカード利用額"))
    points_amount = _amount(text, ("Amazonポイント利用額", "ポイント利用額"))
    return AmazonMailEvent(
        event_type=event_type,
        order_id=order_match.group(0) if order_match else None,
        event_date=_date(text, message, event_type),
        charged_amount=_amount(text, (
            "カードへのご請求額", "カード請求額", "実際の請求額", "今回の請求額", "ご請求額", "請求金額",
        )),
        order_amount=_amount(text, ("注文合計", "ご注文合計")),
        refund_amount=_amount(text, ("返金額", "返金予定額")),
        gift_card_amount=gift_amount,
        points_amount=points_amount,
        coupon_amount=_amount(text, ("クーポン", "クーポン割引")),
        discount_amount=_amount(text, ("割引額", "プロモーション割引")),
        payment_method=payment_method,
        shipment_amount=_amount(text, ("発送分合計", "今回発送分の合計", "発送商品合計")),
        item_count=int(item_count_match.group(0)) if item_count_match else None,
        message_id=str(message.get("Message-ID")).strip() if message.get("Message-ID") else None,
        source_hash=hashlib.sha256(raw).hexdigest(),
        gift_card_used=gift_amount is not None or bool(re.search(r"ギフトカード(?:を)?使用", text)),
        points_used=points_amount is not None or bool(re.search(r"(?:Amazon)?ポイント(?:を)?使用", text)),
    )
