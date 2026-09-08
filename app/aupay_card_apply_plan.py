"""Fail-closed projection boundary for future au PAY card Gmail writes.

This module intentionally has no Sheets writer.  It converts a complete
collection's reconciliation result into an explicitly typed, immutable plan;
raw ``Transaction`` and ``ReconciledTransaction`` objects are not executor
inputs.
"""
from __future__ import annotations

from dataclasses import dataclass

from .transaction_plan import ReconciledTransaction


_PROJECTION_AUTHORITY = object()


@dataclass(frozen=True)
class CanonicalItemDecision:
    status: str
    reasons: tuple[str, ...]
    canonical_identity: str
    source_identities: tuple[str, ...]
    business_fingerprint: str
    transaction_kind: str
    cross_source_state: str


@dataclass(frozen=True, init=False)
class CanonicalApplyCandidate:
    schema_version: int
    identity: str
    source: str
    source_record_id: str
    transaction_date: str
    merchant: str
    amount_yen: int
    transaction_kind: str
    payment_method: str
    business_fingerprint: str
    memo: str
    reconciliation_state: str
    source_identities: tuple[str, ...]
    cross_source_state: str
    cross_source_candidate_identities: tuple[str, ...]
    _authority: object

    def __new__(cls, *args, **kwargs):
        raise TypeError("canonical_apply_candidate_is_projection_only")

    @classmethod
    def _from_canonical(
        cls, item: ReconciledTransaction, *, authority: object,
    ) -> "CanonicalApplyCandidate":
        if authority is not _PROJECTION_AUTHORITY:
            raise TypeError("canonical_apply_candidate_is_projection_only")
        tx = item.canonical
        if (
            item.state not in {"new", "probable_resend"}
            or item.existing_source_identities
            or item.cross_source.state == "cross_source_ambiguous"
            or tx.transaction_kind != "purchase"
            or tx.amount_yen <= 0
        ):
            raise TypeError("ineligible_canonical_apply_candidate")
        candidate = object.__new__(cls)
        values = {
            "schema_version": 2,
            "identity": tx.identity,
            "source": tx.source,
            "source_record_id": tx.source_record_id,
            "transaction_date": tx.transaction_date,
            "merchant": tx.merchant,
            "amount_yen": tx.amount_yen,
            "transaction_kind": tx.transaction_kind,
            "payment_method": tx.payment_method,
            "business_fingerprint": tx.business_fingerprint,
            "memo": tx.memo,
            "reconciliation_state": item.state,
            "source_identities": item.source_identities,
            "cross_source_state": item.cross_source.state,
            "cross_source_candidate_identities": item.cross_source.candidate_identities,
            "_authority": _PROJECTION_AUTHORITY,
        }
        for name, value in values.items():
            object.__setattr__(candidate, name, value)
        return candidate

    def to_import_row(self, *, status: str = "unclassified_card") -> list:
        """Materialize only an authorized canonical projection, never a raw row."""
        if self._authority is not _PROJECTION_AUTHORITY:
            raise TypeError("unauthorized_apply_candidate")
        return [
            self.identity, "", self.source, self.source_record_id,
            self.transaction_date, self.merchant, self.amount_yen,
            self.payment_method, status, "", self.business_fingerprint,
            self.memo,
        ]


