from __future__ import annotations

from collections import Counter
import hashlib
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .drive_payroll import DrivePayrollPreview, _suffix, temporary_payroll_file
from .payroll_models import PayrollReviewReasonCode
from .payroll_sheets import PayrollSheetsSnapshot, usable_aliases
from .payroll_storage import (
    PAYROLL_ITEM_COLUMNS,
    PAYROLL_STATEMENT_COLUMNS,
    PayrollStandardItemRecord,
    PayrollStorageCandidate,
    decide_duplicate,
    phase_a_to_storage_candidate,
    review_reason_counts,
    sync_statement_review_reasons,
)


PlanAction = Literal["append", "skip_duplicate", "needs_review", "blocked_schema"]
DuplicatePreviewStatus = Literal["new", "existing", "needs_review"]
WriteAction = Literal["append", "none"]
WriteEligibility = Literal["eligible", "ineligible"]
WriteStatus = Literal["ready", "skipped_duplicate", "blocked"]
WriteReason = Literal[
    "safe_new_statement",
    "schema_invalid",
    "exact_duplicate",
    "content_hash_duplicate",
    "source_identity_conflict",
    "revision_conflict",
    "employer_id_missing",
    "statement_type_missing",
    "statement_type_other",
    "statement_type_unsupported",
    "source_file_id_missing",
    "content_hash_missing",
    "pay_period_missing",
    "statement_partial",
    "statement_failed",
    "statement_needs_review",
    "item_needs_review",
    "no_planned_items",
]

_HEADER_SUMMARY_FIELDS = ("gross_pay", "total_deductions", "net_pay")


class PayrollItemSavePreview(BaseModel):
    section: str
    raw_item_name: str
    standard_item_name: str | None = None
    value: int | float | str | None = None
    needs_review: bool
    planned_row: dict[str, Any]


class PayrollPlannedRow(BaseModel):
    """An immutable, ordered row that a future writer can consume verbatim."""

    model_config = ConfigDict(frozen=True)

    columns: tuple[str, ...]
    values: tuple[Any, ...]

    @model_validator(mode="after")
    def matching_width(self):
        if len(self.columns) != len(self.values):
            raise ValueError("planned row columns and values must have equal length")
        return self

    def as_dict(self) -> dict[str, Any]:
        return dict(zip(self.columns, self.values))


class PayrollWriteIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement_id: str
    employer_id: str | None = None
    statement_type: str | None = None
    source_file_id: str | None = None
    content_hash: str | None = None
    pay_period: str | None = None


