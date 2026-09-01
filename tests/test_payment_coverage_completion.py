from __future__ import annotations

import pytest

from app.payment_coverage_completion import (
    CoverageCompletionResult,
    NormalizedCoverageInterval,
    evaluate_payment_coverage_completion,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _interval(
    start: str = "2025-01-01",
    end: str = "2025-01-31",
    *,
    provider: str = "paypay",
    coverage_basis: str = "transaction_date",
    sha: str | None = SHA_A,
    operational: str = "usable",
    kind: str = "user_attested",
    claim: str | None = "export_scope",
    proven: bool = False,
    evidence_id: str | None = None,
    diagnostic: str | None = None,
    explicitly_incomplete: bool = False,
) -> NormalizedCoverageInterval:
    return NormalizedCoverageInterval(
        provider=provider,
        coverage_basis=coverage_basis,
        coverage_start=start,
        coverage_end=end,
        content_sha256=sha,
        operational_coverage=operational,
        attestation_kind=kind,
        attestation_claim=claim,
        provider_completeness_proven=proven,
        evidence_id=evidence_id,
        diagnostic=diagnostic,
        explicitly_incomplete=explicitly_incomplete,
    )


def _provider_interval(
    start: str = "2025-01-01",
    end: str = "2025-01-31",
    *,
    provider: str = "paypay",
    sha: str | None = SHA_A,
    operational: str = "usable",
    claim: str = "complete_for_range",
    evidence_id: str | None = None,
) -> NormalizedCoverageInterval:
    return _interval(
        start,
        end,
        provider=provider,
        sha=sha,
        operational=operational,
        kind="provider_proven",
        claim=claim,
        proven=True,
        evidence_id=evidence_id,
    )


def _evaluate(
    intervals: list[NormalizedCoverageInterval],
    *,
    start: str | None = "2025-01-01",
    end: str | None = "2025-01-31",
    provider: str = "paypay",
    basis: str = "transaction_date",
) -> CoverageCompletionResult:
    return evaluate_payment_coverage_completion(
        intervals,
        provider=provider,
        coverage_basis=basis,
        required_start=start,
        required_end=end,
    )


def test_case_01_user_export_scope_is_unknown_without_provider_proof() -> None:
    result = _evaluate([_interval()])

    assert result.completion_status == "unknown"
    assert result.reason == "provider_completeness_not_proven"
    assert result.effective_proof_level == "user_attested"
    assert result.completeness_proven is False


def test_case_02_filename_candidate_only_is_unknown() -> None:
    result = _evaluate([
        _interval(kind="none", claim=None, sha=None, diagnostic="filename_candidate"),
    ])

    assert result.completion_status == "unknown"
    assert result.reason == "provider_completeness_not_proven"


def test_case_03_user_confirmation_filename_conflict_is_unknown() -> None:
    result = _evaluate([
        _interval(operational="needs_confirmation", diagnostic="filename_range_conflict"),
    ])

    assert result.completion_status == "unknown"
    assert result.reason == "operational_evidence_not_usable"


def test_case_04_transaction_outside_confirmed_range_is_unknown() -> None:
    result = _evaluate([
        _interval(operational="rejected", diagnostic="transaction_outside_confirmed_range"),
    ])

    assert result.completion_status == "unknown"
    assert result.reason == "operational_evidence_not_usable"


def test_case_05_provider_proof_and_operational_success_is_complete() -> None:
    result = _evaluate([_provider_interval()])

    assert result.completion_status == "complete"
    assert result.reason == "provider_proven_coverage_complete"
    assert result.completeness_proven is True
    assert result.effective_proof_level == "provider_proven"


def test_case_06_provider_proof_with_operational_failure_is_unknown() -> None:
    result = _evaluate([_provider_interval(operational="rejected")])

    assert result.completion_status == "unknown"
    assert result.reason == "operational_evidence_not_usable"
    assert result.completeness_proven is False


def test_case_07_same_sha_collapses_but_same_scope_different_sha_conflicts() -> None:
    duplicate_result = _evaluate([_provider_interval(), _provider_interval()])
    conflicting_result = _evaluate([
        _provider_interval(sha=SHA_A),
        _provider_interval(sha=SHA_B),
    ])

    assert duplicate_result.completion_status == "complete"
    assert duplicate_result.duplicate_collapse_count == 1
    assert len(duplicate_result.merged_intervals) == 1
    assert conflicting_result.completion_status == "unknown"
    assert [item.reason for item in conflicting_result.conflicts] == [
        "same_scope_evidence_conflict",
    ]


def test_case_08_provider_gap_is_incomplete_but_weak_fill_makes_it_unknown() -> None:
    provider_intervals = [
        _provider_interval("2025-01-01", "2025-01-31", sha=SHA_A),
        _provider_interval("2025-03-01", "2025-03-31", sha=SHA_C),
    ]
    incomplete = _evaluate(provider_intervals, end="2025-03-31")
    mixed = _evaluate(
        provider_intervals
        + [_interval("2025-02-01", "2025-02-28", sha=SHA_B)],
        end="2025-03-31",
    )

    assert incomplete.completion_status == "incomplete"
    assert incomplete.reason == "provider_proven_coverage_gap"
    assert incomplete.gaps[0].coverage_start == "2025-02-01"
    assert incomplete.gaps[0].coverage_end == "2025-02-28"
    assert mixed.completion_status == "unknown"
    assert mixed.reason == "required_window_not_fully_provider_proven"


def test_case_09_compatible_overlap_merges() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-20", sha=SHA_A),
            _provider_interval("2025-01-15", "2025-01-31", sha=SHA_B),
        ],
    )

    assert result.completion_status == "complete"
    assert [(item.coverage_start, item.coverage_end) for item in result.merged_intervals] == [
        ("2025-01-01", "2025-01-31"),
    ]