@dataclass(frozen=True, init=False)
class CanonicalApplyPlan:
    schema_version: int
    status: str
    candidates: tuple[CanonicalApplyCandidate, ...]
    item_decisions: tuple[CanonicalItemDecision, ...]
    blocked_reasons: tuple[str, ...]
    raw_transaction_count: int
    canonical_transaction_count: int
    noncanonical_resend_count: int
    eligible_canonical_count: int
    withheld_ambiguous_count: int
    parser_review_count: int
    parser_review_line_item_count: int
    reconciliation_review_count: int
    rejected_transaction_count: int
    identity_collision_count: int
    cross_source_strong_match: int
    cross_source_ambiguous: int
    cross_source_no_match: int
    existing_identity_duplicate_count: int
    return_transaction_count: int
    withheld_return_count: int
    duplicate_existing_identity_count: int
    withheld_review_count: int
    invalid_item_count: int
    global_withheld_count: int
    item_withheld_reason_counts: tuple[tuple[str, int], ...]
    canonical_accounting_valid: bool
    _authority: object

    def __new__(cls, *args, **kwargs):
        raise TypeError("canonical_apply_plan_is_builder_only")

    @classmethod
    def _create(cls, *, authority: object, **values) -> "CanonicalApplyPlan":
        if authority is not _PROJECTION_AUTHORITY:
            raise TypeError("canonical_apply_plan_is_builder_only")
        plan = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(plan, name, value)
        object.__setattr__(plan, "_authority", _PROJECTION_AUTHORITY)
        return plan

    @property
    def executable(self) -> bool:
        return self.status in {"ready", "ready_with_withheld"}

    def summary(self) -> dict:
        return {
            "apply_plan_status": self.status,
            "apply_plan_blocked_reasons": list(self.blocked_reasons),
            "global_blocked_reasons": list(self.blocked_reasons),
            "apply_candidate_count": len(self.candidates),
            "eligible_canonical_count": self.eligible_canonical_count,
            "withheld_ambiguous_count": self.withheld_ambiguous_count,
            "raw_transaction_count": self.raw_transaction_count,
            "canonical_transaction_count": self.canonical_transaction_count,
            "noncanonical_resend_count": self.noncanonical_resend_count,
            "parser_review_count": self.parser_review_count,
            "parser_review_line_item_count": self.parser_review_line_item_count,
            "reconciliation_review_count": self.reconciliation_review_count,
            "rejected_transaction_count": self.rejected_transaction_count,
            "identity_collision_count": self.identity_collision_count,
            "cross_source_strong_match": self.cross_source_strong_match,
            "cross_source_ambiguous": self.cross_source_ambiguous,
            "cross_source_no_match": self.cross_source_no_match,
            "existing_identity_duplicate_count": self.existing_identity_duplicate_count,
            "return_transaction_count": self.return_transaction_count,
            "withheld_return_count": self.withheld_return_count,
            "duplicate_existing_identity_count": self.duplicate_existing_identity_count,
            "withheld_review_count": self.withheld_review_count,
            "invalid_item_count": self.invalid_item_count,
            "global_withheld_count": self.global_withheld_count,
            "item_status_counts": dict(self.item_status_counts),
            "item_withheld_reason_counts": dict(self.item_withheld_reason_counts),
            "canonical_accounting_valid": self.canonical_accounting_valid,
        }

    @property
    def item_status_counts(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for decision in self.item_decisions:
            counts[decision.status] = counts.get(decision.status, 0) + 1
        return tuple(sorted(counts.items()))


def _base_item_decision(item: ReconciledTransaction) -> CanonicalItemDecision:
    reasons: list[str] = []
    if item.state == "needs_review":
        reasons.append("reconciliation_item_review")
    if item.existing_source_identities:
        reasons.append("existing_exact_source_identity")
    if item.canonical.transaction_kind == "return":
        reasons.append("return_requires_accounting_policy")
    if item.cross_source.state == "cross_source_ambiguous":
        reasons.append("cross_source_ambiguous")

    if item.state == "needs_review":
        status = "needs_review"
    elif item.existing_source_identities:
        status = "duplicate_existing_identity"
    elif item.canonical.transaction_kind == "return":
        status = "withheld_return"
    elif item.cross_source.state == "cross_source_ambiguous":
        status = "withheld_cross_source_ambiguous"
    elif item.state in {"new", "probable_resend"}:
        status = "eligible"
    else:
        status = "invalid"
        reasons.append("unsupported_reconciliation_state")
    return CanonicalItemDecision(
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        canonical_identity=item.canonical.identity,
        source_identities=item.source_identities,
        business_fingerprint=item.canonical.business_fingerprint,
        transaction_kind=item.canonical.transaction_kind,
        cross_source_state=item.cross_source.state,
    )


def build_canonical_apply_plan(collection: dict, reconciliation: dict) -> CanonicalApplyPlan:
    """Project eligible canonical purchases while isolating item-level review."""
    summary = reconciliation["summary"]
    items = tuple(reconciliation["transactions"])
    reasons: list[str] = []

    if not collection.get("collection_complete", False):
        reasons.append("collection_incomplete")
    if not collection.get("listing_complete", False):
        reasons.append("gmail_listing_incomplete")
    if collection.get("gmail_list_failed", 0):
        reasons.append("gmail_list_failure")
    if collection.get("gmail_read_failed", 0):
        reasons.append("gmail_read_failure")
    if collection.get("collection_truncated", False):
        reasons.append("collection_truncated")
    parser_review = int(collection.get("parser_review_mail_count", collection.get("needs_review", 0)))
    collisions = int(summary.get("identity_collisions", 0))
    if collisions:
        reasons.append("identity_collision")
    reconciliation_review = int(summary.get("needs_review", 0))

    raw_count = int(summary.get("raw_transactions", 0))
    rejected = int(summary.get("rejected", 0))
    if rejected:
        reasons.append("reconciliation_rejected_transaction")
    same_source_duplicates = int(summary.get("same_source_duplicates", 0))
    represented = sum(len(item.source_identities) for item in items)
    source_ids = [identity for item in items for identity in item.source_identities]
    calculated_cross_counts = {
        state: sum(item.cross_source.state == state for item in items)
        for state in (
            "cross_source_strong_match",
            "cross_source_ambiguous",
            "cross_source_no_match",
        )
    }
    calculated_purchase_count = sum(
        item.canonical.transaction_kind == "purchase" for item in items
    )
    calculated_return_count = sum(
        item.canonical.transaction_kind == "return" for item in items
    )
    reconciliation_valid = (
        collisions == 0
        and raw_count >= 0
        and int(summary.get("canonical_transactions", -1)) == len(items)
        and represented + same_source_duplicates + rejected == raw_count
        and len(source_ids) == len(set(source_ids))
        and all(item.canonical.identity in item.source_identities for item in items)
        and all(item.state in {"new", "probable_resend", "needs_review"} for item in items)
        and all(item.canonical.transaction_kind in {"purchase", "return"} for item in items)
        and all(
            (item.canonical.transaction_kind == "purchase" and item.canonical.amount_yen > 0)
            or (item.canonical.transaction_kind == "return" and item.canonical.amount_yen < 0)
            for item in items
        )
        and all(item.cross_source.state in {
            "cross_source_strong_match", "cross_source_ambiguous", "cross_source_no_match",
        } for item in items)
        and all(
            int(summary.get(state, -1)) == count
            for state, count in calculated_cross_counts.items()
        )
        and int(summary.get("purchase_canonical_transactions", -1)) == calculated_purchase_count
        and int(summary.get("return_canonical_transactions", -1)) == calculated_return_count
        and calculated_purchase_count + calculated_return_count == len(items)
        and int(summary.get("existing_identity_duplicates", -1)) == sum(
            bool(item.existing_source_identities) for item in items
        )
        and sum(len(item.source_identities) - 1 for item in items)
        == int(summary.get("probable_resend_records", 0))
    )
    if not reconciliation_valid:
        reasons.append("reconciliation_inconsistent")

    blocked_reasons = tuple(dict.fromkeys(reasons))
    base_decisions = tuple(_base_item_decision(item) for item in items)
    if blocked_reasons:
        decisions = tuple(
            CanonicalItemDecision(
                status="withheld_global_failure" if decision.status == "eligible" else decision.status,
                reasons=("global_safety_gate_failed",) + decision.reasons
                if decision.status == "eligible" else decision.reasons,
                canonical_identity=decision.canonical_identity,
                source_identities=decision.source_identities,
                business_fingerprint=decision.business_fingerprint,
                transaction_kind=decision.transaction_kind,
                cross_source_state=decision.cross_source_state,
            )
            for decision in base_decisions
        )
    else:
        decisions = base_decisions
    by_identity = {item.canonical.identity: item for item in items}
    candidates = tuple(
        CanonicalApplyCandidate._from_canonical(
            by_identity[decision.canonical_identity], authority=_PROJECTION_AUTHORITY,
        )
        for decision in decisions if decision.status == "eligible"
    )
    status_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for decision in decisions:
        status_counts[decision.status] = status_counts.get(decision.status, 0) + 1
        if decision.status != "eligible":
            for reason in decision.reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
    parser_review_items = int(collection.get("review_line_items", 0))
    withheld_review = status_counts.get("needs_review", 0)
    canonical_accounted_count = (
        len(candidates)
        + status_counts.get("withheld_return", 0)
        + status_counts.get("withheld_cross_source_ambiguous", 0)
        + status_counts.get("duplicate_existing_identity", 0)
        + status_counts.get("needs_review", 0)
        + status_counts.get("invalid", 0)
        + status_counts.get("withheld_global_failure", 0)
    )
    canonical_accounted = (
        canonical_accounted_count == len(items)
        and len(candidates) == status_counts.get("eligible", 0)
    )
    plan_status = (
        "blocked" if blocked_reasons else
        "ready_with_withheld" if (
            len(candidates) != len(items) or withheld_review or parser_review_items
        ) else
        "ready"
    )
    return CanonicalApplyPlan._create(
        authority=_PROJECTION_AUTHORITY,
        schema_version=3,
        status=plan_status,
        candidates=candidates,
        item_decisions=decisions,
        blocked_reasons=blocked_reasons,
        raw_transaction_count=raw_count,
        canonical_transaction_count=len(items),
        noncanonical_resend_count=int(summary.get("probable_resend_records", 0)),
        eligible_canonical_count=status_counts.get("eligible", 0),
        withheld_ambiguous_count=status_counts.get("withheld_cross_source_ambiguous", 0),
        parser_review_count=parser_review,
        parser_review_line_item_count=parser_review_items,
        reconciliation_review_count=reconciliation_review,
        rejected_transaction_count=rejected,
        identity_collision_count=collisions,
        cross_source_strong_match=int(summary.get("cross_source_strong_match", 0)),
        cross_source_ambiguous=int(summary.get("cross_source_ambiguous", 0)),
        cross_source_no_match=int(summary.get("cross_source_no_match", 0)),
        existing_identity_duplicate_count=int(
            summary.get("existing_identity_duplicates", 0)
        ),
        return_transaction_count=int(summary.get("return_canonical_transactions", 0)),
        withheld_return_count=status_counts.get("withheld_return", 0),
        duplicate_existing_identity_count=status_counts.get("duplicate_existing_identity", 0),
        withheld_review_count=withheld_review,
        invalid_item_count=status_counts.get("invalid", 0),
        global_withheld_count=status_counts.get("withheld_global_failure", 0),
        item_withheld_reason_counts=tuple(sorted(reason_counts.items())),
        canonical_accounting_valid=canonical_accounted,
    )


def require_executable_apply_plan(value: object, *, apply: bool) -> CanonicalApplyPlan:
    """Future writer guard: require the exact safe type and explicit consent."""
    if type(value) is not CanonicalApplyPlan:
        raise TypeError("canonical_apply_plan_required")
    if value._authority is not _PROJECTION_AUTHORITY:
        raise TypeError("unauthorized_apply_plan")
    if not apply:
        raise RuntimeError("explicit_apply_required")
    if not value.executable:
        raise RuntimeError("apply_plan_blocked")
    if not value.canonical_accounting_valid:
        raise RuntimeError("apply_plan_accounting_invalid")
    if any(candidate._authority is not _PROJECTION_AUTHORITY for candidate in value.candidates):
        raise TypeError("unauthorized_apply_candidate")
    eligible_identities = {
        decision.canonical_identity for decision in value.item_decisions
        if decision.status == "eligible"
    }
    candidate_identities = {candidate.identity for candidate in value.candidates}
    if eligible_identities != candidate_identities:
        raise RuntimeError("apply_plan_candidate_manifest_mismatch")
    if any(
        candidate.transaction_kind != "purchase" or candidate.amount_yen <= 0
        for candidate in value.candidates
    ):
        raise RuntimeError("apply_plan_candidate_ineligible")
    return value
