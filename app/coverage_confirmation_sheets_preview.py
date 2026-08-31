from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal, Sequence

from .coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    ConfirmationIdentity,
    CoverageConfirmationAppendPreview,
    CoverageConfirmationIdentityResolution,
    CoverageConfirmationRecord,
    diagnose_coverage_confirmation_rows,
    preview_coverage_confirmation_append,
    resolve_coverage_confirmation_identity,
)


SchemaStatus = Literal[
    "exact_match", "sheet_missing", "header_missing", "schema_mismatch", "invalid"
]
DryRunAction = Literal[
    "create_and_append", "create_sheet", "append", "skip_duplicate", "blocked",
    "inspect_only",
]
HEADER_PLANNED_RANGE = f"{COVERAGE_CONFIRMATION_SHEET}!A1:N1"
APPEND_PLANNED_RANGE = f"{COVERAGE_CONFIRMATION_SHEET}!A:A"


@dataclass(frozen=True)
class CoverageConfirmationSchemaVerification:
    status: SchemaStatus
    expected_headers: list[str]
    actual_headers: list[str]
    missing_headers: list[str]
    unexpected_headers: list[str]
    order_mismatch: bool
    diagnostic: str


@dataclass(frozen=True)
class CoverageConfirmationCreatePlan:
    sheet_name: str
    sheet_exists: bool | None
    create_sheet_required: bool
    header_write_required: bool
    expected_headers: list[str]
    planned_range: str | None
    safe_to_create: bool
    diagnostic: str


def plan_coverage_confirmation_sheet_create(
    sheet_status: SchemaStatus,
) -> CoverageConfirmationCreatePlan:
    """Describe a possible sheet/header creation without constructing API calls."""

    expected = list(COVERAGE_CONFIRMATION_HEADERS)
    if sheet_status == "sheet_missing":
        return CoverageConfirmationCreatePlan(
            COVERAGE_CONFIRMATION_SHEET,
            False,
            True,
            True,
            expected,
            HEADER_PLANNED_RANGE,
            True,
            "sheet_missing_create_and_fixed_header_required",
        )
    if sheet_status == "exact_match":
        return CoverageConfirmationCreatePlan(
            COVERAGE_CONFIRMATION_SHEET,
            True,
            False,
            False,
            expected,
            None,
            False,
            "sheet_and_header_already_exist",
        )
    if sheet_status in {"header_missing", "schema_mismatch"}:
        return CoverageConfirmationCreatePlan(
            COVERAGE_CONFIRMATION_SHEET,
            True,
            False,
            False,
            expected,
            None,
            False,
            f"{sheet_status}_manual_review_required",
        )
    return CoverageConfirmationCreatePlan(
        COVERAGE_CONFIRMATION_SHEET,
        None,
        False,
        False,
        expected,
        None,
        False,
        "invalid_sheet_state",
    )


@dataclass(frozen=True)
class CoverageConfirmationSheetsDryRun:
    action: DryRunAction
    sheet_name: str
    sheet_status: SchemaStatus
    schema_status: SchemaStatus
    create_sheet_planned: bool
    header_write_planned: bool
    expected_headers: list[str]
    header_row: list[str] | None
    header_planned_range: str | None
    append_row_planned: bool
    append_row: list[str] | None
    append_planned_range: str | None
    duplicate_status: str | None
    migration_planned: bool
    header_rewrite_planned: bool
    blocked: bool
    reason: str
    external_write: bool = False


