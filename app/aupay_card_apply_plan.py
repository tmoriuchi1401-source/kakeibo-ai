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


@dataclass(frozen=True, init=False)
class CanonicalApplyCandidate:
    schema_version: int
    identity: str
    source: str
    source_record_id: str
    transaction_date: str
    merchant: str
    amount_yen: int
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
    def _from_canonical(cls, item: ReconciledTransaction) -> "CanonicalApplyCandidate":
        tx = item.canonical
        candidate = object.__new__(cls)
        values = {
            "schema_version": 1,
            "identity": tx.identity,
            "source": tx.source,
            "source_record_id": tx.source_record_id,
            "transaction_date": tx.transaction_date,
            "merchant": tx.merchant,
            "amount_yen": tx.amount_yen,
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
    blocked_reasons: tuple[str, ...]
    raw_transaction_count: int
    canonical_transaction_count: int
    noncanonical_resend_count: int
    eligible_canonical_count: int
    withheld_ambiguous_count: int
    parser_review_count: int
    reconciliation_review_count: int
    rejected_transaction_count: int
    identity_collision_count: int
    cross_source_strong_match: int
    cross_source_ambiguous: int
    cross_source_no_match: int
    existing_identity_duplicate_count: int
    _authority: object

    def __new__(cls, *args, **kwargs):
        raise TypeError("canonical_apply_plan_is_builder_only")

    @classmethod
    def _create(cls, **values) -> "CanonicalApplyPlan":
        plan = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(plan, name, value)
        object.__setattr__(plan, "_authority", _PROJECTION_AUTHORITY)
        return plan

    @property
    def executable(self) -> bool:
        return self.status == "ready"

    def summary(self) -> dict:
        return {
            "apply_plan_status": self.status,
            "apply_plan_blocked_reasons": list(self.blocked_reasons),
            "apply_candidate_count": len(self.candidates),
            "eligible_canonical_count": self.eligible_canonical_count,
            "withheld_ambiguous_count": self.withheld_ambiguous_count,
            "raw_transaction_count": self.raw_transaction_count,
            "canonical_transaction_count": self.canonical_transaction_count,
            "noncanonical_resend_count": self.noncanonical_resend_count,
            "parser_review_count": self.parser_review_count,
            "reconciliation_review_count": self.reconciliation_review_count,
            "rejected_transaction_count": self.rejected_transaction_count,
            "identity_collision_count": self.identity_collision_count,
            "cross_source_strong_match": self.cross_source_strong_match,
            "cross_source_ambiguous": self.cross_source_ambiguous,
            "cross_source_no_match": self.cross_source_no_match,
            "existing_identity_duplicate_count": self.existing_identity_duplicate_count,
        }


def build_canonical_apply_plan(collection: dict, reconciliation: dict) -> CanonicalApplyPlan:
    """Build an immutable plan, exposing no candidates unless every gate passes."""
    summary = reconciliation["summary"]
    items = tuple(reconciliation["transactions"])
    reasons: list[str] = []

    if not collection.get("collection_complete", False):
        reasons.append("collection_incomplete")
    if collection.get("gmail_list_failed", 0):
        reasons.append("gmail_list_failure")
    if collection.get("gmail_read_failed", 0):
        reasons.append("gmail_read_failure")
    if collection.get("collection_truncated", False):
        reasons.append("collection_truncated")
    parser_review = int(collection.get("needs_review", 0))
    if parser_review:
        reasons.append("parser_review_present")
    collisions = int(summary.get("identity_collisions", 0))
    if collisions:
        reasons.append("identity_collision")
    reconciliation_review = int(summary.get("needs_review", 0))
    if reconciliation_review:
        reasons.append("reconciliation_review_present")

    raw_count = int(summary.get("raw_transactions", 0))
    rejected = int(summary.get("rejected", 0))
    if rejected:
        reasons.append("reconciliation_rejected_transaction")
    same_source_duplicates = int(summary.get("same_source_duplicates", 0))
    represented = sum(len(item.source_identities) for item in items)
    source_ids = [identity for item in items for identity in item.source_identities]
    reconciliation_valid = (
        collisions == 0
        and represented + same_source_duplicates + rejected == raw_count
        and len(source_ids) == len(set(source_ids))
        and all(item.canonical.identity in item.source_identities for item in items)
        and all(item.state in {"new", "probable_resend"} for item in items)
        and sum(len(item.source_identities) - 1 for item in items)
        == int(summary.get("probable_resend_records", 0))
    )
    if not reconciliation_valid:
        reasons.append("reconciliation_inconsistent")

    ambiguous = tuple(
        item for item in items
        if item.cross_source.state == "cross_source_ambiguous"
    )
    if ambiguous:
        reasons.append("cross_source_ambiguous")

    eligible = tuple(
        CanonicalApplyCandidate._from_canonical(item)
        for item in items
        if item.cross_source.state != "cross_source_ambiguous"
        and not item.existing_source_identities
    )
    blocked_reasons = tuple(dict.fromkeys(reasons))
    candidates = () if blocked_reasons else eligible
    return CanonicalApplyPlan._create(
        schema_version=1,
        status="blocked" if blocked_reasons else "ready",
        candidates=candidates,
        blocked_reasons=blocked_reasons,
        raw_transaction_count=raw_count,
        canonical_transaction_count=len(items),
        noncanonical_resend_count=int(summary.get("probable_resend_records", 0)),
        eligible_canonical_count=len(eligible),
        withheld_ambiguous_count=len(ambiguous),
        parser_review_count=parser_review,
        reconciliation_review_count=reconciliation_review,
        rejected_transaction_count=rejected,
        identity_collision_count=collisions,
        cross_source_strong_match=int(summary.get("cross_source_strong_match", 0)),
        cross_source_ambiguous=int(summary.get("cross_source_ambiguous", 0)),
        cross_source_no_match=int(summary.get("cross_source_no_match", 0)),
        existing_identity_duplicate_count=int(
            summary.get("existing_identity_duplicates", 0)
        ),
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
    if any(candidate._authority is not _PROJECTION_AUTHORITY for candidate in value.candidates):
        raise TypeError("unauthorized_apply_candidate")
    return value
