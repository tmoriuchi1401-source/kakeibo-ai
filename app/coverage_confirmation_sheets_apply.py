from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Literal

from .coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    CoverageConfirmationRecord,
    coverage_confirmation_to_sheet_row,
)
from .coverage_confirmation_sheets_preview import (
    DryRunAction,
    SchemaStatus,
    preview_coverage_confirmation_sheet,
)


PerformedAction = Literal[
    "create_sheet", "write_header", "append_row", "skip_duplicate",
]


@dataclass(frozen=True)
class CoverageConfirmationWritePlan:
    """Write-free snapshot binding one candidate to one spreadsheet."""

    target_spreadsheet_id: str
    record: CoverageConfirmationRecord
    created_at: datetime
    action_requested: DryRunAction
    expected_sheet_status: SchemaStatus
    expected_duplicate_status: str | None
    candidate_row: tuple[str, ...]
    blocked: bool
    reason: str
    external_write: bool = False


@dataclass(frozen=True)
class CoverageConfirmationWriteStatus:
    """Sanitized state captured by a fresh Sheets read."""

    spreadsheet_matches: bool
    sheet_status: SchemaStatus
    schema_status: SchemaStatus
    current_headers: tuple[str, ...]
    existing_row_count: int | None
    invalid_row_count: int | None
    identity_conflict_count: int | None
    duplicate_status: str | None
    current_action: str
    safe_to_write: bool
    reason: str


