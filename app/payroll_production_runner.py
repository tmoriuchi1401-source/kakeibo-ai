"""Bounded, read-only Payroll production classification.

This orchestration reuses the existing parser output, business authority,
persisted review replay, duplicate decision, reconciliation authority, and
``PayrollWritePlan``.  It deliberately exposes no writer: a ready statement
must still cross the existing single-statement production canary boundary.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict

from .payroll_review_integration import (
    PayrollReviewReloadEvidence,
    PayrollReviewReloadRequest,
    reload_payroll_review_decisions,
)
from .payroll_sheets import PayrollSheetsSnapshot
from .payroll_source_reconciliation import (
    PayrollSourceReconciliationDecision,
    apply_payroll_source_reconciliation,
)
from .payroll_storage import PayrollStorageCandidate
from .payroll_storage_preview import PayrollWritePlan, build_write_plan
from .payroll_writer import preview_payroll_write


PayrollProductionOutcome = Literal[
    "WRITE_READY",
    "EXACT_DUPLICATE",
    "ALTERNATE_SOURCE_DUPLICATE",
    "NEEDS_REVIEW",
    "CONFLICT",
]


class PayrollProductionStatementResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement_id: str
    employer_id: str | None = None
    statement_type: str | None = None
    pay_period: str | None = None
    content_hash: str | None = None
    outcome: PayrollProductionOutcome
    reason: str
    target_statement_id: str | None = None
    planned_header_rows: int = 0
    planned_item_rows: int = 0
    planned_update_rows: int = 0
    review_item_count: int = 0
    writer_candidate: bool = False
    writer_invocations: int = 0
    actual_header_rows: int = 0
    actual_item_rows: int = 0


class PayrollProductionRunReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    read_only: bool = True
    statement_count: int
    outcome_counts: dict[str, int]
    writer_candidate_count: int
    writer_invocation_count: int = 0
    actual_header_rows: int = 0
    actual_item_rows: int = 0
    actual_update_rows: int = 0
    reconciliation_source: Literal[
        "disabled", "explicit_input", "external_journal", "rejected"
    ] = "disabled"
    reconciliation_reload_reason: str = "reconciliation_persistence_disabled"
    reconciliation_record_count: int = 0
    results: tuple[PayrollProductionStatementResult, ...]


def _review_failure(
    evidence: PayrollReviewReloadEvidence,
    request: PayrollReviewReloadRequest | None,
    candidate: PayrollStorageCandidate,
) -> bool:
    return bool(
        request
        and candidate.statement.content_hash == request.expected_content_hash
        and evidence.status == "rejected"
    )


def _classify(plan: PayrollWritePlan) -> tuple[PayrollProductionOutcome, str]:
    if plan.status == "ready":
        return "WRITE_READY", plan.reason
    if plan.reason == "operator_reconciled_alternate_source":
        return "ALTERNATE_SOURCE_DUPLICATE", plan.reason
    if plan.status == "skipped_duplicate":
        return "EXACT_DUPLICATE", plan.reason
    if plan.duplicate.status == "conflict" or plan.reason in {
        "source_identity_conflict", "revision_conflict",
    }:
        return "CONFLICT", plan.reason
    return "NEEDS_REVIEW", plan.reason


def run_payroll_production_preview(
    candidates: Iterable[PayrollStorageCandidate],
    snapshot: PayrollSheetsSnapshot,
    *,
    review_reload_request: PayrollReviewReloadRequest | None = None,
    review_journal_enabled: bool = False,
    reconciliation_decisions: Iterable[
        PayrollSourceReconciliationDecision
    ] = (),
    reconciliation_key: bytes | None = None,
    reconciliation_journal_path=None,
    reconciliation_key_path=None,
    repository_root=None,
) -> PayrollProductionRunReport:
    """Classify every candidate independently without invoking a writer."""

    candidates = tuple(candidates)
    decisions = tuple(reconciliation_decisions)
    reconciliation_source = "explicit_input" if decisions else "disabled"
    reconciliation_reload_reason = (
        "explicit_reconciliation_input"
        if decisions else "reconciliation_persistence_disabled"
    )
    if reconciliation_journal_path is not None or reconciliation_key_path is not None:
        if reconciliation_journal_path is None or reconciliation_key_path is None or repository_root is None:
            decisions = ()
            reconciliation_key = None
            reconciliation_source = "rejected"
            reconciliation_reload_reason = "reconciliation_persistence_configuration_incomplete"
        else:
            try:
                from .payroll_reconciliation_persistence import (
                    load_reconciliation_journal,
                    load_reconciliation_key,
                )
                reconciliation_key = load_reconciliation_key(
                    reconciliation_key_path, repository_root=repository_root,
                )
                journal = load_reconciliation_journal(
                    reconciliation_journal_path,
                    local_key=reconciliation_key,
                    repository_root=repository_root,
                )
                decisions = journal.records
                reconciliation_source = "external_journal"
                reconciliation_reload_reason = "reconciliation_journal_loaded"
            except ValueError as exc:
                decisions = ()
                reconciliation_key = None
                reconciliation_source = "rejected"
                reconciliation_reload_reason = str(exc)
            except (OSError, UnicodeError, KeyError, TypeError):
                decisions = ()
                reconciliation_key = None
                reconciliation_source = "rejected"
                reconciliation_reload_reason = "reconciliation_journal_unavailable"
    decision_counts = Counter(
        decision.alternate_content_hash for decision in decisions
    )
    decisions_by_hash = {
        decision.alternate_content_hash: decision for decision in decisions
        if decision_counts[decision.alternate_content_hash] == 1
    }
    results = []
    plans = []

    for candidate in candidates:
        reload_result = reload_payroll_review_decisions(
            candidate,
            snapshot,
            review_reload_request,
            enabled=review_journal_enabled,
        )
        plan = build_write_plan([reload_result.candidate], snapshot)[0]
        outcome: PayrollProductionOutcome
        reason: str

        if _review_failure(
            reload_result.evidence, review_reload_request, candidate,
        ):
            outcome, reason = "NEEDS_REVIEW", reload_result.evidence.reason_code
        elif plan.status == "skipped_duplicate":
            # Exact/byte-identical duplicates need no reconciliation override.
            outcome, reason = _classify(plan)
        else:
            content_hash = candidate.statement.content_hash
            decision = decisions_by_hash.get(content_hash)
            if content_hash and decision_counts[content_hash] > 1:
                outcome, reason = "CONFLICT", "reconciliation_decision_not_unique"
            elif decision is not None:
                if reconciliation_key is None:
                    outcome, reason = "CONFLICT", "reconciliation_key_required"
                else:
                    reconciled = apply_payroll_source_reconciliation(
                        reload_result.candidate,
                        snapshot,
                        decision,
                        local_key=reconciliation_key,
                        confirmed=True,
                    )
                    plan = reconciled.plan
                    if reconciled.applied:
                        outcome, reason = _classify(plan)
                    else:
                        outcome, reason = "CONFLICT", reconciled.preview.reason_code
            else:
                outcome, reason = _classify(plan)

        writer_candidate = outcome == "WRITE_READY"
        results.append(PayrollProductionStatementResult(
            statement_id=plan.identity.statement_id,
            employer_id=plan.identity.employer_id,
            statement_type=plan.identity.statement_type,
            pay_period=plan.identity.pay_period,
            content_hash=plan.identity.content_hash,
            outcome=outcome,
            reason=reason,
            target_statement_id=plan.duplicate.matched_statement_id,
            planned_header_rows=(len(plan.planned_header_rows)
                                 if writer_candidate else 0),
            planned_item_rows=(len(plan.planned_item_rows)
                               if writer_candidate else 0),
            review_item_count=sum(
                item.needs_review or item.review_status == "pending"
                for item in reload_result.candidate.items
            ),
            writer_candidate=writer_candidate,
        ))
        plans.append(plan)

    preview_payroll_write(plans)
    counts = Counter(result.outcome for result in results)
    return PayrollProductionRunReport(
        statement_count=len(results),
        outcome_counts=dict(sorted(counts.items())),
        writer_candidate_count=sum(result.writer_candidate for result in results),
        reconciliation_source=reconciliation_source,
        reconciliation_reload_reason=reconciliation_reload_reason,
        reconciliation_record_count=len(decisions),
        results=tuple(results),
    )
