from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Literal


SUPPORTED_SCHEMA_VERSION = "1"
USER_CONFIRMED = "user_confirmed"
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
