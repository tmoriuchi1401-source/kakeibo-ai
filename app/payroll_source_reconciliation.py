"""Explicit operator authority for reconciling alternate Payroll sources.

This module never infers that two sources are the same statement.  It validates
one signed operator decision against the fresh source candidate, the exact
stored target statement, and the existing fail-closed ``PayrollWritePlan``.
An accepted decision can only project a zero-row skipped-duplicate plan; it has
no Sheets/Drive writer and does not mutate either source or stored data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
from typing import Literal

from .payroll_sheets import PayrollSheetsSnapshot
from .payroll_storage import PayrollStatementRecord, PayrollStorageCandidate
from .payroll_storage_preview import (
    PayrollWritePlan,
    build_write_plan,
)


RECONCILIATION_VERSION = "payroll-source-reconciliation-v1"
RECONCILIATION_PROVENANCE = "explicit_operator_business_decision_v1"
ReconciliationDecision = Literal["same_statement_alternate_source"]


def _require_key(local_key: bytes) -> None:
    if not isinstance(local_key, bytes) or len(local_key) < 32:
        raise ValueError("local_key_too_short")


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _mac(local_key: bytes, value) -> str:
    return hmac.new(local_key, _canonical(value), hashlib.sha256).hexdigest()


def _source_id_digest(local_key: bytes, source_file_id: str) -> str:
    return _mac(local_key, ("payroll-source-id-v1", source_file_id))


def _require_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("reconciliation_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reconciliation_timestamp_invalid")


@dataclass(frozen=True)
class PayrollSourceReconciliationDecision:
    contract_version: str
    decision_revision: int
    decision: ReconciliationDecision
    operator_id: str = field(repr=False)
    provenance: str
    created_at_utc: str
    target_statement_id: str
    target_source_file_id_digest: str = field(repr=False)
    target_content_hash: str = field(repr=False)
    alternate_source_file_id_digest: str = field(repr=False)
    alternate_content_hash: str = field(repr=False)
    employer_id: str = field(repr=False)
    statement_type: str
    pay_period: str
    gross_pay: int
    total_deductions: int
    net_pay: int
    signature: str = field(repr=False)


@dataclass(frozen=True)
class PayrollSourceReconciliationPreview:
    accepted: bool
    reason_code: str
    classification: Literal["alternate_source", "unresolved"]
    target_statement_id: str | None
    original_plan_status: str
    original_duplicate_status: str
    original_duplicate_reason: str | None
    planned_header_rows: int = 0
    planned_item_rows: int = 0
    planned_update_rows: int = 0


@dataclass(frozen=True)
class PayrollSourceReconciliationResult:
    preview: PayrollSourceReconciliationPreview
    plan: PayrollWritePlan = field(repr=False)
    applied: bool = False


def _decision_payload(decision: PayrollSourceReconciliationDecision) -> dict:
    values = asdict(decision)
    values.pop("signature")
    return values


def create_payroll_source_reconciliation_decision(
    *,
    target: PayrollStatementRecord,
    alternate: PayrollStorageCandidate,
    operator_id: str,
    created_at_utc: str,
    local_key: bytes,
    decision: ReconciliationDecision = "same_statement_alternate_source",
    decision_revision: int = 1,
) -> PayrollSourceReconciliationDecision:
    """Sign an explicit decision without choosing or applying it."""

    _require_key(local_key)
    _require_timestamp(created_at_utc)
    statement = alternate.statement
    if not operator_id.strip():
        raise ValueError("reconciliation_operator_id_required")
    if not target.source_file_id or not target.content_hash:
        raise ValueError("reconciliation_target_source_binding_incomplete")
    if not statement.source_file_id or not statement.content_hash:
        raise ValueError("reconciliation_alternate_source_binding_incomplete")
    if None in (
        statement.employer_id, statement.statement_type, statement.pay_period,
        statement.gross_pay, statement.total_deductions, statement.net_pay,
    ):
        raise ValueError("reconciliation_alternate_business_binding_incomplete")
    unsigned = PayrollSourceReconciliationDecision(
        contract_version=RECONCILIATION_VERSION,
        decision_revision=decision_revision,
        decision=decision,
        operator_id=operator_id,
        provenance=RECONCILIATION_PROVENANCE,
        created_at_utc=created_at_utc,
        target_statement_id=target.statement_id,
        target_source_file_id_digest=_source_id_digest(
            local_key, target.source_file_id,
        ),
        target_content_hash=target.content_hash,
        alternate_source_file_id_digest=_source_id_digest(
            local_key, statement.source_file_id,
        ),
        alternate_content_hash=statement.content_hash,
        employer_id=statement.employer_id,
        statement_type=statement.statement_type,
        pay_period=statement.pay_period,
        gross_pay=statement.gross_pay,
        total_deductions=statement.total_deductions,
        net_pay=statement.net_pay,
        signature="",
    )
    return PayrollSourceReconciliationDecision(
        **{
            **_decision_payload(unsigned),
            "signature": _mac(local_key, _decision_payload(unsigned)),
        },
    )


def serialize_payroll_source_reconciliation_decision(
    decision: PayrollSourceReconciliationDecision,
) -> bytes:
    return (json.dumps(
        asdict(decision), ensure_ascii=True, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n").encode("ascii")


def deserialize_payroll_source_reconciliation_decision(
    payload: bytes | str,
    *,
    local_key: bytes,
) -> PayrollSourceReconciliationDecision:
    """Parse and authenticate a decision for fresh-process replay."""

    _require_key(local_key)
    if isinstance(payload, bytes):
        if len(payload) > 1_000_000:
            raise ValueError("reconciliation_payload_too_large")
        payload = payload.decode("ascii")
    try:
        values = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("reconciliation_payload_invalid") from exc
    expected = set(PayrollSourceReconciliationDecision.__dataclass_fields__)
    if not isinstance(values, dict) or set(values) != expected:
        raise ValueError("reconciliation_payload_shape_invalid")
    try:
        decision = PayrollSourceReconciliationDecision(**values)
    except TypeError as exc:
        raise ValueError("reconciliation_payload_shape_invalid") from exc
    if not hmac.compare_digest(
        decision.signature, _mac(local_key, _decision_payload(decision)),
    ):
        raise ValueError("reconciliation_signature_invalid")
    return decision


def _rejected(plan: PayrollWritePlan, reason: str):
    return PayrollSourceReconciliationPreview(
        accepted=False,
        reason_code=reason,
        classification="unresolved",
        target_statement_id=plan.duplicate.matched_statement_id,
        original_plan_status=plan.status,
        original_duplicate_status=plan.duplicate.status,
        original_duplicate_reason=plan.duplicate.reason,
    )


def preview_payroll_source_reconciliation(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    decision: PayrollSourceReconciliationDecision | None,
    *,
    local_key: bytes,
) -> PayrollSourceReconciliationPreview:
    """Validate a same-statement decision without changing any object."""

    _require_key(local_key)
    plan = build_write_plan([candidate], snapshot)[0]
    if not isinstance(decision, PayrollSourceReconciliationDecision):
        return _rejected(plan, "reconciliation_decision_required")
    try:
        _require_timestamp(decision.created_at_utc)
    except ValueError as exc:
        return _rejected(plan, str(exc))
    if decision.contract_version != RECONCILIATION_VERSION:
        return _rejected(plan, "reconciliation_contract_version_stale")
    if (
        not isinstance(decision.decision_revision, int)
        or isinstance(decision.decision_revision, bool)
        or decision.decision_revision != 1
    ):
        return _rejected(plan, "reconciliation_decision_revision_stale")
    if decision.decision != "same_statement_alternate_source":
        return _rejected(plan, "reconciliation_decision_unsupported")
    if not decision.operator_id.strip():
        return _rejected(plan, "reconciliation_operator_id_required")
    if decision.provenance != RECONCILIATION_PROVENANCE:
        return _rejected(plan, "reconciliation_provenance_invalid")
    if not hmac.compare_digest(
        decision.signature, _mac(local_key, _decision_payload(decision)),
    ):
        return _rejected(plan, "reconciliation_signature_invalid")
    targets = [
        value for value in snapshot.statements
        if value.statement_id == decision.target_statement_id
    ]
    if len(targets) != 1:
        return _rejected(plan, "reconciliation_target_not_unique")
    target = targets[0]
    source = candidate.statement
    if (
        not target.source_file_id
        or _source_id_digest(local_key, target.source_file_id)
        != decision.target_source_file_id_digest
        or target.content_hash != decision.target_content_hash
    ):
        return _rejected(plan, "reconciliation_target_source_mismatch")
    if (
        not source.source_file_id
        or _source_id_digest(local_key, source.source_file_id)
        != decision.alternate_source_file_id_digest
        or source.content_hash != decision.alternate_content_hash
    ):
        return _rejected(plan, "reconciliation_alternate_source_mismatch")
    if (
        source.source_file_id == target.source_file_id
        or source.content_hash == target.content_hash
    ):
        return _rejected(plan, "reconciliation_distinct_source_required")
    decision_business = (
        decision.employer_id,
        decision.statement_type,
        decision.pay_period,
        decision.gross_pay,
        decision.total_deductions,
        decision.net_pay,
    )
    target_business = (
        target.employer_id,
        target.statement_type,
        target.pay_period,
        target.gross_pay,
        target.total_deductions,
        target.net_pay,
    )
    alternate_business = (
        source.employer_id,
        source.statement_type,
        source.pay_period,
        source.gross_pay,
        source.total_deductions,
        source.net_pay,
    )
    if decision_business != target_business:
        return _rejected(plan, "reconciliation_target_business_mismatch")
    if decision_business != alternate_business:
        return _rejected(plan, "reconciliation_alternate_business_mismatch")
    if plan.status != "blocked" or plan.reason != "revision_conflict":
        return _rejected(plan, "reconciliation_statement_key_conflict_required")
    if (
        plan.duplicate.status != "conflict"
        or plan.duplicate.reason != "statement_key"
        or plan.duplicate.matched_statement_id != target.statement_id
    ):
        return _rejected(plan, "reconciliation_target_plan_mismatch")
    return PayrollSourceReconciliationPreview(
        accepted=True,
        reason_code="operator_reconciled_alternate_source",
        classification="alternate_source",
        target_statement_id=target.statement_id,
        original_plan_status=plan.status,
        original_duplicate_status=plan.duplicate.status,
        original_duplicate_reason=plan.duplicate.reason,
    )


def apply_payroll_source_reconciliation(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    decision: PayrollSourceReconciliationDecision | None,
    *,
    local_key: bytes,
    confirmed: bool = False,
) -> PayrollSourceReconciliationResult:
    """Return a zero-row duplicate plan only after explicit confirmation."""

    original = build_write_plan([candidate], snapshot)[0]
    preview = preview_payroll_source_reconciliation(
        candidate, snapshot, decision, local_key=local_key,
    )
    if not confirmed or not preview.accepted:
        return PayrollSourceReconciliationResult(preview, original, False)
    values = original.model_dump(mode="python")
    values.update({
        "eligibility": "ineligible",
        "status": "skipped_duplicate",
        "reason": "operator_reconciled_alternate_source",
        "reasons": ("operator_reconciled_alternate_source",),
        "header_action": "none",
        "item_action": "none",
        "planned_header_rows": (),
        "planned_item_rows": (),
        "duplicate": {
            "status": "duplicate",
            "reason": "operator_reconciled_alternate_source",
            "matched_statement_id": decision.target_statement_id,
        },
    })
    reconciled = PayrollWritePlan.model_validate(values)
    return PayrollSourceReconciliationResult(preview, reconciled, True)
