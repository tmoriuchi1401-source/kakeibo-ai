"""Standing, bounded write authority for unattended bank-PDF runs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterable
from uuid import UUID, uuid4

from .aupay_card_production import (
    MAX_CANARY_CAPABILITY_TTL_SECONDS,
    ProductionWriteCapability,
    ProtectedAuditKeyProvider,
    SqliteAttemptJournal,
    SqliteCapabilityStore,
)
from .aupay_card_writer import (
    ReadOnlySheetsTargetInspector,
    TARGET_BINDING_VERSION,
    TargetBinding,
    validate_target_binding,
)
from .bank_pdf_pipeline import CHIBA_BANK_SOURCE, DOCOMO_SMTB_SOURCE, SOURCE
from .canonical_one_row_production import (
    CANONICAL_ONE_ROW_SCHEMA_VERSION,
    CanonicalFiveRowBatch,
    CanonicalFiveRowManifest,
    validate_canonical_five_row_batch,
    validate_canonical_five_row_manifest,
)
from .drive_receipts import normalize_folder_id


BANK_RECURRING_AUTHORITY_SCHEMA_VERSION = 1
BANK_RECURRING_SOURCE = "bank_pdf_drive"
BANK_RECURRING_TARGET_SHEET = "取込データ"
MAX_AUTHORITY_FILES = 20
MAX_AUTHORITY_ROWS = 100
MIN_OVERLAP_SECONDS = 3600
MAX_OVERLAP_SECONDS = 7 * 86400
MAX_WINDOW_SECONDS = 31 * 86400
SUPPORTED_BANK_SOURCES = frozenset({
    SOURCE,
    DOCOMO_SMTB_SOURCE,
    CHIBA_BANK_SOURCE,
})
_SAFE_POLICY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_RUN_CONTEXT_AUTHORITY = object()


@dataclass(frozen=True)
class BankRecurringAuthority:
    policy_id: str
    source: str
    expected_spreadsheet_id: str = field(repr=False)
    expected_drive_folder_id: str = field(repr=False)
    supported_bank_sources: tuple[str, ...]
    allowed_classifications: tuple[str, ...]
    max_files: int
    max_rows: int
    overlap_seconds: int
    max_window_seconds: int
    initial_start: datetime
    valid_from: datetime
    expires_at: datetime
    expected_worksheet: str = BANK_RECURRING_TARGET_SHEET
    target_binding_version: int = TARGET_BINDING_VERSION
    canonical_schema_version: int = CANONICAL_ONE_ROW_SCHEMA_VERSION
    account_alias: str = ""
    expected_branch: str = "main"
    schema_version: int = BANK_RECURRING_AUTHORITY_SCHEMA_VERSION

    def validate(self) -> None:
        aware = (self.initial_start, self.valid_from, self.expires_at)
        supported = tuple(self.supported_bank_sources)
        if any(value.tzinfo is None or value.utcoffset() is None for value in aware):
            raise ValueError("bank_recurring_authority_timezone_required")
        if (
            self.schema_version != BANK_RECURRING_AUTHORITY_SCHEMA_VERSION
            or not _SAFE_POLICY_ID.fullmatch(self.policy_id)
            or self.source != BANK_RECURRING_SOURCE
            or not self.expected_spreadsheet_id
            or not self.expected_drive_folder_id
            or self.expected_worksheet != BANK_RECURRING_TARGET_SHEET
            or self.target_binding_version != TARGET_BINDING_VERSION
            or self.canonical_schema_version != CANONICAL_ONE_ROW_SCHEMA_VERSION
            or not supported
            or len(supported) != len(set(supported))
            or not set(supported).issubset(SUPPORTED_BANK_SOURCES)
            or self.allowed_classifications != ("expense",)
            or not (1 <= self.max_files <= MAX_AUTHORITY_FILES)
            or not (1 <= self.max_rows <= MAX_AUTHORITY_ROWS)
            or not (MIN_OVERLAP_SECONDS <= self.overlap_seconds <= MAX_OVERLAP_SECONDS)
            or not (self.overlap_seconds < self.max_window_seconds <= MAX_WINDOW_SECONDS)
            or self.initial_start >= self.expires_at
            or self.valid_from >= self.expires_at
            or self.expected_branch != "main"
        ):
            raise ValueError("bank_recurring_authority_invalid")
        normalize_folder_id(self.expected_drive_folder_id)

    def authority_reference(self) -> str:
        self.validate()
        payload = {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "source": self.source,
            "expected_spreadsheet_id": self.expected_spreadsheet_id,
            "expected_drive_folder_id": normalize_folder_id(self.expected_drive_folder_id),
            "expected_worksheet": self.expected_worksheet,
            "target_binding_version": self.target_binding_version,
            "canonical_schema_version": self.canonical_schema_version,
            "supported_bank_sources": sorted(self.supported_bank_sources),
            "allowed_classifications": list(self.allowed_classifications),
            "max_files": self.max_files,
            "max_rows": self.max_rows,
            "overlap_seconds": self.overlap_seconds,
            "max_window_seconds": self.max_window_seconds,
            "initial_start": self.initial_start.isoformat(),
            "valid_from": self.valid_from.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "expected_branch": self.expected_branch,
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"bank-recurring-v1:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:32]}"


class ProtectedBankRecurringAuthorityProvider:
    """Load the standing bank authority from outside the repository."""

    def __init__(self, path: str | Path, *, repo_root: str | Path):
        self.path = Path(path).expanduser().resolve()
        root = Path(repo_root).resolve()
        try:
            self.path.relative_to(root)
        except ValueError:
            pass
        else:
            raise RuntimeError("bank_recurring_authority_must_be_outside_repository")

    def __repr__(self) -> str:
        return "ProtectedBankRecurringAuthorityProvider(path=<redacted>)"

    def load(self) -> BankRecurringAuthority:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            policy = BankRecurringAuthority(
                policy_id=str(raw["policy_id"]),
                source=str(raw["source"]),
                expected_spreadsheet_id=str(raw["expected_spreadsheet_id"]),
                expected_drive_folder_id=str(raw["expected_drive_folder_id"]),
                expected_worksheet=str(raw.get("expected_worksheet", BANK_RECURRING_TARGET_SHEET)),
                target_binding_version=int(raw.get("target_binding_version", TARGET_BINDING_VERSION)),
                canonical_schema_version=int(raw.get("canonical_schema_version", CANONICAL_ONE_ROW_SCHEMA_VERSION)),
                supported_bank_sources=tuple(str(item) for item in raw["supported_bank_sources"]),
                allowed_classifications=tuple(str(item) for item in raw["allowed_classifications"]),
                max_files=int(raw["max_files"]),
                max_rows=int(raw["max_rows"]),
                overlap_seconds=int(raw["overlap_seconds"]),
                max_window_seconds=int(raw["max_window_seconds"]),
                initial_start=datetime.fromisoformat(str(raw["initial_start"])),
                valid_from=datetime.fromisoformat(str(raw["valid_from"])),
                expires_at=datetime.fromisoformat(str(raw["expires_at"])),
                account_alias=str(raw.get("account_alias", "")),
                expected_branch=str(raw.get("expected_branch", "main")),
                schema_version=int(raw.get("schema_version", 0)),
            )
            policy.validate()
            return policy
        except Exception as exc:
            raise RuntimeError("protected_bank_recurring_authority_invalid") from exc


@dataclass(frozen=True, init=False)
class BankRecurringRunContext:
    """In-memory grant minted only after the recurring launcher validates a run."""

    policy: BankRecurringAuthority = field(repr=False)
    run_id: str
    files_seen: int
    candidate_batches: tuple[tuple[str, ...], ...] = field(repr=False)
    total_rows: int
    authority_ref: str
    _authority: object = field(repr=False, compare=False)

    def __new__(cls, *args, **kwargs):
        raise TypeError("bank_recurring_run_context_is_launcher_only")

    @classmethod
    def _create(cls, **values) -> "BankRecurringRunContext":
        context = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(context, name, value)
        object.__setattr__(context, "_authority", _RUN_CONTEXT_AUTHORITY)
        return context


def create_bank_recurring_run_context(
    *,
    authority_provider: ProtectedBankRecurringAuthorityProvider,
    run_id: str,
    now: datetime,
    spreadsheet_id: str,
    drive_folder_id: str,
    files_seen: int,
    candidate_batches: Iterable[Iterable[str]],
) -> BankRecurringRunContext:
    if type(authority_provider) is not ProtectedBankRecurringAuthorityProvider:
        raise RuntimeError("formal_bank_recurring_authority_required")
    try:
        UUID(run_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("bank_recurring_run_id_invalid") from exc
    policy = authority_provider.load()
    batches = tuple(tuple(batch) for batch in candidate_batches)
    identities = tuple(identity for batch in batches for identity in batch)
    if (
        not (policy.valid_from <= now < policy.expires_at)
        or spreadsheet_id != policy.expected_spreadsheet_id
        or normalize_folder_id(drive_folder_id)
        != normalize_folder_id(policy.expected_drive_folder_id)
        or not (0 <= files_seen <= policy.max_files)
        or not (1 <= len(identities) <= policy.max_rows)
        or len(identities) != len(set(identities))
        or any(not batch for batch in batches)
        or len(batches) != len(set(batches))
    ):
        raise RuntimeError("bank_recurring_authority_scope_mismatch")
    return BankRecurringRunContext._create(
        policy=policy,
        run_id=run_id,
        files_seen=files_seen,
        candidate_batches=batches,
        total_rows=len(identities),
        authority_ref=policy.authority_reference(),
    )


def issue_bank_recurring_batch_capability(
    batch: CanonicalFiveRowBatch,
    manifest: CanonicalFiveRowManifest,
    *,
    context: BankRecurringRunContext,
    binding: TargetBinding,
    inspector: ReadOnlySheetsTargetInspector,
    key_provider: ProtectedAuditKeyProvider,
    journal: SqliteAttemptJournal,
    capability_store: SqliteCapabilityStore,
    clock,
) -> ProductionWriteCapability:
    """Mint one short-lived existing-writer capability under standing authority."""
    now = clock()
    if (
        type(context) is not BankRecurringRunContext
        or getattr(context, "_authority", None) is not _RUN_CONTEXT_AUTHORITY
        or type(key_provider) is not ProtectedAuditKeyProvider
        or type(journal) is not SqliteAttemptJournal
        or type(capability_store) is not SqliteCapabilityStore
    ):
        raise RuntimeError("formal_bank_recurring_authority_required")
    policy = context.policy
    policy.validate()
    key = key_provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    batch = validate_canonical_five_row_batch(batch)
    validate_canonical_five_row_manifest(batch, manifest, binding=binding, audit_key=key)
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    identities = tuple(candidate.identity for candidate in batch.candidates)
    if (
        not (policy.valid_from <= now < policy.expires_at)
        or binding.expected_spreadsheet_id != policy.expected_spreadsheet_id
        or binding.expected_worksheet != policy.expected_worksheet
        or binding.binding_version != policy.target_binding_version
        or manifest.expected_branch != policy.expected_branch
        or manifest.authority_mode != "bank_steady_state"
        or batch.authority_mode != "bank_steady_state"
        or identities not in context.candidate_batches
        or batch.max_rows > policy.max_rows
        or context.total_rows > policy.max_rows
        or manifest.income_count != 0
        or manifest.expense_count != batch.max_rows
        or any(candidate.schema_version != policy.canonical_schema_version for candidate in batch.candidates)
        or any(candidate.source not in policy.supported_bank_sources for candidate in batch.candidates)
        or any(candidate.transaction_kind not in policy.allowed_classifications for candidate in batch.candidates)
        or any(candidate.write_eligibility != "eligible" for candidate in batch.candidates)
    ):
        raise RuntimeError("bank_recurring_authority_scope_mismatch")
    if not journal.ready(manifest.run_id):
        raise RuntimeError("attempt_journal_unavailable")
    if not capability_store.ready():
        raise RuntimeError("capability_store_unavailable")
    capability = ProductionWriteCapability(
        capability_id=str(uuid4()),
        run_id=manifest.run_id,
        plan_binding_ref=manifest.plan_binding_ref,
        target_ref=manifest.target_ref,
        candidate_ref=manifest.batch_ref,
        approval_reference=f"recurring:{context.authority_ref}:{context.run_id}",
        issued_at=now,
        expires_at=now + timedelta(seconds=min(120, MAX_CANARY_CAPABILITY_TTL_SECONDS)),
        token=hashlib.sha256(os.urandom(32)).hexdigest(),
    )
    capability_store.issue(capability)
    return capability
