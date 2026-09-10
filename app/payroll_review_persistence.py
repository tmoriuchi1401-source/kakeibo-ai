"""Signed, production-independent persistence for explicit Payroll review decisions.

The journal stores operator decisions and the evidence already accepted by
``payroll_review_authority``. Reloading never invents a field or value: every
record is revalidated against a freshly parsed storage candidate before an
all-or-nothing application to a copy. This module has no Payroll writer,
Sheets writer, Drive mutation, or ownership-production dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Literal

from .payroll_review_authority import (
    PayrollReviewAssertion,
    PayrollReviewEvidence,
    PayrollReviewSourceValueBinding,
    _assertion_payload,
    _mac,
    _require_key,
    apply_payroll_review_assertion,
    preview_payroll_review_assertion,
)
from .payroll_sheets import PayrollSheetsSnapshot
from .payroll_storage import PayrollStorageCandidate


JOURNAL_VERSION = "payroll-review-decision-journal-v1"
RECORD_VERSION = "payroll-review-decision-record-v1"
ALLOWED_DECISIONS = {"confirm_existing_value", "exclude_non_item"}
AppliedProvenance = Literal["confirmed_in_memory_review_authority_v1"]


@dataclass(frozen=True)
class PersistedPayrollReviewDecision:
    record_version: str
    assertion_id: str
    raw_label: str
    parser_raw_value: str | None
    source_value: str | None
    parser_mode: str
    created_at_utc: str
    applied_at_utc: str
    applied_provenance: AppliedProvenance
    evidence: PayrollReviewEvidence = field(repr=False)
    assertion: PayrollReviewAssertion = field(repr=False)
    record_signature: str = field(repr=False)


@dataclass(frozen=True)
class PayrollReviewDecisionJournal:
    journal_version: str
    created_at_utc: str
    records: tuple[PersistedPayrollReviewDecision, ...] = field(repr=False)
    journal_signature: str = field(repr=False)


@dataclass(frozen=True)
class PayrollReviewJournalWritePreview:
    target_path: Path = field(repr=False)
    status: Literal["ready", "already_present", "conflict"]
    record_count: int
    content_sha256: str
    payload: bytes = field(repr=False)


@dataclass(frozen=True)
class PayrollReviewJournalWriteResult:
    preview: PayrollReviewJournalWritePreview
    written: bool


@dataclass(frozen=True)
class PayrollReviewJournalReplayResult:
    candidate: PayrollStorageCandidate = field(repr=False)
    accepted: bool
    reason_code: str
    applied_count: int


def _require_utc(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("review_journal_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("review_journal_timestamp_invalid")


def _binding_dict(binding: PayrollReviewSourceValueBinding | None):
    return asdict(binding) if binding is not None else None


def _evidence_dict(evidence: PayrollReviewEvidence):
    values = asdict(evidence)
    values["source_value_binding"] = _binding_dict(evidence.source_value_binding)
    return values


def _assertion_dict(assertion: PayrollReviewAssertion):
    return asdict(assertion)


def _record_payload(record: PersistedPayrollReviewDecision):
    return {
        "record_version": record.record_version,
        "assertion_id": record.assertion_id,
        "raw_label": record.raw_label,
        "parser_raw_value": record.parser_raw_value,
        "source_value": record.source_value,
        "parser_mode": record.parser_mode,
        "created_at_utc": record.created_at_utc,
        "applied_at_utc": record.applied_at_utc,
        "applied_provenance": record.applied_provenance,
        "evidence": _evidence_dict(record.evidence),
        "assertion": _assertion_dict(record.assertion),
    }


def _record_dict(record: PersistedPayrollReviewDecision):
    return {**_record_payload(record), "record_signature": record.record_signature}


def _journal_payload(journal: PayrollReviewDecisionJournal):
    return {
        "journal_version": journal.journal_version,
        "created_at_utc": journal.created_at_utc,
        "records": [_record_dict(record) for record in journal.records],
    }


def _journal_dict(journal: PayrollReviewDecisionJournal):
    return {**_journal_payload(journal), "journal_signature": journal.journal_signature}


def _assertion_id(assertion: PayrollReviewAssertion, local_key: bytes) -> str:
    return _mac(local_key, (
        "payroll-review-persisted-assertion-id-v1",
        _assertion_payload(assertion),
        assertion.signature,
    ))


def capture_persisted_payroll_review_decision(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    evidence: PayrollReviewEvidence,
    assertion: PayrollReviewAssertion,
    *,
    raw_label: str,
    parser_raw_value: str | None,
    local_key: bytes,
    created_at_utc: str,
    applied_at_utc: str,
    applied_provenance: AppliedProvenance = (
        "confirmed_in_memory_review_authority_v1"
    ),
) -> PersistedPayrollReviewDecision:
    """Capture one decision only after the ordinary preview accepts it."""

    _require_key(local_key)
    _require_utc(created_at_utc)
    _require_utc(applied_at_utc)
    evaluation = preview_payroll_review_assertion(
        candidate, snapshot, evidence, assertion, local_key=local_key,
    )
    if not evaluation.accepted:
        raise ValueError(evaluation.reason_code)
    if assertion.decision not in ALLOWED_DECISIONS:
        raise ValueError("review_journal_unknown_decision")
    item = candidate.items[evidence.item_occurrence]
    if raw_label != item.raw_item_name or parser_raw_value != item.raw_value:
        raise ValueError("review_journal_raw_item_mismatch")
    source_value = (
        evidence.source_value_binding.source_value
        if evidence.source_value_binding is not None else item.raw_value
    )
    unsigned = PersistedPayrollReviewDecision(
        record_version=RECORD_VERSION,
        assertion_id=_assertion_id(assertion, local_key),
        raw_label=raw_label,
        parser_raw_value=parser_raw_value,
        source_value=source_value,
        parser_mode=candidate.parse_method,
        created_at_utc=created_at_utc,
        applied_at_utc=applied_at_utc,
        applied_provenance=applied_provenance,
        evidence=evidence,
        assertion=assertion,
        record_signature="",
    )
    return PersistedPayrollReviewDecision(
        **{**unsigned.__dict__, "record_signature": _mac(
            local_key, _record_payload(unsigned),
        )},
    )


def build_payroll_review_decision_journal(
    records,
    *,
    local_key: bytes,
    created_at_utc: str,
) -> PayrollReviewDecisionJournal:
    """Build one immutable journal, rejecting duplicate item decisions."""

    _require_key(local_key)
    _require_utc(created_at_utc)
    records = tuple(records)
    if not records:
        raise ValueError("review_journal_empty")
    assertion_ids = [record.assertion_id for record in records]
    item_keys = [
        (record.evidence.source_file_id_digest,
         record.evidence.item_occurrence)
        for record in records
    ]
    if len(set(assertion_ids)) != len(assertion_ids):
        raise ValueError("review_journal_duplicate_assertion")
    if len(set(item_keys)) != len(item_keys):
        raise ValueError("review_journal_duplicate_item")
    for record in records:
        _validate_record(record, local_key)
    unsigned = PayrollReviewDecisionJournal(
        JOURNAL_VERSION, created_at_utc, records, "",
    )
    return PayrollReviewDecisionJournal(
        JOURNAL_VERSION,
        created_at_utc,
        records,
        _mac(local_key, _journal_payload(unsigned)),
    )


def serialize_payroll_review_decision_journal(journal) -> bytes:
    return (json.dumps(
        _journal_dict(journal), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n").encode("utf-8")


def _strict_keys(values, expected, reason):
    if not isinstance(values, dict) or set(values) != set(expected):
        raise ValueError(reason)


def _binding_from_dict(values):
    if values is None:
        return None
    expected = PayrollReviewSourceValueBinding.__dataclass_fields__
    _strict_keys(values, expected, "review_journal_binding_shape_invalid")
    return PayrollReviewSourceValueBinding(**values)


def _evidence_from_dict(values):
    expected = PayrollReviewEvidence.__dataclass_fields__
    _strict_keys(values, expected, "review_journal_evidence_shape_invalid")
    values = dict(values)
    values["source_value_binding"] = _binding_from_dict(
        values["source_value_binding"],
    )
    return PayrollReviewEvidence(**values)


def _assertion_from_dict(values):
    expected = PayrollReviewAssertion.__dataclass_fields__
    _strict_keys(values, expected, "review_journal_assertion_shape_invalid")
    if values.get("decision") not in ALLOWED_DECISIONS:
        raise ValueError("review_journal_unknown_decision")
    return PayrollReviewAssertion(**values)


def deserialize_payroll_review_decision_journal(
    payload: bytes | str,
    *,
    local_key: bytes,
) -> PayrollReviewDecisionJournal:
    """Parse and authenticate an untrusted journal without applying it."""

    _require_key(local_key)
    if isinstance(payload, bytes):
        if len(payload) > 10_000_000:
            raise ValueError("review_journal_too_large")
        payload = payload.decode("utf-8")
    try:
        values = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("review_journal_json_invalid") from exc
    _strict_keys(
        values,
        {"journal_version", "created_at_utc", "records", "journal_signature"},
        "review_journal_shape_invalid",
    )
    if values["journal_version"] != JOURNAL_VERSION:
        raise ValueError("review_journal_version_stale")
    if not isinstance(values["records"], list):
        raise ValueError("review_journal_shape_invalid")
    records = []
    record_keys = {
        "record_version", "assertion_id", "raw_label", "parser_raw_value",
        "source_value", "parser_mode", "created_at_utc", "applied_at_utc",
        "applied_provenance", "evidence", "assertion", "record_signature",
    }
    for raw in values["records"]:
        _strict_keys(raw, record_keys, "review_journal_record_shape_invalid")
        if raw.get("assertion", {}).get("decision") not in ALLOWED_DECISIONS:
            raise ValueError("review_journal_unknown_decision")
        records.append(PersistedPayrollReviewDecision(
            record_version=raw["record_version"],
            assertion_id=raw["assertion_id"],
            raw_label=raw["raw_label"],
            parser_raw_value=raw["parser_raw_value"],
            source_value=raw["source_value"],
            parser_mode=raw["parser_mode"],
            created_at_utc=raw["created_at_utc"],
            applied_at_utc=raw["applied_at_utc"],
            applied_provenance=raw["applied_provenance"],
            evidence=_evidence_from_dict(raw["evidence"]),
            assertion=_assertion_from_dict(raw["assertion"]),
            record_signature=raw["record_signature"],
        ))
    journal = PayrollReviewDecisionJournal(
        values["journal_version"], values["created_at_utc"], tuple(records),
        values["journal_signature"],
    )
    _validate_journal(journal, local_key)
    return journal


def _validate_record(record, local_key):
    if record.record_version != RECORD_VERSION:
        raise ValueError("review_journal_record_version_stale")
    _require_utc(record.created_at_utc)
    _require_utc(record.applied_at_utc)
    if record.applied_provenance != "confirmed_in_memory_review_authority_v1":
        raise ValueError("review_journal_applied_provenance_invalid")
    if record.assertion.decision not in ALLOWED_DECISIONS:
        raise ValueError("review_journal_unknown_decision")
    if record.assertion_id != _assertion_id(record.assertion, local_key):
        raise ValueError("review_journal_assertion_id_invalid")
    if record.assertion.evidence_id != record.evidence.evidence_id:
        raise ValueError("review_journal_assertion_evidence_mismatch")
    if record.evidence.raw_label_digest != _mac(
        local_key, ("raw_label", record.raw_label),
    ):
        raise ValueError("review_journal_raw_label_invalid")
    if record.evidence.raw_value_digest != _mac(
        local_key, ("raw_value", record.parser_raw_value),
    ):
        raise ValueError("review_journal_raw_value_invalid")
    expected_source = (
        record.evidence.source_value_binding.source_value
        if record.evidence.source_value_binding is not None
        else record.parser_raw_value
    )
    if record.source_value != expected_source:
        raise ValueError("review_journal_source_value_invalid")
    if not hmac.compare_digest(
        record.record_signature, _mac(local_key, _record_payload(record)),
    ):
        raise ValueError("review_journal_record_signature_invalid")


def _validate_journal(journal, local_key):
    if journal.journal_version != JOURNAL_VERSION:
        raise ValueError("review_journal_version_stale")
    _require_utc(journal.created_at_utc)
    if not journal.records:
        raise ValueError("review_journal_empty")
    for record in journal.records:
        _validate_record(record, local_key)
    assertion_ids = [record.assertion_id for record in journal.records]
    item_keys = [
        (record.evidence.source_file_id_digest,
         record.evidence.item_occurrence)
        for record in journal.records
    ]
    if len(set(assertion_ids)) != len(assertion_ids):
        raise ValueError("review_journal_duplicate_assertion")
    if len(set(item_keys)) != len(item_keys):
        raise ValueError("review_journal_duplicate_item")
    if not hmac.compare_digest(
        journal.journal_signature, _mac(local_key, _journal_payload(journal)),
    ):
        raise ValueError("review_journal_signature_invalid")


def replay_payroll_review_decision_journal(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    journal: PayrollReviewDecisionJournal,
    *,
    local_key: bytes,
) -> PayrollReviewJournalReplayResult:
    """Revalidate and apply all records atomically to a fresh candidate copy."""

    original = candidate.model_copy(deep=True)
    try:
        _validate_journal(journal, local_key)
    except ValueError as exc:
        return PayrollReviewJournalReplayResult(original, False, str(exc), 0)
    working = candidate.model_copy(deep=True)
    applied_count = 0
    for record in journal.records:
        index = record.evidence.item_occurrence
        if not 0 <= index < len(working.items):
            return PayrollReviewJournalReplayResult(
                original, False, "review_journal_item_occurrence_mismatch", 0,
            )
        item = working.items[index]
        if (working.parse_method != record.parser_mode
                or item.raw_item_name != record.raw_label
                or item.raw_value != record.parser_raw_value):
            return PayrollReviewJournalReplayResult(
                original, False, "review_journal_raw_item_mismatch", 0,
            )
        evaluation = preview_payroll_review_assertion(
            working, snapshot, record.evidence, record.assertion,
            local_key=local_key,
        )
        if not evaluation.accepted:
            return PayrollReviewJournalReplayResult(
                original, False, evaluation.reason_code, 0,
            )
        applied = apply_payroll_review_assertion(
            working, snapshot, record.evidence, record.assertion,
            local_key=local_key, confirmed=True,
        )
        if not applied.applied:
            return PayrollReviewJournalReplayResult(
                original, False, applied.evaluation.reason_code, 0,
            )
        working = applied.candidate
        applied_count += 1
    return PayrollReviewJournalReplayResult(
        working, True, "review_journal_applied", applied_count,
    )


def load_payroll_review_decision_journal(
    path, *, local_key: bytes, repository_root,
):
    path = _outside_repository(path, repository_root)
    return deserialize_payroll_review_decision_journal(
        Path(path).read_bytes(), local_key=local_key,
    )


def _outside_repository(path, repository_root):
    target = Path(path).resolve()
    root = Path(repository_root).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return target
    raise ValueError("review_journal_must_be_outside_repository")


def preview_payroll_review_journal_write(
    journal: PayrollReviewDecisionJournal,
    target_path,
    *,
    repository_root,
    local_key: bytes,
) -> PayrollReviewJournalWritePreview:
    """Preview an external local artifact write without changing the filesystem."""

    _validate_journal(journal, local_key)
    target = _outside_repository(target_path, repository_root)
    if target.suffix.lower() != ".json":
        raise ValueError("review_journal_json_target_required")
    if target.is_symlink():
        raise ValueError("review_journal_symlink_target_rejected")
    payload = serialize_payroll_review_decision_journal(journal)
    digest = hashlib.sha256(payload).hexdigest()
    if target.exists():
        status = "already_present" if target.read_bytes() == payload else "conflict"
    else:
        status = "ready"
    return PayrollReviewJournalWritePreview(
        target, status, len(journal.records), digest, payload,
    )


def write_payroll_review_journal(
    preview: PayrollReviewJournalWritePreview,
    *,
    repository_root,
    local_key: bytes,
    confirmed: bool = False,
) -> PayrollReviewJournalWriteResult:
    """Write only this review journal; never invokes a Payroll data writer."""

    journal = deserialize_payroll_review_decision_journal(
        preview.payload, local_key=local_key,
    )
    current = preview_payroll_review_journal_write(
        journal, preview.target_path,
        repository_root=repository_root, local_key=local_key,
    )
    if current != preview:
        raise ValueError("review_journal_preview_stale_or_invalid")
    if not confirmed or current.status != "ready":
        return PayrollReviewJournalWriteResult(preview, False)
    current.target_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with current.target_path.open("xb") as handle:
            handle.write(current.payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            current.target_path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError as exc:
        raise ValueError("review_journal_target_changed_after_preview") from exc
    return PayrollReviewJournalWriteResult(preview, True)


def load_local_payroll_review_hmac_key(path, *, repository_root=None) -> bytes:
    """Load a separate runtime key while rejecting repository-local secrets."""

    key_path = Path(path).resolve()
    if repository_root is not None:
        _outside_repository(key_path, repository_root)
    try:
        key = bytes.fromhex(key_path.read_text(encoding="ascii").strip())
    except Exception as exc:
        raise ValueError("review_hmac_key_unavailable") from exc
    if len(key) != 32:
        raise ValueError("review_hmac_key_invalid")
    return key
