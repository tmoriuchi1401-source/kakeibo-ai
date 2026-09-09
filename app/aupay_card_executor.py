"""Write-free executor contract for canonical au PAY card apply plans.

There is deliberately no production writer in this module.  ``apply=True``
expresses caller intent but remains blocked until a separate future phase
connects a writer behind post-write verification.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import json
from typing import Mapping, Protocol, Sequence

from .aupay_card_apply_plan import (
    CanonicalApplyCandidate,
    CanonicalApplyPlan,
    validate_canonical_apply_plan,
)
from .sheets import HEADERS


EXECUTION_RESULT_SCHEMA_VERSION = 1


class CandidateState(str, Enum):
    NOT_STARTED = "not_started"
    VERIFIED_NEW = "verified_new"
    WRITE_ATTEMPTED = "write_attempted"
    WRITE_CONFIRMED = "write_confirmed"
    ALREADY_PRESENT = "already_present"
    CONFLICT = "conflict"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RETRY_ELIGIBLE = "retry_eligible"
    FAILED = "failed"


@dataclass(frozen=True)
class ExistingCanonicalRecord:
    identity: str
    source: str
    source_record_id: str
    transaction_date: str
    merchant: str
    amount_yen: int
    payment_method: str
    business_fingerprint: str
    memo: str

    @classmethod
    def from_candidate(cls, candidate: CanonicalApplyCandidate) -> "ExistingCanonicalRecord":
        return cls(
            identity=candidate.identity,
            source=candidate.source,
            source_record_id=candidate.source_record_id,
            transaction_date=candidate.transaction_date,
            merchant=candidate.merchant,
            amount_yen=candidate.amount_yen,
            payment_method=candidate.payment_method,
            business_fingerprint=candidate.business_fingerprint,
            memo=candidate.memo,
        )


@dataclass(frozen=True)
class IdentityRead:
    """Raw read-back outcome. Multiple rows are handled as a conflict."""
    records: tuple[ExistingCanonicalRecord, ...] = ()
    readable: bool = True
    reason_code: str = ""


class CanonicalIdentityReader(Protocol):
    def read_identities(self, identities: tuple[str, ...]) -> Mapping[str, IdentityRead]: ...


@dataclass(frozen=True)
class ExecutionSubset:
    """Deterministic selection boundary for a future canary/batch."""
    limit: int | None = None

    def validate(self) -> None:
        if self.limit is not None and self.limit <= 0:
            raise ValueError("execution_subset_limit_invalid")


@dataclass(frozen=True)
class PriorCandidateOutcome:
    identity: str
    state: CandidateState


@dataclass(frozen=True)
class CandidateExecutionResult:
    audit_item_ref: str
    state: CandidateState
    reason_code: str


@dataclass(frozen=True)
class CanonicalExecutionResult:
    schema_version: int
    execution_status: str
    apply_requested: bool
    writer_connected: bool
    plan_audit_ref: str
    audit_ref_scope: str
    input_candidate_count: int
    selected_candidate_count: int
    revalidated_new_count: int
    already_present_count: int
    conflict_count: int
    withheld_count: int
    would_write_count: int
    failed_review_count: int
    reason_codes: tuple[str, ...]
    candidate_results: tuple[CandidateExecutionResult, ...]

    def summary(self) -> dict:
        return {
            "executor_result_schema_version": self.schema_version,
            "execution_status": self.execution_status,
            "apply_requested": self.apply_requested,
            "writer_connected": self.writer_connected,
            "plan_audit_ref": self.plan_audit_ref,
            "audit_ref_scope": self.audit_ref_scope,
            "input_candidate_count": self.input_candidate_count,
            "selected_candidate_count": self.selected_candidate_count,
            "revalidated_new_count": self.revalidated_new_count,
            "already_present_count": self.already_present_count,
            "conflict_count": self.conflict_count,
            "withheld_count": self.withheld_count,
            "would_write_count": self.would_write_count,
            "failed_review_count": self.failed_review_count,
            "executor_reason_codes": list(self.reason_codes),
            "external_write_count": 0,
        }


def _require_audit_key(audit_key: bytes) -> bytes:
    if not isinstance(audit_key, bytes) or len(audit_key) < 32:
        raise ValueError("audit_key_must_be_at_least_32_bytes")
    return audit_key


def _hmac_ref(audit_key: bytes, namespace: str, value: str) -> str:
    digest = hmac.new(
        audit_key, f"{namespace}\x00{value}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:32]
    return f"{namespace}-v1:{digest}"


def _plan_ref(plan: CanonicalApplyPlan, audit_key: bytes) -> str:
    # The HMAC input never leaves the process. Merchant, memo, source records,
    # email content, and credentials are intentionally excluded.
    manifest = {
        "schema_version": plan.schema_version,
        "status": plan.status,
        "canonical_count": plan.canonical_transaction_count,
        "candidate_identities": sorted(candidate.identity for candidate in plan.candidates),
        "withheld": sorted(
            (decision.status, decision.canonical_identity)
            for decision in plan.item_decisions if decision.status != "eligible"
        ),
    }
    serialized = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return _hmac_ref(audit_key, "canonical-plan", serialized)


def _candidate_sort_key(candidate: CanonicalApplyCandidate) -> tuple:
    return (
        candidate.transaction_date,
        candidate.amount_yen,
        candidate.business_fingerprint,
        candidate.identity,
    )


def select_canonical_candidates(
    candidates: Sequence[CanonicalApplyCandidate],
    subset: ExecutionSubset,
    audit_key: bytes,
) -> tuple[CanonicalApplyCandidate, ...]:
    """Select a stable keyed subset and restore canonical execution order."""
    key = _require_audit_key(audit_key)
    subset.validate()
    ordered = tuple(sorted(candidates, key=_candidate_sort_key))
    if subset.limit is None or subset.limit >= len(ordered):
        return ordered
    ranked = sorted(
        ordered,
        key=lambda candidate: (
            hmac.new(key, candidate.identity.encode("utf-8"), hashlib.sha256).digest(),
            _candidate_sort_key(candidate),
        ),
    )[:subset.limit]
    return tuple(sorted(ranked, key=_candidate_sort_key))


def _is_exact(candidate: CanonicalApplyCandidate, record: ExistingCanonicalRecord) -> bool:
    return record == ExistingCanonicalRecord.from_candidate(candidate)


def verify_post_write(
    value: object,
    candidate_identity: str,
    observation: IdentityRead,
    *,
    audit_key: bytes,
) -> CandidateExecutionResult:
    """Pure post-write/read-back classifier for the future writer phase.

    It grants ``write_confirmed`` only to one exact stored projection. An
    unreadable result remains ``outcome_unknown`` and must never be retried
    without another read-back.
    """
    plan = validate_canonical_apply_plan(value)
    matching = tuple(
        candidate for candidate in plan.candidates
        if candidate.identity == candidate_identity
    )
    if len(matching) != 1:
        raise ValueError("post_write_candidate_not_in_plan")
    candidate = matching[0]
    key = _require_audit_key(audit_key)
    item_ref = _hmac_ref(key, "canonical-item", candidate.identity)
    if not observation.readable:
        return CandidateExecutionResult(
            item_ref, CandidateState.OUTCOME_UNKNOWN,
            "post_write_readback_unavailable",
        )
    if not observation.records:
        return CandidateExecutionResult(
            item_ref, CandidateState.FAILED, "post_write_identity_absent",
        )
    if len(observation.records) == 1 and _is_exact(candidate, observation.records[0]):
        return CandidateExecutionResult(
            item_ref, CandidateState.WRITE_CONFIRMED, "post_write_exact_confirmed",
        )
    return CandidateExecutionResult(
        item_ref, CandidateState.CONFLICT, "post_write_identity_conflict",
    )


def execute_canonical_apply_plan(
    value: object,
    reader: CanonicalIdentityReader,
    *,
    apply: bool = False,
    audit_key: bytes,
    audit_ref_scope: str = "caller_managed_key",
    subset: ExecutionSubset | None = None,
    prior_outcomes: Sequence[PriorCandidateOutcome] = (),
) -> CanonicalExecutionResult:
    """Revalidate a safe plan and return a write-free execution result.

    Every invocation rereads identity state. An ``outcome_unknown`` prior
    outcome therefore cannot become retryable without a conclusive read-back.
    A write-capable object may implement the reader protocol, but no method
    except ``read_identities`` is reachable here.
    """
    plan = validate_canonical_apply_plan(value)
    key = _require_audit_key(audit_key)
    selection = subset or ExecutionSubset()
    selection.validate()
    selected = select_canonical_candidates(plan.candidates, selection, key)

    candidate_identities = {candidate.identity for candidate in plan.candidates}
    prior_by_identity: dict[str, CandidateState] = {}
    for prior in prior_outcomes:
        if prior.identity not in candidate_identities:
            raise ValueError("prior_outcome_identity_not_in_plan")
        if prior.identity in prior_by_identity:
            raise ValueError("duplicate_prior_outcome_identity")
        prior_by_identity[prior.identity] = prior.state

    identities = tuple(candidate.identity for candidate in selected)
    read_failed = False
    try:
        reads = reader.read_identities(identities)
    except Exception:
        reads = {}
        read_failed = True

    results: list[CandidateExecutionResult] = []
    reasons: list[str] = []
    for candidate in selected:
        prior = prior_by_identity.get(candidate.identity)
        observation = reads.get(candidate.identity)
        item_ref = _hmac_ref(key, "canonical-item", candidate.identity)
        if read_failed or observation is None or not observation.readable:
            state = CandidateState.OUTCOME_UNKNOWN if prior == CandidateState.OUTCOME_UNKNOWN else CandidateState.FAILED
            reason = "outcome_unknown_readback_required" if prior == CandidateState.OUTCOME_UNKNOWN else "revalidation_failure"
        elif len(observation.records) == 0:
            state = CandidateState.VERIFIED_NEW
            reason = "outcome_unknown_absent_retryable" if prior == CandidateState.OUTCOME_UNKNOWN else "still_new"
        elif len(observation.records) == 1 and _is_exact(candidate, observation.records[0]):
            state = CandidateState.ALREADY_PRESENT
            reason = "outcome_unknown_confirmed_present" if prior == CandidateState.OUTCOME_UNKNOWN else "already_present_exact"
        else:
            state = CandidateState.CONFLICT
            reason = "conflicting_existing_identity"
        reasons.append(reason)
        results.append(CandidateExecutionResult(item_ref, state, reason))

    new_count = sum(result.state == CandidateState.VERIFIED_NEW for result in results)
    present_count = sum(result.state == CandidateState.ALREADY_PRESENT for result in results)
    conflict_count = sum(result.state == CandidateState.CONFLICT for result in results)
    failed_count = sum(
        result.state in {CandidateState.FAILED, CandidateState.OUTCOME_UNKNOWN, CandidateState.CONFLICT}
        for result in results
    )
    if failed_count:
        status = "stopped_review_required"
    elif apply:
        status = "apply_blocked_writer_unavailable"
        reasons.append("production_writer_not_connected")
    elif new_count:
        status = "dry_run_ready"
    else:
        status = "dry_run_noop"

    withheld_count = plan.canonical_transaction_count - len(plan.candidates)
    return CanonicalExecutionResult(
        schema_version=EXECUTION_RESULT_SCHEMA_VERSION,
        execution_status=status,
        apply_requested=apply,
        writer_connected=False,
        plan_audit_ref=_plan_ref(plan, key),
        audit_ref_scope=audit_ref_scope,
        input_candidate_count=len(plan.candidates),
        selected_candidate_count=len(selected),
        revalidated_new_count=new_count,
        already_present_count=present_count,
        conflict_count=conflict_count,
        withheld_count=withheld_count,
        would_write_count=0 if failed_count else new_count,
        failed_review_count=failed_count,
        reason_codes=tuple(sorted(set(reasons))),
        candidate_results=tuple(results),
    )


class SheetsCanonicalIdentityReader:
    """Read-only adapter over the existing SheetsDB ``get`` surface."""

    def __init__(self, db):
        self._db = db

    def read_identities(self, identities: tuple[str, ...]) -> Mapping[str, IdentityRead]:
        wanted = set(identities)
        header_rows = self._db.get("取込データ!A1:L1")
        expected_header = HEADERS["取込データ"]
        if (
            not header_rows
            or list(header_rows[0])[:len(expected_header)] != expected_header
        ):
            return {
                identity: IdentityRead(
                    readable=False, reason_code="import_sheet_schema_invalid",
                )
                for identity in wanted
            }
        found: dict[str, list[ExistingCanonicalRecord]] = {identity: [] for identity in wanted}
        for raw in self._db.get("取込データ!A2:L"):
            row = list(raw) + [""] * max(0, 12 - len(raw))
            identity = str(row[0]).strip()
            if identity not in wanted:
                continue
            try:
                amount = int(float(str(row[6]).replace(",", "")))
            except (TypeError, ValueError):
                # Preserve the identity as a non-exact record, causing conflict.
                amount = 0
            found[identity].append(ExistingCanonicalRecord(
                identity=identity,
                source=str(row[2]).strip(),
                source_record_id=str(row[3]).strip(),
                transaction_date=str(row[4]).strip(),
                merchant=str(row[5]).strip(),
                amount_yen=amount,
                payment_method=str(row[7]).strip(),
                business_fingerprint=str(row[10]).strip(),
                memo=str(row[11]).strip(),
            ))
        return {
            identity: IdentityRead(records=tuple(records))
            for identity, records in found.items()
        }
