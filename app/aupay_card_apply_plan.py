"""Fail-closed projection boundary for future au PAY card Gmail writes.

This module intentionally has no Sheets writer.  It converts a complete
collection's reconciliation result into an explicitly typed, immutable plan;
raw ``Transaction`` and ``ReconciledTransaction`` objects are not executor
inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .transaction_plan import ReconciledTransaction
from .aupay_card_contract import (
    format_import_timestamp,
    is_amazon_merchant,
    is_aupay_charge_merchant,
)


_PROJECTION_AUTHORITY = object()
APPLY_PLAN_SCHEMA_VERSION = 3
APPLY_CANDIDATE_SCHEMA_VERSION = 3


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
    member: str
    source_occurrence: int
    source_hash: str
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
            "schema_version": APPLY_CANDIDATE_SCHEMA_VERSION,
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
            "member": tx.member,
            "source_occurrence": tx.source_occurrence,
            "source_hash": tx.source_hash,
            "reconciliation_state": item.state,
            "source_identities": item.source_identities,
            "cross_source_state": item.cross_source.state,
            "cross_source_candidate_identities": item.cross_source.candidate_identities,
            "_authority": _PROJECTION_AUTHORITY,
        }
        for name, value in values.items():
            object.__setattr__(candidate, name, value)
        return candidate

    def to_import_row(
        self, *, imported_at, status: str, target_id: str = "",
    ) -> list:
        """Materialize only an authorized canonical projection, never a raw row."""
        if self._authority is not _PROJECTION_AUTHORITY:
            raise TypeError("unauthorized_apply_candidate")
        if status == "unclassified_card":
            raise ValueError("source_specific_unclassified_status_forbidden")
        timestamp = format_import_timestamp(imported_at)
        return [
            self.identity, timestamp, self.source, self.source_record_id,
            self.transaction_date, self.merchant, self.amount_yen,
            self.payment_method, status, target_id, self.source_hash,
            self.memo,
        ]


def production_import_status(
    candidate: CanonicalApplyCandidate, *, amazon_status: str | None = None,
) -> str:
    """Classify one authorized candidate without promoting review cases."""
    if candidate.transaction_kind != "purchase" or candidate.amount_yen <= 0:
        raise ValueError("candidate_not_materializable")
    if is_aupay_charge_merchant(candidate.merchant):
        return "transfer_aupay_charge"
    if is_amazon_merchant(candidate.merchant):
        allowed = {"matched_amazon", "amazon_needs_review", "amazon_unmatched"}
        if amazon_status not in allowed:
            raise ValueError("amazon_classification_required")
        return amazon_status
    if candidate.cross_source_state == "cross_source_strong_match":
        return "matched_receipt"
    if candidate.cross_source_state != "cross_source_no_match":
        raise ValueError("candidate_cross_source_review_required")
    return "auto_expense"


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
        schema_version=APPLY_PLAN_SCHEMA_VERSION,
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


def validate_canonical_apply_plan(value: object) -> CanonicalApplyPlan:
    """Revalidate the complete, internally-issued executor authority envelope."""
    if type(value) is not CanonicalApplyPlan:
        raise TypeError("canonical_apply_plan_required")
    if getattr(value, "_authority", None) is not _PROJECTION_AUTHORITY:
        raise TypeError("unauthorized_apply_plan")
    if value.schema_version != APPLY_PLAN_SCHEMA_VERSION:
        raise RuntimeError("apply_plan_schema_invalid")
    if not value.executable:
        raise RuntimeError("apply_plan_blocked")
    if not value.canonical_accounting_valid:
        raise RuntimeError("apply_plan_accounting_invalid")
    if any(
        getattr(candidate, "_authority", None) is not _PROJECTION_AUTHORITY
        for candidate in value.candidates
    ):
        raise TypeError("unauthorized_apply_candidate")
    if any(
        candidate.schema_version != APPLY_CANDIDATE_SCHEMA_VERSION
        for candidate in value.candidates
    ):
        raise RuntimeError("apply_candidate_schema_invalid")

    decisions_by_identity = {
        decision.canonical_identity: decision for decision in value.item_decisions
    }
    if len(decisions_by_identity) != len(value.item_decisions):
        raise RuntimeError("apply_plan_duplicate_decision_identity")
    all_source_identities = [
        identity
        for decision in value.item_decisions
        for identity in decision.source_identities
    ]
    if (
        len(all_source_identities) != len(set(all_source_identities))
        or any(
            decision.canonical_identity not in decision.source_identities
            for decision in value.item_decisions
        )
    ):
        raise RuntimeError("apply_plan_source_identity_manifest_invalid")
    candidate_identities = [candidate.identity for candidate in value.candidates]
    if len(candidate_identities) != len(set(candidate_identities)):
        raise RuntimeError("apply_plan_duplicate_candidate_identity")
    eligible_identities = {
        identity for identity, decision in decisions_by_identity.items()
        if decision.status == "eligible"
    }
    if eligible_identities != set(candidate_identities):
        raise RuntimeError("apply_plan_candidate_manifest_mismatch")
    if any(
        candidate.transaction_kind != "purchase" or candidate.amount_yen <= 0
        for candidate in value.candidates
    ):
        raise RuntimeError("apply_plan_candidate_ineligible")
    if any(
        not re.fullmatch(r"[0-9a-f]{64}", candidate.source_hash)
        for candidate in value.candidates
    ):
        raise RuntimeError("apply_plan_candidate_source_hash_invalid")

    for candidate in value.candidates:
        decision = decisions_by_identity[candidate.identity]
        if (
            decision.status != "eligible"
            or decision.transaction_kind != candidate.transaction_kind
            or decision.business_fingerprint != candidate.business_fingerprint
            or decision.source_identities != candidate.source_identities
            or decision.cross_source_state != candidate.cross_source_state
        ):
            raise RuntimeError("apply_plan_candidate_manifest_mismatch")

    status_counts = dict(value.item_status_counts)
    accounted = sum(status_counts.values()) == value.canonical_transaction_count
    expected_withheld = {
        "eligible": value.eligible_canonical_count,
        "withheld_return": value.withheld_return_count,
        "withheld_cross_source_ambiguous": value.withheld_ambiguous_count,
        "duplicate_existing_identity": value.duplicate_existing_identity_count,
        "needs_review": value.withheld_review_count,
        "invalid": value.invalid_item_count,
        "withheld_global_failure": value.global_withheld_count,
    }
    counts_match = all(
        status_counts.get(status, 0) == expected
        for status, expected in expected_withheld.items()
    )
    expected_status = "ready_with_withheld" if (
        len(value.candidates) != len(value.item_decisions)
        or value.withheld_review_count
        or value.parser_review_line_item_count
    ) else "ready"
    decision_summaries_match = (
        value.return_transaction_count == sum(
            decision.transaction_kind == "return" for decision in value.item_decisions
        )
        and value.cross_source_strong_match == sum(
            decision.cross_source_state == "cross_source_strong_match"
            for decision in value.item_decisions
        )
        and value.cross_source_ambiguous == sum(
            decision.cross_source_state == "cross_source_ambiguous"
            for decision in value.item_decisions
        )
        and value.cross_source_no_match == sum(
            decision.cross_source_state == "cross_source_no_match"
            for decision in value.item_decisions
        )
        and value.existing_identity_duplicate_count
        == value.duplicate_existing_identity_count
    )
    if (
        not accounted
        or not counts_match
        or not decision_summaries_match
        or len(value.candidates) != value.eligible_canonical_count
        or value.blocked_reasons
        or value.status != expected_status
    ):
        raise RuntimeError("apply_plan_accounting_invalid")
    return value


def project_one_candidate_plan(
    source_plan: CanonicalApplyPlan, candidate_identity: str,
) -> CanonicalApplyPlan:
    """Publicly derive an exact-one executable plan from a validated full plan.

    The selected item must already be an eligible candidate.  Withheld, review,
    invalid, missing, and duplicate identities fail closed; the source plan is
    never modified.
    """
    validate_canonical_apply_plan(source_plan)
    matches = tuple(
        candidate for candidate in source_plan.candidates
        if candidate.identity == candidate_identity
    )
    if len(matches) != 1:
        decision_matches = tuple(
            decision for decision in source_plan.item_decisions
            if decision.canonical_identity == candidate_identity
        )
        if decision_matches:
            raise RuntimeError("apply_plan_candidate_not_eligible")
        raise RuntimeError("apply_plan_candidate_not_found")
    candidate = matches[0]
    decision = next(
        decision for decision in source_plan.item_decisions
        if decision.canonical_identity == candidate_identity
    )
    projected = CanonicalApplyPlan._create(
        authority=_PROJECTION_AUTHORITY,
        schema_version=APPLY_PLAN_SCHEMA_VERSION,
        status="ready",
        candidates=(candidate,),
        item_decisions=(decision,),
        blocked_reasons=(),
        raw_transaction_count=len(decision.source_identities),
        canonical_transaction_count=1,
        noncanonical_resend_count=len(decision.source_identities) - 1,
        eligible_canonical_count=1,
        withheld_ambiguous_count=0,
        parser_review_count=0,
        parser_review_line_item_count=0,
        reconciliation_review_count=0,
        rejected_transaction_count=0,
        identity_collision_count=0,
        cross_source_strong_match=int(
            decision.cross_source_state == "cross_source_strong_match"
        ),
        cross_source_ambiguous=0,
        cross_source_no_match=int(
            decision.cross_source_state == "cross_source_no_match"
        ),
        existing_identity_duplicate_count=0,
        return_transaction_count=0,
        withheld_return_count=0,
        duplicate_existing_identity_count=0,
        withheld_review_count=0,
        invalid_item_count=0,
        global_withheld_count=0,
        item_withheld_reason_counts=(),
        canonical_accounting_valid=True,
    )
    return validate_canonical_apply_plan(projected)


def require_executable_apply_plan(value: object, *, apply: bool) -> CanonicalApplyPlan:
    """Future writer guard: require validated authority and explicit consent."""
    plan = validate_canonical_apply_plan(value)
    if not apply:
        raise RuntimeError("explicit_apply_required")
    return plan
