import json

import pytest

import app.medical_layout_evaluation as evaluation
from app.medical_layout_shadow import PageFrame
from app.medical_payment_evidence import collect_payment_evidence, resolve_payment_evidence
from app.medical_receipt_privacy import _StructuredOcrToken as Token


def tokens(label="支払額", competitor=None):
    result = (Token(label, 1, 20, 20, 80, 20, 96, (1, 1, 1, 5)),
              Token("1200円", 1, 120, 20, 80, 20, 96, (1, 1, 1, 5)))
    return result if competitor is None else result + (
        Token(competitor, 1, 220, 20, 80, 20, 69, (1, 1, 1, 5)),)


def evaluate(text="支払額 1200円", observed=None, complete=True):
    return evaluation.evaluate_medical_layout(text, tokens() if observed is None else observed,
        (PageFrame(1, 600, 800),), expected_pages=1, observation_complete=complete)


def test_same_observation_group_is_comparison_not_consensus():
    result = evaluate()
    assert result["evaluation_failed"] == 0
    assert result["ocr_observation_groups"] == 1
    assert result["production_confirmed"] == 1
    assert result["shadow_unresolved_hypotheses"] == 1
    assert not any("amount" == key or "consensus" in key for key in result)


@pytest.mark.parametrize("competitor", ["3400", "3.400", "???円"])
def test_shared_observation_keeps_production_and_shadow_uncertainty(competitor):
    observed = tokens(competitor=competitor)
    baseline = resolve_payment_evidence(collect_payment_evidence("支払額 1200", observed))
    result = evaluate(observed=observed)
    assert baseline.status == "needs_review"
    assert result["production_confirmed"] == 0
    assert result["production_needs_review"] == 1
    assert result["production_amount_observation_low_confidence"] == 1
    assert result["shadow_numeric_regions"] == 2
    assert result["shadow_competing_hypotheses"] == 2
    assert resolve_payment_evidence(collect_payment_evidence("支払額 1200", observed)) == baseline


def test_missing_strong_label_has_geometry_but_never_promotes_production():
    result = evaluate("支□総□ 1200", tokens("支□総□"))
    assert result["production_confirmed"] == 0
    assert result["production_payment_label_not_observed"] == 1
    assert result["shadow_unresolved_hypotheses"] == 1


@pytest.mark.parametrize("observed,complete", [((), True), (tokens(), False)])
def test_empty_or_incomplete_input_does_not_publish_partial_success(observed, complete):
    result = evaluate(observed=observed, complete=complete)
    assert result["evaluation_failed"] == 1
    assert result["shadow_observation_incomplete"] == 1
    assert result["production_confirmed"] == result["production_needs_review"] == 0


def test_exception_and_input_details_never_escape(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION 987654")
    monkeypatch.setattr(evaluation, "collect_payment_evidence", fail)
    result = evaluate("SYNTHETIC_PRIVATE_INPUT")
    assert result["evaluation_failed"] == 1
    assert not any(v for k, v in result.items() if k not in {"evaluation_failed", "ocr_observation_groups"})
    assert "PRIVATE" not in json.dumps(result)
    assert capsys.readouterr() == ("", "")


def test_export_schema_is_fixed_for_success_failure_and_ocr_content():
    results = (evaluate(), evaluate(observed=()), evaluate("SYNTHETIC_PRIVATE_INPUT", tokens("匿名")))
    assert len({tuple(sorted(r)) for r in results}) == 1
    assert all(type(v) is int for r in results for v in r.values())
    assert "PRIVATE" not in json.dumps(results)
    assert "1200" not in json.dumps(results)
