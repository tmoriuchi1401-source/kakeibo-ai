from __future__ import annotations

import hashlib
from typing import Iterable, Literal

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
    active_ids = {item.standard_item_id for item in standard_items if item.active}
    result = candidate.model_copy(deep=True)
    for item in result.items:
        if item.standard_item_id is not None and item.standard_item_id not in active_ids:
            item.standard_item_id = None
            item.value = None
            item.needs_review = True
            item.review_status = "pending"
            result.statement.needs_review = True
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
            ))
        except Exception:
            # Phase A already owns parser diagnostics. Storage planning must allow
            # the remaining candidates to proceed and never invent a row.
            continue
    return candidates
