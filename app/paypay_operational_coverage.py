from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TypeAlias

from .paypay_pipeline import inspect_paypay_csv


OperationalCoverage: TypeAlias = Literal["usable", "needs_confirmation", "rejected"]
RangeSource: TypeAlias = Literal["filename_candidate", "user_confirmed", "manual"]

_FILENAME_RANGE = re.compile(r"Transactions_(\d{8})-(\d{8})\.csv")


@dataclass(frozen=True)
class PayPayOperationalEvidence:
    requested_start: str | None
    requested_end: str | None
    range_source: RangeSource | None
    range_confirmed: bool
    csv_filename: str
    csv_sha256: str | None
    received_at: str | None
    row_count: int | None
    transaction_min_date: str | None
    transaction_max_date: str | None
    filename_candidate_start: str | None
    filename_candidate_end: str | None
    operational_coverage: OperationalCoverage
    reason: str
    parse_error: str | None = None


def extract_filename_range(filename: str) -> tuple[str, str] | None:
    match = _FILENAME_RANGE.fullmatch(Path(filename).name)
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), "%Y%m%d").date()
        end = datetime.strptime(match.group(2), "%Y%m%d").date()
    except ValueError:
        return None
    if start > end:
        return None
    return start.isoformat(), end.isoformat()


def _valid_range(start: str | None, end: str | None) -> tuple[str, str] | None:
    if not start or not end:
        return None
    try:
        parsed_start = date.fromisoformat(start)
        parsed_end = date.fromisoformat(end)
    except ValueError:
        return None
    if parsed_start > parsed_end:
        return None
    return parsed_start.isoformat(), parsed_end.isoformat()


def preview_operational_evidence(
    path: str | Path,
    *, requested_start: str | None = None,
    requested_end: str | None = None,
    range_source: RangeSource | None = None,
    range_confirmed: bool = False,
    received_at: str | None = None,
) -> PayPayOperationalEvidence:
    """Inspect a PayPay CSV locally without importing or mutating it."""
    csv_path = Path(path)
    filename_candidate = extract_filename_range(csv_path.name)
    candidate_start, candidate_end = filename_candidate or (None, None)
    try:
        content_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    except OSError as exc:
        return PayPayOperationalEvidence(
            requested_start, requested_end, range_source, range_confirmed,
            csv_path.name, None, received_at, None, None, None,
            candidate_start, candidate_end, "rejected", "csv_unreadable", str(exc),
        )
    try:
        inspection = inspect_paypay_csv(csv_path)
    except (OSError, UnicodeError, ValueError) as exc:
        return PayPayOperationalEvidence(
            requested_start, requested_end, range_source, range_confirmed,
            csv_path.name, content_hash, received_at, None, None, None,
            candidate_start, candidate_end, "rejected", "parse_error", str(exc),
        )

    row_count = int(inspection["row_count"])
    transaction_min = inspection["observed_start"]
    transaction_max = inspection["observed_end"]
    explicit_range = _valid_range(requested_start, requested_end)
    if requested_start or requested_end:
        if explicit_range is None:
            status, reason = "rejected", "invalid_requested_range"
        elif not range_confirmed:
            status, reason = "needs_confirmation", "range_not_confirmed"
        elif range_source != "user_confirmed":
            status, reason = "needs_confirmation", "range_not_user_confirmed"
        elif filename_candidate and explicit_range != filename_candidate:
            status, reason = (
                "needs_confirmation", "confirmed_range_conflicts_with_filename_candidate",
            )
        elif (
            transaction_min and transaction_max
            and (transaction_min < explicit_range[0] or transaction_max > explicit_range[1])
        ):
            status, reason = "rejected", "transaction_outside_requested_range"
        else:
            status, reason = "usable", "operational_checks_passed"
    elif filename_candidate:
        requested_start, requested_end = filename_candidate
        range_source = "filename_candidate"
        status, reason = "needs_confirmation", "filename_range_requires_confirmation"
    else:
        status, reason = "needs_confirmation", "requested_range_missing"

    return PayPayOperationalEvidence(
        requested_start, requested_end, range_source, range_confirmed,
        csv_path.name, content_hash, received_at, row_count,
        transaction_min, transaction_max, candidate_start, candidate_end,
        status, reason,
    )


def classify_operational_evidence(
    evidences: list[PayPayOperationalEvidence],
) -> tuple[list[PayPayOperationalEvidence], int, int]:
    groups: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(evidences):
        if (
            item.range_confirmed and item.range_source == "user_confirmed"
            and item.requested_start and item.requested_end
        ):
            groups.setdefault((item.requested_start, item.requested_end), []).append(index)
    duplicate_indexes: set[int] = set()
    conflict_indexes: set[int] = set()
    duplicates = conflicts = 0
    for indexes in groups.values():
        hashes = [evidences[index].csv_sha256 for index in indexes
                  if evidences[index].csv_sha256]
        if len(hashes) < 2:
            continue
        if len(set(hashes)) == 1:
            duplicates += len(hashes) - 1
            duplicate_indexes.update(indexes[1:])
        else:
            conflicts += len(set(hashes)) - 1
            conflict_indexes.update(indexes)
    result = []
    for index, item in enumerate(evidences):
        if index in conflict_indexes:
            item = replace(
                item, operational_coverage="rejected",
                reason="conflicting_operational_evidence",
            )
        elif index in duplicate_indexes and item.operational_coverage == "usable":
            item = replace(item, reason="duplicate_operational_evidence")
        result.append(item)
    return result, duplicates, conflicts
