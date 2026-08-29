from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, TypeAlias

from .paypay_operational_coverage import (
    PayPayOperationalEvidence,
    classify_operational_evidence,
    preview_operational_evidence,
)


RangeSource: TypeAlias = Literal["filename_candidate", "user_confirmed", "manual"]

_METADATA_FIELDS = {
    "schema_version", "ingest_id", "requested_start", "requested_end",
    "range_confirmed", "range_source", "original_filename",
    "shortcut_version", "shared_at",
}
_INGEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_RANGE_SOURCES = {"filename_candidate", "user_confirmed", "manual"}


@dataclass(frozen=True)
class PayPayShortcutMetadata:
    schema_version: str
    ingest_id: str
    requested_start: str | None
    requested_end: str | None
    range_confirmed: bool
    range_source: RangeSource | None
    original_filename: str
    shortcut_version: str
    shared_at: str


@dataclass(frozen=True)
class ShortcutIngestPreview:
    ingest_id: str
    folder: str
    csv_filename: str | None
    metadata_present: bool
    metadata_valid: bool
    metadata_reason: str
    pairing_status: str
    requested_start: str | None = None
    requested_end: str | None = None
    range_confirmed: bool = False
    range_source: str | None = None
    csv_parse_status: str = "not_parsed"
    operational_coverage: str | None = None
    operational_reason: str | None = None
    orphan: bool = False
    ambiguous: bool = False
    unexpected_files: tuple[str, ...] = ()
    completion_status: str = "unknown"
    completeness_proven: bool = False
    operational_evidence: PayPayOperationalEvidence | None = None


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _iso_date(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid_type:{field}")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid_date:{field}") from exc


