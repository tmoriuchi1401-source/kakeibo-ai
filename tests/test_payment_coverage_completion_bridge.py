from __future__ import annotations

from dataclasses import asdict
from datetime import date

from app.coverage_confirmation import COVERAGE_REASON_OPERATIONAL_ONLY
from app.payment_coverage_completion_bridge import (
    evaluate_manifest_completion,
    normalized_interval_from_manifest,
)
from app.payment_coverage_manifest import CoverageManifest, csv_manifest
from app.paypay_operational_coverage import preview_operational_evidence


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
PAYPAY_HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)


def _manifest(
    start: str | None = "2025-01-01",
    end: str | None = "2025-01-31",
    *,
    source: str = "paypay",
    basis: str = "transaction_date",
    sha: str | None = SHA_A,
    operational: str | None = "usable",
    operational_reason: str | None = "operational_checks_passed",
    reason: str = COVERAGE_REASON_OPERATIONAL_ONLY,
    evidence_type: str = "csv_file",
    evidence_id: str | None = None,
    proven: bool = False,
    completion_status: str = "unknown",
    parse_error: str | None = None,
) -> CoverageManifest:
    return CoverageManifest(
        source=source,
        coverage_start=start,
        coverage_end=end,
        coverage_basis=basis,
        completion_status=completion_status,
        evidence_type=evidence_type,
        evidence_id=evidence_id,
        content_hash=sha,
        completeness_reason=reason,
        completeness_proven=proven,
        operational_coverage=operational,
        operational_reason=operational_reason,
        parse_error=parse_error,
    )


def _provider_manifest(
    start: str = "2025-01-01",
    end: str = "2025-01-31",
    *,
    source: str = "paypay",
    basis: str = "transaction_date",
    sha: str | None = SHA_A,
    operational: str | None = "usable",
    evidence_id: str | None = None,
) -> CoverageManifest:
    return _manifest(
        start,
        end,
        source=source,
        basis=basis,
        sha=sha,
        operational=operational,
        reason="provider_completeness_verified",
        evidence_type="provider_verified",
        evidence_id=evidence_id,
        proven=True,
    )


def _evaluate(
    manifests: list[CoverageManifest],
    *,
    start: date | None = date(2025, 1, 1),
    end: date | None = date(2025, 1, 31),
    provider: str = "paypay",
    basis: str = "transaction_date",
):
    return evaluate_manifest_completion(
        manifests,
        provider=provider,
        coverage_basis=basis,
        required_start=start,
        required_end=end,
    )


def test_paypay_user_confirmation_maps_to_export_scope_attestation() -> None:
    interval = normalized_interval_from_manifest(_manifest())

    assert interval is not None
    assert interval.attestation_kind == "user_attested"
    assert interval.attestation_claim == "export_scope"
    assert interval.provider_completeness_proven is False


def test_operationally_usable_user_confirmation_remains_unknown() -> None:
    evaluation = _evaluate([_manifest()])

    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "provider_completeness_not_proven"
    assert evaluation.completion.completeness_proven is False


def test_filename_candidate_only_has_no_attestation_and_is_unknown() -> None:
    manifest = _manifest(
        reason="export_scope_not_proven",
        operational="needs_confirmation",
        operational_reason="filename_range_requires_confirmation",
    )
    interval = normalized_interval_from_manifest(manifest)
    evaluation = _evaluate([manifest])

    assert interval is not None
    assert interval.attestation_kind == "none"
    assert interval.attestation_claim is None
    assert evaluation.completion.completion_status == "unknown"


def test_rejected_paypay_manifest_is_not_complete_evidence() -> None:
    evaluation = _evaluate([
        _manifest(
            operational="rejected",
            operational_reason="transaction_outside_requested_range",
        ),
    ])

    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "operational_evidence_not_usable"
    assert evaluation.completion.completeness_proven is False


def test_needs_confirmation_manifest_is_not_complete_evidence() -> None:
    evaluation = _evaluate([
        _manifest(
            operational="needs_confirmation",
            operational_reason="range_not_confirmed",
        ),
    ])

    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "operational_evidence_not_usable"


def test_explicit_provider_proof_maps_and_completes_full_window() -> None:
    interval = normalized_interval_from_manifest(_provider_manifest())
    evaluation = _evaluate([_provider_manifest()])

    assert interval is not None
    assert interval.attestation_kind == "provider_proven"
    assert interval.attestation_claim == "complete_for_range"
    assert interval.provider_completeness_proven is True
    assert evaluation.completion.completion_status == "complete"
    assert evaluation.completion.completeness_proven is True


