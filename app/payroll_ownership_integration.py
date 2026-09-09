"""Default-disabled bridge from authoritative Payroll plans to sidecar evidence.

This module is the only production-facing dependency on ownership provenance.
It exposes no writer, apply, parser, storage, or review mutation capability.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .payroll_ownership_provenance import (
    PayrollOwnershipAdoptionCandidate,
    PayrollOwnershipAttestationEvaluation,
    PayrollOwnershipPlanBinding,
    attest_payroll_write_plan_ownership,
)
from .payroll_storage_preview import PayrollWritePlan
from .payroll_writer import validate_payroll_write_contract


@dataclass(frozen=True)
class PayrollOwnershipAttestationRequest:
    """Typed, ephemeral input for one post-plan sidecar comparison."""

    statement_id: str
    candidate: PayrollOwnershipAdoptionCandidate | None
    binding: PayrollOwnershipPlanBinding


@dataclass(frozen=True)
class PayrollOwnershipAttestationRecord:
    """Read-only ownership evidence associated with an existing plan."""

    statement_id: str | None
    evaluation: PayrollOwnershipAttestationEvaluation


@dataclass(frozen=True)
class PayrollOwnershipIntegrationEvidence:
    """Supplemental metadata that never changes Payroll write authority."""

    enabled: bool
    status: Literal["disabled", "evaluated", "evaluation_failed"]
    records: tuple[PayrollOwnershipAttestationRecord, ...] = ()


DISABLED_PAYROLL_OWNERSHIP_EVIDENCE = PayrollOwnershipIntegrationEvidence(
    enabled=False,
    status="disabled",
)


def _rejection(reason: str) -> PayrollOwnershipAttestationEvaluation:
    return PayrollOwnershipAttestationEvaluation(
        accepted=False,
        reason_code=reason,
        attestation=None,
    )


def evaluate_payroll_ownership_attestation_integration(
    payroll_plans: Iterable[PayrollWritePlan],
    requests: Iterable[PayrollOwnershipAttestationRequest] = (),
    *,
    enabled: bool = False,
    local_key: bytes | None = None,
) -> PayrollOwnershipIntegrationEvidence:
    """Evaluate optional ownership evidence without writing or gating a plan.

    Disabled mode deliberately does not consume or validate either input. In
    enabled mode every error is reduced to read-only rejection metadata; this
    observer cannot block, replace, or rewrite an authoritative plan.
    """

    if enabled is not True:
        return DISABLED_PAYROLL_OWNERSHIP_EVIDENCE

    try:
        plans = validate_payroll_write_contract(payroll_plans)
        request_values = tuple(requests)
    except Exception:
        return PayrollOwnershipIntegrationEvidence(
            enabled=True,
            status="evaluation_failed",
        )

    plans_by_statement = {
        plan.identity.statement_id: plan for plan in plans
    }
    records: list[PayrollOwnershipAttestationRecord] = []
    for request in request_values:
        if not isinstance(request, PayrollOwnershipAttestationRequest):
            records.append(PayrollOwnershipAttestationRecord(
                statement_id=None,
                evaluation=_rejection("ownership_attestation_request_required"),
            ))
            continue
        plan = plans_by_statement.get(request.statement_id)
        if plan is None:
            records.append(PayrollOwnershipAttestationRecord(
                statement_id=request.statement_id,
                evaluation=_rejection("ownership_attestation_plan_not_found"),
            ))
            continue
        try:
            evaluation = attest_payroll_write_plan_ownership(
                plan,
                request.candidate,
                request.binding,
                local_key=local_key,
            )
        except Exception:
            evaluation = _rejection("ownership_attestation_evaluation_failed")
        records.append(PayrollOwnershipAttestationRecord(
            statement_id=request.statement_id,
            evaluation=evaluation,
        ))
    return PayrollOwnershipIntegrationEvidence(
        enabled=True,
        status="evaluated",
        records=tuple(records),
    )
