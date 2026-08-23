from __future__ import annotations

from collections import Counter
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
        if tag in {"br", "p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "tr", "td", "th", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _HTMLContext(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._tables: list[dict] = []
        self._rows: list[dict] = []
        self._cells: list[list[str]] = []
        self._link_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "table":
            table = {
                "rows": [], "links": [], "image_count": 0, "linked_image_count": 0,
                "parent": self._tables[-1] if self._tables else None,
            }
            self.tables.append(table)
            self._tables.append(table)
        elif tag == "tr" and self._tables:
            row = {
                "parts": [], "cells": [], "links": [],
                "image_count": 0, "linked_image_count": 0,
                "table": self._tables[-1],
            }
            self._tables[-1]["rows"].append(row)
            self._rows.append(row)
        elif tag in {"td", "th"} and self._rows:
            self._cells.append([])
        elif tag == "a":
            self._link_depth += 1
            href = attributes.get("href")
            if href:
                if self._rows:
                    self._rows[-1]["links"].append(href)
                for table in self._tables:
                    table["links"].append(href)
        elif tag == "img":
            if self._rows:
                self._rows[-1]["image_count"] += 1
                self._rows[-1]["linked_image_count"] += int(self._link_depth > 0)
            for table in self._tables:
                table["image_count"] += 1
                table["linked_image_count"] += int(self._link_depth > 0)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._rows and self._cells:
            self._rows[-1]["cells"].append(_normalize("".join(self._cells.pop())))
        elif tag == "tr" and self._rows:
            row = self._rows.pop()
            row["text"] = _normalize(" ".join(row.pop("parts")))
        elif tag == "table" and self._tables:
            self._tables.pop()
        elif tag == "a" and self._link_depth:
            self._link_depth -= 1

    def handle_data(self, data: str) -> None:
        for row in self._rows:
            row["parts"].append(data)
        if self._cells:
            self._cells[-1].append(data)


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


TRANSACTION_TERMS = (
    "注文", "ご注文", "注文番号", "請求", "ご請求", "合計", "注文合計",
    "支払い", "お支払い", "決済", "料金", "金額", "小計", "税", "送料",
    "ポイント", "ギフト", "クーポン", "割引", "返金",
)
STRONG_TRANSACTION_TERMS = (
    "注文番号", "請求", "ご請求", "注文合計", "支払い", "お支払い", "決済",
)
ADVERTISEMENT_TERMS = (
    "おすすめ", "あわせて購入", "関連商品", "セール", "タイムセール", "今すぐ購入",
    "詳細を見る", "商品ページ", "カート", "Prime", "商品", "関連", "購入", "詳細",
)
ADJACENT_TRANSACTION_TERMS = (
    "注文", "注文番号", "注文合計", "合計", "小計", "請求", "支払い", "税", "送料",
    "ポイント", "ギフト", "割引",
)
ADJACENT_ADVERTISEMENT_TERMS = (
    "商品", "おすすめ", "関連", "セール", "カート", "Prime", "購入", "詳細",
)


def _amount_occurrences(text: str) -> list[dict]:
    found = []
    for match in re.finditer(r"(?:[￥¥]\s*([0-9][0-9,]*)|([0-9][0-9,]*)\s*円)", text):
        digits = (match.group(1) or match.group(2)).replace(",", "")
        found.append({"value": int(digits), "start": match.start(), "end": match.end()})
    return found


def _link_categories(urls: list[str]) -> set[str]:
    categories: set[str] = set()
    for url in urls:
        lower = url.lower()
        is_amazon = "amazon." in lower or "amzn." in lower
        if "/dp/" in lower or "/gp/product/" in lower:
            categories.add("amazon_product_page")
        elif any(term in lower for term in ("your-orders", "order-details", "order-history")):
            categories.add("order_details")
        elif any(term in lower for term in ("/cart", "checkout", "buy-now")):
            categories.add("cart_or_checkout")
        elif any(term in lower for term in ("payment", "billing", "payments")):
            categories.add("payment_or_billing")
        elif any(term in lower for term in ("account", "membership", "prime")):
            categories.add("account_or_membership")
        elif any(term in lower for term in ("track", "shipment")):
            categories.add("tracking")
        elif is_amazon:
            categories.add("other_amazon_link")
        else:
            categories.add("external_link")
    if not categories:
        categories.add("no_link")
    return categories


def _table_category(row: dict) -> str:
    text = row.get("text", "")
    cells = [cell for cell in row.get("cells", []) if cell]
    has_money = bool(_amount_occurrences(text))
    if not has_money:
        return "unknown_table_row"
    if any(term in text for term in STRONG_TRANSACTION_TERMS + ("小計", "税", "送料")):
        return "summary_row"
    links = _link_categories(row.get("links", []))
    if row.get("image_count", 0) or "amazon_product_page" in links or any(term in text for term in ADVERTISEMENT_TERMS):
        return "product_card_row"
    if len(cells) == 1:
        return "single_price_cell"
    if len(cells) == 2:
        return "label_value_row"
    return "unknown_table_row"


def _count_band(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 5:
        return "2-5"
    return "6+"


def _append_identity_unique(items: list, candidate) -> None:
    if all(existing is not candidate for existing in items):
        items.append(candidate)


def _identity_index(items: list, candidate) -> int:
    for index, existing in enumerate(items):
        if existing is candidate:
            return index
    raise ValueError("candidate is not present by identity")


def _table_text(table: dict | None) -> str:
    if not table:
        return ""
    return _normalize(" ".join(row.get("text", "") for row in table.get("rows", [])))


def _row_keyword_flags(row: dict | None) -> dict[str, bool]:
    text = row.get("text", "") if row else ""
    return {
        "transaction": any(term in text for term in ADJACENT_TRANSACTION_TERMS),
        "advertisement": any(term in text for term in ADJACENT_ADVERTISEMENT_TERMS),
    }


def _placement_pattern(row: dict, table: dict) -> str:
    text = row.get("text", "")
    cells = [cell for cell in row.get("cells", []) if cell]
    money_cells = [index for index, cell in enumerate(cells) if _amount_occurrences(cell)]
    links = _link_categories(row.get("links", []))
    if any(term in text for term in STRONG_TRANSACTION_TERMS + ("小計", "税", "送料")):
        return "summary_total"
    if (
        row.get("linked_image_count", 0)
        or "amazon_product_page" in links
        or any(term in text for term in ADVERTISEMENT_TERMS)
    ):
        return "product_card_price"
    if len(_amount_occurrences(_table_text(table))) > 1:
        return "multi_price_table"
    if len(cells) == 1:
        return "standalone_amount"
    if money_cells:
        index = money_cells[0]
        before = " ".join(cells[:index])
        after = " ".join(cells[index + 1:])
        if any(term in before for term in TRANSACTION_TERMS):
            return "label_then_amount"
        if any(term in after for term in TRANSACTION_TERMS):
            return "amount_then_label"
    return "unknown_layout"


def _order_proximity(text: str, value: int, same_block: bool) -> str:
    if same_block:
        return "same_block"
    order_positions = [match.start() for match in ORDER_ID_RE.finditer(text)]
    if not order_positions:
        return "absent"
    amount_positions = [
        item["start"] for item in _amount_occurrences(text) if item["value"] == value
    ]
    if not amount_positions:
        return "far"
    distance = min(abs(amount - order) for amount in amount_positions for order in order_positions)
    return "near" if distance <= 300 else "far"


def diagnose_amazon_email_money_context(
    raw_email: bytes | Message,
    event: AmazonMailEvent | None = None,
) -> dict:
    message, _ = _message(raw_email)
    plain, html_visible, html_sources = _body_parts(message)
    combined = _normalize("\n".join(part for part in (plain, html_visible) if part))
    event = event or parse_amazon_email(raw_email)
    plain_occurrences = _amount_occurrences(plain)
    html_occurrences = _amount_occurrences(html_visible)
    values = sorted({item["value"] for item in plain_occurrences + html_occurrences})

    tables: list[dict] = []
    for html_source in html_sources:
        parser = _HTMLContext()
        parser.feed(html_source)
        tables.extend(parser.tables)
    rows = [row for table in tables for row in table.get("rows", [])]

    classifications = Counter()
    source_patterns = Counter()
    table_categories: set[str] = set()
    link_categories: set[str] = set()
    proximities: set[str] = set()
    table_row_bands = Counter()
    same_table_money_bands = Counter()
    image_count_bands = Counter()
    linked_image_count_bands = Counter()
    placement_patterns: set[str] = set()
    adjacent_flags = {
        "previous": {"transaction": False, "advertisement": False},
        "same": {"transaction": False, "advertisement": False},
        "next": {"transaction": False, "advertisement": False},
    }
    for value in values:
        in_plain = any(item["value"] == value for item in plain_occurrences)
        in_html = any(item["value"] == value for item in html_occurrences)
        source_pattern = "both" if in_plain and in_html else "plain_only" if in_plain else "html_only"
        source_patterns[source_pattern] += 1
        matching_rows = [
            row for row in rows
            if any(item["value"] == value for item in _amount_occurrences(row.get("text", "")))
        ]
        matching_tables = []
        for row in matching_rows:
            table = row["table"]
            _append_identity_unique(matching_tables, table)
            parent = table.get("parent")
            if parent is not None:
                _append_identity_unique(matching_tables, parent)
        block_text = _normalize(" ".join(_table_text(table) for table in matching_tables))
        same_order_block = bool(ORDER_ID_RE.search(block_text))
        proximity = _order_proximity(combined, value, same_order_block)
        proximities.add(proximity)
        candidate_tables = {_table_category(row) for row in matching_rows}
        table_categories.update(candidate_tables)
        candidate_links = _link_categories([
            url for table in matching_tables for url in table.get("links", [])
        ])
        link_categories.update(candidate_links)
        candidate_placements: set[str] = set()
        candidate_adjacent = {
            "previous": {"transaction": False, "advertisement": False},
            "same": {"transaction": False, "advertisement": False},
            "next": {"transaction": False, "advertisement": False},
        }
        image_count = 0
        linked_image_count = 0
        row_link_categories = _link_categories([
            url for row in matching_rows for url in row.get("links", [])
        ])
        row_linked_images = sum(row.get("linked_image_count", 0) for row in matching_rows)
        for table in matching_tables:
            table_text = _table_text(table)
            table_row_bands[_count_band(len(table.get("rows", [])))] += 1
            same_table_money_bands[_count_band(len({
                item["value"] for item in _amount_occurrences(table_text)
            }))] += 1
            image_count += table.get("image_count", 0)
            linked_image_count += table.get("linked_image_count", 0)
        image_count_bands[_count_band(image_count)] += 1
        linked_image_count_bands[_count_band(linked_image_count)] += 1
        for row in matching_rows:
            table = row["table"]
            candidate_placements.add(_placement_pattern(row, table))
            row_index = _identity_index(table["rows"], row)
            neighbors = {
                "previous": table["rows"][row_index - 1] if row_index > 0 else None,
                "same": row,
                "next": table["rows"][row_index + 1] if row_index + 1 < len(table["rows"]) else None,
            }
            for position, neighbor in neighbors.items():
                flags = _row_keyword_flags(neighbor)
                candidate_adjacent[position]["transaction"] |= flags["transaction"]
                candidate_adjacent[position]["advertisement"] |= flags["advertisement"]
                adjacent_flags[position]["transaction"] |= flags["transaction"]
                adjacent_flags[position]["advertisement"] |= flags["advertisement"]
        placement_patterns.update(candidate_placements)
        adjacent_transaction = any(
            candidate_adjacent[position]["transaction"] for position in ("previous", "same", "next")
        )
        adjacent_advertisement = any(
            candidate_adjacent[position]["advertisement"] for position in ("previous", "same", "next")
        )
        strong_transaction = (
            same_order_block
            or (adjacent_transaction and any(term in block_text for term in STRONG_TRANSACTION_TERMS))
            or "summary_total" in candidate_placements
            or bool({"order_details", "payment_or_billing"} & candidate_links)
        )
        advertisement_score = sum((
            adjacent_advertisement,
            "amazon_product_page" in row_link_categories,
            row_linked_images > 0,
            "product_card_price" in candidate_placements,
            "multi_price_table" in candidate_placements,
        ))
        transaction_score = sum((
            same_order_block,
            adjacent_transaction,
            "summary_total" in candidate_placements,
            bool({"order_details", "payment_or_billing"} & candidate_links),
            strong_transaction and row_linked_images == 0 and "amazon_product_page" not in row_link_categories,
        ))
        direct_product_evidence = (
            "amazon_product_page" in row_link_categories
            or row_linked_images > 0
            or "product_card_price" in candidate_placements
        )
        same_row_conflict = (
            candidate_adjacent["same"]["transaction"]
            and candidate_adjacent["same"]["advertisement"]
            and direct_product_evidence
        )
        if same_row_conflict:
            classification = "ambiguous"
        elif strong_transaction and transaction_score >= 2 and not direct_product_evidence:
            classification = "transaction_likely"
        elif advertisement_score >= 2 and direct_product_evidence:
            classification = "advertisement_likely"
        else:
            classification = "ambiguous"
        classifications[classification] += 1

    transaction = classifications["transaction_likely"]
    advertisement = classifications["advertisement_likely"]
    ambiguous = classifications["ambiguous"]
    if not values:
        message_class = "no_money_candidates"
    elif transaction and advertisement:
        message_class = "mixed_context"
    elif transaction and not advertisement and not ambiguous:
        message_class = "transaction_amount_present"
    elif advertisement and not transaction and not ambiguous:
        message_class = "advertisement_prices_only"
    else:
        message_class = "inconclusive"
    return {
        "money_candidate_count": len(values),
        "transaction_likely_count": transaction,
        "advertisement_likely_count": advertisement,
        "ambiguous_count": ambiguous,
        "order_id_present": event.order_id is not None,
        "order_id_proximity": sorted(proximities) if proximities else ["absent"],
        "source_presence_patterns": dict(source_patterns),
        "table_structure_categories": sorted(table_categories),
        "link_context_categories": sorted(link_categories),
        "parent_table_row_count_bands": dict(table_row_bands),
        "same_table_money_count_bands": dict(same_table_money_bands),
        "image_count_bands": dict(image_count_bands),
        "linked_image_count_bands": dict(linked_image_count_bands),
        "adjacent_row_keywords": adjacent_flags,
        "placement_patterns": sorted(placement_patterns),
        "message_classification": message_class,
    }


def _amount(text: str, labels: tuple[str, ...]) -> int | None:
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:{names})\s*[：:]?\s*{AMOUNT}", text, re.I)
    return int(match.group(1).replace(",", "")) if match else None


def _standalone_total(text: str) -> tuple[int | None, re.Match | None]:
    gap = r" \t\n\u00ad\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\u3000\ufeff"
    matches = list(re.finditer(
        rf"(?:^|\n)[ \t]*合計(?=[{gap}：:]{{0,16}}[￥¥])"
        rf"[{gap}]*[：:]?[{gap}]*[￥¥][ \t]*([0-9][0-9,]*)[ \t]*円?",
        text,
        re.I,
    ))
    values = {int(match.group(1).replace(",", "")) for match in matches}
    if len(values) != 1:
        return None, None
    return values.pop(), matches[0]


def _order_item_count(text: str, total_match: re.Match | None) -> int | None:
    if total_match is None:
        return None
    anchors = [match.end() for match in ORDER_ID_RE.finditer(text) if match.end() < total_match.start()]
    if not anchors:
        greeting = text.find("ご注文ありがとうございます。")
        if greeting < 0 or greeting >= total_match.start():
            return None
        anchors = [greeting + len("ご注文ありがとうございます。")]
    quantities = re.findall(r"数量\s*[：:]\s*(\d+)", text[max(anchors):total_match.start()])
    return sum(int(value) for value in quantities) if quantities else None


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
    if subject.startswith("注文済み:") or (
        "ご注文ありがとうございます。" in text and "注文番号" in text
    ):
        return "order"
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
    standalone_total, standalone_total_match = _standalone_total(text)
    gift_amount = _amount(text, ("ギフトカード利用額", "Amazonギフトカード利用額"))
    points_amount = _amount(text, ("Amazonポイント利用額", "ポイント利用額"))
    return AmazonMailEvent(
        event_type=event_type,
        order_id=order_match.group(0) if order_match else None,
        event_date=_date(text, message, event_type),
        charged_amount=_amount(text, (
            "カードへのご請求額", "カード請求額", "実際の請求額", "今回の請求額", "ご請求額", "請求金額",
        )),
        order_amount=_amount(text, ("注文合計", "ご注文合計")) or standalone_total,
        refund_amount=_amount(text, ("返金額", "返金予定額")),
        gift_card_amount=gift_amount,
        points_amount=points_amount,
        coupon_amount=_amount(text, ("クーポン", "クーポン割引")),
        discount_amount=_amount(text, ("割引額", "プロモーション割引")),
        payment_method=payment_method,
        shipment_amount=_amount(text, ("発送分合計", "今回発送分の合計", "発送商品合計")),
        item_count=(
            int(item_count_match.group(0)) if item_count_match
            else _order_item_count(text, standalone_total_match)
        ),
        message_id=str(message.get("Message-ID")).strip() if message.get("Message-ID") else None,
        source_hash=hashlib.sha256(raw).hexdigest(),
        gift_card_used=gift_amount is not None or bool(re.search(r"ギフトカード(?:を)?使用", text)),
        points_used=points_amount is not None or bool(re.search(r"(?:Amazon)?ポイント(?:を)?使用", text)),
    )
