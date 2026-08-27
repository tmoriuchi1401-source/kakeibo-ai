from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
import re

from .amazon_cancellation_order_id_diagnose import _locate_source
from .amazon_cancellation_return_preview import fetch_gmail_thread_messages
from .amazon_email import (
    _CANCELLATION_QUANTITY_PATTERNS,
    _body_parts,
    _cancellation_quantity,
    _message,
    _normalize,
    diagnose_cancellation_quantity,
)
from .amazon_gmail_storage import GmailRawMessage
from .amazon_status_sync_preview import _cell, _order_item_counts, _positive_int, _scope


_FIELDS = (
    "target_events",
    "ambiguous_quantity_events",
    "candidate_occurrences_zero",
    "candidate_occurrences_one",
    "candidate_occurrences_multiple",
    "distinct_quantity_values_one",
    "distinct_quantity_values_multiple",
    "duplicate_same_label_same_value",
    "duplicate_different_label_same_value",
    "conflicting_values",
    "html_plaintext_duplicate",
    "same_value_repeated_other",
    "source_has_plain_text",
    "source_has_html",
    "source_has_both",
    "ambiguity_same_value_duplicate",
    "ambiguity_conflicting_values",
    "ambiguity_malformed_quantity",
    "ambiguity_source_structure",
    "ambiguity_other",
    "would_be_safe_with_unique_distinct_value_rule",
    "would_still_be_ambiguous_with_unique_distinct_value_rule",
)

_QUANTITY_LABEL_LINE = re.compile(
    r"(?m)^\s*(?:キャンセル(?:された)?数量|キャンセル対象数量|数量)\s*[：:]\s*([^\n]*)$"
)


@dataclass(frozen=True)
class _Candidate:
    value: int
    label_kind: str
    source: str


def _candidates(text: str, source: str) -> list[_Candidate]:
    return [
        _Candidate(int(match.group(1)), f"label_{index}", source)
        for index, pattern in enumerate(_CANCELLATION_QUANTITY_PATTERNS)
        for match in pattern.finditer(text)
    ]


def _scope_without_header(event: list, detail_count: int | None) -> str:
    quantity = _positive_int(_cell(event, 17))
    if quantity is None or detail_count is None or quantity > detail_count:
        return "unknown"
    return "full_order" if quantity == detail_count else "item_or_partial"


def diagnose_amazon_cancellation_quantity_ambiguity(
    service,
    db,
    *,
    thread_fetcher: Callable[[object, str], list[GmailRawMessage]] = fetch_gmail_thread_messages,
) -> dict[str, int]:
    """Classify ambiguous explicit quantities without exposing or changing them."""

    result: Counter[str] = Counter({field: 0 for field in _FIELDS})
    order_rows = [list(row) for row in db.get("Amazon注文!A2:O") if row]
    detail_counts = _order_item_counts(order_rows)
    detail_ids = {_cell(row, 1) for row in order_rows if _cell(row, 1)}
    headers_by_id: defaultdict[str, list[list]] = defaultdict(list)
    for raw in db.get("Amazon注文ヘッダ!A2:O"):
        row = list(raw)
        if _cell(row, 0):
            headers_by_id[_cell(row, 0)].append(row)

    for _row_number, raw in db.amazon_event_rows():
        event = list(raw)
        order_id = _cell(event, 6)
        if _cell(event, 5) != "cancellation" or not order_id:
            continue
        headers = headers_by_id.get(order_id, [])
        if len(headers) > 1 or (not headers and order_id not in detail_ids):
            continue
        header = headers[0] if headers else None
        current_scope = (
            _scope(event, header, detail_counts.get(order_id))
            if header else _scope_without_header(event, detail_counts.get(order_id))
        )
        if current_scope != "unknown":
            continue
        source = _locate_source(service, event, thread_fetcher=thread_fetcher)
        if source.message is None:
            continue

        message, _raw_mime = _message(source.message.raw_mime)
        plain, html, _html_sources = _body_parts(message)
        combined = _normalize("\n".join(part for part in (plain, html) if part))
        candidates = _candidates(plain, "plain") + _candidates(html, "html")
        values = {candidate.value for candidate in candidates}
        order_total = (
            _positive_int(_cell(header, 4)) if header else None
        ) or detail_counts.get(order_id)
        parsed_quantity, parser_status = _cancellation_quantity(combined)
        source_status = diagnose_cancellation_quantity(source.message.raw_mime)
        invalid_for_preview = (
            parser_status == "invalid_or_ambiguous"
            or source_status == "not_cancellation"
            or (
                parser_status == "found"
                and order_total is not None
                and parsed_quantity is not None
                and parsed_quantity > order_total
            )
        )
        if not invalid_for_preview:
            continue
        result["target_events"] += 1
        result["ambiguous_quantity_events"] += 1

        occurrence_count = len(candidates)
        result[
            "candidate_occurrences_zero" if occurrence_count == 0
            else "candidate_occurrences_one" if occurrence_count == 1
            else "candidate_occurrences_multiple"
        ] += 1
        if len(values) == 1:
            result["distinct_quantity_values_one"] += 1
        elif len(values) > 1:
            result["distinct_quantity_values_multiple"] += 1

        grouped = Counter((candidate.label_kind, candidate.value) for candidate in candidates)
        same_label_duplicate = any(count > 1 for count in grouped.values())
        labels_by_value: defaultdict[int, set[str]] = defaultdict(set)
        sources_by_value: defaultdict[int, set[str]] = defaultdict(set)
        for candidate in candidates:
            labels_by_value[candidate.value].add(candidate.label_kind)
            sources_by_value[candidate.value].add(candidate.source)
        different_label_duplicate = any(len(labels) > 1 for labels in labels_by_value.values())
        html_plain_duplicate = any(len(sources) > 1 for sources in sources_by_value.values())
        result["duplicate_same_label_same_value"] += same_label_duplicate
        result["duplicate_different_label_same_value"] += different_label_duplicate
        result["conflicting_values"] += len(values) > 1
        result["html_plaintext_duplicate"] += html_plain_duplicate
        result["same_value_repeated_other"] += bool(
            occurrence_count > 1 and len(values) == 1
            and not same_label_duplicate and not different_label_duplicate
        )

        result["source_has_plain_text"] += bool(plain)
        result["source_has_html"] += bool(html)
        result["source_has_both"] += bool(plain and html)
        label_lines = len(_QUANTITY_LABEL_LINE.findall(combined))
        malformed = occurrence_count == 0 and label_lines > 0
        nonpositive = any(candidate.value <= 0 for candidate in candidates)
        if len(values) > 1:
            reason = "ambiguity_conflicting_values"
        elif malformed or nonpositive:
            reason = "ambiguity_malformed_quantity"
        elif occurrence_count > 1 and len(values) == 1:
            reason = "ambiguity_same_value_duplicate"
        elif plain and html:
            reason = "ambiguity_source_structure"
        else:
            reason = "ambiguity_other"
        result[reason] += 1

        positive_values = {value for value in values if value > 0}
        safe = bool(
            occurrence_count > 1
            and len(positive_values) == 1
            and len(positive_values) == len(values)
            and (order_total is None or next(iter(positive_values)) <= order_total)
        )
        if safe:
            result["would_be_safe_with_unique_distinct_value_rule"] += 1
        else:
            result["would_still_be_ambiguous_with_unique_distinct_value_rule"] += 1

    return {field: result[field] for field in _FIELDS}