def plan_coverage_confirmation_sheets_dry_run(
    sheet_status: SchemaStatus,
    *,
    append_preview: CoverageConfirmationAppendPreview | None = None,
    unsafe_existing_rows: bool = False,
) -> CoverageConfirmationSheetsDryRun:
    """Unify create, fixed-header, and Phase 2-A append plans without I/O."""

    create = plan_coverage_confirmation_sheet_create(sheet_status)
    common = {
        "sheet_name": create.sheet_name,
        "sheet_status": sheet_status,
        "schema_status": sheet_status,
        "create_sheet_planned": create.create_sheet_required,
        "header_write_planned": create.header_write_required,
        "expected_headers": list(create.expected_headers),
        "header_row": (
            list(create.expected_headers) if create.header_write_required else None
        ),
        "header_planned_range": create.planned_range,
        "migration_planned": False,
        "header_rewrite_planned": False,
        "external_write": False,
    }

    if sheet_status not in {"sheet_missing", "exact_match"}:
        return CoverageConfirmationSheetsDryRun(
            action="blocked",
            append_row_planned=False,
            append_row=None,
            append_planned_range=None,
            duplicate_status=None,
            blocked=True,
            reason=create.diagnostic,
            **common,
        )

    if append_preview is None:
        return CoverageConfirmationSheetsDryRun(
            action="create_sheet" if sheet_status == "sheet_missing" else "inspect_only",
            append_row_planned=False,
            append_row=None,
            append_planned_range=None,
            duplicate_status=None,
            blocked=False,
            reason=(
                "confirmation_input_not_supplied_create_plan_only"
                if sheet_status == "sheet_missing"
                else "confirmation_input_not_supplied_schema_only"
            ),
            **common,
        )

    duplicate_status = append_preview.lookup_status
    if duplicate_status == "exact_duplicate":
        return CoverageConfirmationSheetsDryRun(
            action="skip_duplicate",
            append_row_planned=False,
            append_row=None,
            append_planned_range=None,
            duplicate_status=duplicate_status,
            blocked=False,
            reason=append_preview.diagnostic,
            **common,
        )
    if duplicate_status in {"identity_conflict", "invalid"}:
        return CoverageConfirmationSheetsDryRun(
            action="blocked",
            append_row_planned=False,
            append_row=None,
            append_planned_range=None,
            duplicate_status=duplicate_status,
            blocked=True,
            reason=append_preview.diagnostic,
            **common,
        )
    if unsafe_existing_rows:
        return CoverageConfirmationSheetsDryRun(
            action="blocked",
            append_row_planned=False,
            append_row=None,
            append_planned_range=None,
            duplicate_status=duplicate_status,
            blocked=True,
            reason="unsafe_existing_rows",
            **common,
        )
    append_row = append_preview.append_row
    if (
        duplicate_status != "not_found"
        or not append_preview.append_planned
        or append_row is None
        or len(append_row) != len(COVERAGE_CONFIRMATION_HEADERS)
        or any(value is None for value in append_row)
    ):
        return CoverageConfirmationSheetsDryRun(
            action="blocked",
            append_row_planned=False,
            append_row=None,
            append_planned_range=None,
            duplicate_status=duplicate_status,
            blocked=True,
            reason="invalid_append_preview_state",
            **common,
        )
    return CoverageConfirmationSheetsDryRun(
        action="create_and_append" if sheet_status == "sheet_missing" else "append",
        append_row_planned=True,
        append_row=list(append_row),
        append_planned_range=APPEND_PLANNED_RANGE,
        duplicate_status=duplicate_status,
        blocked=False,
        reason=(
            "sheet_header_and_confirmation_row_would_be_created"
            if sheet_status == "sheet_missing"
            else "confirmation_row_would_be_appended"
        ),
        **common,
    )


def verify_coverage_confirmation_schema(
    *,
    sheet_exists: bool,
    actual_headers: Sequence[object] | None,
) -> CoverageConfirmationSchemaVerification:
    """Compare one header with the fixed Phase 2-A schema without modifying it."""

    expected = list(COVERAGE_CONFIRMATION_HEADERS)
    if not isinstance(sheet_exists, bool):
        return CoverageConfirmationSchemaVerification(
            "invalid", expected, [], expected, [], False, "invalid_sheet_state",
        )
    if not sheet_exists:
        return CoverageConfirmationSchemaVerification(
            "sheet_missing", expected, [], expected, [], False, "sheet_missing",
        )
    if actual_headers is None:
        return CoverageConfirmationSchemaVerification(
            "invalid", expected, [], expected, [], False, "invalid_header_state",
        )
    if isinstance(actual_headers, (str, bytes, bytearray)):
        return CoverageConfirmationSchemaVerification(
            "invalid", expected, [], expected, [], False, "invalid_header_type",
        )
    actual_values = list(actual_headers)
    if not actual_values:
        return CoverageConfirmationSchemaVerification(
            "header_missing", expected, [], expected, [], False, "header_missing",
        )
    if any(not isinstance(value, str) for value in actual_values):
        return CoverageConfirmationSchemaVerification(
            "invalid", expected, [], expected, [], False, "invalid_header_value",
        )
    actual = [value.strip() for value in actual_values]
    if any(not value for value in actual):
        return CoverageConfirmationSchemaVerification(
            "invalid", expected, actual, expected, [], False, "empty_header_value",
        )
    if actual == expected:
        return CoverageConfirmationSchemaVerification(
            "exact_match", expected, actual, [], [], False, "schema_exact_match",
        )

    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    missing = list((expected_counts - actual_counts).elements())
    unexpected = list((actual_counts - expected_counts).elements())
    order_mismatch = not missing and not unexpected and actual != expected
    return CoverageConfirmationSchemaVerification(
        "schema_mismatch",
        expected,
        actual,
        missing,
        unexpected,
        order_mismatch,
        "schema_mismatch",
    )