@dataclass(frozen=True)
class CoverageConfirmationApplyResult:
    action_requested: str
    action_performed: tuple[PerformedAction, ...]
    created_sheet: bool
    wrote_header: bool
    appended_row: bool
    skipped_duplicate: bool
    blocked: bool
    reason: str
    prewrite_status: CoverageConfirmationWriteStatus | None
    postwrite_status: CoverageConfirmationWriteStatus | None
    external_write: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _spreadsheet_id(db) -> str:
    value = getattr(db, "sid", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("spreadsheet_id_required")
    return value.strip()


def _candidate_row(
    record: CoverageConfirmationRecord,
    created_at: datetime,
) -> tuple[str, ...]:
    return tuple(coverage_confirmation_to_sheet_row(
        record, created_at=created_at,
    ).to_sheet_row())


def build_coverage_confirmation_write_plan(
    db,
    *,
    record: CoverageConfirmationRecord,
    created_at: datetime,
) -> CoverageConfirmationWritePlan:
    """Build a write-free plan from the current sheet state."""

    target_spreadsheet_id = _spreadsheet_id(db)
    candidate_row = _candidate_row(record, created_at)
    report = preview_coverage_confirmation_sheet(
        db, record=record, created_at=created_at,
    )
    return CoverageConfirmationWritePlan(
        target_spreadsheet_id=target_spreadsheet_id,
        record=record,
        created_at=created_at,
        action_requested=report["action"],
        expected_sheet_status=report["sheet_status"],
        expected_duplicate_status=report["duplicate_status"],
        candidate_row=candidate_row,
        blocked=bool(report["blocked"]),
        reason=str(report["reason"]),
    )


def _status_from_report(
    report: dict,
    *,
    spreadsheet_matches: bool,
    safe_to_write: bool,
    reason: str,
) -> CoverageConfirmationWriteStatus:
    sheet_status = report.get("sheet_status", "invalid")
    if sheet_status not in {
        "exact_match", "sheet_missing", "header_missing", "schema_mismatch",
        "invalid",
    }:
        sheet_status = "invalid"
    schema_status = report.get("schema_status", "invalid")
    if schema_status not in {
        "exact_match", "sheet_missing", "header_missing", "schema_mismatch",
        "invalid",
    }:
        schema_status = "invalid"
    headers = report.get("actual_headers")
    current_headers = tuple(headers) if isinstance(headers, list) else ()
    return CoverageConfirmationWriteStatus(
        spreadsheet_matches=spreadsheet_matches,
        sheet_status=sheet_status,
        schema_status=schema_status,
        current_headers=current_headers,
        existing_row_count=report.get("existing_row_count"),
        invalid_row_count=report.get("invalid_row_count"),
        identity_conflict_count=report.get("identity_conflict_count"),
        duplicate_status=report.get("duplicate_status"),
        current_action=str(report.get("action", "blocked")),
        safe_to_write=safe_to_write,
        reason=reason,
    )


def _plan_is_valid(plan: CoverageConfirmationWritePlan) -> bool:
    if not isinstance(plan, CoverageConfirmationWritePlan):
        return False
    if plan.external_write:
        return False
    try:
        expected_row = _candidate_row(plan.record, plan.created_at)
    except (TypeError, ValueError):
        return False
    row_is_valid = (
        plan.candidate_row == expected_row
        and len(plan.candidate_row) == len(COVERAGE_CONFIRMATION_HEADERS)
        and all(isinstance(value, str) for value in plan.candidate_row)
    )
    if not row_is_valid:
        return False
    if plan.blocked:
        return plan.action_requested == "blocked"
    expected_states = {
        "create_and_append": ("sheet_missing", "not_found"),
        "append": ("exact_match", "not_found"),
        "skip_duplicate": ("exact_match", "exact_duplicate"),
    }
    return expected_states.get(plan.action_requested) == (
        plan.expected_sheet_status,
        plan.expected_duplicate_status,
    )


def _action_transition_is_safe(
    expected: str,
    current: str,
) -> bool:
    allowed = {
        "create_and_append": {"create_and_append", "append", "skip_duplicate"},
        "append": {"append", "skip_duplicate"},
        "skip_duplicate": {"skip_duplicate"},
    }
    return current in allowed.get(expected, set())


def revalidate_coverage_confirmation_write(
    db,
    plan: CoverageConfirmationWritePlan,
) -> CoverageConfirmationWriteStatus:
    """Re-read schema and rows, then compare them with the earlier plan."""

    try:
        spreadsheet_matches = (
            _spreadsheet_id(db) == plan.target_spreadsheet_id
        )
    except (AttributeError, TypeError, ValueError):
        spreadsheet_matches = False

    if not _plan_is_valid(plan):
        report = {"sheet_status": "invalid", "schema_status": "invalid"}
        return _status_from_report(
            report,
            spreadsheet_matches=spreadsheet_matches,
            safe_to_write=False,
            reason="invalid_write_plan",
        )
    if not spreadsheet_matches:
        report = {"sheet_status": "invalid", "schema_status": "invalid"}
        return _status_from_report(
            report,
            spreadsheet_matches=False,
            safe_to_write=False,
            reason="target_spreadsheet_changed",
        )
    if plan.blocked:
        report = {"sheet_status": "invalid", "schema_status": "invalid"}
        return _status_from_report(
            report,
            spreadsheet_matches=True,
            safe_to_write=False,
            reason="planned_action_was_blocked",
        )

    report = preview_coverage_confirmation_sheet(
        db, record=plan.record, created_at=plan.created_at,
    )
    if report.get("blocked"):
        return _status_from_report(
            report,
            spreadsheet_matches=True,
            safe_to_write=False,
            reason=str(report.get("reason", "current_state_blocked")),
        )
    current_action = str(report.get("action", "blocked"))
    if not _action_transition_is_safe(plan.action_requested, current_action):
        return _status_from_report(
            report,
            spreadsheet_matches=True,
            safe_to_write=False,
            reason="expected_action_no_longer_matches",
        )
    return _status_from_report(
        report,
        spreadsheet_matches=True,
        safe_to_write=True,
        reason="prewrite_revalidation_passed",
    )


def verify_coverage_confirmation_write(
    db,
    plan: CoverageConfirmationWritePlan,
) -> CoverageConfirmationWriteStatus:
    """Require the persisted candidate to read back as an exact duplicate."""

    status = revalidate_coverage_confirmation_write(db, plan)
    verified = (
        status.spreadsheet_matches
        and status.sheet_status == "exact_match"
        and status.schema_status == "exact_match"
        and status.duplicate_status == "exact_duplicate"
        and status.current_action == "skip_duplicate"
        and status.invalid_row_count == 0
        and status.identity_conflict_count == 0
    )
    return replace(
        status,
        safe_to_write=verified,
        reason=(
            "postwrite_verification_passed"
            if verified else "postwrite_verification_failed"
        ),
    )


def _result(
    plan: CoverageConfirmationWritePlan,
    *,
    actions: list[PerformedAction],
    created_sheet: bool,
    wrote_header: bool,
    appended_row: bool,
    skipped_duplicate: bool,
    blocked: bool,
    reason: str,
    prewrite_status: CoverageConfirmationWriteStatus | None,
    postwrite_status: CoverageConfirmationWriteStatus | None,
    external_write: bool,
) -> dict:
    return CoverageConfirmationApplyResult(
        action_requested=plan.action_requested,
        action_performed=tuple(actions),
        created_sheet=created_sheet,
        wrote_header=wrote_header,
        appended_row=appended_row,
        skipped_duplicate=skipped_duplicate,
        blocked=blocked,
        reason=reason,
        prewrite_status=prewrite_status,
        postwrite_status=postwrite_status,
        external_write=external_write,
    ).to_dict()


def apply_coverage_confirmation_write(
    db,
    plan: CoverageConfirmationWritePlan,
    *,
    apply: bool = False,
) -> dict:
    """Apply one plan only after an explicit guard and repeated revalidation."""

    actions: list[PerformedAction] = []
    created_sheet = False
    wrote_header = False
    appended_row = False
    external_write = False

    def finish(
        *,
        blocked: bool,
        reason: str,
        prewrite_status: CoverageConfirmationWriteStatus | None,
        postwrite_status: CoverageConfirmationWriteStatus | None = None,
        skipped_duplicate: bool = False,
    ) -> dict:
        return _result(
            plan,
            actions=actions,
            created_sheet=created_sheet,
            wrote_header=wrote_header,
            appended_row=appended_row,
            skipped_duplicate=skipped_duplicate,
            blocked=blocked,
            reason=reason,
            prewrite_status=prewrite_status,
            postwrite_status=postwrite_status,
            external_write=external_write,
        )

    if apply is not True:
        return finish(
            blocked=True,
            reason="explicit_apply_required",
            prewrite_status=None,
        )

    prewrite = revalidate_coverage_confirmation_write(db, plan)
    if not prewrite.safe_to_write:
        return finish(
            blocked=True,
            reason=prewrite.reason,
            prewrite_status=prewrite,
        )
    if prewrite.duplicate_status == "exact_duplicate":
        actions.append("skip_duplicate")
        postwrite = verify_coverage_confirmation_write(db, plan)
        return finish(
            blocked=not postwrite.safe_to_write,
            reason=postwrite.reason,
            prewrite_status=prewrite,
            postwrite_status=postwrite,
            skipped_duplicate=postwrite.safe_to_write,
        )

    current = prewrite
    if current.sheet_status == "sheet_missing":
        create_check = revalidate_coverage_confirmation_write(db, plan)
        if not create_check.safe_to_write:
            return finish(
                blocked=True,
                reason=create_check.reason,
                prewrite_status=prewrite,
                postwrite_status=create_check,
            )
        if create_check.duplicate_status == "exact_duplicate":
            actions.append("skip_duplicate")
            postwrite = verify_coverage_confirmation_write(db, plan)
            return finish(
                blocked=not postwrite.safe_to_write,
                reason=postwrite.reason,
                prewrite_status=prewrite,
                postwrite_status=postwrite,
                skipped_duplicate=postwrite.safe_to_write,
            )
        current = create_check
        if current.sheet_status == "sheet_missing":
            external_write = True
            try:
                db.create_sheet(COVERAGE_CONFIRMATION_SHEET)
            except Exception:
                recovery = revalidate_coverage_confirmation_write(db, plan)
                if not (
                    recovery.safe_to_write
                    and recovery.sheet_status == "exact_match"
                ):
                    return finish(
                        blocked=True,
                        reason="sheet_create_failed",
                        prewrite_status=prewrite,
                        postwrite_status=recovery,
                    )
                current = recovery
            else:
                created_sheet = True
                actions.append("create_sheet")
                after_create = revalidate_coverage_confirmation_write(db, plan)
                if after_create.sheet_status == "header_missing":
                    external_write = True
                    try:
                        db.write_header_raw(
                            COVERAGE_CONFIRMATION_SHEET,
                            list(COVERAGE_CONFIRMATION_HEADERS),
                        )
                    except Exception:
                        return finish(
                            blocked=True,
                            reason="header_write_failed",
                            prewrite_status=prewrite,
                            postwrite_status=after_create,
                        )
                    wrote_header = True
                    actions.append("write_header")
                    current = revalidate_coverage_confirmation_write(db, plan)
                elif (
                    after_create.safe_to_write
                    and after_create.sheet_status == "exact_match"
                ):
                    current = after_create
                else:
                    return finish(
                        blocked=True,
                        reason="new_sheet_header_state_changed",
                        prewrite_status=prewrite,
                        postwrite_status=after_create,
                    )

    append_check = revalidate_coverage_confirmation_write(db, plan)
    if not append_check.safe_to_write:
        return finish(
            blocked=True,
            reason=append_check.reason,
            prewrite_status=prewrite,
            postwrite_status=append_check,
        )
    if append_check.duplicate_status == "exact_duplicate":
        actions.append("skip_duplicate")
        postwrite = verify_coverage_confirmation_write(db, plan)
        return finish(
            blocked=not postwrite.safe_to_write,
            reason=postwrite.reason,
            prewrite_status=prewrite,
            postwrite_status=postwrite,
            skipped_duplicate=postwrite.safe_to_write,
        )
    if not (
        append_check.sheet_status == "exact_match"
        and append_check.schema_status == "exact_match"
        and append_check.duplicate_status == "not_found"
        and append_check.current_action == "append"
    ):
        return finish(
            blocked=True,
            reason="append_preconditions_failed",
            prewrite_status=prewrite,
            postwrite_status=append_check,
        )

    candidate = list(_candidate_row(plan.record, plan.created_at))
    if tuple(candidate) != plan.candidate_row:
        return finish(
            blocked=True,
            reason="candidate_row_changed",
            prewrite_status=prewrite,
            postwrite_status=append_check,
        )
    external_write = True
    try:
        db.append_raw(COVERAGE_CONFIRMATION_SHEET, [candidate])
    except Exception:
        postwrite = verify_coverage_confirmation_write(db, plan)
        return finish(
            blocked=True,
            reason="append_write_failed",
            prewrite_status=prewrite,
            postwrite_status=postwrite,
        )
    appended_row = True
    actions.append("append_row")

    postwrite = verify_coverage_confirmation_write(db, plan)
    return finish(
        blocked=not postwrite.safe_to_write,
        reason=postwrite.reason,
        prewrite_status=prewrite,
        postwrite_status=postwrite,
    )
