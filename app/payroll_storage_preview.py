from __future__ import annotations

import hashlib
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from .drive_payroll import DrivePayrollPreview, _suffix, temporary_payroll_file
from .payroll_sheets import PayrollSheetsSnapshot, usable_aliases
from .payroll_storage import (
    PayrollStandardItemRecord,
    PayrollStorageCandidate,
    decide_duplicate,
    phase_a_to_storage_candidate,
)


PlanAction = Literal["append", "skip_duplicate", "needs_review", "blocked_schema"]
DuplicatePreviewStatus = Literal["new", "existing", "needs_review"]


class PayrollItemSavePreview(BaseModel):
    section: str
    raw_item_name: str
    standard_item_name: str | None = None
    value: int | float | str | None = None
    needs_review: bool
    planned_row: dict[str, Any]


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
    planned_header: dict[str, Any]
    items: list[PayrollItemSavePreview] = Field(default_factory=list)


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
    return result


def build_append_plan(
    candidates: Iterable[PayrollStorageCandidate],
    snapshot: PayrollSheetsSnapshot,
    *,
    employer_required: bool = True,
) -> list[PayrollAppendPlan]:
    schema_ok = snapshot.schema_ok
    plans = []
    for raw_candidate in candidates:
        candidate = enforce_active_standard_items(raw_candidate, snapshot.standard_items)
        statement = candidate.statement
        duplicate = decide_duplicate(statement, snapshot.statements)
        reasons = []
        if not schema_ok:
            action: PlanAction = "blocked_schema"
            reasons.append("schema_invalid")
        elif duplicate.status == "duplicate":
            action = "skip_duplicate"
        else:
            if duplicate.status == "needs_review":
                reasons.append("possible_reissue_or_revision")
            if statement.needs_review:
                reasons.append("statement_needs_review")
            if employer_required and not statement.employer_id:
                reasons.append("employer_unresolved")
            if statement.statement_type == "other":
                reasons.append("statement_type_other")
            action = "needs_review" if reasons else "append"
        appendable = action == "append"
        plans.append(PayrollAppendPlan(
            action=action,
            statement_id=statement.statement_id,
            statement_type=statement.statement_type,
            pay_period=statement.pay_period,
            header_rows_to_append=1 if appendable else 0,
            item_rows_to_append=len(candidate.items) if appendable else 0,
            item_count=len(candidate.items),
            resolved_item_count=sum(item.standard_item_id is not None and
                                    not item.needs_review for item in candidate.items),
            review_item_count=sum(item.needs_review for item in candidate.items),
            duplicate_reason=(duplicate.reason if duplicate.reason != "none" else None),
            review_reason=reasons,
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
    plans: list[PayrollSavePlan] = []
    for raw_candidate in candidates:
        candidate = enforce_active_standard_items(raw_candidate, snapshot.standard_items)
        legacy = build_append_plan(
            [candidate], snapshot, employer_required=employer_required,
        )[0]
        duplicate = decide_duplicate(candidate.statement, snapshot.statements)
        duplicate_status: DuplicatePreviewStatus = {
            "new": "new", "duplicate": "existing", "needs_review": "needs_review",
        }[duplicate.status]
        can_create = snapshot.schema_ok and duplicate.status == "new"
        eligible_items = [
            item for item in candidate.items
            if item.raw_value is not None
            and (not isinstance(item.raw_value, str) or item.raw_value.strip() != "")
        ]
        item_previews = [PayrollItemSavePreview(
            section=item.section,
            raw_item_name=item.raw_item_name,
            standard_item_name=standard_names.get(item.standard_item_id),
            # Preserve the human-readable source value when an uncertain value
            # is intentionally excluded from the prospective stored row.
            value=item.value if item.value is not None else item.raw_value,
            needs_review=item.needs_review,
            planned_row=item.model_dump(mode="json"),
        ) for item in eligible_items]
        statement = candidate.statement
        plans.append(PayrollSavePlan(
            file_name=candidate.file_name,
            statement_date=statement.pay_date or statement.pay_period,
            employer=employer_names.get(statement.employer_id),
            employee=candidate.employee,
            parse_method=candidate.parse_method,
            header_action=legacy.action,
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
            duplicate_reason=(duplicate.reason if duplicate.reason != "none" else None),
            would_create_header=can_create,
            would_create_items=len(eligible_items) if can_create else 0,
            review_reason=legacy.review_reason,
            planned_header=statement.model_dump(mode="json"),
            items=item_previews,
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
        "statements": [plan.model_dump(mode="json") for plan in plans],
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
