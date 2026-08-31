from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from .coverage_confirmation import (
    COVERAGE_REASON_OPERATIONAL_ONLY,
    COVERAGE_STATUS_USER_CONFIRMED,
    SUPPORTED_SCHEMA_VERSION,
    USER_CONFIRMED,
    CoverageConfirmationRecord,
    coverage_confirmation_id,
)
from .coverage_confirmation_sheets_apply import (
    CoverageConfirmationWritePlan,
    apply_coverage_confirmation_write,
    build_coverage_confirmation_write_plan_with_report,
)


SUPPORTED_CONFIRMATION_PROVIDERS = frozenset({"paypay"})
CONFIRMATION_INPUT_FIELDS = frozenset({
    "schema_version",
    "provider",
    "content_sha256",
    "confirmed_start",
    "confirmed_end",
    "range_source",
    "coverage_status",
    "coverage_reason",
    "confirmed_at",
    "confirmation_version",
    "source_filename",
    "drive_file_id",
    "created_at",
})


class CoverageConfirmationInputError(ValueError):
    """Raised when explicit CLI input cannot safely reconstruct one record."""


@dataclass(frozen=True)
class CoverageConfirmationExplicitInput:
    record: CoverageConfirmationRecord
    created_at: datetime

    @property
    def confirmation_id(self) -> str:
        return coverage_confirmation_id(self.record.identity)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _required_string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CoverageConfirmationInputError(f"invalid_{field}")
    return value.strip()


def _utc_timestamp(payload: dict, field: str) -> datetime:
    text = _required_string(payload, field)
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoverageConfirmationInputError(f"invalid_{field}") from exc
    if (
        value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise CoverageConfirmationInputError(f"{field}_must_be_utc")
    return value.astimezone(timezone.utc)


def load_coverage_confirmation_input(
    path: str | Path,
) -> CoverageConfirmationExplicitInput:
    """Load one strict, explicit JSON record without discovering other sources."""

    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise CoverageConfirmationInputError(
            "coverage_confirmation_input_unreadable"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != CONFIRMATION_INPUT_FIELDS:
        raise CoverageConfirmationInputError(
            "invalid_coverage_confirmation_input_fields"
        )

    provider = _required_string(payload, "provider")
    if provider not in SUPPORTED_CONFIRMATION_PROVIDERS:
        raise CoverageConfirmationInputError("unsupported_provider")
    if _required_string(payload, "schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise CoverageConfirmationInputError("unsupported_schema_version")
    if _required_string(payload, "range_source") != USER_CONFIRMED:
        raise CoverageConfirmationInputError("invalid_range_source")
    if _required_string(payload, "coverage_status") != COVERAGE_STATUS_USER_CONFIRMED:
        raise CoverageConfirmationInputError("invalid_coverage_status")
    if _required_string(payload, "coverage_reason") != COVERAGE_REASON_OPERATIONAL_ONLY:
        raise CoverageConfirmationInputError("invalid_coverage_reason")
    drive_file_id = payload["drive_file_id"]
    if drive_file_id is not None and (
        not isinstance(drive_file_id, str) or not drive_file_id.strip()
    ):
        raise CoverageConfirmationInputError("invalid_drive_file_id")

    confirmed_at = _utc_timestamp(payload, "confirmed_at")
    created_at = _utc_timestamp(payload, "created_at")
    try:
        record = CoverageConfirmationRecord(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            provider=provider,
            content_sha256=_required_string(payload, "content_sha256"),
            confirmed_start=_required_string(payload, "confirmed_start"),
            confirmed_end=_required_string(payload, "confirmed_end"),
            range_source=USER_CONFIRMED,
            confirmed_at=confirmed_at,
            confirmation_version=_required_string(
                payload, "confirmation_version",
            ),
            source_filename=_required_string(payload, "source_filename"),
            drive_file_id=(
                drive_file_id.strip() if isinstance(drive_file_id, str) else None
            ),
        )
    except (TypeError, ValueError) as exc:
        reason = str(exc) if str(exc) else "invalid_confirmation_record"
        raise CoverageConfirmationInputError(reason) from exc
    return CoverageConfirmationExplicitInput(record=record, created_at=created_at)


def build_coverage_confirmation_preflight(
    db,
    explicit_input: CoverageConfirmationExplicitInput,
) -> tuple[dict, CoverageConfirmationWritePlan]:
    """Return a sanitized human-review report and its exact write-free plan."""

    plan, report = build_coverage_confirmation_write_plan_with_report(
        db,
        record=explicit_input.record,
        created_at=explicit_input.created_at,
    )
    record = explicit_input.record
    preflight = {
        "spreadsheet_identified": True,
        "sheet_name": report["sheet_name"],
        "sheet_status": report["sheet_status"],
        "schema_status": report["schema_status"],
        "expected_action": plan.action_requested,
        "duplicate_status": plan.expected_duplicate_status,
        "create_sheet_planned": bool(report["create_sheet_planned"]),
        "header_write_planned": bool(report["header_write_planned"]),
        "append_row_planned": bool(report["append_row_planned"]),
        "blocked": plan.blocked,
        "reason": plan.reason,
        "confirmation_id": explicit_input.confirmation_id,
        "provider": record.provider,
        "content_sha256": record.content_sha256,
        "confirmed_start": record.confirmed_start,
        "confirmed_end": record.confirmed_end,
        "range_source": record.range_source,
        "coverage_status": COVERAGE_STATUS_USER_CONFIRMED,
        "coverage_reason": COVERAGE_REASON_OPERATIONAL_ONLY,
        "external_write": False,
    }
    return preflight, plan


def run_coverage_confirmation_preflight(
    db,
    explicit_input: CoverageConfirmationExplicitInput,
) -> dict:
    preflight, _plan = build_coverage_confirmation_preflight(db, explicit_input)
    return preflight


def run_coverage_confirmation_apply(
    db,
    explicit_input: CoverageConfirmationExplicitInput,
    *,
    apply: bool = False,
    confirm_id: str | None = None,
) -> dict:
    """Run preflight by default; delegate writes only after both acknowledgements."""

    preflight, plan = build_coverage_confirmation_preflight(db, explicit_input)
    if apply is not True:
        return preflight
    if confirm_id != explicit_input.confirmation_id:
        return {
            "preflight": preflight,
            "apply_result": {
                "action_requested": plan.action_requested,
                "action_performed": (),
                "created_sheet": False,
                "wrote_header": False,
                "appended_row": False,
                "skipped_duplicate": False,
                "blocked": True,
                "reason": "confirmation_id_mismatch",
                "prewrite_status": None,
                "postwrite_status": None,
                "external_write": False,
            },
            "external_write": False,
        }
    apply_result = apply_coverage_confirmation_write(db, plan, apply=True)
    return {
        "preflight": preflight,
        "apply_result": apply_result,
        "external_write": bool(apply_result["external_write"]),
    }
