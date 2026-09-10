"""Default-disabled bridge from persisted review evidence to Payroll planning.

This module sits after parser/storage/business-scope resolution and before
``PayrollWritePlan`` creation. It can only replay an existing signed review
journal onto a candidate copy. It exposes no writer, materialization apply, or
ownership-production capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

from .payroll_review_persistence import (
    load_local_payroll_review_hmac_key,
    load_payroll_review_decision_journal,
    replay_payroll_review_decision_journal,
)
from .payroll_sheets import PayrollSheetsSnapshot
from .payroll_storage import PayrollStorageCandidate


ReloadStatus = Literal["disabled", "not_applicable", "applied", "rejected"]


@dataclass(frozen=True)
class PayrollReviewReloadRequest:
    """Explicit caller opt-in scoped to one exact source content hash."""

    expected_content_hash: str = field(repr=False)
    journal_path: str | Path = field(repr=False)
    hmac_key_path: str | Path = field(repr=False)
    repository_root: str | Path = field(repr=False)


@dataclass(frozen=True)
class PayrollReviewReloadEvidence:
    enabled: bool
    status: ReloadStatus
    reason_code: str
    key_load_count: int = 0
    journal_read_count: int = 0
    persisted_decision_count: int = 0
    applied_decision_count: int = 0
    rejected_decision_count: int = 0


@dataclass(frozen=True)
class PayrollReviewReloadResult:
    candidate: PayrollStorageCandidate = field(repr=False)
    evidence: PayrollReviewReloadEvidence


DISABLED_PAYROLL_REVIEW_RELOAD_EVIDENCE = PayrollReviewReloadEvidence(
    enabled=False,
    status="disabled",
    reason_code="review_journal_reload_disabled",
)


def reload_payroll_review_decisions(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    request: PayrollReviewReloadRequest | None = None,
    *,
    enabled: bool = False,
    key_loader=load_local_payroll_review_hmac_key,
    journal_loader=load_payroll_review_decision_journal,
) -> PayrollReviewReloadResult:
    """Replay an exact signed journal or return the untouched input fail-closed.

    Disabled mode deliberately does not inspect ``request`` and invokes no
    loader. Enabled failures never clear review state and never build a plan.
    """

    if enabled is not True:
        return PayrollReviewReloadResult(
            candidate, DISABLED_PAYROLL_REVIEW_RELOAD_EVIDENCE,
        )
    if not isinstance(request, PayrollReviewReloadRequest):
        return PayrollReviewReloadResult(candidate, PayrollReviewReloadEvidence(
            True, "rejected", "review_journal_reload_request_required",
        ))
    if not re.fullmatch(r"[0-9a-f]{64}", request.expected_content_hash):
        return PayrollReviewReloadResult(candidate, PayrollReviewReloadEvidence(
            True, "rejected", "review_journal_source_hash_invalid",
        ))
    if candidate.statement.content_hash != request.expected_content_hash:
        return PayrollReviewReloadResult(candidate, PayrollReviewReloadEvidence(
            True, "not_applicable", "review_journal_source_not_selected",
        ))
    try:
        local_key = key_loader(
            request.hmac_key_path,
            repository_root=request.repository_root,
        )
    except Exception:
        return PayrollReviewReloadResult(candidate, PayrollReviewReloadEvidence(
            True, "rejected", "review_journal_key_load_failed",
            key_load_count=1,
        ))
    try:
        journal = journal_loader(
            request.journal_path,
            local_key=local_key,
            repository_root=request.repository_root,
        )
    except Exception:
        return PayrollReviewReloadResult(candidate, PayrollReviewReloadEvidence(
            True, "rejected", "review_journal_load_failed",
            key_load_count=1, journal_read_count=1,
        ))
    persisted_count = len(journal.records)
    try:
        replay = replay_payroll_review_decision_journal(
            candidate, snapshot, journal, local_key=local_key,
        )
    except Exception:
        return PayrollReviewReloadResult(candidate, PayrollReviewReloadEvidence(
            True, "rejected", "review_journal_replay_failed",
            key_load_count=1, journal_read_count=1,
            persisted_decision_count=persisted_count,
            rejected_decision_count=persisted_count,
        ))
    if not replay.accepted:
        return PayrollReviewReloadResult(candidate, PayrollReviewReloadEvidence(
            True, "rejected", replay.reason_code,
            key_load_count=1, journal_read_count=1,
            persisted_decision_count=persisted_count,
            rejected_decision_count=persisted_count,
        ))
    return PayrollReviewReloadResult(
        replay.candidate,
        PayrollReviewReloadEvidence(
            True, "applied", replay.reason_code,
            key_load_count=1, journal_read_count=1,
            persisted_decision_count=persisted_count,
            applied_decision_count=replay.applied_count,
        ),
    )


def summarize_payroll_review_reload(evidence_values):
    """Return privacy-safe, read-only aggregate metadata for an opted-in run."""

    evidence_values = tuple(evidence_values)
    status_counts = {}
    reason_counts = {}
    for evidence in evidence_values:
        status_counts[evidence.status] = status_counts.get(evidence.status, 0) + 1
        reason_counts[evidence.reason_code] = reason_counts.get(evidence.reason_code, 0) + 1
    return {
        "enabled": True,
        "status_counts": dict(sorted(status_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "key_load_count": sum(value.key_load_count for value in evidence_values),
        "journal_read_count": sum(value.journal_read_count for value in evidence_values),
        "persisted_decision_count": sum(
            value.persisted_decision_count for value in evidence_values
        ),
        "applied_decision_count": sum(
            value.applied_decision_count for value in evidence_values
        ),
        "rejected_decision_count": sum(
            value.rejected_decision_count for value in evidence_values
        ),
        "writer_invocation_count": 0,
        "apply_invocation_count": 0,
    }
