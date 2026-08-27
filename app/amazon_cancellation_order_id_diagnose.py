from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
import hashlib

from .amazon_cancellation_return_preview import (
    _diagnose_order_candidates,
    _load_matching_candidates,
    diagnose_cancellation_order_id,
    diagnose_forwarded_cancellation_order_id,
    fetch_gmail_thread_messages,
)
from .amazon_email import AmazonMailEvent, parse_amazon_email
from .amazon_gmail_preview import _raw_bytes
from .amazon_gmail_storage import GmailRawMessage


_FIELDS = (
    "missing_order_id_events",
    "has_gmail_message_id",
    "has_gmail_thread_id",
    "has_rfc_message_id",
    "has_source_hash",
    "gmail_source_found",
    "gmail_source_not_found",
    "gmail_source_ambiguous",
    "source_order_id_pattern_present",
    "source_order_id_format_issue_clue",
    "forwarded_message_clue_present",
    "forwarded_order_id_unique_candidate_present",
    "reparse_order_id_found",
    "reparse_order_id_still_missing",
    "reparse_error",
    "thread_order_id_unique",
    "thread_order_id_none",
    "thread_order_id_ambiguous",
    "order_candidate_unique",
    "order_candidate_none",
    "order_candidate_multiple",
    "order_candidate_unique_strong",
    "recoverable_by_reparse",
    "recoverable_by_thread",
    "recoverable_by_unique_candidate",
    "still_requires_review",
)


@dataclass(frozen=True)
class _Source:
    message: GmailRawMessage | None
    thread_messages: tuple[GmailRawMessage, ...]
    ambiguous: bool = False


