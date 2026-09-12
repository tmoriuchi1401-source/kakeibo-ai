"""Opt-in receipt-inbox handoff to the existing local Medical review workflow.

The adapter receives only the opaque Drive source identity and the already
data-minimised privacy-gate result.  It performs no OCR, network, Sheets, or
Drive I/O and grants no production write authority.
"""

from __future__ import annotations

from .medical_review_workflow_shadow import (
    MedicalReviewItem,
    ReviewReconciliation,
    build_medical_review_item,
    reconcile_medical_review_item,
)
from .receipt_privacy_gate import ReceiptPrivacyGateResult


class MedicalInboxHandoffShadow:
    """In-memory, idempotent queue for unresolved Medical review items."""

    def __init__(
        self,
        *,
        identity_key: bytes,
        parser_version: str = "receipt-privacy-gate-v1",
        policy_version: str = "medical-review-policy-v1",
    ) -> None:
        self._identity_key = identity_key
        self._parser_version = parser_version
        self._policy_version = policy_version
        self._items: dict[str, MedicalReviewItem] = {}

    def observe(
        self, *, source_id: str, gate: ReceiptPrivacyGateResult
    ) -> ReviewReconciliation | None:
        """Create one review item for an unresolved Medical gate decision.

        Confirmed Medical decisions already have a complete local privacy
        preview.  Sensitive-unknown decisions deliberately remain on hold and
        are never promoted into the Medical queue by this adapter.
        """
        if gate.classification != "medical" or gate.status != "needs_review":
            return None

        materialization_status = (
            "stable" if gate.extraction_status == "extracted" and gate.text_present
            else "incomplete"
        )
        proposed = build_medical_review_item(
            source_unit_identity=source_id,
            identity_key=self._identity_key,
            gate=gate,
            materialization_status=materialization_status,
            parser_version=self._parser_version,
            policy_version=self._policy_version,
        )
        existing = self._items.get(proposed.review_item_id)
        result = reconcile_medical_review_item(existing, proposed)
        if result.action == "new_item":
            self._items[proposed.review_item_id] = proposed
        return result

    def items(self) -> tuple[MedicalReviewItem, ...]:
        """Return the safe, value-free artifacts currently awaiting review."""
        return tuple(self._items.values())