def test_completeness_boolean_without_explicit_provenance_is_not_provider_proof() -> None:
    manifest = _manifest(
        evidence_type="csv_file",
        proven=True,
        reason="legacy_completion_flag",
    )
    interval = normalized_interval_from_manifest(manifest)
    evaluation = _evaluate([manifest])

    assert interval is not None
    assert interval.attestation_kind == "none"
    assert interval.provider_completeness_proven is False
    assert evaluation.completion.completion_status == "unknown"


def test_provider_proof_left_shortage_is_incomplete() -> None:
    evaluation = _evaluate([_provider_manifest("2025-01-05", "2025-01-31")])

    assert evaluation.completion.completion_status == "incomplete"
    assert asdict(evaluation.completion.gaps[0]) == {
        "coverage_start": "2025-01-01",
        "coverage_end": "2025-01-04",
    }


def test_provider_proof_right_shortage_is_incomplete() -> None:
    evaluation = _evaluate([_provider_manifest("2025-01-01", "2025-01-27")])

    assert evaluation.completion.completion_status == "incomplete"
    assert asdict(evaluation.completion.gaps[0]) == {
        "coverage_start": "2025-01-28",
        "coverage_end": "2025-01-31",
    }


def test_provider_proof_gap_is_incomplete() -> None:
    evaluation = _evaluate([
        _provider_manifest("2025-01-01", "2025-01-10", sha=SHA_A),
        _provider_manifest("2025-01-15", "2025-01-31", sha=SHA_B),
    ])

    assert evaluation.completion.completion_status == "incomplete"
    assert asdict(evaluation.completion.gaps[0]) == {
        "coverage_start": "2025-01-11",
        "coverage_end": "2025-01-14",
    }


def test_same_sha_duplicate_manifests_are_collapsed_by_evaluator() -> None:
    evaluation = _evaluate([_provider_manifest(), _provider_manifest()])

    assert evaluation.completion.completion_status == "complete"
    assert evaluation.completion.duplicate_collapse_count == 1


def test_same_sha_range_conflict_is_unknown() -> None:
    evaluation = _evaluate([
        _provider_manifest("2025-01-01", "2025-01-20", sha=SHA_A),
        _provider_manifest("2025-01-01", "2025-01-31", sha=SHA_A),
    ])

    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "conflicting_evidence"
    assert evaluation.completion.conflicts[0].reason == "same_sha_claim_conflict"


def test_same_scope_different_sha_is_unknown_conflict() -> None:
    evaluation = _evaluate([
        _provider_manifest(sha=SHA_A),
        _provider_manifest(sha=SHA_B),
    ])

    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.conflicts[0].reason == "same_scope_evidence_conflict"


def test_adjacent_provider_manifests_merge_to_complete() -> None:
    evaluation = _evaluate([
        _provider_manifest("2025-01-01", "2025-01-15", sha=SHA_A),
        _provider_manifest("2025-01-16", "2025-01-31", sha=SHA_B),
    ])

    assert evaluation.completion.completion_status == "complete"
    assert len(evaluation.completion.merged_intervals) == 1


def test_overlapping_provider_manifests_merge_to_complete() -> None:
    evaluation = _evaluate([
        _provider_manifest("2025-01-01", "2025-01-20", sha=SHA_A),
        _provider_manifest("2025-01-15", "2025-01-31", sha=SHA_B),
    ])

    assert evaluation.completion.completion_status == "complete"
    assert len(evaluation.completion.merged_intervals) == 1


def test_user_attested_interval_mixed_with_provider_proof_prevents_complete() -> None:
    evaluation = _evaluate(
        [
            _provider_manifest("2025-01-01", "2025-01-10", sha=SHA_A),
            _manifest("2025-01-11", "2025-01-20", sha=SHA_B),
            _provider_manifest("2025-01-21", "2025-01-31", sha=SHA_C),
        ],
    )

    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "required_window_not_fully_provider_proven"
    assert evaluation.completion.completeness_proven is False


def test_different_provider_manifests_are_filtered_before_union() -> None:
    evaluation = _evaluate([
        _provider_manifest(),
        _provider_manifest(source="other", operational="rejected", sha=SHA_B),
    ])

    assert evaluation.completion.completion_status == "complete"
    assert len(evaluation.normalized_intervals) == 1
    assert evaluation.normalized_intervals[0].provider == "paypay"