class CoverageConfirmationReadOnlyAdapter:
    """Read only the Coverage confirmation tab through an existing SheetsDB."""

    def __init__(self, db):
        self.db = db

    def sheet_exists(self) -> bool:
        titles = self.db.sheet_titles()
        if isinstance(titles, (str, bytes, bytearray)):
            raise ValueError("invalid_sheet_titles")
        values = list(titles)
        if any(not isinstance(title, str) for title in values):
            raise ValueError("invalid_sheet_title")
        return COVERAGE_CONFIRMATION_SHEET in values

    def header(self) -> list[object]:
        values = self.db.get(f"{COVERAGE_CONFIRMATION_SHEET}!1:1")
        if not values:
            return []
        if len(values) != 1 or isinstance(values[0], (str, bytes, bytearray)):
            raise ValueError("invalid_header_response")
        return list(values[0])

    def data_rows(self) -> list[list[object]]:
        values = self.db.get(f"{COVERAGE_CONFIRMATION_SHEET}!A2:N")
        if isinstance(values, (str, bytes, bytearray)):
            raise ValueError("invalid_data_rows_response")
        rows = []
        for row in values:
            if isinstance(row, (str, bytes, bytearray)):
                raise ValueError("invalid_data_row")
            rows.append(list(row))
        return rows


class CoverageConfirmationReadOnlyResolver:
    """Resolve stored confirmations through read-only adapter operations only."""

    def __init__(self, adapter: CoverageConfirmationReadOnlyAdapter):
        if not isinstance(adapter, CoverageConfirmationReadOnlyAdapter):
            raise TypeError("coverage_confirmation_read_only_adapter_required")
        self.adapter = adapter

    def resolve(
        self,
        identity: ConfirmationIdentity,
    ) -> CoverageConfirmationIdentityResolution:
        if not isinstance(identity, ConfirmationIdentity):
            raise TypeError("confirmation_identity_required")
        try:
            sheet_exists = self.adapter.sheet_exists()
        except Exception:
            return CoverageConfirmationIdentityResolution(
                "invalid_store", "sheet_titles_read_failed",
            )
        if not sheet_exists:
            return CoverageConfirmationIdentityResolution(
                "not_found", "sheet_missing",
            )

        try:
            header = self.adapter.header()
        except Exception:
            return CoverageConfirmationIdentityResolution(
                "invalid_store", "header_read_failed",
            )
        verification = verify_coverage_confirmation_schema(
            sheet_exists=True,
            actual_headers=header,
        )
        if verification.status != "exact_match":
            return CoverageConfirmationIdentityResolution(
                "invalid_store", verification.diagnostic,
            )

        try:
            rows = self.adapter.data_rows()
        except Exception:
            return CoverageConfirmationIdentityResolution(
                "invalid_store", "data_rows_read_failed",
            )
        return resolve_coverage_confirmation_identity(identity, rows)


def _base_report() -> dict:
    report = {
        "sheet_name": COVERAGE_CONFIRMATION_SHEET,
        "sheet_exists": False,
        "schema_status": "invalid",
        "expected_headers": list(COVERAGE_CONFIRMATION_HEADERS),
        "actual_headers": [],
        "missing_headers": [],
        "unexpected_headers": [],
        "order_mismatch": False,
        "existing_row_count": None,
        "valid_row_count": None,
        "invalid_row_count": None,
        "empty_row_count": None,
        "duplicate_identity_count": None,
        "identity_conflict_count": None,
        "row_reads_performed": False,
        "lookup_integration": None,
        "diagnostic": "not_started",
        "external_write": False,
    }
    report.update(asdict(plan_coverage_confirmation_sheets_dry_run("invalid")))
    return report


