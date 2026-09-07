"""Synthetic contract tests for the read-only Level 2 promotion gate."""
import ast
from dataclasses import replace
from pathlib import Path

from app.medical_payment_level2_promotion import (
    Level2PromotionEvidence,
    NegativeSafetyEvidence,
    PromotionReadiness,
    evaluate_level2_production_readiness,
)


COMPLETE_NEGATIVE_SAFETY = NegativeSafetyEvidence(
    unresolved_or_blocked_competitor=True,
    ambiguity=True,
    uncertain_relation=True,
    multiple_strong_labels=True,
    malformed_numeric=True,
    negative_context=True,
    insufficient_ocr_evidence=True,
)


def complete_evidence(**changes):
    evidence = Level2PromotionEvidence(
        evaluated_count=3,
        real_safe_positive_count=1,
        safe_unresolved_count=2,
        competitor_rejection_evidence_count=2,
        stability_evidence_count=1,
        negative_safety=COMPLETE_NEGATIVE_SAFETY,
    )
    return replace(evidence, **changes)


def decide(**changes):
    return evaluate_level2_production_readiness(complete_evidence(**changes))


def test_zero_real_safe_positive_can_never_be_ready():
    result = decide(real_safe_positive_count=0)
    assert result.status is PromotionReadiness.INSUFFICIENT_EVIDENCE
    assert "no_real_safe_positive_evidence" in result.reason_codes


def test_synthetic_positive_does_not_replace_real_evaluation_evidence():
    result = decide(real_safe_positive_count=0, synthetic_safe_positive_count=10)
    assert result.status is not PromotionReadiness.READY
    assert "no_real_safe_positive_evidence" in result.reason_codes


def test_unresolved_competitor_safety_invariant_can_never_be_ready():
    result = decide(unresolved_safety_invariant_count=1)
    assert result.status is PromotionReadiness.NOT_READY
    assert result.reason_codes == ("unresolved_safety_invariant",)


def test_unsafe_positive_can_never_be_ready():
    result = decide(unsafe_positive_count=1, evaluated_count=4)
    assert result.status is PromotionReadiness.NOT_READY
    assert result.reason_codes == ("unsafe_positive_observed",)


def test_evaluation_failure_can_never_be_ready():
    result = decide(evaluation_failure_count=1, evaluated_count=4)
    assert result.status is PromotionReadiness.NOT_READY
    assert result.reason_codes == ("evaluation_failure_observed",)


def test_complete_synthetic_contract_scenario_can_be_ready():
    result = decide()
    assert result.status is PromotionReadiness.READY
    assert result.reason_codes == ()


def test_safe_unresolved_receipts_are_not_failures_when_evidence_is_complete():
    assert decide(safe_unresolved_count=2).status is PromotionReadiness.READY


def test_missing_negative_or_stability_evidence_is_insufficient():
    missing_negative = decide(negative_safety=replace(
        COMPLETE_NEGATIVE_SAFETY, malformed_numeric=False))
    missing_stability = decide(stability_evidence_count=0)
    assert missing_negative.status is PromotionReadiness.INSUFFICIENT_EVIDENCE
    assert missing_stability.status is PromotionReadiness.INSUFFICIENT_EVIDENCE
    assert missing_negative.reason_codes == ("insufficient_evaluation_coverage",)


def test_unproven_competitor_rejection_is_insufficient():
    result = decide(competitor_rejection_evidence_count=0)
    assert result.status is PromotionReadiness.INSUFFICIENT_EVIDENCE
    assert result.reason_codes == ("insufficient_evaluation_coverage",)


def test_production_isolation_violation_can_never_be_ready():
    result = decide(production_isolation_violation_count=1)
    assert result.status is PromotionReadiness.NOT_READY
    assert result.reason_codes == ("production_isolation_violation",)


def test_invalid_aggregate_fails_closed():
    result = decide(evaluated_count=-1)
    assert result.status is PromotionReadiness.INSUFFICIENT_EVIDENCE
    assert result.reason_codes == ("insufficient_evaluation_coverage",)


def test_gate_output_and_aggregate_cannot_carry_payment_data():
    evidence = complete_evidence()
    result = evaluate_level2_production_readiness(evidence)
    assert not hasattr(result, "amount")
    assert set(evidence.aggregate()) == {
        "evaluated_count", "safe_positive", "safe_unresolved", "unsafe_positive",
        "evaluation_failure", "competitor_rejection_evidence", "stability_evidence",
    }


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    }


def test_gate_has_no_shadow_or_production_authority_dependency():
    root = Path(__file__).resolve().parents[1]
    imports = _imports(root / "app" / "medical_payment_level2_promotion.py")
    assert imports <= {"__future__", "dataclasses", "enum"}


def test_no_other_app_module_imports_promotion_gate():
    root = Path(__file__).resolve().parents[1] / "app"
    for path in root.glob("*.py"):
        if path.name != "medical_payment_level2_promotion.py":
            assert "medical_payment_level2_promotion" not in path.read_text(encoding="utf-8")