def test_case_10_same_sha_physical_duplicates_are_one_logical_evidence() -> None:
    result = _evaluate([_interval(), _interval(), _interval()])

    assert result.duplicate_collapse_count == 2
    assert len(result.merged_intervals) == 1
    assert result.completion_status == "unknown"


def test_case_11_zero_transaction_user_export_scope_is_unknown() -> None:
    result = _evaluate([_interval(diagnostic="parsed_zero_transactions")])

    assert result.completion_status == "unknown"
    assert result.completeness_proven is False


def test_case_12_possible_non_use_is_not_a_no_activity_proof() -> None:
    result = _evaluate([
        _interval(kind="user_attested", claim="no_activity", sha=None),
    ])

    assert result.completion_status == "unknown"
    assert result.effective_proof_level == "user_attested"


def test_case_13_invalid_store_is_unknown() -> None:
    result = _evaluate([
        _interval(operational="rejected", diagnostic="coverage_confirmation_store_invalid"),
    ])

    assert result.completion_status == "unknown"
    assert result.reason == "operational_evidence_not_usable"


def test_case_14_lookup_failure_is_unknown() -> None:
    result = _evaluate([
        _interval(operational="rejected", diagnostic="coverage_confirmation_lookup_failed"),
    ])

    assert result.completion_status == "unknown"
    assert result.reason == "operational_evidence_not_usable"


def test_case_15_missing_csv_is_unknown() -> None:
    result = _evaluate([])

    assert result.completion_status == "unknown"
    assert result.reason == "matching_coverage_evidence_missing"


def test_case_16_exact_daily_adjacency_has_no_gap() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-15", sha=SHA_A),
            _provider_interval("2025-01-16", "2025-01-31", sha=SHA_B),
        ],
    )

    assert result.completion_status == "complete"
    assert result.gaps == ()
    assert len(result.merged_intervals) == 1


def test_case_17_one_day_gap_is_reported() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-15", sha=SHA_A),
            _provider_interval("2025-01-17", "2025-01-31", sha=SHA_B),
        ],
    )

    assert result.completion_status == "incomplete"
    assert [(gap.coverage_start, gap.coverage_end) for gap in result.gaps] == [
        ("2025-01-16", "2025-01-16"),
    ]


def test_case_18_multiple_day_gap_is_reported() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-10", sha=SHA_A),
            _provider_interval("2025-01-15", "2025-01-31", sha=SHA_B),
        ],
    )

    assert result.completion_status == "incomplete"
    assert [(gap.coverage_start, gap.coverage_end) for gap in result.gaps] == [
        ("2025-01-11", "2025-01-14"),
    ]


