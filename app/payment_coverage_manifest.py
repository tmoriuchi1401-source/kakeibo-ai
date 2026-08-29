from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, TypeAlias
from uuid import UUID, uuid5

from .aupay_card_pipeline import parse_aupay_card_csv
from .paypay_evidence_bundle import (
    EvidenceVerificationResult,
    SignatureVerifier,
    load_evidence_bundle,
    verify_evidence_bundle,
)
from .paypay_pipeline import inspect_paypay_csv


CompletionStatus: TypeAlias = Literal["complete", "incomplete", "unknown"]
CoverageBasis: TypeAlias = Literal["transaction_date", "billing_cycle", "message_date"]
PeriodType: TypeAlias = Literal["calendar_month", "explicit_range", "file_defined_range"]

_MANIFEST_NAMESPACE = UUID("46996176-9ad0-4adc-a1b8-3c5bfd01a2b6")
_SOURCES = ("paypay", "au_pay_card", "amazon_gmail", "au_pay_gmail")
@dataclass(frozen=True)
class CoverageManifest:
    source: str
    coverage_start: str | None = None
    coverage_end: str | None = None
    period_type: PeriodType = "file_defined_range"
    coverage_basis: CoverageBasis = "transaction_date"
    completion_status: CompletionStatus = "unknown"
    evidence_type: str = "none"
    evidence_id: str | None = None
    evidence_filename: str | None = None
    source_period_label: str | None = None
    imported_at: str | None = None
    row_count: int | None = None
    content_hash: str | None = None
    completeness_reason: str = "no_completion_evidence"
    completeness_proven: bool = False
    supersedes_manifest_id: str | None = None
    manifest_id: str = ""
    parse_error: str | None = None
    candidate_complete: bool = False

    def __post_init__(self) -> None:
        if self.completion_status == "complete" and not self.completeness_proven:
            raise ValueError("complete requires completeness_proven=true")
        if self.parse_error and self.completion_status == "complete":
            raise ValueError("parse errors cannot be complete")
        if not self.manifest_id:
            identity = "|".join((
                self.source, self.coverage_basis, self.coverage_start or "",
                self.coverage_end or "", self.evidence_id or "",
                self.content_hash or "",
            ))
            object.__setattr__(self, "manifest_id", str(uuid5(_MANIFEST_NAMESPACE, identity)))


def _file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _range(rows: list[dict]) -> tuple[str | None, str | None]:
    dates = sorted(str(row["date"]) for row in rows if row.get("date"))
    return (dates[0], dates[-1]) if dates else (None, None)


def _period_label(start: str | None, end: str | None) -> str | None:
    if not start or not end:
        return None
    return start[:7] if start[:7] == end[:7] else f"{start}..{end}"


def _filename_period(path: Path) -> str | None:
    match = re.search(r"(?<!\d)(20\d{2})[-_]?([01]\d)(?!\d)", path.stem)
    return f"{match.group(1)}-{match.group(2)}" if match else None


def csv_manifest(path: str | Path, source: str, *, evidence_id: str | None = None,
                 paypay_evidence_verification: EvidenceVerificationResult | None = None,
                 coverage_basis: CoverageBasis | None = None,
                 period_type: PeriodType = "file_defined_range",
                 imported_at: str | None = None) -> CoverageManifest:
    """Inspect a local CSV without importing it or mutating its source."""
    path = Path(path)
    basis = coverage_basis or ("billing_cycle" if source == "au_pay_card"
                               else "transaction_date")
    content_hash = _file_hash(path)
    try:
        if source == "paypay":
            inspection = inspect_paypay_csv(path)
            rows = []
            observed_start = inspection["observed_start"]
            observed_end = inspection["observed_end"]
            row_count = int(inspection["row_count"])
        elif source == "au_pay_card":
            rows = parse_aupay_card_csv(str(path))
            observed_start, observed_end = _range(rows)
            row_count = len(rows)
        else:
            raise ValueError(f"unsupported CSV source: {source}")
    except (OSError, UnicodeError, ValueError) as exc:
        return CoverageManifest(
            source=source, coverage_basis=basis, evidence_type="csv_file",
            evidence_id=evidence_id or content_hash, evidence_filename=path.name,
            content_hash=content_hash, completeness_reason="parse_error",
            parse_error=str(exc), imported_at=imported_at,
        )

    # Card rows carry transaction dates, not billing-cycle boundaries.  Keep the
    # billing range unset until an upstream statement/export supplies it.
    start, end = observed_start, observed_end
    filename_period = _filename_period(path)
    if source == "au_pay_card" and basis == "billing_cycle":
        start = end = None
    verification = paypay_evidence_verification if source == "paypay" else None
    scope = None
    if verification and verification.accepted and verification.candidate_complete:
        scope = (verification.requested_start, verification.requested_end)
    scope_conflict = bool(
        scope and observed_start and observed_end
        and (observed_start < scope[0] or observed_end > scope[1])
    )
    if scope and not scope_conflict:
        start, end = scope
    candidate = bool(start and end) or bool(scope)
    proven = False  # Provider verification and complete activation are not implemented.
    if scope_conflict:
        reason = "observed_transaction_outside_export_scope"
    elif verification:
        reason = verification.reason
    else:
        reason = "export_scope_not_proven"
    return CoverageManifest(
        source=source, coverage_start=start, coverage_end=end,
        coverage_basis=basis, period_type=period_type,
        completion_status="unknown",
        evidence_type=(verification.trust_tier if verification else "csv_file"),
        evidence_id=(verification.evidence_id if verification and verification.evidence_id
                     else evidence_id or content_hash), evidence_filename=path.name,
        source_period_label=filename_period or _period_label(start, end), imported_at=imported_at,
        row_count=row_count, content_hash=content_hash,
        completeness_reason=reason,
        completeness_proven=proven, candidate_complete=candidate,
    )


