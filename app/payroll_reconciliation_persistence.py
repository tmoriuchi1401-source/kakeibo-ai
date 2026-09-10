"""Repo-external persistence for explicit Payroll source reconciliation.

The journal contains already signed operator decisions and adds a journal-level
HMAC. It has no source discovery, same-statement inference, Payroll writer, or
production mutation dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from typing import Literal

from .payroll_source_reconciliation import (
    RECONCILIATION_PROVENANCE,
    RECONCILIATION_VERSION,
    PayrollSourceReconciliationDecision,
    deserialize_payroll_source_reconciliation_decision,
    serialize_payroll_source_reconciliation_decision,
)


JOURNAL_VERSION = "payroll-source-reconciliation-journal-v1"


@dataclass(frozen=True)
class PayrollReconciliationDecisionJournal:
    journal_version: str
    created_at_utc: str
    records: tuple[PayrollSourceReconciliationDecision, ...] = field(repr=False)
    journal_signature: str = field(repr=False)


@dataclass(frozen=True)
class ReconciliationJournalWritePreview:
    target_path: Path = field(repr=False)
    status: Literal["ready", "already_present", "conflict"]
    record_count: int
    content_sha256: str
    preview_signature: str = field(repr=False)
    payload: bytes = field(repr=False)


@dataclass(frozen=True)
class ReconciliationJournalWriteResult:
    preview: ReconciliationJournalWritePreview
    written: bool


def _require_key(local_key: bytes) -> None:
    if type(local_key) is not bytes or len(local_key) != 32:
        raise ValueError("reconciliation_hmac_key_invalid")


def _require_utc(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("reconciliation_journal_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reconciliation_journal_timestamp_invalid")


def _outside_repository(path, repository_root) -> Path:
    target = Path(path).resolve()
    root = Path(repository_root).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return target
    raise ValueError("reconciliation_journal_must_be_outside_repository")


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _mac(local_key: bytes, value) -> str:
    return hmac.new(local_key, _canonical(value), hashlib.sha256).hexdigest()


def _record_dict(record: PayrollSourceReconciliationDecision) -> dict:
    return asdict(record)


def _journal_payload(journal: PayrollReconciliationDecisionJournal) -> dict:
    return {
        "journal_version": journal.journal_version,
        "created_at_utc": journal.created_at_utc,
        "records": [_record_dict(record) for record in journal.records],
    }


def _journal_dict(journal: PayrollReconciliationDecisionJournal) -> dict:
    return {
        **_journal_payload(journal),
        "journal_signature": journal.journal_signature,
    }


def _validated_record(
    record: PayrollSourceReconciliationDecision,
    local_key: bytes,
) -> PayrollSourceReconciliationDecision:
    if not isinstance(record, PayrollSourceReconciliationDecision):
        raise ValueError("reconciliation_journal_record_invalid")
    validated = deserialize_payroll_source_reconciliation_decision(
        serialize_payroll_source_reconciliation_decision(record),
        local_key=local_key,
    )
    _validate_record_contract(validated)
    return validated


def _validate_record_contract(record: PayrollSourceReconciliationDecision) -> None:
    if record.contract_version != RECONCILIATION_VERSION:
        raise ValueError("reconciliation_contract_version_stale")
    if type(record.decision_revision) is not int or record.decision_revision != 1:
        raise ValueError("reconciliation_decision_revision_stale")
    if record.decision != "same_statement_alternate_source":
        raise ValueError("reconciliation_decision_unsupported")
    if record.provenance != RECONCILIATION_PROVENANCE:
        raise ValueError("reconciliation_provenance_invalid")
    _require_utc(record.created_at_utc)


def build_reconciliation_journal(
    records,
    *,
    local_key: bytes,
    created_at_utc: str,
) -> PayrollReconciliationDecisionJournal:
    """Build one immutable journal for one explicitly approved alternate."""

    _require_key(local_key)
    _require_utc(created_at_utc)
    records = tuple(_validated_record(record, local_key) for record in records)
    if len(records) != 1:
        raise ValueError("reconciliation_journal_record_count_invalid")
    unsigned = PayrollReconciliationDecisionJournal(
        JOURNAL_VERSION, created_at_utc, records, "",
    )
    return PayrollReconciliationDecisionJournal(
        JOURNAL_VERSION,
        created_at_utc,
        records,
        _mac(local_key, _journal_payload(unsigned)),
    )


def serialize_reconciliation_journal(
    journal: PayrollReconciliationDecisionJournal,
) -> bytes:
    return _canonical(_journal_dict(journal)) + b"\n"


def _strict_keys(values, expected, reason) -> None:
    if not isinstance(values, dict) or set(values) != set(expected):
        raise ValueError(reason)


def deserialize_reconciliation_journal(
    payload: bytes | str,
    *,
    local_key: bytes,
) -> PayrollReconciliationDecisionJournal:
    """Strictly parse and authenticate an untrusted journal."""

    _require_key(local_key)
    if isinstance(payload, bytes):
        if len(payload) > 1_000_000:
            raise ValueError("reconciliation_journal_too_large")
        try:
            payload = payload.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("reconciliation_journal_json_invalid") from exc
    try:
        values = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("reconciliation_journal_json_invalid") from exc
    _strict_keys(
        values,
        {"journal_version", "created_at_utc", "records", "journal_signature"},
        "reconciliation_journal_shape_invalid",
    )
    if values["journal_version"] != JOURNAL_VERSION:
        raise ValueError("reconciliation_journal_version_stale")
    _require_utc(values["created_at_utc"])
    if not isinstance(values["records"], list) or len(values["records"]) != 1:
        raise ValueError("reconciliation_journal_record_count_invalid")
    record_fields = PayrollSourceReconciliationDecision.__dataclass_fields__
    raw_record = values["records"][0]
    _strict_keys(
        raw_record, record_fields, "reconciliation_journal_record_shape_invalid",
    )
    record = deserialize_payroll_source_reconciliation_decision(
        _canonical(raw_record), local_key=local_key,
    )
    _validate_record_contract(record)
    journal = PayrollReconciliationDecisionJournal(
        values["journal_version"],
        values["created_at_utc"],
        (record,),
        values["journal_signature"],
    )
    if not isinstance(journal.journal_signature, str) or not hmac.compare_digest(
        journal.journal_signature,
        _mac(local_key, _journal_payload(journal)),
    ):
        raise ValueError("reconciliation_journal_signature_invalid")
    return journal


def load_reconciliation_journal(
    path,
    *,
    local_key: bytes,
    repository_root,
) -> PayrollReconciliationDecisionJournal:
    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("reconciliation_journal_unavailable")
    target = _outside_repository(supplied, repository_root)
    if not target.is_file():
        raise ValueError("reconciliation_journal_unavailable")
    return deserialize_reconciliation_journal(
        target.read_bytes(), local_key=local_key,
    )


def preview_reconciliation_journal_write(
    journal: PayrollReconciliationDecisionJournal,
    target_path,
    *,
    repository_root,
    local_key: bytes,
) -> ReconciliationJournalWritePreview:
    """Preview an exact repo-external write without touching the filesystem."""

    validated = deserialize_reconciliation_journal(
        serialize_reconciliation_journal(journal), local_key=local_key,
    )
    supplied = Path(target_path)
    if supplied.is_symlink():
        raise ValueError("reconciliation_journal_target_invalid")
    target = _outside_repository(supplied, repository_root)
    if target.suffix.lower() != ".json":
        raise ValueError("reconciliation_journal_target_invalid")
    payload = serialize_reconciliation_journal(validated)
    status: Literal["ready", "already_present", "conflict"]
    if target.exists():
        status = "already_present" if target.read_bytes() == payload else "conflict"
    else:
        status = "ready"
    content_sha256 = hashlib.sha256(payload).hexdigest()
    preview_signature = _mac(local_key, (
        "payroll-reconciliation-journal-write-preview-v1",
        str(target), status, len(validated.records), content_sha256,
    ))
    return ReconciliationJournalWritePreview(
        target,
        status,
        len(validated.records),
        content_sha256,
        preview_signature,
        payload,
    )


def write_reconciliation_journal(
    preview: ReconciliationJournalWritePreview,
    *,
    repository_root,
    local_key: bytes,
    confirmed: bool = False,
) -> ReconciliationJournalWriteResult:
    """Write only the previewed journal with exclusive-create semantics."""

    journal = deserialize_reconciliation_journal(
        preview.payload, local_key=local_key,
    )
    current = preview_reconciliation_journal_write(
        journal,
        preview.target_path,
        repository_root=repository_root,
        local_key=local_key,
    )
    if current != preview:
        raise ValueError("reconciliation_journal_preview_stale_or_invalid")
    if not confirmed or current.status != "ready":
        return ReconciliationJournalWriteResult(preview, False)
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
        raise ValueError("reconciliation_journal_target_changed_after_preview") from exc
    return ReconciliationJournalWriteResult(preview, True)


def load_reconciliation_key(path, *, repository_root) -> bytes:
    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("reconciliation_hmac_key_unavailable")
    target = _outside_repository(supplied, repository_root)
    if not target.is_file():
        raise ValueError("reconciliation_hmac_key_unavailable")
    try:
        key = bytes.fromhex(target.read_text(encoding="ascii").strip())
    except Exception as exc:
        raise ValueError("reconciliation_hmac_key_unavailable") from exc
    _require_key(key)
    return key


def write_new_reconciliation_key(path, *, repository_root) -> Path:
    """Create one dedicated random key outside the repository, never overwrite."""

    supplied = Path(path)
    if supplied.is_symlink():
        raise ValueError("reconciliation_hmac_key_target_invalid")
    target = _outside_repository(supplied, repository_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        with target.open("x", encoding="ascii", newline="\n") as handle:
            handle.write(key.hex() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            target.chmod(0o600)
        except OSError:
            pass
    except FileExistsError as exc:
        raise ValueError("reconciliation_hmac_key_already_exists") from exc
    return target
