from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from email import policy
from email.message import Message
from email.parser import BytesParser
import html
from html.parser import HTMLParser
import re
import unicodedata

from .amazon_email import AmazonMailEvent, ORDER_ID_RE, parse_amazon_email
from .amazon_gmail_preview import _raw_bytes
from .amazon_gmail_storage import GmailRawMessage, fetch_amazon_gmail_messages


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        for name, value in attrs:
            if name.lower() == "href" and value is not None:
                self.hrefs.append(value)

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _QuotedHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._quoted_stack: list[bool] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = " ".join(
            value or "" for name, value in attrs if name.lower() in {"class", "id"}
        ).lower()
        starts_quote = tag.lower() == "blockquote" or bool(re.search(
            r"(?:^|[-_\s])(?:quote|quoted|forwarded)(?:$|[-_\s])", attributes,
        ))
        self._quoted_stack.append(starts_quote or any(self._quoted_stack))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._quoted_stack:
            self._quoted_stack.pop()

    def handle_data(self, data: str) -> None:
        if any(self._quoted_stack):
            self.parts.append(data)


def _part_text(part: Message) -> str:
    try:
        return str(part.get_content())
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _diagnostic_text(raw_mime: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    parts = [str(message.get("Subject", ""))]
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        content_type = part.get_content_type()
        if content_type == "text/plain":
            parts.append(_part_text(part))
        elif content_type == "text/html":
            parser = _VisibleHTML()
            parser.feed(_part_text(part))
            parts.append(" ".join(parser.parts))
    return unicodedata.normalize("NFKC", "\n".join(parts))


_UNICODE_DASH_RE = re.compile(
    r"(?<!\d)\d{3}[\u2010-\u2015\u2212\uff0d]\d{7}"
    r"[\u2010-\u2015\u2212\uff0d]\d{7}(?!\d)"
)
_FULLWIDTH_DIGITS = frozenset("０１２３４５６７８９")
_MIXED_DIGIT_ORDER_RE = re.compile(
    r"(?<![0-9０-９])[0-9０-９]{3}-[0-9０-９]{7}-[0-9０-９]{7}(?![0-9０-９])"
)
_LABEL_RE = re.compile(r"(?:注文番号|注文\s*ID|order\s*(?:#|id))", re.IGNORECASE)
_NEARBY_NUMERIC_RE = re.compile(r"[0-9０-９][0-9０-９\s\-‐-―−－]{8,}")
_ALT_ORDER_RE = re.compile(
    r"(?<![0-9０-９])[0-9０-９]{3}(?:[\s_./:‐-―−－]+)"
    r"[0-9０-９]{7}(?:[\s_./:‐-―−－]+)[0-9０-９]{7}(?![0-9０-９])"
    r"|(?<![0-9０-９])[0-9０-９]{17}(?![0-9０-９])"
)

_FORWARDED_MARKER_RE = re.compile(
    r"(?im)^\s*(?:-{2,}\s*(?:forwarded|original)\s+message\s*-*"
    r"|転送されたメッセージ(?:\s*[-―ー]*)?|転送メッセージ(?:\s*[-―ー]*)?"
    r"|元のメッセージ(?:\s*[-―ー]*)?)\s*$"
)
_FORWARDED_HEADER_RE = re.compile(
    r"(?im)^\s*(?:>\s*)?(from|date|sent|subject|to|cc|差出人|送信者|日時|送信日時|件名|宛先)\s*[：:]"
)
_ORIGINAL_SUBJECT_RE = re.compile(
    r"(?im)^\s*(?:>\s*)?(?:subject|件名)\s*[：:]\s*(.*)$"
)
_QUOTED_LINE_RE = re.compile(r"(?m)^\s*>+\s?(.*)$")


def _message_text(message: Message) -> str:
    """Return decoded headers and bodies for an in-memory pattern check."""

    parts = [str(message.get("Subject", ""))]
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        if part.get_content_type() == "text/plain":
            parts.append(_part_text(part))
        elif part.get_content_type() == "text/html":
            parser = _VisibleHTML()
            parser.feed(_part_text(part))
            parts.append("\n".join(parser.parts))
    return unicodedata.normalize("NFKC", "\n".join(parts))


def _forwarded_header_blocks(text: str) -> list[str]:
    """Find compact header-like runs without retaining or returning their values."""

    lines = text.splitlines()
    blocks: list[str] = []
    for index, line in enumerate(lines):
        if not (_FORWARDED_MARKER_RE.match(line) or _FORWARDED_HEADER_RE.match(line)):
            continue
        window = lines[index:index + 16]
        header_lines = [candidate for candidate in window if _FORWARDED_HEADER_RE.match(candidate)]
        labels = {
            _FORWARDED_HEADER_RE.match(candidate).group(1).lower()
            for candidate in header_lines
        }
        if len(labels) >= 2:
            blocks.append("\n".join(header_lines))
    return blocks


def diagnose_forwarded_cancellation_order_id(raw_mime: bytes) -> dict[str, bool]:
    """Classify forwarded-message evidence without exposing message contents."""

    message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    outer_text = _message_text(message)

    nested_texts: list[str] = []
    nested_subjects: list[str] = []
    nested_rfc822_present = False
    for part in message.walk():
        if part.get_content_type() != "message/rfc822":
            continue
        nested_rfc822_present = True
        payload = part.get_payload()
        nested_messages = payload if isinstance(payload, list) else []
        for nested in nested_messages:
            if isinstance(nested, Message):
                nested_texts.append(_message_text(nested))
                nested_subjects.append(unicodedata.normalize(
                    "NFKC", str(nested.get("Subject", "")),
                ))

    header_blocks = _forwarded_header_blocks(outer_text)
    quoted_blocks = [match.group(1) for match in _QUOTED_LINE_RE.finditer(outer_text)]
    for part in message.walk():
        if part.get_content_type() == "text/html":
            quoted_html = _QuotedHTML()
            quoted_html.feed(_part_text(part))
            if quoted_html.parts:
                quoted_blocks.append("\n".join(quoted_html.parts))
    original_subjects = [
        match.group(1) for match in _ORIGINAL_SUBJECT_RE.finditer(outer_text)
    ] + nested_subjects

    nested_candidates = set(ORDER_ID_RE.findall("\n".join(nested_texts)))
    header_candidates = set(ORDER_ID_RE.findall("\n".join(header_blocks)))
    quoted_candidates = set(ORDER_ID_RE.findall("\n".join(quoted_blocks)))
    subject_candidates = set(ORDER_ID_RE.findall("\n".join(original_subjects)))
    candidates = (
        nested_candidates | header_candidates | quoted_candidates | subject_candidates
    )
    count = len(candidates)

    header_block_present = bool(header_blocks)
    original_subject_present = bool(original_subjects)
    return {
        "forwarded_message_clue_present": bool(
            _FORWARDED_MARKER_RE.search(outer_text)
            or header_block_present
            or nested_rfc822_present
        ),
        "nested_rfc822_present": nested_rfc822_present,
        "nested_order_id_pattern_present": bool(nested_candidates),
        "forwarded_header_block_present": header_block_present,
        "forwarded_header_order_id_pattern_present": bool(header_candidates),
        "quoted_block_present": bool(quoted_blocks),
        "quoted_order_id_pattern_present": bool(quoted_candidates),
        "original_subject_clue_present": original_subject_present,
        "original_subject_order_id_pattern_present": bool(subject_candidates),
        "forwarded_order_id_candidate_count_0": count == 0,
        "forwarded_order_id_candidate_count_1": count == 1,
        "forwarded_order_id_candidate_count_2plus": count >= 2,
        "forwarded_order_id_unique_candidate_present": count == 1,
    }


def _mime_diagnostic_sources(raw_mime: bytes) -> tuple[str, str, str, str, str]:
    """Return subject/plain/visible HTML/raw HTML/hrefs for in-memory checks only."""

    message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    subject = str(message.get("Subject", ""))
    plain_parts: list[str] = []
    visible_parts: list[str] = []
    raw_html_parts: list[str] = []
    hrefs: list[str] = []
    candidates = message.walk() if message.is_multipart() else (message,)
    for part in candidates:
        if part.get_content_type() == "text/plain":
            plain_parts.append(_part_text(part))
        elif part.get_content_type() == "text/html":
            raw_html = _part_text(part)
            parser = _VisibleHTML()
            parser.feed(raw_html)
            raw_html_parts.append(raw_html)
            visible_parts.append("".join(parser.parts))
            hrefs.extend(parser.hrefs)
    return (
        subject,
        "\n".join(plain_parts),
        "\n".join(visible_parts),
        "\n".join(raw_html_parts),
        "\n".join(hrefs),
    )


def diagnose_cancellation_order_id(raw_mime: bytes) -> dict[str, bool]:
    """Classify Order ID evidence without returning any source text or identifier."""

    subject, plain, visible, raw_html, hrefs = _mime_diagnostic_sources(raw_mime)
    all_text = "\n".join((subject, plain, visible, raw_html, hrefs))
    labels = list(_LABEL_RE.finditer(all_text))
    label_near = any(
        _NEARBY_NUMERIC_RE.search(all_text[match.end():match.end() + 160])
        for match in labels
    )

    # Removing markup and whitespace models an ID whose characters are separated
    # by formatting tags. The raw source itself must not already contain the ID.
    without_tags = html.unescape(re.sub(r"<[^>]+>", "", raw_html))
    compact_html = re.sub(r"\s+", "", without_tags)
    tag_between_order_chars = bool(re.search(
        r"[0-9０-９-]\s*<[^>]+>\s*[0-9０-９-]", raw_html,
    ))
    html_tag_split = (
        tag_between_order_chars
        and ORDER_ID_RE.search(raw_html) is None
        and ORDER_ID_RE.search(unicodedata.normalize("NFKC", compact_html)) is not None
    )
    whitespace_split = any(
        ORDER_ID_RE.search(source) is None
        and re.search(r"[0-9０-９-]\s+[0-9０-９-]", source) is not None
        and ORDER_ID_RE.search(unicodedata.normalize(
            "NFKC", re.sub(r"\s+", "", source),
        )) is not None
        for source in (subject, plain, visible)
    )
    fullwidth_matches = _MIXED_DIGIT_ORDER_RE.finditer(all_text)

    return {
        "subject_order_id_pattern_present": ORDER_ID_RE.search(subject) is not None,
        "plain_order_id_pattern_present": ORDER_ID_RE.search(plain) is not None,
        "html_visible_order_id_pattern_present": ORDER_ID_RE.search(visible) is not None,
        "html_raw_order_id_pattern_present": ORDER_ID_RE.search(raw_html) is not None,
        "href_order_id_pattern_present": ORDER_ID_RE.search(hrefs) is not None,
        "unicode_dash_candidate_present": _UNICODE_DASH_RE.search(all_text) is not None,
        "fullwidth_digit_candidate_present": any(
            any(char in _FULLWIDTH_DIGITS for char in match.group())
            for match in fullwidth_matches
        ),
        "split_order_id_candidate_present": html_tag_split or whitespace_split,
        "label_near_numeric_candidate_present": label_near,
        "alternate_format_candidate_present": (
            _ALT_ORDER_RE.search(all_text) is not None
            and ORDER_ID_RE.search(all_text) is None
        ),
    }


ORDER_ID_DIAGNOSTIC_FIELDS = tuple(diagnose_cancellation_order_id(b"").keys())
FORWARDED_DIAGNOSTIC_FIELDS = tuple(
    diagnose_forwarded_cancellation_order_id(b"").keys()
)


def cancellation_clues(text: str) -> dict[str, bool | str]:
    partial = any(term in text for term in (
        "この商品をキャンセル", "キャンセルされた商品", "一部の商品", "一部商品",
    )) or bool(re.search(r"数量\s*[：:]?\s*\d+", text))
    full = any(term in text for term in (
        "注文をキャンセルしました", "注文全体", "ご注文はキャンセル",
        "注文がキャンセルされました",
    ))
    classification = "ambiguous"
    if full and not partial:
        classification = "full_likely"
    elif partial and not full:
        classification = "partial_likely"
    return {"full": full, "partial": partial, "classification": classification}


def classify_cancellation_clues(text: str) -> str:
    return str(cancellation_clues(text)["classification"])


def return_clues(text: str) -> dict[str, bool | str]:
    item = any(term in text for term in (
        "返品商品", "返品対象商品", "商品を返品", "返品する商品",
    ))
    quantity = bool(re.search(r"数量\s*[：:]?\s*\d+", text))
    amount = any(term in text for term in ("返金予定", "返金予定額", "返品金額"))
    order_level = any(term in text for term in ("注文全体", "ご注文全体", "すべての商品"))
    if item or quantity:
        classification = "item_specific"
    elif order_level:
        classification = "order_level"
    else:
        classification = "ambiguous"
    return {
        "item": item,
        "quantity": quantity,
        "amount": amount,
        "classification": classification,
    }


def _looks_like(text: str, event_type: str) -> bool:
    terms = {
        "cancellation": ("キャンセル", "取消", "cancelled", "canceled"),
        "return": ("返品", "返送", "return"),
    }[event_type]
    lowered = text.lower()
    return any(term in lowered for term in terms)


def fetch_gmail_thread_messages(service, thread_id: str) -> list[GmailRawMessage]:
    """Fetch one Gmail thread as raw messages through a read-only service."""

    users = service.users()
    thread = users.threads().get(
        userId="me", id=thread_id, format="minimal",
    ).execute()
    messages_api = users.messages()
    messages: list[GmailRawMessage] = []
    for item in thread.get("messages", []):
        message_id = item.get("id")
        if not message_id:
            continue
        raw = messages_api.get(
            userId="me", id=message_id, format="raw",
        ).execute()
        messages.append(GmailRawMessage(
            gmail_message_id=str(message_id),
            thread_id=str(raw.get("threadId") or thread_id),
            raw_mime=_raw_bytes(raw.get("raw", "")),
        ))
    return messages


_THREAD_EVENT_TYPES = ("order", "delivery", "cancellation", "return", "unknown")
_THREAD_DIAGNOSTIC_FIELDS = (
    "cancellation_thread_id_present",
    "cancellation_thread_fetched",
    "cancellation_thread_fetch_errors",
    "cancellation_thread_message_count_1",
    "cancellation_thread_message_count_2plus",
    "thread_other_message_count",
    *tuple(f"thread_{event_type}_event_present" for event_type in _THREAD_EVENT_TYPES),
    "thread_other_parser_errors",
    "thread_other_order_id_present",
    "thread_order_id_candidate_count_0",
    "thread_order_id_candidate_count_1",
    "thread_order_id_candidate_count_2plus",
    "thread_unique_order_id_candidate_present",
)


def _diagnose_cancellation_thread(
    service,
    message: GmailRawMessage,
    *,
    parser: Callable[[bytes], AmazonMailEvent],
    thread_fetcher: Callable[[object, str], list[GmailRawMessage]],
) -> dict[str, int]:
    result = {field: 0 for field in _THREAD_DIAGNOSTIC_FIELDS}
    if not message.thread_id:
        result["thread_order_id_candidate_count_0"] = 1
        return result

    result["cancellation_thread_id_present"] = 1
    try:
        thread_messages = thread_fetcher(service, message.thread_id)
    except Exception:
        result["cancellation_thread_fetch_errors"] = 1
        result["thread_order_id_candidate_count_0"] = 1
        return result

    result["cancellation_thread_fetched"] = 1
    result["cancellation_thread_message_count_1"] = int(len(thread_messages) == 1)
    result["cancellation_thread_message_count_2plus"] = int(len(thread_messages) >= 2)
    other_messages = [
        candidate for candidate in thread_messages
        if candidate.gmail_message_id != message.gmail_message_id
    ]
    result["thread_other_message_count"] = len(other_messages)

    candidates: set[str] = set()
    parser_error = False
    event_types: set[str] = set()
    for other in other_messages:
        try:
            event = parser(other.raw_mime)
        except Exception:
            parser_error = True
            result["thread_other_parser_errors"] += 1
            continue
        event_type = event.event_type if event.event_type in _THREAD_EVENT_TYPES else "unknown"
        event_types.add(event_type)
        if event.order_id:
            candidates.add(event.order_id)

    for event_type in _THREAD_EVENT_TYPES:
        result[f"thread_{event_type}_event_present"] = int(event_type in event_types)
    count = len(candidates)
    result["thread_other_order_id_present"] = int(count > 0)
    result["thread_order_id_candidate_count_0"] = int(count == 0)
    result["thread_order_id_candidate_count_1"] = int(count == 1)
    result["thread_order_id_candidate_count_2plus"] = int(count >= 2)
    result["thread_unique_order_id_candidate_present"] = int(
        count == 1 and not parser_error
    )
    return result


def preview_amazon_cancellation_returns(
    service,
    *,
    fetcher: Callable[[object], list[GmailRawMessage]] = fetch_amazon_gmail_messages,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    thread_fetcher: Callable[
        [object, str], list[GmailRawMessage]
    ] = fetch_gmail_thread_messages,
) -> dict[str, int]:
    """Diagnose cancellation/return Gmail messages without persisting anything."""

    messages = fetcher(service)
    counts: Counter[str] = Counter()
    for message in messages:
        text = _diagnostic_text(message.raw_mime)
        try:
            event = parser(message.raw_mime)
        except Exception:
            for event_type in ("cancellation", "return"):
                if _looks_like(text, event_type):
                    counts[f"{event_type} parser errors"] += 1
            continue

        if event.event_type not in {"cancellation", "return"}:
            if event.event_type == "unknown":
                for event_type in ("cancellation", "return"):
                    if _looks_like(text, event_type):
                        counts[f"unknown {event_type} candidates"] += 1
            continue

        event_type = event.event_type
        counts[f"{event_type} count"] += 1
        counts[f"{event_type} order_id present"] += event.order_id is not None
        counts[f"{event_type} event_date present"] += event.event_date is not None
        if event_type == "cancellation":
            if event.order_id is None:
                counts.update(_diagnose_cancellation_thread(
                    service, message, parser=parser, thread_fetcher=thread_fetcher,
                ))
            for field, present in diagnose_cancellation_order_id(message.raw_mime).items():
                counts[field] += present
            for field, present in diagnose_forwarded_cancellation_order_id(
                message.raw_mime,
            ).items():
                counts[field] += present
            clues = cancellation_clues(text)
            counts["full cancellation clue present"] += bool(clues["full"])
            counts["partial cancellation clue present"] += bool(clues["partial"])
            counts[f"cancellation {clues['classification']}"] += 1
        else:
            clues = return_clues(text)
            counts["return item clue present"] += bool(clues["item"])
            counts["return quantity clue present"] += bool(clues["quantity"])
            counts["return amount clue present"] += bool(clues["amount"])
            counts[f"return {clues['classification']}"] += 1

    fields = (
        "cancellation count", "return count",
        "cancellation order_id present", "return order_id present",
        "cancellation event_date present", "return event_date present",
        "cancellation parser errors", "return parser errors",
        "full cancellation clue present", "partial cancellation clue present",
        "cancellation full_likely", "cancellation partial_likely",
        "ambiguous cancellation",
        "return item clue present", "return quantity clue present",
        "return amount clue present", "return item_specific",
        "return order_level", "return ambiguous",
        "unknown cancellation candidates", "unknown return candidates",
        *ORDER_ID_DIAGNOSTIC_FIELDS,
        *FORWARDED_DIAGNOSTIC_FIELDS,
        *_THREAD_DIAGNOSTIC_FIELDS,
    )
    result = {"fetched Amazon messages": len(messages), **{
        field: counts[field] for field in fields
    }}
    result["ambiguous cancellation"] = counts["cancellation ambiguous"]
    return result
