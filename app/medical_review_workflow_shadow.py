"""Privacy-minimised, read-only review workflow for unresolved medical receipts.

This module has no production callers and performs no I/O.  It turns an existing
privacy-gate decision into a stable review reference, never into payment or write
authority.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .receipt_privacy_gate import ReceiptPrivacyGateResult


ReviewReasonCode = Literal[
    "no_candidate",
    "weak_candidate_only",
    "conflicting_candidates",
    "payment_label_not_observed",
    "amount_not_observed",
    "amount_observation_low_confidence",
    "ambiguous_numeric_observations",
    "structural_relationship_unresolved",
    "conflicting_payment_candidates",
    "observation_incomplete",
    "ocr_or_text_extraction_failed",
    "empty_text",
    "insufficient_evidence",
    "other_fail_closed",
]
AmbiguityCategory = Literal[
    "no_payment_candidate",
    "numeric_ambiguity",
    "unsupported_label_shape",
    "geometry_ambiguity",
    "unstable_materialization",
    "insufficient_evidence",
    "parser_failure",
    "other_fail_closed",
]
ReviewStatus = Literal["pending", "reviewed", "closed_unresolved"]
MaterializationStatus = Literal["stable", "incomplete", "conflicting"]
UxRequirement = Literal[
    "original_receipt",
    "payment_candidate_position",
    "ocr_result",
    "parser_rerun",
    "automatic_processing_unavailable",
]

_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class SafeReviewValidationError(ValueError):
    """Fixed error that cannot echo rejected review input."""

    __slots__ = ()

    def __init__(self) -> None:
        super().__init__("safe review validation failed")


class _SafeReviewModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    @classmethod
    def safe_validate(cls, value: object) -> Self:
        try:
            return cls.model_validate(value)
        except ValidationError:
            value = None
            raise SafeReviewValidationError() from None

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update is not None:
            raise SafeReviewValidationError()
        return super().model_copy(deep=deep)

    def copy(
        self,
        *,
        include: object = None,
        exclude: object = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        if include is not None or exclude is not None or update is not None:
            raise SafeReviewValidationError()
        return self.model_copy(deep=deep)


class ReviewProvenance(_SafeReviewModel):
    schema_version: Literal["medical-review-shadow-v1"] = "medical-review-shadow-v1"
    parser_version: str
    policy_version: str

    @model_validator(mode="after")
    def validate_versions(self) -> Self:
        if not _SAFE_VERSION.fullmatch(self.parser_version):
            raise ValueError("invalid parser version")
        if not _SAFE_VERSION.fullmatch(self.policy_version):
            raise ValueError("invalid policy version")
        return self


class MedicalReviewItem(_SafeReviewModel):
    """Data-minimised queue item; it deliberately contains no decision value."""

    review_item_id: str
    receipt_unit_ref: str
    reason_code: ReviewReasonCode
    parser_status: Literal["complete", "incomplete", "failed"]
    materialization_status: MaterializationStatus
    candidate_count: int = Field(ge=0)
    ambiguity_category: AmbiguityCategory
    review_status: ReviewStatus = "pending"
    provenance: ReviewProvenance
    ux_requirement: UxRequirement

    @model_validator(mode="after")
    def validate_item(self) -> Self:
        if not _HEX_DIGEST.fullmatch(self.review_item_id):
            raise ValueError("invalid review item id")
        if not _HEX_DIGEST.fullmatch(self.receipt_unit_ref):
            raise ValueError("invalid receipt unit reference")
        if self.parser_status == "failed" and self.ambiguity_category != "parser_failure":
            raise ValueError("failed parser requires parser failure category")
        return self


class ReviewReconciliation(_SafeReviewModel):
    action: Literal[
        "new_item",
        "duplicate_suppressed",
        "stale_provenance",
        "materialization_mismatch",
    ]
    retained_item: MedicalReviewItem
    duplicate_created: Literal[False] = False
    status_overwritten: Literal[False] = False


class MedicalReviewDecision(_SafeReviewModel):
    """Value-free manual assertion; not a payment/write instruction."""

    review_item_id: str
    provenance: ReviewProvenance
    disposition: Literal[
        "confirmed_from_original",
        "unable_to_determine",
        "parser_rerun_requested",
    ]

    @model_validator(mode="after")
    def validate_id(self) -> Self:
        if not _HEX_DIGEST.fullmatch(self.review_item_id):
            raise ValueError("invalid review item id")
        return self


class ReviewDecisionHandoff(_SafeReviewModel):
    review_item_id: str
    disposition: Literal[
        "confirmed_from_original",
        "unable_to_determine",
        "parser_rerun_requested",
    ]
    status: Literal["requires_existing_production_authority"] = (
        "requires_existing_production_authority"
    )
    write_authorized: Literal[False] = False


def _opaque_digest(identity_key: bytes, purpose: bytes, value: bytes) -> str:
    return hmac.new(identity_key, purpose + b"\0" + value, hashlib.sha256).hexdigest()


def _reason_and_category(
    gate: ReceiptPrivacyGateResult, materialization_status: MaterializationStatus
) -> tuple[ReviewReasonCode, AmbiguityCategory]:
    diagnostics = set(gate.diagnostic_codes)
    if materialization_status != "stable" or "observation_incomplete" in diagnostics:
        return "observation_incomplete", "unstable_materialization"
    precedence: tuple[tuple[str, ReviewReasonCode, AmbiguityCategory], ...] = (
        ("ambiguous_numeric_observations", "ambiguous_numeric_observations", "numeric_ambiguity"),
        ("conflicting_payment_candidates", "conflicting_payment_candidates", "numeric_ambiguity"),
        ("structural_relationship_unresolved", "structural_relationship_unresolved", "geometry_ambiguity"),
        ("payment_label_not_observed", "payment_label_not_observed", "unsupported_label_shape"),
        ("amount_not_observed", "amount_not_observed", "no_payment_candidate"),
        ("amount_observation_low_confidence", "amount_observation_low_confidence", "insufficient_evidence"),
    )
    for diagnostic, reason, category in precedence:
        if diagnostic in diagnostics:
            return reason, category
    base: dict[str, tuple[ReviewReasonCode, AmbiguityCategory]] = {
        "no_candidate": ("no_candidate", "no_payment_candidate"),
        "weak_candidate_only": ("weak_candidate_only", "insufficient_evidence"),
        "conflicting_candidates": ("conflicting_candidates", "numeric_ambiguity"),
        "ocr_or_text_extraction_failed": ("ocr_or_text_extraction_failed", "parser_failure"),
        "empty_text": ("empty_text", "parser_failure"),
        "insufficient_evidence": ("insufficient_evidence", "insufficient_evidence"),
    }
    return base.get(gate.reason_code, ("other_fail_closed", "other_fail_closed"))


def _ux_for(category: AmbiguityCategory) -> UxRequirement:
    if category == "parser_failure":
        return "parser_rerun"
    if category in {"numeric_ambiguity", "geometry_ambiguity"}:
        return "payment_candidate_position"
    if category == "unstable_materialization":
        return "ocr_result"
    if category in {"no_payment_candidate", "unsupported_label_shape"}:
        return "original_receipt"
    return "automatic_processing_unavailable"


def build_medical_review_item(
    *,
    source_unit_identity: str,
    identity_key: bytes,
    gate: ReceiptPrivacyGateResult,
    materialization_status: MaterializationStatus,
    parser_version: str,
    policy_version: str,
) -> MedicalReviewItem:
    """Create one shadow item from an existing fail-closed gate decision.

    The source identity and key are used transiently and are not represented in
    the result.  A stable keyed digest prevents low-entropy source IDs from being
    exposed or trivially enumerated.
    """
    if type(source_unit_identity) is not str or not source_unit_identity or len(source_unit_identity) > 256:
        raise SafeReviewValidationError()
    if type(identity_key) is not bytes or len(identity_key) < 16:
        raise SafeReviewValidationError()
    reviewable = (
        gate.classification == "medical" and gate.status == "needs_review"
    ) or (
        gate.classification == "sensitive_unknown" and gate.status == "blocked"
    )
    if not reviewable or gate.medical_payment_amount is not None:
        raise SafeReviewValidationError()
    provenance = ReviewProvenance.safe_validate(
        {"parser_version": parser_version, "policy_version": policy_version}
    )
    unit_ref = _opaque_digest(identity_key, b"medical-receipt-unit", source_unit_identity.encode("utf-8"))
    item_id = _opaque_digest(identity_key, b"medical-review-item-v1", unit_ref.encode("ascii"))
    reason, category = _reason_and_category(gate, materialization_status)
    parser_status: Literal["complete", "incomplete", "failed"] = (
        "complete" if gate.extraction_status == "extracted" and materialization_status == "stable"
        else "incomplete" if gate.extraction_status == "extracted"
        else "failed"
    )
    if parser_status == "failed":
        reason = "ocr_or_text_extraction_failed" if gate.reason_code not in {"empty_text"} else "empty_text"
        category = "parser_failure"
    return MedicalReviewItem(
        review_item_id=item_id,
        receipt_unit_ref=unit_ref,
        reason_code=reason,
        parser_status=parser_status,
        materialization_status=materialization_status,
        candidate_count=gate.medical_candidate_count,
        ambiguity_category=category,
        provenance=provenance,
        ux_requirement=_ux_for(category),
    )


def reconcile_medical_review_item(
    existing: MedicalReviewItem | None, proposed: MedicalReviewItem
) -> ReviewReconciliation:
    """Suppress duplicates and preserve all existing review status."""
    if existing is None or existing.review_item_id != proposed.review_item_id:
        return ReviewReconciliation(action="new_item", retained_item=proposed)
    if existing.provenance != proposed.provenance:
        return ReviewReconciliation(action="stale_provenance", retained_item=existing)
    comparable = (
        "receipt_unit_ref",
        "reason_code",
        "parser_status",
        "materialization_status",
        "candidate_count",
        "ambiguity_category",
        "ux_requirement",
    )
    if any(getattr(existing, field) != getattr(proposed, field) for field in comparable):
        return ReviewReconciliation(action="materialization_mismatch", retained_item=existing)
    return ReviewReconciliation(action="duplicate_suppressed", retained_item=existing)


def validate_review_decision_handoff(
    item: MedicalReviewItem,
    decision: MedicalReviewDecision,
    *,
    current_provenance: ReviewProvenance,
) -> ReviewDecisionHandoff:
    """Validate binding/staleness, while withholding all production authority."""
    if (
        decision.review_item_id != item.review_item_id
        or decision.provenance != item.provenance
        or current_provenance != item.provenance
        or item.review_status != "pending"
    ):
        raise SafeReviewValidationError()
    return ReviewDecisionHandoff(
        review_item_id=item.review_item_id,
        disposition=decision.disposition,
    )
