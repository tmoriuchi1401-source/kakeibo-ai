"""Authenticated, value-free handoff for reviewed Medical review items.

This opt-in shadow has no production callers and performs no I/O.  A validated
assertion can only request evaluation by existing production authority; it can
never authorize a write or construct a write plan.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .medical_review_workflow_shadow import (
    MedicalReviewItem,
    ReviewProvenance,
    SafeReviewValidationError,
)


ReviewDecisionType = Literal[
    "confirmed_from_original",
    "unable_to_determine",
    "parser_rerun_requested",
]
HandoffStatus = Literal[
    "validated_for_authority_evaluation",
    "rejected_binding",
    "stale_provenance",
    "invalid_review_state",
    "duplicate_replay",
]

_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HANDOFF_SCHEMA = "medical-review-handoff-v1"
_DECISIONS = frozenset(
    {"confirmed_from_original", "unable_to_determine", "parser_rerun_requested"}
)


class _SafeHandoffModel(BaseModel):
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


class ReviewedAssertion(_SafeHandoffModel):
    """Minimal reviewed fact, authenticated but deliberately value-free."""

    handoff_schema_version: Literal["medical-review-handoff-v1"] = _HANDOFF_SCHEMA
    review_item_ref: str
    receipt_unit_ref: str
    review_status: Literal["reviewed"] = "reviewed"
    decision_type: ReviewDecisionType
    provenance: ReviewProvenance
    materialization_binding_digest: str
    review_revision: int = Field(ge=1)
    assertion_id: str
    integrity_tag: str

    @model_validator(mode="after")
    def validate_digests(self) -> Self:
        values = (
            self.review_item_ref,
            self.receipt_unit_ref,
            self.materialization_binding_digest,
            self.assertion_id,
            self.integrity_tag,
        )
        if not all(_HEX_DIGEST.fullmatch(value) for value in values):
            raise ValueError("invalid assertion digest")
        return self


class ReviewHandoffOutcome(_SafeHandoffModel):
    status: HandoffStatus
    assertion_id: str | None = None
    decision_type: ReviewDecisionType | None = None
    next_boundary: Literal["requires_existing_production_authority", "none"]
    write_authorized: Literal[False] = False
    write_plan_created: Literal[False] = False
    state_changed: Literal[False] = False
    replay_detected: bool = False

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.assertion_id is not None and not _HEX_DIGEST.fullmatch(self.assertion_id):
            raise ValueError("invalid assertion id")
        valid = self.status == "validated_for_authority_evaluation"
        if valid != (self.next_boundary == "requires_existing_production_authority"):
            raise ValueError("invalid authority boundary")
        if valid and (self.assertion_id is None or self.decision_type is None):
            raise ValueError("validated outcome requires assertion reference")
        if self.status == "duplicate_replay" and not self.replay_detected:
            raise ValueError("duplicate outcome requires replay marker")
        return self


def _hmac_digest(key: bytes, purpose: bytes, value: bytes) -> str:
    return hmac.new(key, purpose + b"\0" + value, hashlib.sha256).hexdigest()


def _validate_inputs(source_unit_identity: object, identity_key: object) -> tuple[str, bytes]:
    if (
        type(source_unit_identity) is not str
        or not source_unit_identity
        or len(source_unit_identity) > 256
        or type(identity_key) is not bytes
        or len(identity_key) < 16
    ):
        raise SafeReviewValidationError()
    return source_unit_identity, identity_key


def _unit_ref(source_unit_identity: str, key: bytes) -> str:
    return _hmac_digest(key, b"medical-receipt-unit", source_unit_identity.encode("utf-8"))


def _item_ref(unit_ref: str, key: bytes) -> str:
    return _hmac_digest(key, b"medical-review-item-v1", unit_ref.encode("ascii"))


def _assertion_payload(
    *,
    item_ref: str,
    unit_ref: str,
    decision_type: ReviewDecisionType,
    provenance: ReviewProvenance,
    materialization_digest: str,
    review_revision: int,
) -> bytes:
    return json.dumps(
        {
            "handoff_schema_version": _HANDOFF_SCHEMA,
            "review_item_ref": item_ref,
            "receipt_unit_ref": unit_ref,
            "review_status": "reviewed",
            "decision_type": decision_type,
            "review_schema_version": provenance.schema_version,
            "parser_version": provenance.parser_version,
            "policy_version": provenance.policy_version,
            "materialization_binding_digest": materialization_digest,
            "review_revision": review_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def build_reviewed_assertion(
    *,
    item: MedicalReviewItem,
    source_unit_identity: str,
    identity_key: bytes,
    materialization_binding_digest: str,
    decision_type: ReviewDecisionType,
    review_revision: int,
) -> ReviewedAssertion:
    """Bind a reviewed item to a trusted materialization without copying values."""
    source_unit_identity, identity_key = _validate_inputs(source_unit_identity, identity_key)
    if (
        type(item) is not MedicalReviewItem
        or item.review_status != "reviewed"
        or type(materialization_binding_digest) is not str
        or not _HEX_DIGEST.fullmatch(materialization_binding_digest)
        or type(decision_type) is not str
        or decision_type not in _DECISIONS
        or type(review_revision) is not int
        or review_revision < 1
    ):
        raise SafeReviewValidationError()
    expected_unit = _unit_ref(source_unit_identity, identity_key)
    expected_item = _item_ref(expected_unit, identity_key)
    if not (
        hmac.compare_digest(item.receipt_unit_ref, expected_unit)
        and hmac.compare_digest(item.review_item_id, expected_item)
    ):
        raise SafeReviewValidationError()
    payload = _assertion_payload(
        item_ref=item.review_item_id,
        unit_ref=item.receipt_unit_ref,
        decision_type=decision_type,
        provenance=item.provenance,
        materialization_digest=materialization_binding_digest,
        review_revision=review_revision,
    )
    assertion_id = _hmac_digest(identity_key, b"medical-reviewed-assertion-id", payload)
    integrity_tag = _hmac_digest(
        identity_key,
        b"medical-reviewed-assertion-integrity",
        assertion_id.encode("ascii") + payload,
    )
    return ReviewedAssertion(
        review_item_ref=item.review_item_id,
        receipt_unit_ref=item.receipt_unit_ref,
        decision_type=decision_type,
        provenance=item.provenance,
        materialization_binding_digest=materialization_binding_digest,
        review_revision=review_revision,
        assertion_id=assertion_id,
        integrity_tag=integrity_tag,
    )


def _outcome(status: HandoffStatus, *, replay: bool = False) -> ReviewHandoffOutcome:
    return ReviewHandoffOutcome(
        status=status,
        next_boundary="none",
        replay_detected=replay,
    )


def evaluate_reviewed_assertion(
    assertion: ReviewedAssertion,
    *,
    item: MedicalReviewItem,
    source_unit_identity: str,
    identity_key: bytes,
    expected_materialization_binding_digest: str,
    current_provenance: ReviewProvenance,
    previously_accepted_assertion_id: str | None = None,
) -> ReviewHandoffOutcome:
    """Validate a reviewed assertion and stop before production authority.

    No mutable replay ledger is owned here.  A caller may supply the previously
    accepted assertion ID; replay is reported without creating state or plans.
    """
    try:
        source_unit_identity, identity_key = _validate_inputs(source_unit_identity, identity_key)
    except SafeReviewValidationError:
        return _outcome("rejected_binding")
    if type(assertion) is not ReviewedAssertion or type(item) is not MedicalReviewItem:
        return _outcome("invalid_review_state")
    try:
        assertion = ReviewedAssertion.safe_validate(
            {name: getattr(assertion, name) for name in ReviewedAssertion.model_fields}
        )
        item = MedicalReviewItem.safe_validate(
            {name: getattr(item, name) for name in MedicalReviewItem.model_fields}
        )
        current_provenance = ReviewProvenance.safe_validate(
            {name: getattr(current_provenance, name) for name in ReviewProvenance.model_fields}
        )
    except (SafeReviewValidationError, AttributeError):
        return _outcome("invalid_review_state")
    if item.review_status != "reviewed" or assertion.review_status != "reviewed":
        return _outcome("invalid_review_state")
    if (
        assertion.provenance != item.provenance
        or current_provenance != item.provenance
    ):
        return _outcome("stale_provenance")
    if (
        type(expected_materialization_binding_digest) is not str
        or not _HEX_DIGEST.fullmatch(expected_materialization_binding_digest)
    ):
        return _outcome("rejected_binding")
    expected_unit = _unit_ref(source_unit_identity, identity_key)
    expected_item = _item_ref(expected_unit, identity_key)
    if not all(
        (
            hmac.compare_digest(item.receipt_unit_ref, expected_unit),
            hmac.compare_digest(item.review_item_id, expected_item),
            hmac.compare_digest(assertion.receipt_unit_ref, expected_unit),
            hmac.compare_digest(assertion.review_item_ref, expected_item),
            hmac.compare_digest(
                assertion.materialization_binding_digest,
                expected_materialization_binding_digest,
            ),
        )
    ):
        return _outcome("rejected_binding")
    payload = _assertion_payload(
        item_ref=assertion.review_item_ref,
        unit_ref=assertion.receipt_unit_ref,
        decision_type=assertion.decision_type,
        provenance=assertion.provenance,
        materialization_digest=assertion.materialization_binding_digest,
        review_revision=assertion.review_revision,
    )
    expected_assertion_id = _hmac_digest(
        identity_key, b"medical-reviewed-assertion-id", payload
    )
    expected_integrity = _hmac_digest(
        identity_key,
        b"medical-reviewed-assertion-integrity",
        expected_assertion_id.encode("ascii") + payload,
    )
    if not (
        hmac.compare_digest(assertion.assertion_id, expected_assertion_id)
        and hmac.compare_digest(assertion.integrity_tag, expected_integrity)
    ):
        return _outcome("rejected_binding")
    if previously_accepted_assertion_id is not None:
        if (
            type(previously_accepted_assertion_id) is not str
            or not _HEX_DIGEST.fullmatch(previously_accepted_assertion_id)
        ):
            return _outcome("rejected_binding")
        if hmac.compare_digest(assertion.assertion_id, previously_accepted_assertion_id):
            return _outcome("duplicate_replay", replay=True)
        return _outcome("rejected_binding", replay=True)
    return ReviewHandoffOutcome(
        status="validated_for_authority_evaluation",
        assertion_id=assertion.assertion_id,
        decision_type=assertion.decision_type,
        next_boundary="requires_existing_production_authority",
    )