def _cell(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index else ""


def _rfc_message_id(raw_mime: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw_mime, headersonly=True)
    return str(message.get("Message-ID") or "").strip()


def _raw_message(response: dict, fallback_thread_id: str = "") -> GmailRawMessage:
    return GmailRawMessage(
        gmail_message_id=str(response.get("id") or ""),
        thread_id=str(response.get("threadId") or fallback_thread_id),
        raw_mime=_raw_bytes(response.get("raw", "")),
    )


def _source_matches(message: GmailRawMessage, row: list) -> bool:
    gmail_id = _cell(row, 1)
    rfc_id = _cell(row, 2)
    source_hash = _cell(row, 4)
    return bool(
        (gmail_id and message.gmail_message_id == gmail_id)
        or (rfc_id and _rfc_message_id(message.raw_mime) == rfc_id)
        or (source_hash and hashlib.sha256(message.raw_mime).hexdigest() == source_hash)
    )


def _locate_source(
    service,
    row: list,
    *,
    thread_fetcher: Callable[[object, str], list[GmailRawMessage]],
) -> _Source:
    gmail_id = _cell(row, 1)
    thread_id = _cell(row, 3)
    if gmail_id:
        try:
            response = service.users().messages().get(
                userId="me", id=gmail_id, format="raw",
            ).execute()
            if str(response.get("id") or "") == gmail_id:
                return _Source(_raw_message(response, thread_id), ())
        except Exception:
            pass

    if not thread_id:
        return _Source(None, ())
    try:
        messages = tuple(thread_fetcher(service, thread_id))
        matches = tuple(message for message in messages if _source_matches(message, row))
    except Exception:
        return _Source(None, ())
    if len(matches) == 1:
        return _Source(matches[0], messages)
    return _Source(None, messages, ambiguous=len(matches) > 1)


def _thread_order_ids(
    messages: tuple[GmailRawMessage, ...],
    parser: Callable[[bytes], AmazonMailEvent],
) -> set[str]:
    order_ids: set[str] = set()
    for message in messages:
        try:
            order_id = parser(message.raw_mime).order_id
        except Exception:
            continue
        if order_id:
            order_ids.add(order_id)
    return order_ids


def diagnose_amazon_cancellation_order_ids(
    service,
    db,
    *,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
    thread_fetcher: Callable[[object, str], list[GmailRawMessage]] = fetch_gmail_thread_messages,
) -> dict[str, int]:
    """Diagnose missing cancellation Order IDs without returning or writing data."""

    result: Counter[str] = Counter({field: 0 for field in _FIELDS})
    try:
        candidates = _load_matching_candidates(db)
        candidate_read_error = False
    except Exception:
        candidates = {}
        candidate_read_error = True

    for _row_number, raw in db.amazon_event_rows():
        row = list(raw)
        if _cell(row, 5) != "cancellation" or _cell(row, 6):
            continue
        result["missing_order_id_events"] += 1
        result["has_gmail_message_id"] += bool(_cell(row, 1))
        result["has_rfc_message_id"] += bool(_cell(row, 2))
        result["has_gmail_thread_id"] += bool(_cell(row, 3))
        result["has_source_hash"] += bool(_cell(row, 4))

        source = _locate_source(service, row, thread_fetcher=thread_fetcher)
        if source.ambiguous:
            result["gmail_source_ambiguous"] += 1
        elif source.message is None:
            result["gmail_source_not_found"] += 1
        else:
            result["gmail_source_found"] += 1

        parsed: AmazonMailEvent | None = None
        if source.message is not None:
            order_id_diagnostics = diagnose_cancellation_order_id(source.message.raw_mime)
            result["source_order_id_pattern_present"] += any(
                order_id_diagnostics[field]
                for field in (
                    "subject_order_id_pattern_present",
                    "plain_order_id_pattern_present",
                    "html_visible_order_id_pattern_present",
                    "html_raw_order_id_pattern_present",
                    "href_order_id_pattern_present",
                )
            )
            result["source_order_id_format_issue_clue"] += any(
                order_id_diagnostics[field]
                for field in (
                    "unicode_dash_candidate_present",
                    "fullwidth_digit_candidate_present",
                    "split_order_id_candidate_present",
                    "label_near_numeric_candidate_present",
                    "alternate_format_candidate_present",
                )
            )
            forwarded = diagnose_forwarded_cancellation_order_id(source.message.raw_mime)
            result["forwarded_message_clue_present"] += forwarded[
                "forwarded_message_clue_present"
            ]
            result["forwarded_order_id_unique_candidate_present"] += forwarded[
                "forwarded_order_id_unique_candidate_present"
            ]
            try:
                parsed = parser(source.message.raw_mime)
            except Exception:
                result["reparse_error"] += 1
            else:
                if parsed.order_id:
                    result["reparse_order_id_found"] += 1
                    result["recoverable_by_reparse"] += 1
                    continue
                result["reparse_order_id_still_missing"] += 1

        thread_messages = source.thread_messages
        thread_id = _cell(row, 3)
        if not thread_messages and thread_id:
            try:
                thread_messages = tuple(thread_fetcher(service, thread_id))
            except Exception:
                thread_messages = ()
        thread_ids = _thread_order_ids(thread_messages, parser)
        if len(thread_ids) == 1:
            result["thread_order_id_unique"] += 1
            result["recoverable_by_thread"] += 1
            continue
        if len(thread_ids) > 1:
            result["thread_order_id_ambiguous"] += 1
            result["still_requires_review"] += 1
            continue
        result["thread_order_id_none"] += 1

        if parsed is None or source.message is None or candidate_read_error:
            result["order_candidate_none"] += 1
            result["still_requires_review"] += 1
            continue
        matching = _diagnose_order_candidates(
            source.message.raw_mime,
            parsed,
            candidates,
            unsafe_due_to_error=candidate_read_error,
        )
        if matching["candidate_count_1"]:
            result["order_candidate_unique"] += 1
            if matching["unique_candidate_strong"]:
                result["order_candidate_unique_strong"] += 1
                result["recoverable_by_unique_candidate"] += 1
            else:
                result["still_requires_review"] += 1
        elif matching["candidate_count_2plus"]:
            result["order_candidate_multiple"] += 1
            result["still_requires_review"] += 1
        else:
            result["order_candidate_none"] += 1
            result["still_requires_review"] += 1

    return {field: result[field] for field in _FIELDS}
