from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .payroll_google_sheets_adapter import (
    PayrollRecoveryAssessment,
    PayrollRecoveryReadAdapter,
    inspect_payroll_recovery,
)
from .payroll_storage_preview import PayrollWriteIdentity, PayrollWritePlan
from .payroll_writer import validate_payroll_write_contract


class PayrollRecoveryPreview(BaseModel):
    """Read-only decision for a previously attempted ready write plan.

    This object never authorizes a write. ``fresh_plan_required`` means a caller
    must rebuild against a new snapshot and use the existing explicit apply
    boundary; no recovery path appends, updates, deletes, or retries here.
    """

    model_config = ConfigDict(frozen=True)

    statement_id: str
    provenance: PayrollWriteIdentity
    verification: Literal[
        "confirmed", "missing", "mismatch", "ambiguous", "read_failed",
    ]
    disposition: Literal[
        "no_write_required", "fresh_plan_required", "manual_review_required",
    ]
    assessment: PayrollRecoveryAssessment | None = None
    read_error_type: str | None = None
    safe_to_automatic_retry: Literal[False] = False
    external_write_authorized: Literal[False] = False


def build_payroll_recovery_preview(
    plan: PayrollWritePlan,
    reader: PayrollRecoveryReadAdapter,
) -> PayrollRecoveryPreview:
    """Compare expected rows with stored rows and fail closed on every doubt."""
    validate_payroll_write_contract([plan])
    if plan.status != "ready":
        raise ValueError("recovery preview requires an attempted ready plan")

    try:
        assessment = inspect_payroll_recovery(plan, reader)
    except Exception as exc:
        return PayrollRecoveryPreview(
            statement_id=plan.identity.statement_id,
            provenance=plan.identity,
            verification="read_failed",
            disposition="manual_review_required",
            read_error_type=type(exc).__name__,
        )

    if (
        assessment.header_state == "identity_confirmed"
        and assessment.header_matches_expected
        and assessment.item_state == "complete"
    ):
        verification = "confirmed"
        disposition = "no_write_required"
    elif assessment.header_state == "absent" and assessment.item_state == "absent":
        verification = "missing"
        disposition = "fresh_plan_required"
    elif assessment.header_state == "conflict_or_duplicate":
        verification = "ambiguous"
        disposition = "manual_review_required"
    else:
        verification = "mismatch"
        disposition = "manual_review_required"

    return PayrollRecoveryPreview(
        statement_id=plan.identity.statement_id,
        provenance=plan.identity,
        verification=verification,
        disposition=disposition,
        assessment=assessment,
    )
