"""Content-free per-file bank outcomes allowed across the production boundary."""
from __future__ import annotations

import re

STATUSES = frozenset({"withheld", "pending", "processed", "would_process", "already_processed"})
REASONS = frozenset({
    "parse_issue", "balance_consistency_failure", "parsed_zero", "collision",
    "bank_document_empty", "bank_document_unrecognized", "native_text_unavailable",
    "header_geometry_unresolved", "column_boundary_unresolved",
    "transaction_review", "unresolved_income", "income_readback_failed",
    "existing_content_mismatch", "new_expense_outside_write_window",
    "deposit_outside_write_window", "outside_write_window", "archive_readback_failed",
    "archive_pending", "archive_verified", "processed_marker_present",
    "processed_move_failed",
})


def safe_file_statuses(value):
    """Reject arbitrary text, extra fields and unbounded child output."""
    if not isinstance(value, list) or len(value) > 100:
        return []
    safe = []
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"file_ref", "status", "reasons"}
                or not isinstance(row["file_ref"], str)
                or not re.fullmatch(r"[0-9a-f]{24}", row["file_ref"])
                or not isinstance(row["status"], str) or row["status"] not in STATUSES
                or not isinstance(row["reasons"], list)
                or not row["reasons"] or len(row["reasons"]) > len(REASONS)
                or any(not isinstance(reason, str) or reason not in REASONS for reason in row["reasons"])):
            continue
        safe.append({"file_ref": row["file_ref"], "status": row["status"],
                     "reasons": sorted(set(row["reasons"]))})
    return safe