def gmail_manifest(source: str) -> CoverageManifest:
    return CoverageManifest(
        source=source, coverage_basis="message_date", evidence_type="gmail_search",
        completion_status="unknown", completeness_proven=False,
        completeness_reason=(
            "pagination_query_range_result_cap_forwarded_mail_and_upstream_total_not_proven"
        ),
    )


def manifest_for_required_window(manifest: CoverageManifest, required_start: date,
                                 required_end: date,
                                 *, coverage_basis: CoverageBasis) -> CompletionStatus:
    """Future bridge: only same-basis proven manifests can complete coverage."""
    if manifest.coverage_basis != coverage_basis:
        return "unknown"
    if not manifest.coverage_start or not manifest.coverage_end:
        return "unknown"
    start = date.fromisoformat(manifest.coverage_start)
    end = date.fromisoformat(manifest.coverage_end)
    if start > required_start or end < required_end:
        return "incomplete"
    return "complete" if manifest.completeness_proven and not manifest.parse_error else "unknown"


def classify_evidence(manifests: list[CoverageManifest]) -> tuple[list[CoverageManifest], int, int]:
    groups: dict[tuple[str, str, str | None, str | None], list[int]] = {}
    for index, item in enumerate(manifests):
        key = (item.source, item.coverage_basis,
               item.source_period_label or item.coverage_start, item.coverage_end)
        groups.setdefault(key, []).append(index)
    duplicate_indexes: set[int] = set()
    conflict_indexes: set[int] = set()
    duplicate_count = conflict_count = 0
    for indexes in groups.values():
        hashes = [manifests[index].content_hash for index in indexes
                  if manifests[index].content_hash]
        if len(hashes) < 2:
            continue
        if len(set(hashes)) == 1:
            duplicate_count += len(hashes) - 1
            duplicate_indexes.update(indexes[1:])
        else:
            conflict_count += len(set(hashes)) - 1
            conflict_indexes.update(indexes)
    result = []
    for index, item in enumerate(manifests):
        if index in conflict_indexes:
            item = replace(item, completion_status="unknown", completeness_proven=False,
                           completeness_reason="conflicting_evidence")
        elif index in duplicate_indexes:
            item = replace(item, completeness_reason="duplicate_evidence")
        result.append(item)
    return result, duplicate_count, conflict_count


def preview_payment_coverage_manifests(
    *, paypay_csvs: list[str] | None = None,
    paypay_export_evidence_files: list[str] | None = None,
    paypay_status_image_files: list[str] | None = None,
    au_pay_card_csvs: list[str] | None = None,
    signature_verifier: SignatureVerifier | None = None,
) -> dict:
    paypay_paths = paypay_csvs or []
    evidence_paths = paypay_export_evidence_files or []
    image_paths = paypay_status_image_files or []
    if evidence_paths and (
        len(evidence_paths) != len(paypay_paths) or len(image_paths) != len(paypay_paths)
    ):
        raise ValueError("PayPay CSV、export evidence、status imageは同じ件数で指定してください")
    verifications: list[EvidenceVerificationResult | None] = []
    if evidence_paths:
        for csv_path, evidence_path, image_path in zip(
            paypay_paths, evidence_paths, image_paths,
        ):
            try:
                bundle = load_evidence_bundle(evidence_path)
                result = verify_evidence_bundle(
                    bundle, csv_path=csv_path, status_image_path=image_path,
                    signature_verifier=signature_verifier,
                )
            except (TypeError, ValueError) as exc:
                result = EvidenceVerificationResult(False, str(exc))
            verifications.append(result)
    else:
        verifications = [None] * len(paypay_paths)
    manifests = [csv_manifest(path, "paypay", paypay_evidence_verification=verification)
                 for path, verification in zip(paypay_paths, verifications)]
    manifests += [csv_manifest(path, "au_pay_card") for path in (au_pay_card_csvs or [])]
    present = {item.source for item in manifests}
    manifests += [CoverageManifest(source=source,
                                   coverage_basis=("billing_cycle" if source == "au_pay_card"
                                                   else "transaction_date"))
                  for source in ("paypay", "au_pay_card") if source not in present]
    manifests += [gmail_manifest("amazon_gmail"), gmail_manifest("au_pay_gmail")]
    manifests, duplicates, conflicts = classify_evidence(manifests)
    counts = {status: sum(item.completion_status == status for item in manifests)
              for status in ("complete", "incomplete", "unknown")}
    return {
        "previewed_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "candidate_manifest_count": len(manifests),
        "complete_count": counts["complete"],
        "incomplete_count": counts["incomplete"],
        "unknown_count": counts["unknown"],
        "duplicate_evidence_count": duplicates,
        "conflicting_evidence_count": conflicts,
        "paypay_evidence_verifications": [
            asdict(item) for item in verifications if item is not None
        ],
        "manifests": [asdict(item) for item in manifests],
        "future_coverage_bridge": (
            "completeness_proven=true AND same coverage_basis AND required_window "
            "is contained by manifest coverage"
        ),
    }
