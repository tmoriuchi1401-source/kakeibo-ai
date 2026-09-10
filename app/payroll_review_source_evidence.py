"""Read-only PDF-text source/value binding for Payroll review assertions."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pypdf import PdfReader

from .payroll_ocr import extract_payroll_text
from .payroll_parser import compact
from .payroll_review_authority import (
    SOURCE_VALUE_BINDING_VERSION,
    PayrollReviewSourceValueBinding,
    _mac,
    _require_key,
    _source_value_binding_payload,
)
from .payroll_storage import PayrollStorageCandidate


def _physical_token_id(local_key, content_hash, index, token):
    return _mac(local_key, (
        "payroll-review-physical-token-v1", content_hash, index,
        token.text, token.page, token.x, token.y, token.width, token.height,
        token.confidence,
    ))


def capture_pdf_text_review_source_value(
    path: str | Path,
    candidate: PayrollStorageCandidate,
    item_occurrence: int,
    source_value: str,
    *,
    local_key: bytes,
) -> PayrollReviewSourceValueBinding:
    """Bind one exact same-row value token to one pending review label."""

    _require_key(local_key)
    path = Path(path)
    if candidate.parse_method != "pdf_text":
        raise ValueError("pdf_text_review_required")
    if not 0 <= item_occurrence < len(candidate.items):
        raise IndexError("item_occurrence_out_of_range")
    item = candidate.items[item_occurrence]
    if not item.needs_review or item.review_status != "pending":
        raise ValueError("item_not_pending")
    source_bytes = path.read_bytes()
    content_hash = hashlib.sha256(source_bytes).hexdigest()
    if content_hash != candidate.statement.content_hash:
        raise ValueError("source_content_mismatch")
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("encrypted_pdf")
    page_count = len(reader.pages)
    first = extract_payroll_text(path)
    second = extract_payroll_text(path)
    if (first.file_type != "pdf" or first.extraction_method != "pdf_text"
            or second.file_type != "pdf" or second.extraction_method != "pdf_text"):
        raise ValueError("pdf_text_replay_required")
    if (first.text != second.text or first.tokens != second.tokens
            or path.read_bytes() != source_bytes):
        raise ValueError("source_replay_unstable")
    if (page_count < 1 or not first.tokens
            or any(not 1 <= token.page <= page_count for token in first.tokens)):
        raise ValueError("page_scope_incomplete")
    label_matches = [
        (index, token) for index, token in enumerate(first.tokens)
        if compact(token.text) == compact(item.raw_item_name)
    ]
    if len(label_matches) != 1:
        raise ValueError("review_label_occurrence_ambiguous")
    label_index, label = label_matches[0]
    value_matches = [
        (index, token) for index, token in enumerate(first.tokens)
        if token.page == label.page
        and token.text.strip() == source_value.strip()
        and abs(token.y - label.y) <= max(token.height, label.height) * .65
        and token.x >= label.x + label.width - 3
    ]
    if len(value_matches) != 1:
        raise ValueError("review_value_occurrence_ambiguous")
    value_index, value = value_matches[0]
    rerun_digest = _mac(local_key, (
        "payroll-review-pdf-text-replay-v1", content_hash,
        tuple((token.text, token.page, token.x, token.y, token.width,
               token.height, token.confidence) for token in first.tokens),
    ))
    values = {
        "contract_version": SOURCE_VALUE_BINDING_VERSION,
        "content_hash": content_hash,
        "parser_mode": "pdf_text",
        "page_count": page_count,
        "item_occurrence": item_occurrence,
        "raw_label_digest": _mac(local_key, ("raw_label", item.raw_item_name)),
        "source_value": source_value,
        "source_value_digest": _mac(local_key, ("source_value", source_value)),
        "label_token_id": _physical_token_id(
            local_key, content_hash, label_index, label,
        ),
        "value_token_id": _physical_token_id(
            local_key, content_hash, value_index, value,
        ),
        "page": label.page,
        "relation": "exact_same_row_right",
        "rerun_digest": rerun_digest,
    }
    unsigned = PayrollReviewSourceValueBinding(**values, signature="")
    return PayrollReviewSourceValueBinding(
        **values, signature=_mac(local_key, _source_value_binding_payload(unsigned)),
    )