def test_case_19_left_required_window_shortage_is_incomplete() -> None:
    result = _evaluate([_provider_interval("2025-01-05", "2025-01-31")])

    assert result.completion_status == "incomplete"
    assert result.gaps[0].coverage_start == "2025-01-01"
    assert result.gaps[0].coverage_end == "2025-01-04"


def test_case_20_right_required_window_shortage_is_incomplete() -> None:
    result = _evaluate([_provider_interval("2025-01-01", "2025-01-27")])

    assert result.completion_status == "incomplete"
    assert result.gaps[0].coverage_start == "2025-01-28"
    assert result.gaps[0].coverage_end == "2025-01-31"


def test_case_21_required_window_fully_contained_is_complete() -> None:
    result = _evaluate([
        _provider_interval("2024-12-01", "2025-02-28"),
    ])

    assert result.completion_status == "complete"
    assert result.gaps == ()


def test_case_22_coverage_basis_mismatch_is_unknown() -> None:
    result = _evaluate([
        _provider_interval(),
    ], basis="message_date")

    assert result.completion_status == "unknown"
    assert result.reason == "matching_coverage_evidence_missing"


def test_case_23_provider_mismatch_is_unknown() -> None:
    result = _evaluate([_provider_interval(provider="other")])

    assert result.completion_status == "unknown"
    assert result.reason == "matching_coverage_evidence_missing"


def test_case_24_mixed_user_and_provider_proof_is_unknown() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-31", sha=SHA_A),
            _interval("2025-02-01", "2025-02-28", sha=SHA_B),
            _provider_interval("2025-03-01", "2025-03-31", sha=SHA_C),
        ],
        end="2025-03-31",
    )

    assert result.completion_status == "unknown"
    assert result.reason == "required_window_not_fully_provider_proven"
    assert result.completeness_proven is False
    assert result.effective_proof_level == "user_attested"


def test_case_25_provider_proven_overlap_is_merged() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-20", sha=SHA_A),
            _provider_interval("2025-01-10", "2025-01-31", sha=SHA_B),
        ],
    )

    assert result.completion_status == "complete"
    assert len(result.merged_intervals) == 1
    assert result.merged_intervals[0].effective_proof_level == "provider_proven"


def test_case_26_conflicting_overlap_is_unknown() -> None:
    result = _evaluate(
        [
            _provider_interval(
                "2025-01-01", "2025-01-20", sha=SHA_A, evidence_id="proof-1",
            ),
            _provider_interval(
                "2025-01-10", "2025-01-31", sha=SHA_B, evidence_id="proof-1",
            ),
        ],
    )

    assert result.completion_status == "unknown"
    assert result.reason == "conflicting_evidence"
    assert result.conflicts[0].reason == "evidence_identity_conflict"


def test_case_27_same_sha_with_different_claimed_range_is_conflict() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-20", sha=SHA_A),
            _provider_interval("2025-01-01", "2025-01-31", sha=SHA_A),
        ],
    )

    assert result.completion_status == "unknown"
    assert result.conflicts[0].reason == "same_sha_claim_conflict"


def test_case_28_same_scope_with_different_sha_is_conflict() -> None:
    result = _evaluate([
        _provider_interval(sha=SHA_A),
        _provider_interval(sha=SHA_B),
    ])

    assert result.completion_status == "unknown"
    assert result.conflicts[0].reason == "same_scope_evidence_conflict"


def test_case_29_needs_confirmation_does_not_enter_complete_union() -> None:
    result = _evaluate([_provider_interval(operational="needs_confirmation")])

    assert result.completion_status == "unknown"
    assert result.merged_intervals == ()
    assert result.completeness_proven is False


def test_case_30_rejected_does_not_enter_complete_union() -> None:
    result = _evaluate([_provider_interval(operational="rejected")])

    assert result.completion_status == "unknown"
    assert result.merged_intervals == ()
    assert result.completeness_proven is False