class PayrollDuplicateDiagnosis(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["new", "exact_duplicate", "duplicate", "conflict"]
    reason: str | None = None
    matched_statement_id: str | None = None


class PayrollWritePlan(BaseModel):
    """The single fail-closed authority for future payroll data writes."""

    model_config = ConfigDict(frozen=True)

    eligibility: WriteEligibility
    status: WriteStatus
    reason: WriteReason
    reasons: tuple[WriteReason, ...]
    header_action: WriteAction
    item_action: WriteAction
    identity: PayrollWriteIdentity
    duplicate: PayrollDuplicateDiagnosis
    planned_header_rows: tuple[PayrollPlannedRow, ...] = ()
    planned_item_rows: tuple[PayrollPlannedRow, ...] = ()

    @model_validator(mode="after")
    def enforce_action_row_invariants(self):
        if ((self.eligibility == "eligible") != (self.status == "ready")):
            raise ValueError("eligibility and status must agree")
        eligible = self.status == "ready"
        if eligible:
            if self.reason != "safe_new_statement":
                raise ValueError("ready plans must be safe new statements")
            if self.header_action != "append" or self.item_action != "append":
                raise ValueError("eligible plans must append header and item rows")
            if len(self.planned_header_rows) != 1 or not self.planned_item_rows:
                raise ValueError("eligible plans require one header and item rows")
        elif (self.header_action != "none" or self.item_action != "none"
              or self.planned_header_rows or self.planned_item_rows):
            raise ValueError("ineligible plans cannot contain write actions or rows")
        return self

    @property
    def would_create_header(self) -> bool:
        return self.header_action == "append"

    @property
    def would_create_items(self) -> int:
        return len(self.planned_item_rows) if self.item_action == "append" else 0


class PayrollSavePlan(BaseModel):
    file_name: str | None = None
    statement_date: str | None = None
    employer: str | None = None
    employee: str | None = None
    parse_method: str | None = None
    header_action: PlanAction
    item_count: int
    recognized_item_count: int
    recognized_without_value_count: int
    unknown_item_count: int
    needs_review_count: int
    duplicate_status: DuplicatePreviewStatus
    duplicate_reason: str | None = None
    would_create_header: bool
    would_create_items: int
    review_reason: list[str] = Field(default_factory=list)
    review_reason_counts: dict[PayrollReviewReasonCode, int] = Field(default_factory=dict)
    planned_header: dict[str, Any]
    items: list[PayrollItemSavePreview] = Field(default_factory=list)
    write_plan: PayrollWritePlan = Field(exclude=True)


class PayrollAppendPlan(BaseModel):
    action: PlanAction
    statement_id: str
    statement_type: str
    pay_period: str | None = None
    header_rows_to_append: int = 0
    item_rows_to_append: int = 0
    item_count: int = 0
    resolved_item_count: int = 0
    review_item_count: int = 0
    duplicate_reason: str | None = None
    review_reason: list[str] = Field(default_factory=list)


def enforce_active_standard_items(
    candidate: PayrollStorageCandidate,
    standard_items: Iterable[PayrollStandardItemRecord],
) -> PayrollStorageCandidate:
    """Return a copy where inactive or unknown standard IDs cannot be confirmed."""
    active_items = {
        item.standard_item_id: item for item in standard_items if item.active
    }
    result = candidate.model_copy(deep=True)
    for item in result.items:
        if item.standard_item_id is not None and item.standard_item_id not in active_items:
            item.standard_item_id = None
            item.value = None
            item.needs_review = True
            item.review_status = "pending"
            result.statement.needs_review = True
        elif item.standard_item_id is not None:
            item.section = active_items[item.standard_item_id].section
    sync_statement_review_reasons(result.statement, result.items)
    return result


def exclude_header_duplicate_summary_items(
    candidate: PayrollStorageCandidate,
) -> PayrollStorageCandidate:
    """Exclude confirmed summary items that exactly duplicate header values."""
    result = candidate.model_copy(deep=True)
    result.items = [
        item for item in result.items
        if not (
            item.standard_item_id in _HEADER_SUMMARY_FIELDS
            and item.value is not None
            and getattr(result.statement, item.standard_item_id) is not None
            and item.value == getattr(result.statement, item.standard_item_id)
        )
    ]
    sync_statement_review_reasons(result.statement, result.items)
    return result


def _prepare_candidate(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
) -> PayrollStorageCandidate:
    candidate = exclude_header_duplicate_summary_items(candidate)
    return enforce_active_standard_items(candidate, snapshot.standard_items)


def _storage_items(candidate: PayrollStorageCandidate):
    return tuple(
        item for item in candidate.items
        if item.raw_value is not None
        and (not isinstance(item.raw_value, str) or item.raw_value.strip() != "")
    )


def _planned_row(record, columns: tuple[str, ...]) -> PayrollPlannedRow:
    values = record.model_dump(mode="json")
    return PayrollPlannedRow(
        columns=columns,
        values=tuple(values.get(column) for column in columns),
    )


def _duplicate_diagnosis(duplicate) -> PayrollDuplicateDiagnosis:
    if duplicate.reason == "exact_duplicate":
        status = "exact_duplicate"
    elif duplicate.status == "duplicate":
        status = "duplicate"
    elif duplicate.status == "needs_review":
        status = "conflict"
    else:
        status = "new"
    return PayrollDuplicateDiagnosis(
        status=status,
        reason=None if duplicate.reason == "none" else duplicate.reason,
        matched_statement_id=duplicate.matched_statement_id,
    )


def _eligibility_reasons(candidate, snapshot, duplicate, items) -> list[WriteReason]:
    statement = candidate.statement
    if not snapshot.schema_ok:
        return ["schema_invalid"]
    if duplicate.status == "duplicate":
        return [
            "exact_duplicate" if duplicate.reason == "exact_duplicate"
            else "content_hash_duplicate"
        ]

    reasons: list[WriteReason] = []
    if duplicate.reason == "source_identity_conflict":
        reasons.append("source_identity_conflict")
    elif duplicate.reason == "statement_key":
        reasons.append("revision_conflict")
    if not statement.employer_id:
        reasons.append("employer_id_missing")
    if statement.statement_type is None:
        reasons.append("statement_type_missing")
    elif statement.statement_type == "other":
        reasons.append("statement_type_other")
    elif statement.statement_type not in {"salary", "bonus", "adjustment"}:
        reasons.append("statement_type_unsupported")
    if not statement.source_file_id:
        reasons.append("source_file_id_missing")
    if not statement.content_hash:
        reasons.append("content_hash_missing")
    if not statement.pay_period:
        reasons.append("pay_period_missing")
    if statement.parse_status == "partial":
        reasons.append("statement_partial")
    elif statement.parse_status == "failed":
        reasons.append("statement_failed")
    if statement.needs_review:
        reasons.append("statement_needs_review")
    if any(item.needs_review or item.review_status == "pending"
           for item in candidate.items):
        reasons.append("item_needs_review")
    if not items:
        reasons.append("no_planned_items")
    return list(dict.fromkeys(reasons))


def build_write_plan(
    candidates: Iterable[PayrollStorageCandidate],
    snapshot: PayrollSheetsSnapshot,
) -> list[PayrollWritePlan]:
    """Build the only authoritative eligibility decision and prospective rows."""
    plans = []
    for raw_candidate in candidates:
        candidate = _prepare_candidate(raw_candidate, snapshot)
        statement = candidate.statement
        items = _storage_items(candidate)
        duplicate = decide_duplicate(statement, snapshot.statements)
        reasons = _eligibility_reasons(candidate, snapshot, duplicate, items)
        eligible = not reasons
        if eligible:
            status: WriteStatus = "ready"
            reason: WriteReason = "safe_new_statement"
        elif duplicate.status == "duplicate" and snapshot.schema_ok:
            status = "skipped_duplicate"
            reason = reasons[0]
        else:
            status = "blocked"
            reason = reasons[0]
        plans.append(PayrollWritePlan(
            eligibility="eligible" if eligible else "ineligible",
            status=status,
            reason=reason,
            reasons=tuple(reasons) if reasons else ("safe_new_statement",),
            header_action="append" if eligible else "none",
            item_action="append" if eligible else "none",
            identity=PayrollWriteIdentity(
                statement_id=statement.statement_id,
                employer_id=statement.employer_id,
                statement_type=statement.statement_type,
                source_file_id=statement.source_file_id,
                content_hash=statement.content_hash,
                pay_period=statement.pay_period,
            ),
            duplicate=_duplicate_diagnosis(duplicate),
            planned_header_rows=(
                (_planned_row(statement, PAYROLL_STATEMENT_COLUMNS),)
                if eligible else ()
            ),
            planned_item_rows=(
                tuple(_planned_row(item, PAYROLL_ITEM_COLUMNS) for item in items)
                if eligible else ()
            ),
        ))
    return plans


def _legacy_review_reasons(plan: PayrollWritePlan) -> list[str]:
    aliases = {
        "revision_conflict": "possible_reissue_or_revision",
        "employer_id_missing": "employer_unresolved",
    }
    return [aliases.get(reason, reason) for reason in plan.reasons
            if reason not in {"safe_new_statement", "exact_duplicate",
                              "content_hash_duplicate"}]


def build_append_plan(
    candidates: Iterable[PayrollStorageCandidate],
    snapshot: PayrollSheetsSnapshot,
    *,
    employer_required: bool = True,
) -> list[PayrollAppendPlan]:
    """Backward-compatible summary projected from :func:`build_write_plan`."""
    del employer_required  # Identity is now unconditionally required fail-closed.
    candidates = list(candidates)
    write_plans = build_write_plan(candidates, snapshot)
    plans = []
    for raw_candidate, write_plan in zip(candidates, write_plans):
        candidate = _prepare_candidate(raw_candidate, snapshot)
        if write_plan.status == "ready":
            action: PlanAction = "append"
        elif write_plan.status == "skipped_duplicate":
            action = "skip_duplicate"
        elif write_plan.reason == "schema_invalid":
            action = "blocked_schema"
        else:
            action = "needs_review"
        plans.append(PayrollAppendPlan(
            action=action,
            statement_id=write_plan.identity.statement_id,
            statement_type=write_plan.identity.statement_type or "other",
            pay_period=write_plan.identity.pay_period,
            header_rows_to_append=len(write_plan.planned_header_rows),
            item_rows_to_append=len(write_plan.planned_item_rows),
            item_count=len(candidate.items),
            resolved_item_count=sum(item.standard_item_id is not None and
                                    not item.needs_review for item in candidate.items),
            review_item_count=sum(item.needs_review for item in candidate.items),
            duplicate_reason=write_plan.duplicate.reason,
            review_reason=_legacy_review_reasons(write_plan),
        ))
    return plans


def build_save_plan(
    candidates: Iterable[PayrollStorageCandidate],
    snapshot: PayrollSheetsSnapshot,
    *,
    employer_required: bool = True,
) -> list[PayrollSavePlan]:
    """Build complete prospective Sheets rows without performing any I/O."""
    standard_names = {
        item.standard_item_id: item.standard_name
        for item in snapshot.standard_items if item.active
    }
    employer_names = {
        employer.employer_id: employer.employer_label
        for employer in snapshot.employers if employer.active
    }
    del employer_required  # Identity is now unconditionally required fail-closed.
    candidates = list(candidates)
    write_plans = build_write_plan(candidates, snapshot)
    plans: list[PayrollSavePlan] = []
    for raw_candidate, write_plan in zip(candidates, write_plans):
        candidate = _prepare_candidate(raw_candidate, snapshot)
        eligible_items = list(_storage_items(candidate))
        duplicate_status: DuplicatePreviewStatus = {
            "new": "new", "exact_duplicate": "existing",
            "duplicate": "existing", "conflict": "needs_review",
        }[write_plan.duplicate.status]
        if write_plan.status == "ready":
            header_action: PlanAction = "append"
        elif write_plan.status == "skipped_duplicate":
            header_action = "skip_duplicate"
        elif write_plan.reason == "schema_invalid":
            header_action = "blocked_schema"
        else:
            header_action = "needs_review"
        planned_header = (
            write_plan.planned_header_rows[0].as_dict()
            if write_plan.planned_header_rows else {}
        )
        planned_item_rows = [row.as_dict() for row in write_plan.planned_item_rows]
        item_previews = [PayrollItemSavePreview(
            section=item.section,
            raw_item_name=item.raw_item_name,
            standard_item_name=standard_names.get(item.standard_item_id),
            # Preserve the human-readable source value when an uncertain value
            # is intentionally excluded from the prospective stored row.
            value=item.value if item.value is not None else item.raw_value,
            needs_review=item.needs_review,
            planned_row=(planned_item_rows[index]
                         if index < len(planned_item_rows) else {}),
        ) for index, item in enumerate(eligible_items)]
        statement = candidate.statement
        plans.append(PayrollSavePlan(
            file_name=candidate.file_name,
            statement_date=statement.pay_date or statement.pay_period,
            employer=employer_names.get(statement.employer_id),
            employee=candidate.employee,
            parse_method=candidate.parse_method,
            header_action=header_action,
            item_count=len(eligible_items),
            recognized_item_count=sum(
                item.standard_item_id is not None for item in eligible_items
            ),
            recognized_without_value_count=sum(
                item.standard_item_id is not None
                and (item.raw_value is None
                     or (isinstance(item.raw_value, str)
                         and item.raw_value.strip() == ""))
                for item in candidate.items
            ),
            unknown_item_count=sum(
                item.standard_item_id is None for item in eligible_items
            ),
            needs_review_count=sum(item.needs_review for item in eligible_items),
            duplicate_status=duplicate_status,
            duplicate_reason=write_plan.duplicate.reason,
            would_create_header=write_plan.would_create_header,
            would_create_items=write_plan.would_create_items,
            review_reason=_legacy_review_reasons(write_plan),
            review_reason_counts=review_reason_counts(candidate.items),
            planned_header=planned_header,
            items=item_previews,
            write_plan=write_plan,
        ))
    return plans


def save_preview_summary(
    plans: Iterable[PayrollSavePlan],
    snapshot: PayrollSheetsSnapshot,
    *,
    sampled_files: int,
    failed_files: int,
) -> dict[str, Any]:
    plans = list(plans)
    reason_counts = Counter()
    for plan in plans:
        reason_counts.update(plan.review_reason_counts)
    return {
        "read_only": True,
        "schema_ok": snapshot.schema_ok,
        "sampled_files": sampled_files,
        "parsed_files": len(plans),
        "failed_files": failed_files,
        "would_create_headers": sum(plan.would_create_header for plan in plans),
        "would_create_items": sum(plan.would_create_items for plan in plans),
        "duplicate_count": sum(
            plan.duplicate_status != "new" for plan in plans
        ),
        "unknown_item_count": sum(plan.unknown_item_count for plan in plans),
        "needs_review_count": sum(plan.needs_review_count for plan in plans),
        "recognized_item_count": sum(
            plan.recognized_item_count for plan in plans
        ),
        "recognized_without_value_count": sum(
            plan.recognized_without_value_count for plan in plans
        ),
        "review_reason_counts": dict(reason_counts),
        "statements": [plan.model_dump(mode="json") for plan in plans],
        "write_plans": [plan.write_plan.model_dump(mode="json") for plan in plans],
    }


def preview_summary(
    plans: Iterable[PayrollAppendPlan],
    snapshot: PayrollSheetsSnapshot,
) -> dict:
    plans = list(plans)
    return {
        "schema_ok": snapshot.schema_ok,
        "statements_found": len(plans),
        "append_count": sum(plan.action == "append" for plan in plans),
        "duplicate_count": sum(plan.action == "skip_duplicate" for plan in plans),
        "needs_review_count": sum(plan.action == "needs_review" for plan in plans),
        "blocked_count": sum(plan.action == "blocked_schema" for plan in plans),
        "schemas": [result.model_dump() for result in snapshot.schemas],
        "statements": [
            {
                "action": plan.action,
                "statement_type": plan.statement_type,
                "pay_period": plan.pay_period,
                "item_count": plan.item_count,
                "resolved_item_count": plan.resolved_item_count,
                "review_item_count": plan.review_item_count,
                "duplicate_reason": plan.duplicate_reason,
                "review_reason": plan.review_reason,
            }
            for plan in plans
        ],
    }


def drive_storage_candidates(
    folder_id: str,
    snapshot: PayrollSheetsSnapshot,
    *,
    service=None,
    downloader=None,
    parser=None,
    employer_id: str | None = None,
    statement_type: str | None = None,
) -> list[PayrollStorageCandidate]:
    """Read Phase A files and make B1 candidates without Drive/Sheets writes."""
    adapter = DrivePayrollPreview(
        folder_id, service=service, downloader=downloader, parser=parser,
    )
    aliases = usable_aliases(snapshot.standard_items, snapshot.aliases)
    candidates = []
    for file in adapter._files():
        suffix = _suffix(file)
        if suffix is None:
            continue
        try:
            data = adapter.downloader(file["id"])
            with temporary_payroll_file(data, suffix) as path:
                result = adapter.parser(path)
            candidates.append(phase_a_to_storage_candidate(
                result,
                employer_id=employer_id,
                statement_type=statement_type,
                source_type="drive",
                source_file_id=file["id"],
                content_hash=hashlib.sha256(data).hexdigest(),
                aliases=aliases,
                standard_items=snapshot.standard_items,
                file_name=file.get("name"),
            ))
        except Exception:
            # Phase A already owns parser diagnostics. Storage planning must allow
            # the remaining candidates to proceed and never invent a row.
            continue
    return candidates


def drive_save_preview(
    folder_id: str,
    snapshot: PayrollSheetsSnapshot,
    *,
    service=None,
    downloader=None,
    parser=None,
    employer_id: str | None = None,
    statement_type: str | None = None,
) -> dict[str, Any]:
    """Parse Drive statements and return a complete, strictly read-only plan."""
    adapter = DrivePayrollPreview(
        folder_id, service=service, downloader=downloader, parser=parser,
    )
    aliases = usable_aliases(snapshot.standard_items, snapshot.aliases)
    files = adapter._files()
    candidates = []
    failed = 0
    for file in files:
        suffix = _suffix(file)
        if suffix is None:
            failed += 1
            continue
        try:
            data = adapter.downloader(file["id"])
            with temporary_payroll_file(data, suffix) as path:
                result = adapter.parser(path)
            candidates.append(phase_a_to_storage_candidate(
                result,
                employer_id=employer_id,
                statement_type=statement_type,
                source_type="drive",
                source_file_id=file["id"],
                content_hash=hashlib.sha256(data).hexdigest(),
                aliases=aliases,
                standard_items=snapshot.standard_items,
                file_name=file.get("name"),
            ))
        except Exception:
            failed += 1
    plans = build_save_plan(candidates, snapshot)
    return save_preview_summary(
        plans, snapshot, sampled_files=len(files), failed_files=failed,
    )
