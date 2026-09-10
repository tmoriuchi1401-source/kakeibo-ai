"""Read-only projection of the production PDF-text token universe.

This bridge adds no parser or ownership semantics.  It binds the exact tokens
already emitted by ``extract_payroll_text`` to source bytes, full page scope,
and a deterministic replay before exposing them to the existing observer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from .payroll_diagnostic_evidence import Snapshot, observe_tokens
from .payroll_ocr import extract_payroll_text


EXTRACTION_VERSION = "production-pdf-text-snapshot-bridge-v1"


@dataclass(frozen=True)
class PdfTextSourceBinding:
    source_id: str
    page_count: int
    byte_count: int
    extraction_version: str
    complete: bool
    reason: str


@dataclass(frozen=True)
class PdfTextOwnershipSnapshot:
    source: PdfTextSourceBinding
    snapshot: Snapshot = field(repr=False)
    rerun_stable: bool = False
    page_scope_complete: bool = False
    parser_mode: str = "pdf"
    ownership_ready: bool = False
    reason: str = "not_evaluated"


def _digest(key: bytes, payload: object) -> str:
    return hmac.new(
        key,
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()


def capture_pdf_text_ownership_snapshot(path, *, local_key=None):
    """Bind two identical production PDF-text extractions to one source."""

    path = Path(path)
    key = secrets.token_bytes(32) if local_key is None else local_key
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("local_key_too_short")
    source_bytes = path.read_bytes()
    source_id = _digest(key, ("payroll-pdf-text-source-v1", source_bytes.hex()))
    blank_context = (EXTRACTION_VERSION, source_id, 0)
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("encrypted_pdf")
        page_count = len(reader.pages)
        first = extract_payroll_text(path)
        second = extract_payroll_text(path)
        if first.extraction_method != "pdf_text":
            raise ValueError("production_did_not_select_pdf_text")
        context = (EXTRACTION_VERSION, source_id, page_count)
        snapshot = observe_tokens(
            first.tokens,
            local_key=key,
            parser_mode="pdf",
            snapshot_context=context,
        )
        replay = observe_tokens(
            second.tokens,
            local_key=key,
            parser_mode="pdf",
            snapshot_context=context,
        )
    except Exception as exc:
        blank = observe_tokens(
            (), local_key=key, parser_mode="pdf",
            snapshot_context=blank_context,
        )
        reason = str(exc) if str(exc) in {
            "encrypted_pdf", "production_did_not_select_pdf_text",
        } else "pdf_text_capture_failed"
        source = PdfTextSourceBinding(
            source_id, 0, len(source_bytes), EXTRACTION_VERSION,
            False, reason,
        )
        return PdfTextOwnershipSnapshot(source, blank, reason=reason)

    bytes_current = path.read_bytes() == source_bytes
    page_scope = bool(
        page_count > 0
        and first.tokens
        and all(1 <= token.page <= page_count for token in first.tokens)
    )
    rerun_stable = bool(
        first.file_type == second.file_type == "pdf"
        and second.extraction_method == "pdf_text"
        and first.text == second.text
        and first.tokens == second.tokens
        and snapshot.snapshot_id == replay.snapshot_id
        and snapshot.facts == replay.facts
    )
    complete = bool(bytes_current and page_scope and rerun_stable)
    reason = "ownership_ready" if complete else (
        "source_bytes_changed_during_capture" if not bytes_current
        else "incomplete_page_scope" if not page_scope
        else "pdf_text_rerun_unstable"
    )
    source = PdfTextSourceBinding(
        source_id, page_count, len(source_bytes), EXTRACTION_VERSION,
        bytes_current, "same_source_bytes_bound" if bytes_current else reason,
    )
    return PdfTextOwnershipSnapshot(
        source, snapshot, rerun_stable, page_scope, "pdf", complete, reason,
    )
