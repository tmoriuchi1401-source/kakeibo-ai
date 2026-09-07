"""Read-only production-promotion readiness gate for the Level 2 shadow.

The gate consumes only privacy-safe, system-level evidence counts.  It cannot
return a payment amount, inspect receipt content, or grant production authority.
Receipt-local abstention and safely rejected competitors are evidence outcomes,
not evaluation failures.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum


class PromotionReadiness(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class NegativeSafetyEvidence:
    """Whether every required unsafe-positive family was safely rejected."""

    unresolved_or_blocked_competitor: bool = False
    ambiguity: bool = False
    uncertain_relation: bool = False
    multiple_strong_labels: bool = False
    malformed_numeric: bool = False
    negative_context: bool = False
    insufficient_ocr_evidence: bool = False

    @property
    def complete(self) -> bool:
        return all(getattr(self, item.name) is True for item in fields(self))


@dataclass(frozen=True)
class Level2PromotionEvidence:
    """Data-minimized aggregate; counts contain no receipt identifiers or text."""

    evaluated_count: int
    real_safe_positive_count: int
    synthetic_safe_positive_count: int = 0
    safe_unresolved_count: int = 0
    unsafe_positive_count: int = 0
    evaluation_failure_count: int = 0
    competitor_rejection_evidence_count: int = 0
    stability_evidence_count: int = 0
    unresolved_safety_invariant_count: int = 0
    production_isolation_violation_count: int = 0
    negative_safety: NegativeSafetyEvidence = field(default_factory=NegativeSafetyEvidence)

    def aggregate(self) -> dict[str, int]:
        """Return the privacy-safe fields used in readiness reporting."""
        return {
            "evaluated_count": self.evaluated_count,
            "safe_positive": self.real_safe_positive_count,
            "safe_unresolved": self.safe_unresolved_count,
            "unsafe_positive": self.unsafe_positive_count,
            "evaluation_failure": self.evaluation_failure_count,
            "competitor_rejection_evidence": self.competitor_rejection_evidence_count,
            "stability_evidence": self.stability_evidence_count,
        }


@dataclass(frozen=True)
class Level2PromotionDecision:
    status: PromotionReadiness
    reason_codes: tuple[str, ...]


_COUNT_FIELDS = tuple(
    item.name for item in fields(Level2PromotionEvidence)
    if item.name != "negative_safety"
)


def _valid_evidence(evidence: object) -> bool:
    if type(evidence) is not Level2PromotionEvidence:
        return False
    if type(evidence.negative_safety) is not NegativeSafetyEvidence:
        return False
    counts = tuple(getattr(evidence, name) for name in _COUNT_FIELDS)
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    classified_real_outcomes = (
        evidence.real_safe_positive_count
        + evidence.safe_unresolved_count
        + evidence.unsafe_positive_count
        + evidence.evaluation_failure_count
    )
    return classified_real_outcomes <= evidence.evaluated_count


def evaluate_level2_production_readiness(
    evidence: Level2PromotionEvidence,
) -> Level2PromotionDecision:
    """Fail closed unless all system-level safety evidence is present.

    Synthetic positives exercise this contract but never substitute for a real
    safe-positive evaluation.  A safe unresolved receipt does not itself add a
    failure reason; an unresolved *system safety invariant* does.
    """
    if not _valid_evidence(evidence):
        return Level2PromotionDecision(
            PromotionReadiness.INSUFFICIENT_EVIDENCE,
            ("insufficient_evaluation_coverage",),
        )

    reasons: list[str] = []
    hard_failure = False
    if evidence.unsafe_positive_count:
        reasons.append("unsafe_positive_observed")
        hard_failure = True
    if evidence.evaluation_failure_count:
        reasons.append("evaluation_failure_observed")
        hard_failure = True
    if evidence.unresolved_safety_invariant_count:
        reasons.append("unresolved_safety_invariant")
        hard_failure = True
    if evidence.production_isolation_violation_count:
        reasons.append("production_isolation_violation")
        hard_failure = True

    if evidence.real_safe_positive_count == 0:
        reasons.append("no_real_safe_positive_evidence")

    coverage_complete = (
        evidence.evaluated_count > 0
        and evidence.competitor_rejection_evidence_count > 0
        and evidence.stability_evidence_count > 0
        and evidence.negative_safety.complete
    )
    if not coverage_complete:
        reasons.append("insufficient_evaluation_coverage")

    if hard_failure:
        status = PromotionReadiness.NOT_READY
    elif reasons:
        status = PromotionReadiness.INSUFFICIENT_EVIDENCE
    else:
        status = PromotionReadiness.READY
    return Level2PromotionDecision(status, tuple(reasons))