def parse_shortcut_metadata(text: str) -> PayPayShortcutMetadata:
    try:
        data = json.loads(text, object_pairs_hook=_strict_object)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed_metadata") from exc
    if not isinstance(data, dict):
        raise ValueError("metadata_must_be_object")
    if set(data) != _METADATA_FIELDS:
        raise ValueError("unknown_or_missing_metadata_fields")
    if not isinstance(data["schema_version"], str):
        raise ValueError("invalid_type:schema_version")
    if data["schema_version"] != "1":
        raise ValueError("unsupported_schema_version")
    for field in ("ingest_id", "original_filename", "shortcut_version", "shared_at"):
        if not isinstance(data[field], str):
            raise ValueError(f"invalid_type:{field}")
    if not isinstance(data["range_confirmed"], bool):
        raise ValueError("invalid_type:range_confirmed")
    if data["range_source"] is not None:
        if not isinstance(data["range_source"], str):
            raise ValueError("invalid_type:range_source")
        if data["range_source"] not in _RANGE_SOURCES:
            raise ValueError("invalid_range_source")
    if not _INGEST_ID.fullmatch(data["ingest_id"]):
        raise ValueError("invalid_ingest_id")
    if not data["shortcut_version"]:
        raise ValueError("invalid_shortcut_version")
    if (
        not data["original_filename"]
        or Path(data["original_filename"]).name != data["original_filename"]
    ):
        raise ValueError("invalid_original_filename")
    if not _UTC_TIMESTAMP.fullmatch(data["shared_at"]):
        raise ValueError("invalid_shared_at")
    try:
        datetime.strptime(data["shared_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError("invalid_shared_at") from exc
    start = _iso_date(data["requested_start"], "requested_start")
    end = _iso_date(data["requested_end"], "requested_end")
    if (start is None) != (end is None):
        raise ValueError("requested_range_must_be_both_or_neither")
    if start and end and start > end:
        raise ValueError("invalid_requested_range")
    if data["range_confirmed"] and not (start and end):
        raise ValueError("confirmed_range_requires_dates")
    if data["range_source"] == "user_confirmed" and not data["range_confirmed"]:
        raise ValueError("user_confirmed_requires_confirmation")
    return PayPayShortcutMetadata(
        schema_version="1", ingest_id=data["ingest_id"],
        requested_start=start, requested_end=end,
        range_confirmed=data["range_confirmed"], range_source=data["range_source"],
        original_filename=data["original_filename"],
        shortcut_version=data["shortcut_version"], shared_at=data["shared_at"],
    )


def load_shortcut_metadata(path: str | Path) -> PayPayShortcutMetadata:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("metadata_unreadable") from exc
    return parse_shortcut_metadata(text)


def _csv_only_preview(folder: Path, csv_path: Path, *, pairing_status: str,
                      metadata_present: bool, metadata_reason: str,
                      unexpected: tuple[str, ...] = ()) -> ShortcutIngestPreview:
    evidence = preview_operational_evidence(csv_path)
    return ShortcutIngestPreview(
        ingest_id=folder.name, folder=str(folder), csv_filename=csv_path.name,
        metadata_present=metadata_present, metadata_valid=False,
        metadata_reason=metadata_reason, pairing_status=pairing_status,
        csv_parse_status="failed" if evidence.parse_error else "success",
        operational_coverage=evidence.operational_coverage,
        operational_reason=evidence.reason, unexpected_files=unexpected,
        operational_evidence=evidence,
    )


def preview_shortcut_ingest_folder(path: str | Path) -> ShortcutIngestPreview:
    folder = Path(path)
    try:
        files = [item for item in folder.iterdir() if item.is_file()]
    except OSError:
        return ShortcutIngestPreview(
            folder.name, str(folder), None, False, False, "folder_unreadable",
            "ambiguous", operational_coverage="rejected",
            operational_reason="folder_unreadable", ambiguous=True,
        )
    csvs = [item for item in files if item.suffix.lower() == ".csv"]
    metadata_files = [item for item in files if item.name.endswith(".kakeibo.json")]
    expected = set(csvs + metadata_files)
    unexpected = tuple(sorted(item.name for item in files if item not in expected))
    if len(csvs) > 1 or len(metadata_files) > 1 or unexpected:
        return ShortcutIngestPreview(
            folder.name, str(folder), None, bool(metadata_files), False,
            "ambiguous_folder_contents", "ambiguous",
            operational_coverage="rejected", operational_reason="ambiguous_folder",
            ambiguous=True, unexpected_files=unexpected,
        )
    csv_path = csvs[0] if csvs else None
    metadata_path = metadata_files[0] if metadata_files else None
    if not csv_path and not metadata_path:
        return ShortcutIngestPreview(
            folder.name, str(folder), None, False, False, "no_expected_files",
            "ambiguous", operational_coverage="rejected",
            operational_reason="no_expected_files", ambiguous=True,
        )
    if csv_path and not metadata_path:
        return _csv_only_preview(
            folder, csv_path, pairing_status="csv_only", metadata_present=False,
            metadata_reason="metadata_missing",
        )
    if metadata_path and not csv_path:
        try:
            metadata = load_shortcut_metadata(metadata_path)
            valid = True
            reason = ("metadata_valid" if metadata.ingest_id == folder.name
                      else "ingest_id_mismatch")
            requested_start, requested_end = metadata.requested_start, metadata.requested_end
            confirmed, source = metadata.range_confirmed, metadata.range_source
        except ValueError as exc:
            valid, reason = False, str(exc)
            requested_start = requested_end = source = None
            confirmed = False
        return ShortcutIngestPreview(
            folder.name, str(folder), None, True, valid, reason,
            "metadata_only" if reason == "metadata_valid" else "ingest_id_mismatch",
            requested_start, requested_end, confirmed, source,
            operational_coverage=None, operational_reason="orphan_metadata", orphan=True,
        )

    assert csv_path is not None and metadata_path is not None
    try:
        metadata = load_shortcut_metadata(metadata_path)
    except ValueError as exc:
        return _csv_only_preview(
            folder, csv_path, pairing_status="metadata_invalid", metadata_present=True,
            metadata_reason=str(exc),
        )
    if metadata.ingest_id != folder.name:
        return _csv_only_preview(
            folder, csv_path, pairing_status="ingest_id_mismatch", metadata_present=True,
            metadata_reason="ingest_id_mismatch",
        )
    if metadata.original_filename != csv_path.name:
        return _csv_only_preview(
            folder, csv_path, pairing_status="original_filename_mismatch",
            metadata_present=True, metadata_reason="original_filename_mismatch",
        )
    evidence = preview_operational_evidence(
        csv_path, requested_start=metadata.requested_start,
        requested_end=metadata.requested_end, range_source=metadata.range_source,
        range_confirmed=metadata.range_confirmed, received_at=metadata.shared_at,
    )
    return ShortcutIngestPreview(
        folder.name, str(folder), csv_path.name, True, True, "metadata_valid",
        "paired", metadata.requested_start, metadata.requested_end,
        metadata.range_confirmed, metadata.range_source,
        "failed" if evidence.parse_error else "success",
        evidence.operational_coverage, evidence.reason,
        operational_evidence=evidence,
    )


def preview_shortcut_inbox(path: str | Path) -> dict:
    root = Path(path)
    try:
        folders = sorted((item for item in root.iterdir() if item.is_dir()),
                         key=lambda item: item.name)
    except OSError as exc:
        return {
            "previewed_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
            "inbox": str(root), "error": f"inbox_unreadable:{exc}", "ingests": [],
        }
    previews = [preview_shortcut_ingest_folder(folder) for folder in folders]
    evidence_indexes = [index for index, item in enumerate(previews)
                        if item.operational_evidence is not None]
    classified, duplicates, conflicts = classify_operational_evidence([
        previews[index].operational_evidence for index in evidence_indexes
        if previews[index].operational_evidence is not None
    ])
    for index, evidence in zip(evidence_indexes, classified):
        previews[index] = replace(
            previews[index], operational_coverage=evidence.operational_coverage,
            operational_reason=evidence.reason, operational_evidence=evidence,
        )
    return {
        "previewed_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True, "inbox": str(root),
        "ingest_count": len(previews), "duplicate_count": duplicates,
        "conflict_count": conflicts,
        "usable_count": sum(item.operational_coverage == "usable" for item in previews),
        "needs_confirmation_count": sum(
            item.operational_coverage == "needs_confirmation" for item in previews
        ),
        "rejected_count": sum(item.operational_coverage == "rejected" for item in previews),
        "ingests": [asdict(item) for item in previews],
    }
