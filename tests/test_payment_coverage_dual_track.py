from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

import app.payment_coverage_dual_track as dual_track
from app.coverage_confirmation import COVERAGE_REASON_OPERATIONAL_ONLY
from app.payment_coverage_dual_track import evaluate_payment_coverage_dual_track
from app.payment_coverage_manifest import CoverageManifest
from app.payment_coverage_status_preview import CoverageEvidence


START = date(2025, 1, 1)
END = date(2025, 1, 31)
SHA_A = "a" * 64


def _manifest(
    start: str = "2025-01-01",
    end: str = "2025-01-31",
    *,
    source: str = "paypay",
    basis: str = "transaction_date",
    provider_proven: bool = True,
) -> CoverageManifest:
    return CoverageManifest(
        source=source,
        coverage_start=start,
        coverage_end=end,
        coverage_basis=basis,
        evidence_type=("provider_verified" if provider_proven else "csv_file"),
        content_hash=SHA_A,
        completeness_reason=(
            "provider_completeness_verified"
            if provider_proven
            else COVERAGE_REASON_OPERATIONAL_ONLY
        ),
        completeness_proven=provider_proven,
        operational_coverage="usable",
        operational_reason="operational_checks_passed",
    )


def _dual(
    legacy,
    manifests=(),
    *,
    required=("paypay",),
    basis="transaction_date",
    start=START,
    end=END,
):
    return evaluate_payment_coverage_dual_track(
        legacy,
        manifests=manifests,
        required_providers=required,
        coverage_basis=basis,
        required_start=start,
        required_end=end,
    )


def test_legacy_only_keeps_exact_result_and_does_not_call_strict(monkeypatch) -> None:
    legacy = {"overall_payment_coverage_status": "incomplete"}

    def unexpected(*args, **kwargs):
        raise AssertionError("strict service called")

    monkeypatch.setattr(dual_track, "evaluate_strict_payment_coverage", unexpected)

    result = evaluate_payment_coverage_dual_track(legacy)

    assert result.legacy_result is legacy
    assert result.strict_result.evaluation_status == "not_evaluated"
    assert result.strict_result.coverage_status is None
    assert result.strict_result.reason == "required_providers_not_supplied"