def _apply_schema_verification(
    report: dict,
    verification: CoverageConfirmationSchemaVerification,
) -> None:
    values = asdict(verification)
    report["schema_status"] = values.pop("status")
    report.update(values)


def _apply_dry_run(
    report: dict,
    append_preview: CoverageConfirmationAppendPreview | None = None,
    *,
    unsafe_existing_rows: bool = False,
) -> None:
    status = report["schema_status"]
    if status not in {
        "exact_match", "sheet_missing", "header_missing", "schema_mismatch", "invalid",
    }:
        status = "invalid"
    dry_run = plan_coverage_confirmation_sheets_dry_run(
        status,
        append_preview=append_preview,
        unsafe_existing_rows=unsafe_existing_rows,
    )
    report.update(asdict(dry_run))


def preview_coverage_confirmation_sheet(
    db,
    *,
    record: CoverageConfirmationRecord | None = None,
    created_at: datetime | None = None,
) -> dict:
    """Diagnose the live-shaped tab and optionally run the Phase 2-A preview."""

    if (record is None) != (created_at is None):
        raise ValueError("record_and_created_at_must_be_supplied_together")
    report = _base_report()
    adapter = CoverageConfirmationReadOnlyAdapter(db)
    try:
        sheet_exists = adapter.sheet_exists()
    except Exception:
        report["diagnostic"] = "sheet_titles_read_failed"
        return report
    report["sheet_exists"] = sheet_exists
    if not sheet_exists:
        verification = verify_coverage_confirmation_schema(
            sheet_exists=False, actual_headers=[],
        )
        _apply_schema_verification(report, verification)
        report["existing_row_count"] = 0
        report["valid_row_count"] = 0
        report["invalid_row_count"] = 0
        report["empty_row_count"] = 0
        report["duplicate_identity_count"] = 0
        report["identity_conflict_count"] = 0
        append_preview = None
        if record is not None and created_at is not None:
            append_preview = preview_coverage_confirmation_append(
                record, [], created_at=created_at,
            )
            report["lookup_integration"] = {
                "action": append_preview.action,
                "append_planned": append_preview.append_planned,
                "duplicate_skip_planned": append_preview.duplicate_skip_planned,
                "diagnostic": append_preview.diagnostic,
                "lookup_status": append_preview.lookup_status,
            }
        _apply_dry_run(report, append_preview)
        return report

    try:
        header = adapter.header()
    except Exception:
        report["diagnostic"] = "header_read_failed"
        return report
    verification = verify_coverage_confirmation_schema(
        sheet_exists=True, actual_headers=header,
    )
    _apply_schema_verification(report, verification)
    if verification.status != "exact_match":
        _apply_dry_run(report)
        return report

    try:
        rows = adapter.data_rows()
    except Exception:
        report["schema_status"] = "invalid"
        report["diagnostic"] = "data_rows_read_failed"
        _apply_dry_run(report)
        return report
    report["row_reads_performed"] = True
    diagnostics = diagnose_coverage_confirmation_rows(rows)
    report.update(asdict(diagnostics))
    if diagnostics.invalid_row_count or diagnostics.identity_conflict_count:
        report["diagnostic"] = "unsafe_existing_rows"
    append_preview = None
    if record is not None and created_at is not None:
        append_preview = preview_coverage_confirmation_append(
            record, rows, created_at=created_at,
        )
        report["lookup_integration"] = {
            "action": append_preview.action,
            "append_planned": append_preview.append_planned,
            "duplicate_skip_planned": append_preview.duplicate_skip_planned,
            "diagnostic": append_preview.diagnostic,
            "lookup_status": append_preview.lookup_status,
        }
    _apply_dry_run(
        report,
        append_preview,
        unsafe_existing_rows=(
            bool(diagnostics.invalid_row_count)
            or bool(diagnostics.identity_conflict_count)
        ),
    )
    return report
