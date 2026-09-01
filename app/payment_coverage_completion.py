from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Literal, TypeAlias


CompletionStatus: TypeAlias = Literal["complete", "incomplete", "unknown"]
OperationalCoverage: TypeAlias = Literal["usable", "needs_confirmation", "rejected"]
AttestationKind: TypeAlias = Literal["none", "user_attested", "provider_proven"]
AttestationClaim: TypeAlias = Literal[
    "export_scope", "complete_for_range", "no_activity",
]
ProofLevel: TypeAlias = Literal["none", "user_attested", "provider_proven"]

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DAILY_COVERAGE_BASES = {"transaction_date", "message_date"}
_PROOF_RANK = {"none": 0, "user_attested": 1, "provider_proven": 2}


def _iso_date(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid_{field}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid_{field}") from exc


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid_{field}")
    return value.strip()


@dataclass(frozen=True)
class NormalizedCoverageInterval:
    """One normalized coverage claim with no I/O or storage assumptions."""

    provider: str
    coverage_basis: str
    coverage_start: str
    coverage_end: str
    content_sha256: str | None
    operational_coverage: OperationalCoverage
    attestation_kind: AttestationKind = "none"
    attestation_claim: AttestationClaim | None = None
    provider_completeness_proven: bool = False
    evidence_id: str | None = None
    diagnostic: str | None = None
    explicitly_incomplete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(
            self, "coverage_basis",
            _required_text(self.coverage_basis, "coverage_basis"),
        )
        start = _iso_date(self.coverage_start, "coverage_start")
        end = _iso_date(self.coverage_end, "coverage_end")
        if start > end:
            raise ValueError("invalid_coverage_range")
        object.__setattr__(self, "coverage_start", start)
        object.__setattr__(self, "coverage_end", end)
        if self.content_sha256 is not None:
            digest = self.content_sha256.lower()
            if not _SHA256.fullmatch(digest):
                raise ValueError("invalid_content_sha256")
            object.__setattr__(self, "content_sha256", digest)
        if self.operational_coverage not in {
            "usable", "needs_confirmation", "rejected",
        }:
            raise ValueError("invalid_operational_coverage")
        if self.attestation_kind not in {
            "none", "user_attested", "provider_proven",
        }:
            raise ValueError("invalid_attestation_kind")
        if self.attestation_claim not in {
            None, "export_scope", "complete_for_range", "no_activity",
        }:
            raise ValueError("invalid_attestation_claim")
        if self.attestation_kind == "none" and self.attestation_claim is not None:
            raise ValueError("attestation_claim_requires_attestation")
        if self.attestation_kind != "none" and self.attestation_claim is None:
            raise ValueError("attestation_kind_requires_claim")
        if self.provider_completeness_proven and not (
            self.attestation_kind == "provider_proven"
            and self.attestation_claim in {"complete_for_range", "no_activity"}
        ):
            raise ValueError("provider_proof_requires_provider_completeness_claim")
        if self.evidence_id is not None:
            object.__setattr__(
                self, "evidence_id", _required_text(self.evidence_id, "evidence_id"),
            )


@dataclass(frozen=True)
class MergedCoverageInterval:
    provider: str
    coverage_basis: str
    coverage_start: str
    coverage_end: str
    effective_proof_level: ProofLevel
    attestation_claims: tuple[str, ...]


@dataclass(frozen=True)
class CoverageGap:
    coverage_start: str
    coverage_end: str


@dataclass(frozen=True)
class CoverageConflict:
    reason: str
    evidence_ids: tuple[str, ...] = ()
    content_sha256s: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageCompletionResult:
    completion_status: CompletionStatus
    reason: str
    merged_intervals: tuple[MergedCoverageInterval, ...] = ()
    gaps: tuple[CoverageGap, ...] = ()
    conflicts: tuple[CoverageConflict, ...] = ()
    duplicate_collapse_count: int = 0
    effective_proof_level: ProofLevel = "none"
    completeness_proven: bool = False

    def __post_init__(self) -> None:
        if self.completion_status == "complete" and not self.completeness_proven:
            raise ValueError("complete requires provider completeness proof")
        if self.completion_status == "complete" and (
            self.effective_proof_level != "provider_proven"
        ):
            raise ValueError("complete requires provider_proven effective proof")


def _semantic_signature(item: NormalizedCoverageInterval) -> tuple[object, ...]:
    return (
        item.coverage_start,
        item.coverage_end,
        item.operational_coverage,
        item.attestation_kind,
        item.attestation_claim,
        item.provider_completeness_proven,
        item.explicitly_incomplete,
    )


def _conflict(reason: str, items: Iterable[NormalizedCoverageInterval]) -> CoverageConflict:
    values = list(items)
    return CoverageConflict(
        reason=reason,
        evidence_ids=tuple(sorted({item.evidence_id for item in values if item.evidence_id})),
        content_sha256s=tuple(sorted({
            item.content_sha256 for item in values if item.content_sha256
        })),
    )


def _collapse_logical_duplicates(
    intervals: list[NormalizedCoverageInterval],
) -> tuple[list[NormalizedCoverageInterval], int, list[CoverageConflict]]:
    active = set(range(len(intervals)))
    duplicate_count = 0
    conflicts: list[CoverageConflict] = []

    hash_groups: dict[tuple[str, str, str], list[int]] = {}
    for index, item in enumerate(intervals):
        if item.content_sha256:
            key = (item.provider, item.coverage_basis, item.content_sha256)
            hash_groups.setdefault(key, []).append(index)
    for indexes in hash_groups.values():
        if len(indexes) < 2:
            continue
        items = [intervals[index] for index in indexes]
        if len({_semantic_signature(item) for item in items}) == 1:
            duplicate_count += len(indexes) - 1
            active.difference_update(indexes[1:])
        else:
            conflicts.append(_conflict("same_sha_claim_conflict", items))
            active.difference_update(indexes)

    evidence_groups: dict[tuple[str, str, str], list[int]] = {}
    for index in sorted(active):
        item = intervals[index]
        if item.evidence_id:
            key = (item.provider, item.coverage_basis, item.evidence_id)
            evidence_groups.setdefault(key, []).append(index)
    for indexes in evidence_groups.values():
        if len(indexes) < 2:
            continue
        items = [intervals[index] for index in indexes]
        identities = {(item.content_sha256, _semantic_signature(item)) for item in items}
        if len(identities) != 1:
            conflicts.append(_conflict("evidence_identity_conflict", items))
            active.difference_update(indexes)

    scope_groups: dict[tuple[str, str, str, str], list[int]] = {}
    for index in sorted(active):
        item = intervals[index]
        key = (
            item.provider, item.coverage_basis,
            item.coverage_start, item.coverage_end,
        )
        scope_groups.setdefault(key, []).append(index)
    for indexes in scope_groups.values():
        if len(indexes) < 2:
            continue
        items = [intervals[index] for index in indexes]
        hashes = {item.content_sha256 for item in items if item.content_sha256}
        claims = {item.attestation_claim for item in items}
        if len(hashes) > 1 or len(claims) > 1:
            conflicts.append(_conflict("same_scope_evidence_conflict", items))
            active.difference_update(indexes)

    return [intervals[index] for index in sorted(active)], duplicate_count, conflicts


def _strict_provider_proof(item: NormalizedCoverageInterval) -> bool:
    return (
        item.operational_coverage == "usable"
        and item.attestation_kind == "provider_proven"
        and item.attestation_claim in {"complete_for_range", "no_activity"}
        and item.provider_completeness_proven
        and not item.explicitly_incomplete
    )


def _proof_level(item: NormalizedCoverageInterval) -> ProofLevel:
    if _strict_provider_proof(item):
        return "provider_proven"
    if (
        item.operational_coverage == "usable"
        and item.attestation_kind == "user_attested"
        and not item.explicitly_incomplete
    ):
        return "user_attested"
    return "none"


def _weaker(first: ProofLevel, second: ProofLevel) -> ProofLevel:
    return first if _PROOF_RANK[first] <= _PROOF_RANK[second] else second


def _merge_intervals(
    intervals: Iterable[NormalizedCoverageInterval],
    *,
    coverage_basis: str,
) -> list[MergedCoverageInterval]:
    usable = [item for item in intervals if _proof_level(item) != "none"]
    usable.sort(key=lambda item: (item.coverage_start, item.coverage_end))
    merged: list[MergedCoverageInterval] = []
    for item in usable:
        level = _proof_level(item)
        claim = (item.attestation_claim,) if item.attestation_claim else ()
        candidate = MergedCoverageInterval(
            item.provider, item.coverage_basis,
            item.coverage_start, item.coverage_end, level, claim,
        )
        if not merged:
            merged.append(candidate)
            continue
        previous = merged[-1]
        previous_end = date.fromisoformat(previous.coverage_end)
        candidate_start = date.fromisoformat(candidate.coverage_start)
        adjacent = (
            coverage_basis in _DAILY_COVERAGE_BASES
            and candidate_start <= previous_end + timedelta(days=1)
        )
        overlaps = candidate_start <= previous_end
        if previous.provider == candidate.provider and (overlaps or adjacent):
            merged[-1] = MergedCoverageInterval(
                previous.provider,
                previous.coverage_basis,
                previous.coverage_start,
                max(previous.coverage_end, candidate.coverage_end),
                _weaker(previous.effective_proof_level, level),
                tuple(sorted(set(previous.attestation_claims + claim))),
            )
        else:
            merged.append(candidate)
    return merged


def _gaps(
    intervals: Iterable[MergedCoverageInterval],
    required_start: date,
    required_end: date,
) -> list[CoverageGap]:
    cursor = required_start
    gaps: list[CoverageGap] = []
    for item in intervals:
        start = date.fromisoformat(item.coverage_start)
        end = date.fromisoformat(item.coverage_end)
        if end < required_start or start > required_end:
            continue
        start = max(start, required_start)
        end = min(end, required_end)
        if start > cursor:
            gaps.append(CoverageGap(cursor.isoformat(), (start - timedelta(days=1)).isoformat()))
        cursor = max(cursor, end + timedelta(days=1))
        if cursor > required_end:
            break
    if cursor <= required_end:
        gaps.append(CoverageGap(cursor.isoformat(), required_end.isoformat()))
    return gaps


def _intersecting_interval_count(
    intervals: Iterable[MergedCoverageInterval],
    required_start: date,
    required_end: date,
) -> int:
    return sum(
        date.fromisoformat(item.coverage_end) >= required_start
        and date.fromisoformat(item.coverage_start) <= required_end
        for item in intervals
    )


def evaluate_payment_coverage_completion(
    intervals: Iterable[NormalizedCoverageInterval],
    *,
    provider: str,
    coverage_basis: str,
    required_start: str | None,
    required_end: str | None,
) -> CoverageCompletionResult:
    """Evaluate strict provider completeness for one required window.

    User attestations may explain an otherwise unproven span, but only a
    gapless union of operationally usable provider proofs can return complete.
    """

    provider = _required_text(provider, "provider")
    coverage_basis = _required_text(coverage_basis, "coverage_basis")
    if required_start is None or required_end is None:
        return CoverageCompletionResult("unknown", "required_window_missing")
    try:
        start = date.fromisoformat(_iso_date(required_start, "required_start"))
        end = date.fromisoformat(_iso_date(required_end, "required_end"))
    except ValueError:
        return CoverageCompletionResult("unknown", "invalid_required_window")
    if start > end:
        return CoverageCompletionResult("unknown", "invalid_required_window")

    matching = [
        item for item in intervals
        if item.provider == provider and item.coverage_basis == coverage_basis
    ]
    collapsed, duplicate_count, conflicts = _collapse_logical_duplicates(matching)
    merged_all = _merge_intervals(collapsed, coverage_basis=coverage_basis)
    strict = [item for item in collapsed if _strict_provider_proof(item)]
    merged_provider = _merge_intervals(strict, coverage_basis=coverage_basis)
    provider_gaps = _gaps(merged_provider, start, end)
    all_gaps = _gaps(merged_all, start, end)
    user_attested_in_window = any(
        item.operational_coverage == "usable"
        and item.attestation_kind == "user_attested"
        and date.fromisoformat(item.coverage_end) >= start
        and date.fromisoformat(item.coverage_start) <= end
        for item in collapsed
    )

    common = {
        "merged_intervals": tuple(merged_all),
        "gaps": tuple(provider_gaps),
        "conflicts": tuple(conflicts),
        "duplicate_collapse_count": duplicate_count,
    }
    if conflicts:
        return CoverageCompletionResult(
            "unknown", "conflicting_evidence",
            effective_proof_level="none", **common,
        )
    if any(
        item.operational_coverage == "usable"
        and item.attestation_kind == "provider_proven"
        and item.explicitly_incomplete
        for item in collapsed
    ):
        return CoverageCompletionResult(
            "incomplete", "explicit_incomplete_evidence",
            effective_proof_level="none", **common,
        )
    if any(item.operational_coverage != "usable" for item in collapsed):
        return CoverageCompletionResult(
            "unknown", "operational_evidence_not_usable",
            effective_proof_level="none", **common,
        )
    if (
        coverage_basis not in _DAILY_COVERAGE_BASES
        and not provider_gaps
        and _intersecting_interval_count(merged_provider, start, end) > 1
    ):
        return CoverageCompletionResult(
            "unknown", "coverage_basis_adjacency_not_defined",
            effective_proof_level="none", **common,
        )
    if strict and not provider_gaps:
        return CoverageCompletionResult(
            "complete", "provider_proven_coverage_complete",
            effective_proof_level="provider_proven",
            completeness_proven=True,
            **common,
        )
    if strict and user_attested_in_window:
        return CoverageCompletionResult(
            "unknown", "required_window_not_fully_provider_proven",
            effective_proof_level=("user_attested" if not all_gaps else "none"),
            **common,
        )
    if strict:
        return CoverageCompletionResult(
            "incomplete", "provider_proven_coverage_gap",
            effective_proof_level="none", **common,
        )
    if not matching:
        reason = "matching_coverage_evidence_missing"
    elif not merged_all and any(
        item.operational_coverage != "usable" for item in collapsed
    ):
        reason = "operational_evidence_not_usable"
    else:
        reason = "provider_completeness_not_proven"
    effective: ProofLevel = "user_attested" if not all_gaps else "none"
    return CoverageCompletionResult(
        "unknown", reason, effective_proof_level=effective, **common,
    )