def test_required_provider_omission_never_infers_strict_completion(monkeypatch) -> None:
    calls = 0

    def unexpected(*args, **kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(dual_track, "evaluate_strict_payment_coverage", unexpected)

    result = evaluate_payment_coverage_dual_track(
        {"legacy": "complete"},
        manifests=[_manifest()],
        coverage_basis="transaction_date",
        required_start=START,
        required_end=END,
    )

    assert calls == 0
    assert result.strict_result.coverage_status is None


def test_paypay_user_attested_is_strict_unknown_without_changing_legacy() -> None:
    legacy = {"overall_payment_coverage_status": "incomplete"}
    result = _dual(legacy, [_manifest(provider_proven=False)])

    assert result.legacy_result is legacy
    assert result.legacy_result["overall_payment_coverage_status"] == "incomplete"
    assert result.strict_result.evaluation_status == "evaluated"
    assert result.strict_result.coverage_status == "unknown"


def test_provider_proven_fixture_is_strict_complete_and_separate() -> None:
    legacy = {"overall_payment_coverage_status": "unknown"}
    result = _dual(legacy, [_manifest()])

    assert result.legacy_result["overall_payment_coverage_status"] == "unknown"
    assert result.strict_result.coverage_status == "complete"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.status == "complete"


def test_provider_proof_gap_is_strict_incomplete_without_overwriting_legacy() -> None:
    legacy = {"overall_payment_coverage_status": "complete"}
    result = _dual(legacy, [_manifest(end="2025-01-10")])

    assert result.legacy_result["overall_payment_coverage_status"] == "complete"
    assert result.strict_result.coverage_status == "incomplete"


def test_legacy_incomplete_evidence_is_not_reused_as_strict_incomplete() -> None:
    legacy = CoverageEvidence(
        source="paypay",
        explicitly_incomplete=True,
        completeness_reason="legacy_search_limit",
    )
    result = _dual(legacy, [_manifest()])

    assert result.legacy_result is legacy
    assert result.strict_result.coverage_status == "complete"


def test_missing_strict_window_fails_closed_without_calling_service(monkeypatch) -> None:
    legacy = {"legacy": "unchanged"}

    def unexpected(*args, **kwargs):
        raise AssertionError("strict service called")

    monkeypatch.setattr(dual_track, "evaluate_strict_payment_coverage", unexpected)

    result = _dual(legacy, [_manifest()], start=None)

    assert result.legacy_result is legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.reason == "strict_inputs_incomplete"
    assert result.strict_result.diagnostic == "missing_strict_inputs=required_start"


def test_missing_basis_fails_closed_without_inferring_from_manifest() -> None:
    result = _dual({"legacy": "complete"}, [_manifest()], basis=None)

    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.diagnostic == "missing_strict_inputs=coverage_basis"


def test_invalid_window_is_captured_as_strict_failure() -> None:
    legacy = {"overall_payment_coverage_status": "complete"}
    result = _dual(
        legacy,
        [_manifest()],
        start=date(2025, 2, 1),
        end=date(2025, 1, 31),
    )

    assert result.legacy_result is legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert "invalid_required_window" in (result.strict_result.diagnostic or "")


def test_strict_service_exception_is_diagnostic_and_legacy_survives(monkeypatch) -> None:
    legacy = {"overall_payment_coverage_status": "incomplete"}

    def fail(*args, **kwargs):
        raise RuntimeError("provider evaluation unavailable")

    monkeypatch.setattr(dual_track, "evaluate_strict_payment_coverage", fail)

    result = _dual(legacy, [_manifest()])

    assert result.legacy_result is legacy
    assert result.legacy_result["overall_payment_coverage_status"] == "incomplete"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.reason == "strict_evaluation_failed"
    assert result.strict_result.diagnostic == (
        "RuntimeError: provider evaluation unavailable"
    )


def test_explicit_empty_required_set_uses_strict_not_applicable_semantics() -> None:
    result = _dual(
        {"legacy": "unknown"},
        [_manifest()],
        required=(),
    )

    assert result.strict_result.evaluation_status == "evaluated"
    assert result.strict_result.coverage_status == "not_applicable"


def test_missing_required_provider_is_unknown_not_not_applicable() -> None:
    result = _dual(
        {"legacy": "unknown"},
        [],
        required=("paypay",),
    )

    assert result.strict_result.evaluation_status == "evaluated"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.reason == "required_provider_result_missing"


def test_basis_mismatch_is_missing_required_provider_unknown() -> None:
    result = _dual(
        {"legacy": "complete"},
        [_manifest(basis="message_date")],
        basis="transaction_date",
    )

    assert result.strict_result.evaluation_status == "evaluated"
    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.reason == "required_provider_result_missing"


def test_legacy_evidence_passed_as_manifest_fails_without_breaking_legacy() -> None:
    legacy = CoverageEvidence(source="paypay", explicitly_incomplete=True)

    result = _dual(legacy, [legacy])

    assert result.legacy_result is legacy
    assert result.strict_result.evaluation_status == "failed"
    assert result.strict_result.coverage_status == "unknown"
    assert "invalid_coverage_manifest_type" in (
        result.strict_result.diagnostic or ""
    )


@pytest.mark.parametrize("source", ["imported_data", "amazon_gmail"])
def test_non_required_source_is_not_normalized_to_required_provider(source) -> None:
    result = _dual(
        {"legacy": "complete"},
        [_manifest(source=source)],
        required=("amazon",),
    )

    assert result.strict_result.coverage_status == "unknown"
    assert result.strict_result.overall_result is not None
    assert result.strict_result.overall_result.required_providers == ("amazon",)
    assert result.strict_result.overall_result.reason == "required_provider_result_missing"


def test_legacy_result_is_not_mutated() -> None:
    legacy = {
        "source_coverage": [{"source": "paypay", "coverage_status": "incomplete"}],
        "overall_payment_coverage_status": "incomplete",
    }
    before = deepcopy(legacy)

    result = _dual(legacy, [_manifest()])

    assert legacy == before
    assert result.legacy_result is legacy


def test_bridge_delegates_strict_truth_to_existing_service(monkeypatch) -> None:
    calls = []
    original = dual_track.evaluate_strict_payment_coverage

    def spy(manifests, **kwargs):
        manifest_values = tuple(manifests)
        calls.append((manifest_values, kwargs))
        return original(manifest_values, **kwargs)

    monkeypatch.setattr(dual_track, "evaluate_strict_payment_coverage", spy)

    result = _dual({"legacy": "unknown"}, [_manifest()])

    assert result.strict_result.coverage_status == "complete"
    assert len(calls) == 1
    assert calls[0][1] == {
        "required_providers": ("paypay",),
        "coverage_basis": "transaction_date",
        "required_start": START,
        "required_end": END,
    }