def test_different_coverage_basis_is_filtered_before_union() -> None:
    evaluation = _evaluate([
        _provider_manifest(),
        _provider_manifest(basis="message_date", operational="rejected", sha=SHA_B),
    ])

    assert evaluation.completion.completion_status == "complete"
    assert len(evaluation.normalized_intervals) == 1
    assert evaluation.normalized_intervals[0].coverage_basis == "transaction_date"


def test_missing_required_window_does_not_perform_completion_evaluation() -> None:
    evaluation = _evaluate([_provider_manifest()], start=None)

    assert evaluation.evaluated is False
    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "required_window_missing"


def test_manifest_without_content_hash_does_not_get_a_fake_sha() -> None:
    manifest = _provider_manifest(sha=None, evidence_id="provider-record-1")
    interval = normalized_interval_from_manifest(manifest)

    assert interval is not None
    assert interval.content_sha256 is None
    assert interval.evidence_id == "provider-record-1"


def test_invalid_content_hash_is_omitted_instead_of_rewritten() -> None:
    interval = normalized_interval_from_manifest(_manifest(sha="not-a-sha"))

    assert interval is not None
    assert interval.content_sha256 is None


def test_bridge_does_not_mutate_coverage_manifest() -> None:
    manifest = _manifest()
    before = asdict(manifest)

    evaluation = _evaluate([manifest])

    assert asdict(manifest) == before
    assert evaluation.normalized_intervals[0] is not manifest


def test_missing_manifest_range_is_skipped_and_remains_unknown() -> None:
    manifest = _manifest(start=None, end=None)
    evaluation = _evaluate([manifest])

    assert evaluation.normalized_intervals == ()
    assert evaluation.skipped_manifest_ids == (manifest.manifest_id,)
    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "matching_coverage_evidence_missing"


def test_manifest_incomplete_status_is_not_reinterpreted_as_explicit_evidence() -> None:
    manifest = _manifest(
        completion_status="incomplete",
        reason="legacy_window_evaluation",
    )
    interval = normalized_interval_from_manifest(manifest)
    evaluation = _evaluate([manifest])

    assert interval is not None
    assert interval.explicitly_incomplete is False
    assert evaluation.completion.completion_status == "unknown"


def test_actual_paypay_csv_manifest_bridge_uses_export_scope_only(tmp_path) -> None:
    path = tmp_path / "Transactions_20250101-20250131.csv"
    path.write_text(
        PAYPAY_HEADER
        + "2025/01/15 12:34,100,,,,,,支払い,店,PayPay残高,一回払い,本人,TX-1\n",
        encoding="utf-8-sig",
    )
    operational = preview_operational_evidence(
        path,
        requested_start="2025-01-01",
        requested_end="2025-01-31",
        range_source="user_confirmed",
        range_confirmed=True,
    )
    manifest = csv_manifest(
        path,
        "paypay",
        paypay_operational_evidence=operational,
        paypay_confirmed_range=("2025-01-01", "2025-01-31"),
    )

    interval = normalized_interval_from_manifest(manifest)
    evaluation = _evaluate([manifest])

    assert interval is not None
    assert interval.content_sha256 == operational.csv_sha256
    assert interval.attestation_kind == "user_attested"
    assert interval.attestation_claim == "export_scope"
    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.completeness_proven is False


def test_provider_verified_label_without_proven_boolean_is_not_proof() -> None:
    manifest = _manifest(
        evidence_type="provider_verified",
        proven=False,
        reason="provider_verification_not_implemented",
    )
    interval = normalized_interval_from_manifest(manifest)

    assert interval is not None
    assert interval.attestation_kind == "none"
    assert interval.provider_completeness_proven is False
    assert _evaluate([manifest]).completion.completion_status == "unknown"


def test_parse_error_overrides_inconsistent_usable_operational_status() -> None:
    manifest = _manifest(
        operational="usable",
        parse_error="malformed CSV",
    )
    interval = normalized_interval_from_manifest(manifest)
    evaluation = _evaluate([manifest])

    assert interval is not None
    assert interval.operational_coverage == "rejected"
    assert evaluation.completion.completion_status == "unknown"
    assert evaluation.completion.reason == "operational_evidence_not_usable"