def test_case_31_no_activity_requires_provider_proof() -> None:
    user_result = _evaluate([
        _interval(kind="user_attested", claim="no_activity", sha=None),
    ])
    provider_result = _evaluate([
        _provider_interval(sha=None, claim="no_activity"),
    ])

    assert user_result.completion_status == "unknown"
    assert user_result.completeness_proven is False
    assert provider_result.completion_status == "complete"
    assert provider_result.completeness_proven is True


def test_case_32_missing_required_window_is_unknown() -> None:
    assert _evaluate([_provider_interval()], start=None).reason == "required_window_missing"
    assert _evaluate([_provider_interval()], end=None).reason == "required_window_missing"


def test_explicit_provider_incomplete_evidence_is_incomplete() -> None:
    result = _evaluate([
        _interval(
            kind="provider_proven",
            claim="export_scope",
            proven=False,
            explicitly_incomplete=True,
        ),
    ])

    assert result.completion_status == "incomplete"
    assert result.reason == "explicit_incomplete_evidence"
    assert result.completeness_proven is False


def test_user_attested_complete_for_range_still_is_not_provider_complete() -> None:
    result = _evaluate([
        _interval(kind="user_attested", claim="complete_for_range"),
    ])

    assert result.completion_status == "unknown"
    assert result.effective_proof_level == "user_attested"
    assert result.completeness_proven is False


def test_complete_result_invariant_requires_provider_proof() -> None:
    with pytest.raises(ValueError, match="complete requires provider completeness proof"):
        CoverageCompletionResult(
            completion_status="complete",
            reason="invalid",
            effective_proof_level="provider_proven",
            completeness_proven=False,
        )

    with pytest.raises(ValueError, match="complete requires provider_proven effective proof"):
        CoverageCompletionResult(
            completion_status="complete",
            reason="invalid",
            effective_proof_level="user_attested",
            completeness_proven=True,
        )


@pytest.mark.parametrize("operational", ["needs_confirmation", "rejected"])
def test_unusable_matching_evidence_blocks_otherwise_complete_result(
    operational: str,
) -> None:
    result = _evaluate([
        _provider_interval(sha=SHA_A),
        _interval(
            "2025-01-10",
            "2025-01-20",
            sha=SHA_B,
            operational=operational,
        ),
    ])

    assert result.completion_status == "unknown"
    assert result.reason == "operational_evidence_not_usable"
    assert result.completeness_proven is False


def test_unasserted_provider_claim_is_not_downgraded_to_user_attestation() -> None:
    result = _evaluate([
        _interval(
            kind="provider_proven",
            claim="complete_for_range",
            proven=False,
        ),
    ])

    assert result.completion_status == "unknown"
    assert result.merged_intervals == ()
    assert result.effective_proof_level == "none"


def test_mixed_proof_with_a_remaining_gap_is_unknown_not_incomplete() -> None:
    result = _evaluate(
        [
            _provider_interval("2025-01-01", "2025-01-31", sha=SHA_A),
            _interval("2025-02-01", "2025-02-15", sha=SHA_B),
        ],
        end="2025-03-31",
    )

    assert result.completion_status == "unknown"
    assert result.reason == "required_window_not_fully_provider_proven"
    assert result.effective_proof_level == "none"
    assert result.gaps[0].coverage_start == "2025-02-01"
    assert result.gaps[0].coverage_end == "2025-03-31"


def test_non_daily_basis_requires_explicit_adjacency_semantics() -> None:
    intervals = [
        NormalizedCoverageInterval(
            provider="paypay",
            coverage_basis="statement_sequence",
            coverage_start="2025-01-01",
            coverage_end="2025-01-15",
            content_sha256=SHA_A,
            operational_coverage="usable",
            attestation_kind="provider_proven",
            attestation_claim="complete_for_range",
            provider_completeness_proven=True,
        ),
        NormalizedCoverageInterval(
            provider="paypay",
            coverage_basis="statement_sequence",
            coverage_start="2025-01-16",
            coverage_end="2025-01-31",
            content_sha256=SHA_B,
            operational_coverage="usable",
            attestation_kind="provider_proven",
            attestation_claim="complete_for_range",
            provider_completeness_proven=True,
        ),
    ]

    result = _evaluate(intervals, basis="statement_sequence")

    assert result.completion_status == "unknown"
    assert result.reason == "coverage_basis_adjacency_not_defined"
    assert result.completeness_proven is False
