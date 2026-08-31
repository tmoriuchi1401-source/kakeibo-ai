from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Literal, Sequence


SUPPORTED_SCHEMA_VERSION = "1"
USER_CONFIRMED = "user_confirmed"
COVERAGE_CONFIRMATION_SHEET = "Coverage確認"
COVERAGE_CONFIRMATION_HEADERS = [
    "Confirmation ID",
    "Schema Version",
    "Provider",
    "Content SHA-256",
    "Confirmed Start",
    "Confirmed End",
    "Range Source",
    "Coverage Status",
    "Coverage Reason",
    "Confirmed At",
    "Confirmation Version",
    "Source Filename",
    "Drive File ID",
    "Created At",
]
COVERAGE_STATUS_USER_CONFIRMED = "user_confirmed"
COVERAGE_REASON_OPERATIONAL_ONLY = (
    "explicit_user_confirmation_not_provider_completeness"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ConfirmationValidationError(ValueError):
    """Raised when a confirmation record cannot be safely used."""


def content_sha256(data: bytes | bytearray | memoryview) -> str:
    """Return the canonical lowercase SHA-256 for source CSV bytes only."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("content_sha256_requires_bytes")
    return hashlib.sha256(data).hexdigest()


def normalize_content_sha256(value: str) -> str:
    """Validate and normalize a SHA-256 digest to lowercase hexadecimal."""
    if not isinstance(value, str):
        raise ConfirmationValidationError("invalid_content_sha256")
    normalized = value.lower()
    if not _SHA256.fullmatch(normalized):
        raise ConfirmationValidationError("invalid_content_sha256")
    return normalized


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfirmationValidationError(f"invalid_{field}")
    return value.strip()


def _iso_date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ConfirmationValidationError(f"invalid_{field}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ConfirmationValidationError(f"invalid_{field}") from exc


@dataclass(frozen=True)
class ConfirmationIdentity:
    """Stable confirmation key; source filename and Drive ID are not identity."""

    provider: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(
            self, "content_sha256", normalize_content_sha256(self.content_sha256),
        )


@dataclass(frozen=True)
class CoverageConfirmationRecord:
    """An explicit user confirmation of one provider CSV's requested range.

    This record is operational evidence only.  It never proves provider-side
    completeness and must not be used to set a coverage manifest to complete.
    """

    schema_version: str
    provider: str
    content_sha256: str
    confirmed_start: str
    confirmed_end: str
    range_source: str
    confirmed_at: datetime
    confirmation_version: str
    source_filename: str
    drive_file_id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ConfirmationValidationError("unsupported_schema_version")
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(
            self, "content_sha256", normalize_content_sha256(self.content_sha256),
        )
        start = _iso_date(self.confirmed_start, "confirmed_start")
        end = _iso_date(self.confirmed_end, "confirmed_end")
        if start > end:
            raise ConfirmationValidationError("invalid_confirmed_range")
        object.__setattr__(self, "confirmed_start", start)
        object.__setattr__(self, "confirmed_end", end)
        if self.range_source != USER_CONFIRMED:
            raise ConfirmationValidationError("invalid_range_source")
        if not isinstance(self.confirmed_at, datetime):
            raise ConfirmationValidationError("invalid_confirmed_at")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ConfirmationValidationError("confirmed_at_must_be_timezone_aware")
        if self.confirmed_at.utcoffset() != timedelta(0):
            raise ConfirmationValidationError("confirmed_at_must_be_utc")
        object.__setattr__(self, "confirmed_at", self.confirmed_at.astimezone(timezone.utc))
        object.__setattr__(
            self, "confirmation_version",
            _required_text(self.confirmation_version, "confirmation_version"),
        )
        object.__setattr__(
            self, "source_filename", _required_text(self.source_filename, "source_filename"),
        )
        if self.drive_file_id is not None:
            object.__setattr__(
                self, "drive_file_id", _required_text(self.drive_file_id, "drive_file_id"),
            )

    @property
    def identity(self) -> ConfirmationIdentity:
        return ConfirmationIdentity(self.provider, self.content_sha256)


LookupStatus = Literal["missing", "confirmed", "conflict", "invalid"]


@dataclass(frozen=True)
class ConfirmationLookupResult:
    status: LookupStatus
    record: CoverageConfirmationRecord | None = None
    duplicate_same_range: bool = False


def evaluate_confirmation_records(
    identity: ConfirmationIdentity,
    records: Iterable[object],
) -> ConfirmationLookupResult:
    """Safely classify records already selected for one confirmation identity."""
    items = list(records)
    if not items:
        return ConfirmationLookupResult("missing")
    if any(not isinstance(item, CoverageConfirmationRecord) for item in items):
        return ConfirmationLookupResult("invalid")

    confirmations = [item for item in items if isinstance(item, CoverageConfirmationRecord)]
    if any(item.identity != identity for item in confirmations):
        return ConfirmationLookupResult("invalid")

    ranges = {(item.confirmed_start, item.confirmed_end) for item in confirmations}
    if len(ranges) != 1:
        return ConfirmationLookupResult("conflict")
    return ConfirmationLookupResult(
        "confirmed", confirmations[0], duplicate_same_range=len(confirmations) > 1,
    )


def coverage_confirmation_id(identity: ConfirmationIdentity) -> str:
    """Return a stable storage ID based only on the Phase 1 identity."""

    if not isinstance(identity, ConfirmationIdentity):
        raise TypeError("confirmation_identity_required")
    canonical = f"{identity.provider}\0{identity.content_sha256}".encode("utf-8")
    return f"CC-{hashlib.sha256(canonical).hexdigest()[:24]}"


def _utc_iso_timestamp(value: object, field: str) -> str:
    if not isinstance(value, datetime):
        raise ConfirmationValidationError(f"invalid_{field}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfirmationValidationError(f"{field}_must_be_timezone_aware")
    if value.utcoffset() != timedelta(0):
        raise ConfirmationValidationError(f"{field}_must_be_utc")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_utc_timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfirmationValidationError(f"invalid_{field}") from exc
    _utc_iso_timestamp(parsed, field)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CoverageConfirmationSheetRow:
    """Fixed Sheets representation; constructing it performs no I/O."""

    confirmation_id: str
    schema_version: str
    provider: str
    content_sha256: str
    confirmed_start: str
    confirmed_end: str
    range_source: str
    coverage_status: str
    coverage_reason: str
    confirmed_at: str
    confirmation_version: str
    source_filename: str
    drive_file_id: str
    created_at: str

    def to_sheet_row(self) -> list[str]:
        """Return values in the exact order of COVERAGE_CONFIRMATION_HEADERS."""

        return [
            self.confirmation_id,
            self.schema_version,
            self.provider,
            self.content_sha256,
            self.confirmed_start,
            self.confirmed_end,
            self.range_source,
            self.coverage_status,
            self.coverage_reason,
            self.confirmed_at,
            self.confirmation_version,
            self.source_filename,
            self.drive_file_id,
            self.created_at,
        ]


def coverage_confirmation_to_sheet_row(
    record: CoverageConfirmationRecord,
    *,
    created_at: datetime,
) -> CoverageConfirmationSheetRow:
    """Convert a Phase 1 record to its fixed row without writing it."""

    if not isinstance(record, CoverageConfirmationRecord):
        raise TypeError("coverage_confirmation_record_required")
    return CoverageConfirmationSheetRow(
        confirmation_id=coverage_confirmation_id(record.identity),
        schema_version=record.schema_version,
        provider=record.provider,
        content_sha256=record.content_sha256,
        confirmed_start=record.confirmed_start,
        confirmed_end=record.confirmed_end,
        range_source=record.range_source,
        coverage_status=COVERAGE_STATUS_USER_CONFIRMED,
        coverage_reason=COVERAGE_REASON_OPERATIONAL_ONLY,
        confirmed_at=_utc_iso_timestamp(record.confirmed_at, "confirmed_at"),
        confirmation_version=record.confirmation_version,
        source_filename=record.source_filename,
        drive_file_id=record.drive_file_id or "",
        created_at=_utc_iso_timestamp(created_at, "created_at"),
    )


SheetLookupStatus = Literal[
    "exact_duplicate", "not_found", "identity_conflict", "invalid"
]


@dataclass(frozen=True)
class CoverageConfirmationSheetLookupResult:
    status: SheetLookupStatus
    diagnostic: str
    matching_row_number: int | None = None


@dataclass(frozen=True)
class StoredCoverageConfirmation:
    """One fully validated confirmation restored from the fixed Sheets row."""

    confirmation_id: str
    record: CoverageConfirmationRecord
    coverage_status: str
    coverage_reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.record, CoverageConfirmationRecord):
            raise TypeError("coverage_confirmation_record_required")
        if self.confirmation_id != coverage_confirmation_id(self.record.identity):
            raise ConfirmationValidationError("invalid_confirmation_id")
        if self.coverage_status != COVERAGE_STATUS_USER_CONFIRMED:
            raise ConfirmationValidationError("invalid_coverage_status")
        if self.coverage_reason != COVERAGE_REASON_OPERATIONAL_ONLY:
            raise ConfirmationValidationError("invalid_coverage_reason")
        _utc_iso_timestamp(self.created_at, "created_at")
        object.__setattr__(
            self, "created_at", self.created_at.astimezone(timezone.utc),
        )

    @property
    def identity(self) -> ConfirmationIdentity:
        return self.record.identity

    @property
    def schema_version(self) -> str:
        return self.record.schema_version

    @property
    def provider(self) -> str:
        return self.record.provider

    @property
    def content_sha256(self) -> str:
        return self.record.content_sha256

    @property
    def confirmed_start(self) -> str:
        return self.record.confirmed_start

    @property
    def confirmed_end(self) -> str:
        return self.record.confirmed_end

    @property
    def range_source(self) -> str:
        return self.record.range_source

    @property
    def confirmed_at(self) -> datetime:
        return self.record.confirmed_at

    @property
    def confirmation_version(self) -> str:
        return self.record.confirmation_version

    @property
    def source_filename(self) -> str:
        return self.record.source_filename

    @property
    def drive_file_id(self) -> str | None:
        return self.record.drive_file_id


IdentityResolutionStatus = Literal["exact_match", "not_found", "invalid_store"]


@dataclass(frozen=True)
class CoverageConfirmationIdentityResolution:
    status: IdentityResolutionStatus
    diagnostic: str
    stored_confirmation: StoredCoverageConfirmation | None = None
    matching_row_number: int | None = None

    @property
    def record(self) -> CoverageConfirmationRecord | None:
        if self.stored_confirmation is None:
            return None
        return self.stored_confirmation.record


def parse_stored_coverage_confirmation(
    raw_row: Sequence[object],
) -> StoredCoverageConfirmation | None:
    """Restore and validate one fixed 14-column row without performing I/O."""

    if isinstance(raw_row, (str, bytes, bytearray)):
        raise ConfirmationValidationError("invalid_sheet_row")
    row = list(raw_row)
    if not row or not any(str(value or "").strip() for value in row):
        return None
    if len(row) != len(COVERAGE_CONFIRMATION_HEADERS):
        raise ConfirmationValidationError("invalid_sheet_row_length")
    cells = ["" if value is None else str(value).strip() for value in row]
    identity = ConfirmationIdentity(cells[2], cells[3])
    expected_id = coverage_confirmation_id(identity)
    if cells[0] != expected_id:
        raise ConfirmationValidationError("invalid_confirmation_id")
    if cells[1] != SUPPORTED_SCHEMA_VERSION:
        raise ConfirmationValidationError("unsupported_schema_version")
    start = _iso_date(cells[4], "confirmed_start")
    end = _iso_date(cells[5], "confirmed_end")
    if start > end:
        raise ConfirmationValidationError("invalid_confirmed_range")
    if cells[6] != USER_CONFIRMED:
        raise ConfirmationValidationError("invalid_range_source")
    if cells[7] != COVERAGE_STATUS_USER_CONFIRMED:
        raise ConfirmationValidationError("invalid_coverage_status")
    if cells[8] != COVERAGE_REASON_OPERATIONAL_ONLY:
        raise ConfirmationValidationError("invalid_coverage_reason")
    record = CoverageConfirmationRecord(
        schema_version=cells[1],
        provider=identity.provider,
        content_sha256=identity.content_sha256,
        confirmed_start=start,
        confirmed_end=end,
        range_source=cells[6],
        confirmed_at=_parse_utc_timestamp(cells[9], "confirmed_at"),
        confirmation_version=cells[10],
        source_filename=cells[11],
        drive_file_id=cells[12] or None,
    )
    return StoredCoverageConfirmation(
        confirmation_id=cells[0],
        record=record,
        coverage_status=cells[7],
        coverage_reason=cells[8],
        created_at=_parse_utc_timestamp(cells[13], "created_at"),
    )


def _parse_confirmation_sheet_row(
    raw_row: Sequence[object],
) -> StoredCoverageConfirmation | None:
    return parse_stored_coverage_confirmation(raw_row)


@dataclass(frozen=True)
class CoverageConfirmationRowsDiagnostics:
    existing_row_count: int
    valid_row_count: int
    invalid_row_count: int
    empty_row_count: int
    duplicate_identity_count: int
    identity_conflict_count: int


def diagnose_coverage_confirmation_rows(
    existing_rows: Iterable[Sequence[object]],
) -> CoverageConfirmationRowsDiagnostics:
    """Count row health without exposing row values or changing lookup semantics."""

    valid: list[StoredCoverageConfirmation] = []
    invalid_count = 0
    empty_count = 0
    for raw_row in existing_rows:
        try:
            parsed = _parse_confirmation_sheet_row(raw_row)
        except (ConfirmationValidationError, TypeError):
            invalid_count += 1
            continue
        if parsed is None:
            empty_count += 1
        else:
            valid.append(parsed)

    identities: dict[ConfirmationIdentity, list[StoredCoverageConfirmation]] = {}
    for item in valid:
        identities.setdefault(item.identity, []).append(item)
    duplicate_count = sum(len(items) - 1 for items in identities.values())
    conflict_count = sum(
        len({(item.confirmed_start, item.confirmed_end) for item in items}) > 1
        for items in identities.values()
    )
    return CoverageConfirmationRowsDiagnostics(
        existing_row_count=len(valid) + invalid_count,
        valid_row_count=len(valid),
        invalid_row_count=invalid_count,
        empty_row_count=empty_count,
        duplicate_identity_count=duplicate_count,
        identity_conflict_count=conflict_count,
    )


def resolve_coverage_confirmation_identity(
    identity: ConfirmationIdentity,
    existing_rows: Iterable[Sequence[object]],
) -> CoverageConfirmationIdentityResolution:
    """Resolve exactly one stored confirmation by identity, failing closed globally."""

    if not isinstance(identity, ConfirmationIdentity):
        raise TypeError("confirmation_identity_required")
    try:
        rows = list(existing_rows)
    except TypeError:
        return CoverageConfirmationIdentityResolution(
            "invalid_store", "invalid_existing_rows",
        )

    diagnostics = diagnose_coverage_confirmation_rows(rows)
    if diagnostics.invalid_row_count:
        return CoverageConfirmationIdentityResolution(
            "invalid_store", "invalid_existing_row",
        )
    if diagnostics.identity_conflict_count:
        return CoverageConfirmationIdentityResolution(
            "invalid_store", "identity_range_conflict",
        )
    if diagnostics.duplicate_identity_count:
        return CoverageConfirmationIdentityResolution(
            "invalid_store", "duplicate_identity",
        )

    matches: list[tuple[int, StoredCoverageConfirmation]] = []
    for row_number, raw_row in enumerate(rows, start=2):
        stored = _parse_confirmation_sheet_row(raw_row)
        if stored is not None and stored.identity == identity:
            matches.append((row_number, stored))
    if not matches:
        return CoverageConfirmationIdentityResolution(
            "not_found", "confirmation_not_found",
        )
    if len(matches) != 1:
        return CoverageConfirmationIdentityResolution(
            "invalid_store", "target_identity_not_unique",
        )
    row_number, stored = matches[0]
    return CoverageConfirmationIdentityResolution(
        "exact_match",
        "exact_identity_match",
        stored_confirmation=stored,
        matching_row_number=row_number,
    )


def lookup_coverage_confirmation_rows(
    record: CoverageConfirmationRecord,
    existing_rows: Iterable[Sequence[object]],
) -> CoverageConfirmationSheetLookupResult:
    """Find only an exact Phase 1 identity/range match in supplied Sheets rows."""

    if not isinstance(record, CoverageConfirmationRecord):
        raise TypeError("coverage_confirmation_record_required")
    matches: list[tuple[int, _ParsedSheetConfirmation]] = []
    for row_number, raw_row in enumerate(existing_rows, start=2):
        try:
            parsed = _parse_confirmation_sheet_row(raw_row)
        except (ConfirmationValidationError, TypeError):
            return CoverageConfirmationSheetLookupResult(
                "invalid", "invalid_existing_row", row_number,
            )
        if parsed is not None and parsed.identity == record.identity:
            matches.append((row_number, parsed))

    if not matches:
        return CoverageConfirmationSheetLookupResult(
            "not_found", "confirmation_not_found",
        )
    if any(
        (item.confirmed_start, item.confirmed_end)
        != (record.confirmed_start, record.confirmed_end)
        for _, item in matches
    ):
        return CoverageConfirmationSheetLookupResult(
            "identity_conflict", "identity_range_conflict", matches[0][0],
        )
    return CoverageConfirmationSheetLookupResult(
        "exact_duplicate", "exact_identity_and_range_duplicate", matches[0][0],
    )


AppendPreviewAction = Literal["append", "skip"]


@dataclass(frozen=True)
class CoverageConfirmationAppendPreview:
    action: AppendPreviewAction
    append_planned: bool
    duplicate_skip_planned: bool
    append_row: list[str] | None
    diagnostic: str
    lookup_status: SheetLookupStatus


def preview_coverage_confirmation_append(
    record: CoverageConfirmationRecord,
    existing_rows: Iterable[Sequence[object]],
    *,
    created_at: datetime,
) -> CoverageConfirmationAppendPreview:
    """Plan one append from in-memory rows without calling a Sheets client."""

    candidate = coverage_confirmation_to_sheet_row(
        record, created_at=created_at,
    ).to_sheet_row()
    lookup = lookup_coverage_confirmation_rows(record, existing_rows)
    if lookup.status == "not_found":
        return CoverageConfirmationAppendPreview(
            action="append",
            append_planned=True,
            duplicate_skip_planned=False,
            append_row=candidate,
            diagnostic="append_new_confirmation",
            lookup_status=lookup.status,
        )
    return CoverageConfirmationAppendPreview(
        action="skip",
        append_planned=False,
        duplicate_skip_planned=lookup.status == "exact_duplicate",
        append_row=None,
        diagnostic=lookup.diagnostic,
        lookup_status=lookup.status,
    )
