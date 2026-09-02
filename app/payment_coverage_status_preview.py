from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Literal, TypeAlias

from .payment_coverage_dual_track import (
    PaymentCoverageDualTrackResult,
    evaluate_payment_coverage_dual_track,
)
from .payment_coverage_manifest import CoverageManifest


CoverageStatus: TypeAlias = Literal["complete", "incomplete", "unknown"]

SOURCES = ("au_pay_card", "au_pay", "paypay", "amazon_gmail", "imported_data")


@dataclass(frozen=True)
class CoverageEvidence:
    source: str
    coverage_start: str | None = None
    coverage_end: str | None = None
    fetched_at: str | None = None
    evidence_type: str = "none"
    evidence_detail: str = ""
    completion_proven: bool = False
    explicitly_incomplete: bool = False
    completeness_reason: str = "no_import_completeness_record"


def _date(value: object) -> date | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _cell(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index and row[index] is not None else ""


def evaluate_coverage(
    evidence: CoverageEvidence,
    *,
    required_start: date | None = None,
    required_end: date | None = None,
) -> dict:
    """Evaluate coverage without treating an observed date range as completeness."""

    observed_start = _date(evidence.coverage_start)
    observed_end = _date(evidence.coverage_end)
    range_known = observed_start is not None and observed_end is not None
    covers_required = None
    if required_start is not None and required_end is not None and range_known:
        covers_required = observed_start <= required_start and observed_end >= required_end

    if evidence.explicitly_incomplete:
        status: CoverageStatus = "incomplete"
        reason = evidence.completeness_reason
    elif covers_required is False:
        status = "incomplete"
        reason = "observed_range_does_not_cover_required_window"
    elif evidence.completion_proven and (covers_required is True or required_start is None):
        status = "complete"
        reason = evidence.completeness_reason or "explicit_completion_evidence"
    else:
        status = "unknown"
        reason = evidence.completeness_reason or "no_import_completeness_record"

    return {
        **asdict(evidence),
        "coverage_status": status,
        "covers_required_window": covers_required,
        "completeness_reason": reason,
    }


def overall_status(source_rows: list[dict]) -> CoverageStatus:
    statuses = {row["coverage_status"] for row in source_rows}
    if "incomplete" in statuses:
        return "incomplete"
    if "unknown" in statuses or not statuses:
        return "unknown"
    return "complete"


def _range(values: list[str]) -> tuple[str | None, str | None]:
    parsed = sorted(value for value in (_date(item) for item in values) if value is not None)
    return (
        parsed[0].isoformat() if parsed else None,
        parsed[-1].isoformat() if parsed else None,
    )


def _latest(values: list[str]) -> str | None:
    usable = [value for value in values if _date(value) is not None]
    return max(usable, key=lambda value: _date(value)) if usable else None


def _source_evidence(import_rows: list[list], event_rows: list[list]) -> dict[str, CoverageEvidence]:
    by_source: dict[str, list[list]] = {source: [] for source in SOURCES}
    mapping = {"au PAYカード": "au_pay_card", "au PAY": "au_pay", "PayPay": "paypay"}
    for row in import_rows:
        source = mapping.get(_cell(row, 2))
        if source:
            by_source[source].append(row)
        by_source["imported_data"].append(row)

    amazon_dates = [_cell(row, 7) for row in event_rows]
    amazon_start, amazon_end = _range(amazon_dates)
    evidence = {
        "amazon_gmail": CoverageEvidence(
            source="amazon_gmail",
            coverage_start=amazon_start,
            coverage_end=amazon_end,
            fetched_at=_latest([_cell(row, 22) for row in event_rows]),
            evidence_type="stored_amazon_event_range",
            evidence_detail="Gmail searches use a 1-year window and per-category result caps",
            explicitly_incomplete=True,
            completeness_reason="search_window_or_result_limit",
        ),
    }
    for source in ("au_pay_card", "au_pay", "paypay", "imported_data"):
        rows = by_source[source]
        start, end = _range([_cell(row, 4) for row in rows])
        reason = "no_import_completeness_record"
        explicitly_incomplete = False
        detail = "Observed rows do not prove that all source records were imported"
        if source == "au_pay":
            explicitly_incomplete = True
            reason = "search_window_or_result_limit"
            detail = "Default Gmail query is limited to 30 days and import is capped by max-results"
        evidence[source] = CoverageEvidence(
            source=source,
            coverage_start=start,
            coverage_end=end,
            fetched_at=_latest([_cell(row, 1) for row in rows]),
            evidence_type="observed_import_row_range" if rows else "none",
            evidence_detail=detail,
            explicitly_incomplete=explicitly_incomplete,
            completeness_reason=reason,
        )
    return evidence


def _order_windows(header_rows: list[list], event_rows: list[list], as_of: date) -> list[dict]:
    headers: dict[str, list[list]] = {}
    for row in header_rows:
        order_id = _cell(row, 0)
        if order_id:
            headers.setdefault(order_id, []).append(row)
    events: dict[str, list[list]] = {}
    for row in event_rows:
        order_id = _cell(row, 6)
        if order_id:
            events.setdefault(order_id, []).append(row)

    windows = []
    for order_id, rows in sorted(events.items()):
        cancellations = [row for row in rows if _cell(row, 5) == "cancellation"]
        if not cancellations:
            continue
        matching_headers = headers.get(order_id, [])
        header = matching_headers[0] if len(matching_headers) == 1 else []
        order_dates = [_cell(row, 7) for row in rows if _cell(row, 5) == "order"]
        cancellation_dates = [_cell(row, 7) for row in cancellations]
        order_date = _cell(header, 1) or (_range(order_dates)[0] or "")
        cancellation_date = _range(cancellation_dates)[0]
        start = _date(order_date) or _date(cancellation_date)
        end = max(as_of, _date(cancellation_date) or as_of)
        windows.append({
            "order_id": order_id,
            "order_date": order_date or None,
            "cancellation_date": cancellation_date,
            "required_window": {
                "start": start.isoformat() if start else None,
                "end": end.isoformat(),
                "basis": "order_date_to_preview_date_no_fixed_grace_period",
            },
            "required_start": start,
            "required_end": end,
        })
    return windows


def build_payment_coverage_context(
    import_rows: list[list],
    event_rows: list[list],
    header_rows: list[list],
    *,
    as_of: date,
    strict_evaluation: bool = False,
    strict_manifests: Iterable[CoverageManifest] = (),
    strict_required_providers: Iterable[str] | None = None,
    strict_coverage_basis: str | None = None,
    strict_required_start: date | None = None,
    strict_required_end: date | None = None,
) -> dict | PaymentCoverageDualTrackResult[dict]:
    """Build the shared source and per-order coverage evaluation."""

    evidence = _source_evidence(import_rows, event_rows)
    source_coverage = [evaluate_coverage(evidence[source]) for source in SOURCES]
    orders = []
    for window in _order_windows(header_rows, event_rows, as_of):
        per_source = [
            evaluate_coverage(
                evidence[source],
                required_start=window["required_start"],
                required_end=window["required_end"],
            )
            for source in SOURCES
        ]
        orders.append({
            key: value for key, value in window.items()
            if key not in {"required_start", "required_end"}
        } | {
            "source_coverage": {
                row["source"]: {
                    "coverage_status": row["coverage_status"],
                    "covers_required_window": row["covers_required_window"],
                    "completeness_reason": row["completeness_reason"],
                }
                for row in per_source
            },
            "overall_payment_coverage_status": overall_status(per_source),
        })
    legacy_result = {"source_coverage": source_coverage, "orders": orders}
    if not strict_evaluation:
        return legacy_result
    return evaluate_payment_coverage_dual_track(
        legacy_result,
        manifests=strict_manifests,
        required_providers=strict_required_providers,
        coverage_basis=strict_coverage_basis,
        required_start=strict_required_start,
        required_end=strict_required_end,
    )


def preview_payment_coverage_status(
    db,
    *,
    as_of: date | None = None,
    strict_evaluation: bool = False,
    strict_manifests: Iterable[CoverageManifest] = (),
    strict_required_providers: Iterable[str] | None = None,
    strict_coverage_basis: str | None = None,
    strict_required_start: date | None = None,
    strict_required_end: date | None = None,
) -> dict | PaymentCoverageDualTrackResult[dict]:
    """Report source and order coverage using Sheets reads only."""

    as_of = as_of or datetime.now(timezone.utc).date()
    import_rows = [list(row) for row in db.get("取込データ!A2:L")]
    event_rows = [list(row) for row in db.get("Amazonイベント!A2:X")]
    header_rows = [list(row) for row in db.get("Amazon注文ヘッダ!A2:O")]
    context = build_payment_coverage_context(
        import_rows,
        event_rows,
        header_rows,
        as_of=as_of,
        strict_evaluation=strict_evaluation,
        strict_manifests=strict_manifests,
        strict_required_providers=strict_required_providers,
        strict_coverage_basis=strict_coverage_basis,
        strict_required_start=strict_required_start,
        strict_required_end=strict_required_end,
    )
    if isinstance(context, PaymentCoverageDualTrackResult):
        legacy_context = context.legacy_result
        strict_result = context.strict_result
    else:
        legacy_context = context
        strict_result = None
    source_coverage = legacy_context["source_coverage"]
    orders = legacy_context["orders"]

    counts = {status: sum(row["coverage_status"] == status for row in source_coverage)
              for status in ("complete", "incomplete", "unknown")}
    legacy_result = {
        "previewed_at": as_of.isoformat(),
        "source_count": len(source_coverage),
        "source_status_counts": counts,
        "source_coverage": source_coverage,
        "sampled_order_count": len(orders),
        "orders": orders,
    }
    if not strict_evaluation:
        return legacy_result
    if strict_result is None:
        raise ValueError("strict_context_result_missing")
    return PaymentCoverageDualTrackResult(legacy_result, strict_result)
