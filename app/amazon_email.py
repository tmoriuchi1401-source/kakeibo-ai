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
    plain, html_visible, _ = _body_parts(message)
    return _normalize("\n".join(part for part in (plain, html_visible) if part))


def _part_content(part: Message) -> str:
    try:
        return str(part.get_content())
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _body_parts(message: Message) -> tuple[str, str, list[str]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    html_sources: list[str] = []
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        content = _part_content(part)
        if content_type == "text/html":
            html_sources.append(content)
            parser = _HTMLText()
            parser.feed(content)
            html_parts.append("".join(parser.parts))
        else:
            plain_parts.append(content)
    return (
        _normalize("\n".join(plain_parts)),
        _normalize("\n".join(html_parts)),
        html_sources,
    )


def _length_band(length: int) -> str:
    if length <= 0:
        return "0"
    if length <= 500:
        return "1-500"
    if length <= 2000:
        return "501-2000"
    if length <= 5000:
        return "2001-5000"
    return "5001+"


def _money_candidates(text: str) -> list[str]:
    return re.findall(r"(?:[￥¥]\s*[0-9][0-9,]*|[0-9][0-9,]*\s*円)", text)


def diagnose_amazon_email_structure(
    raw_email: bytes | Message,
    event: AmazonMailEvent | None = None,
) -> dict:
    message, _ = _message(raw_email)
    plain, html_visible, html_sources = _body_parts(message)
    combined = _normalize("\n".join(part for part in (plain, html_visible) if part))
    subject = _normalize(str(message.get("Subject", "")))
    searchable = f"{subject}\n{combined}"
    raw_symbols = f"{subject}\n{plain}\n{html_visible}"
    event = event or parse_amazon_email(raw_email)
    parts = list(message.walk()) if message.is_multipart() else [message]
    has_plain_part = any(part.get_content_type() == "text/plain" for part in parts)
    keyword_terms = {
        "order": "注文", "polite_order": "ご注文", "order_number": "注文番号",
        "total": "合計", "order_total": "注文合計", "polite_billing": "ご請求",
        "billing": "請求", "payment": "支払い", "polite_payment": "お支払い",
        "amount": "金額", "yen_kanji": "円", "points": "ポイント",
        "amazon_points": "Amazonポイント", "gift": "ギフト",
        "gift_card": "ギフトカード", "coupon": "クーポン", "discount": "割引",
        "shipment": "発送", "delivery": "お届け", "refund": "返金",
        "return": "返品", "cancellation": "キャンセル",
    }
    keywords = {name: term in searchable for name, term in keyword_terms.items()}
    keywords["yen_fullwidth"] = "￥" in raw_symbols
    keywords["yen_ascii"] = "¥" in raw_symbols
    money_count = len(_money_candidates(combined))
    html_joined = "\n".join(html_sources)
    html_lower = html_joined.lower()
    script_blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", html_joined, re.I | re.S)
    html_structure = {
        "table_present": bool(re.search(r"<table\b", html_joined, re.I)),
        "json_ld_present": bool(re.search(
            r"<script\b[^>]*type\s*=\s*['\"]application/ld\+json['\"]", html_joined, re.I,
        )),
        "script_json_present": any("{" in block and ":" in block for block in script_blocks),
        "hidden_text_heavy": sum(
            html_lower.count(marker) for marker in ("display:none", "display: none", "visibility:hidden", " hidden")
        ) >= 3,
        "visible_money_candidate_count": len(_money_candidates(html_visible)),
    }
    amount_fields = (
        event.charged_amount, event.order_amount, event.refund_amount,
        event.gift_card_amount, event.points_amount, event.coupon_amount,
        event.discount_amount, event.shipment_amount,
    )
    if html_sources and not has_plain_part and not html_visible:
        failure = "html_only_not_extracted"
    elif not combined:
        failure = "no_body_text"
    elif event.event_type != "unknown":
        failure = "other"
    elif event.order_id is not None and money_count == 0:
        failure = "order_id_only"
    elif money_count > 0 and not any(value is not None for value in amount_fields):
        failure = "money_present_but_labels_unknown"
    elif any(keywords.values()):
        failure = "labels_not_recognized"
    else:
        failure = "template_unknown"
    attachment_present = any(
        part.get_content_disposition() == "attachment" or part.get_filename() is not None
        for part in parts
    )
    return {
        "has_text_plain": has_plain_part,
        "has_text_html": bool(html_sources),
        "multipart": message.is_multipart(),
        "mime_part_count": len(parts),
        "attachment_present": attachment_present,
        "plain_length_band": _length_band(len(plain)),
        "html_visible_length_band": _length_band(len(html_visible)),
        "body_length_band": _length_band(len(combined)),
        "money_candidate_count": money_count,
        "money_candidate_band": "0" if money_count == 0 else "1" if money_count == 1 else "2-5" if money_count <= 5 else "6+",
        "keywords": keywords,
        "html_structure": html_structure,
        "order_id_present": event.order_id is not None,
        "parser_failure_reason": failure,
    }


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
