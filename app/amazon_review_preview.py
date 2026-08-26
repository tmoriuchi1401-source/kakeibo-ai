from __future__ import annotations

from collections.abc import Callable

from .amazon_cancellation_return_preview import (
    _build_review_plan,
    _diagnose_order_candidates,
    _diagnostic_text,
    _load_matching_candidates,
    _looks_like,
    classify_cancellation_clues,
    cancellation_review_event_row,
)
from .amazon_email import AmazonMailEvent, parse_amazon_email
from .amazon_gmail_storage import GmailRawMessage, fetch_amazon_gmail_messages
from .amazon_review import AMAZON_REVIEW_HEADERS, AMAZON_REVIEW_SHEET, AmazonReviewEventRow
from .utils import now_jst_string


_STATUSES = {
    "未確認": "unreviewed",
    "選択済み": "selected",
    "反映済み": "applied",
    "保留": "hold",
    "エラー": "error",
}
_EVENT_TYPES = ("cancellation", "return", "refund", "unmatched")
_REASONS = (
    "missing_order_id",
    "multiple_candidates",
    "no_candidate",
    "unique_but_not_strong",
    "parser_error",
    "source_read_error",
)


def _empty_summary() -> dict[str, int]:
    result = {
        "review_sheet_exists": 0,
        "review_schema_match": 0,
        "review_required_events": 0,
        "planned_review_rows": 0,
        "existing_review_rows": 0,
        "existing_review_ids": 0,
        "duplicate_review_ids": 0,
        "planned_new_rows": 0,
    }
    result.update({f"planned_status_{name}": 0 for name in _STATUSES.values()})
    result.update({f"planned_{event_type}": 0 for event_type in _EVENT_TYPES})
    result.update({f"planned_{reason}": 0 for reason in _REASONS})
    return result


def _planned_cancellation_rows(
    service,
    db,
    *,
    created_at: str,
    fetcher: Callable[[object], list[GmailRawMessage]],
    parser: Callable[[bytes], AmazonMailEvent],
) -> list[AmazonReviewEventRow]:
    matching_read_error = False
    try:
        matching_candidates = _load_matching_candidates(db)
    except Exception:
        matching_candidates = {}
        matching_read_error = True

    rows: list[AmazonReviewEventRow] = []
    for message in fetcher(service):
        text = _diagnostic_text(message.raw_mime)
        try:
            event = parser(message.raw_mime)
        except Exception:
            if not _looks_like(text, "cancellation"):
                continue
            plan = _build_review_plan(
                message.raw_mime,
                source_hash=None,
                event_date=None,
                matching={},
                cancellation_scope=classify_cancellation_clues(text),
                parser_error=True,
                source_read_error=matching_read_error,
            )
        else:
            if event.event_type != "cancellation" or event.order_id is not None:
                continue
            matching = _diagnose_order_candidates(
                message.raw_mime,
                event,
                matching_candidates,
                unsafe_due_to_error=matching_read_error,
            )
            plan = _build_review_plan(
                message.raw_mime,
                source_hash=event.source_hash,
                event_date=event.event_date,
                matching=matching,
                cancellation_scope=classify_cancellation_clues(text),
                source_read_error=matching_read_error,
            )
        if plan is not None:
            rows.append(cancellation_review_event_row(plan, created_at=created_at))
    return rows


def preview_amazon_reviews(
    service,
    db,
    *,
    created_at: str | None = None,
    fetcher: Callable[[object], list[GmailRawMessage]] = fetch_amazon_gmail_messages,
    parser: Callable[[bytes], AmazonMailEvent] = parse_amazon_email,
) -> dict[str, int]:
    """Summarize future Amazon review rows without exposing or writing row data."""

    result = _empty_summary()
    sheet_exists = AMAZON_REVIEW_SHEET in db.sheet_titles()
    result["review_sheet_exists"] = int(sheet_exists)
    existing_ids: set[str] = set()
    if sheet_exists:
        header_rows = db.get(f"{AMAZON_REVIEW_SHEET}!1:1")
        schema_match = bool(header_rows and list(header_rows[0]) == AMAZON_REVIEW_HEADERS)
        result["review_schema_match"] = int(schema_match)
        if not schema_match:
            return result
        existing_rows = [list(row) for row in db.get(f"{AMAZON_REVIEW_SHEET}!A2:N") if row]
        existing_ids = {str(row[0]).strip() for row in existing_rows if row and str(row[0]).strip()}
        result["existing_review_rows"] = len(existing_rows)
        result["existing_review_ids"] = len(existing_ids)
    else:
        result["review_schema_match"] = 0

    planned = _planned_cancellation_rows(
        service,
        db,
        created_at=created_at or now_jst_string(),
        fetcher=fetcher,
        parser=parser,
    )
    result["review_required_events"] = len(planned)
    result["planned_review_rows"] = len(planned)
    seen_ids = set(existing_ids)
    for row in planned:
        if row.review_id in seen_ids:
            result["duplicate_review_ids"] += 1
        else:
            result["planned_new_rows"] += 1
            seen_ids.add(row.review_id)
        status_name = _STATUSES.get(row.review_status)
        if status_name:
            result[f"planned_status_{status_name}"] += 1
        if row.event_type in _EVENT_TYPES:
            result[f"planned_{row.event_type}"] += 1
        for reason in row.reasons:
            if reason in _REASONS:
                result[f"planned_{reason}"] += 1
    return result
