"""Source-independent sealed transport for one canonical import row."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable, Mapping, Protocol
from uuid import UUID, uuid4

from .aupay_card_executor import (
    CandidateState,
    CanonicalIdentityReader,
    ExistingCanonicalRecord,
    IdentityRead,
)
from .aupay_card_production import (
    MAX_CANARY_CAPABILITY_TTL_SECONDS,
    CanaryApproval,
    ProtectedAuditKeyProvider,
    ProtectedCanaryApprovalProvider,
    ProductionWriteCapability,
    SqliteAttemptJournal,
    SqliteCapabilityStore,
    SqliteLeaseManager,
    target_binding_reference,
)
from .aupay_card_writer import (
    JournalEvent,
    JournalStage,
    PersistentAuditKey,
    ReadBackPolicy,
    TargetBinding,
    TargetInspector,
    WriteDisposition,
    WriteRequestResult,
    canonical_candidate_reference_v2,
    validate_target_binding,
)
from .canonical_import import materialize_import_row
from .sheets import HEADERS


CANONICAL_ONE_ROW_SCHEMA_VERSION = 1
CANONICAL_ONE_ROW_MAX_ROWS = 1
CANONICAL_BOUNDED_BATCH_ROWS = 5
CANONICAL_INITIAL_BACKFILL_ROWS = 51
CANONICAL_TRANSPORT_ROW_BOUNDS = frozenset({
    CANONICAL_ONE_ROW_MAX_ROWS,
    CANONICAL_BOUNDED_BATCH_ROWS,
    CANONICAL_INITIAL_BACKFILL_ROWS,
})
_CANDIDATE_AUTHORITY = object()
_BATCH_CANDIDATE_AUTHORITY = object()
_DISPATCH_AUTHORITY = object()
_HEAD = re.compile(r"[0-9a-f]{40}")


def _aware(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError(reason)
    return value


@dataclass(frozen=True, init=False)
class CanonicalOneRowCandidate:
    """Immutable writer input issued only from a validated bank canary plan."""

    schema_version: int
    identity: str
    source: str
    source_record_id: str
    transaction_date: str
    merchant: str = field(repr=False)
    amount_yen: int = field(repr=False)
    transaction_kind: str
    payment_method: str
    business_fingerprint: str
    memo: str = field(repr=False)
    member: str
    source_occurrence: int
    source_hash: str
    reconciliation_state: str
    source_identities: tuple[str, ...]
    cross_source_state: str
    cross_source_candidate_identities: tuple[str, ...]
    import_status: str
    write_eligibility: str
    max_rows: int
    _authority: object = field(repr=False)

    def __new__(cls, *args, **kwargs):
        raise TypeError("canonical_one_row_candidate_is_projection_only")

    @classmethod
    def _create(cls, *, authority_token: object, **values) -> "CanonicalOneRowCandidate":
        if authority_token not in {
            _CANDIDATE_AUTHORITY, _BATCH_CANDIDATE_AUTHORITY,
        }:
            raise TypeError("canonical_one_row_candidate_is_projection_only")
        candidate = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(candidate, name, value)
        object.__setattr__(candidate, "_authority", authority_token)
        return validate_canonical_one_row_candidate(candidate)

    def to_import_row(self, *, imported_at) -> list:
        validate_canonical_one_row_candidate(self)
        return materialize_import_row(
            self, imported_at=imported_at, status=self.import_status,
        )


def _project_bank_candidate(
    transaction,
    *,
    classification: str,
    write_eligibility: str,
    import_status: str,
    max_rows: int,
    authority_token: object = _CANDIDATE_AUTHORITY,
) -> CanonicalOneRowCandidate:
    return CanonicalOneRowCandidate._create(
        authority_token=authority_token,
        schema_version=CANONICAL_ONE_ROW_SCHEMA_VERSION,
        identity=transaction.identity,
        source=transaction.source,
        source_record_id=transaction.source_record_id,
        transaction_date=transaction.transaction_date,
        merchant=transaction.merchant,
        amount_yen=transaction.amount_yen,
        transaction_kind=classification,
        payment_method=transaction.payment_method,
        business_fingerprint=transaction.business_fingerprint,
        memo=transaction.memo,
        member=transaction.member,
        source_occurrence=transaction.source_occurrence,
        source_hash=transaction.source_hash,
        reconciliation_state="bank_preview_eligible",
        source_identities=(transaction.identity,),
        cross_source_state="not_applicable",
        cross_source_candidate_identities=(),
        import_status=import_status,
        write_eligibility=write_eligibility,
        max_rows=max_rows,
    )


def project_bank_canary_candidate(plan) -> CanonicalOneRowCandidate:
    """Bind classification authority without teaching the transport bank rules."""
    from .bank_canary import validate_bank_canary_plan

    plan = validate_bank_canary_plan(plan)
    return _project_bank_candidate(
        plan.transaction,
        classification=plan.classification,
        write_eligibility=plan.write_eligibility,
        import_status=plan.import_status,
        max_rows=plan.authority.max_rows,
    )


def validate_canonical_one_row_candidate(value: object) -> CanonicalOneRowCandidate:
    if type(value) is not CanonicalOneRowCandidate:
        raise TypeError("canonical_one_row_candidate_required")
    authority_token = getattr(value, "_authority", None)
    if authority_token not in {
        _CANDIDATE_AUTHORITY, _BATCH_CANDIDATE_AUTHORITY,
    }:
        raise TypeError("unauthorized_canonical_one_row_candidate")
    if value.schema_version != CANONICAL_ONE_ROW_SCHEMA_VERSION:
        raise RuntimeError("canonical_one_row_schema_invalid")
    if value.transaction_kind not in {"income", "expense"}:
        raise RuntimeError("canonical_one_row_classification_withheld")
    if value.write_eligibility != "eligible":
        raise RuntimeError("canonical_one_row_write_eligibility_invalid")
    if value.import_status != f"bank_{value.transaction_kind}":
        raise RuntimeError("canonical_one_row_import_status_invalid")
    if authority_token is _CANDIDATE_AUTHORITY:
        if value.max_rows != CANONICAL_ONE_ROW_MAX_ROWS:
            raise RuntimeError("canonical_one_row_max_rows_must_be_one")
    elif value.max_rows not in {
        CANONICAL_BOUNDED_BATCH_ROWS, CANONICAL_INITIAL_BACKFILL_ROWS,
    }:
        raise RuntimeError("canonical_batch_row_bound_invalid")
    if value.source_identities != (value.identity,):
        raise RuntimeError("canonical_one_row_source_identity_invalid")
    if value.source_record_id != value.identity:
        raise RuntimeError("canonical_one_row_source_record_invalid")
    if value.reconciliation_state != "bank_preview_eligible":
        raise RuntimeError("canonical_one_row_reconciliation_invalid")
    if value.cross_source_state != "not_applicable":
        raise RuntimeError("canonical_one_row_cross_source_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", value.source_hash):
        raise RuntimeError("canonical_one_row_source_hash_invalid")
    if (
        value.transaction_kind == "income" and value.amount_yen <= 0
    ) or (
        value.transaction_kind == "expense" and value.amount_yen >= 0
    ):
        raise RuntimeError("canonical_one_row_amount_semantics_invalid")
    return value


@dataclass(frozen=True, init=False)
class CanonicalFiveRowBatch:
    candidates: tuple[CanonicalOneRowCandidate, ...] = field(repr=False)
    min_rows: int = CANONICAL_BOUNDED_BATCH_ROWS
    max_rows: int = CANONICAL_BOUNDED_BATCH_ROWS
    _authority: object = field(repr=False, compare=False)

    def __new__(cls, *args, **kwargs):
        raise TypeError("canonical_five_row_batch_is_projection_only")

    @classmethod
    def _create(cls, *, authority_token: object, **values) -> "CanonicalFiveRowBatch":
        if authority_token is not _BATCH_CANDIDATE_AUTHORITY:
            raise TypeError("canonical_five_row_batch_is_projection_only")
        batch = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(batch, name, value)
        object.__setattr__(batch, "_authority", _BATCH_CANDIDATE_AUTHORITY)
        return validate_canonical_five_row_batch(batch)


def project_bank_five_row_batch(plan) -> CanonicalFiveRowBatch:
    if plan.authority.max_rows != CANONICAL_BOUNDED_BATCH_ROWS:
        raise RuntimeError("canonical_batch_requires_exactly_five_rows")
    return _project_bank_batch(plan)


def project_bank_initial_backfill_batch(plan) -> CanonicalFiveRowBatch:
    if plan.authority.max_rows != CANONICAL_INITIAL_BACKFILL_ROWS:
        raise RuntimeError("canonical_backfill_requires_exactly_51_rows")
    return _project_bank_batch(plan)


def _project_bank_batch(plan) -> CanonicalFiveRowBatch:
    from .bank_canary import validate_bank_batch_plan

    plan = validate_bank_batch_plan(plan)
    candidates = tuple(
        _project_bank_candidate(
            item.transaction,
            classification=item.classification,
            write_eligibility=item.write_eligibility,
            import_status=item.import_status,
            max_rows=plan.authority.max_rows,
            authority_token=_BATCH_CANDIDATE_AUTHORITY,
        )
        for item in plan.items
    )
    return CanonicalFiveRowBatch._create(
        authority_token=_BATCH_CANDIDATE_AUTHORITY,
        candidates=candidates,
        min_rows=plan.authority.min_rows,
        max_rows=plan.authority.max_rows,
    )


def validate_canonical_five_row_batch(value: object) -> CanonicalFiveRowBatch:
    if type(value) is not CanonicalFiveRowBatch:
        raise TypeError("canonical_five_row_batch_required")
    if getattr(value, "_authority", None) is not _BATCH_CANDIDATE_AUTHORITY:
        raise TypeError("unauthorized_canonical_five_row_batch")
    if (
        value.min_rows != value.max_rows
        or value.max_rows not in {
            CANONICAL_BOUNDED_BATCH_ROWS, CANONICAL_INITIAL_BACKFILL_ROWS,
        }
        or len(value.candidates) != value.max_rows
    ):
        if value.max_rows == CANONICAL_BOUNDED_BATCH_ROWS:
            raise RuntimeError("canonical_batch_requires_exactly_five_rows")
        raise RuntimeError("canonical_batch_requires_exact_authorized_rows")
    identities = tuple(candidate.identity for candidate in value.candidates)
    if len(set(identities)) != value.max_rows:
        raise RuntimeError("canonical_batch_duplicate_identity")
    for candidate in value.candidates:
        validate_canonical_one_row_candidate(candidate)
        if (
            getattr(candidate, "_authority", None) is not _BATCH_CANDIDATE_AUTHORITY
            or candidate.max_rows != value.max_rows
        ):
            raise RuntimeError("canonical_batch_candidate_authority_invalid")
    return value


@dataclass(frozen=True)
class CanonicalOneRowManifest:
    run_id: str
    created_at: datetime
    selected_source_identity: str
    candidate_ref: str
    target_ref: str
    plan_binding_ref: str
    expected_git_head: str
    expected_branch: str
    authority_provenance: str = "phase5_exact_source_identity"
    max_rows: int = CANONICAL_ONE_ROW_MAX_ROWS
    schema_version: int = CANONICAL_ONE_ROW_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != CANONICAL_ONE_ROW_SCHEMA_VERSION:
            raise RuntimeError("canonical_one_row_schema_invalid")
        try:
            UUID(self.run_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("canonical_one_row_run_id_invalid") from exc
        _aware(self.created_at, "canonical_one_row_created_at_timezone_required")
        if not self.selected_source_identity:
            raise RuntimeError("canonical_one_row_source_identity_required")
        if not re.fullmatch(r"canonical-item-v2:[0-9a-f]{32}", self.candidate_ref):
            raise RuntimeError("canonical_one_row_candidate_ref_invalid")
        if not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", self.target_ref):
            raise RuntimeError("canonical_one_row_target_ref_invalid")
        if not re.fullmatch(r"canonical-one-row-v1:[0-9a-f]{32}", self.plan_binding_ref):
            raise RuntimeError("canonical_one_row_plan_ref_invalid")
        if not _HEAD.fullmatch(self.expected_git_head):
            raise RuntimeError("canonical_one_row_git_head_invalid")
        if self.expected_branch != "agent/bank-csv-ingestion":
            raise RuntimeError("canonical_one_row_branch_invalid")
        if self.authority_provenance != "phase5_exact_source_identity":
            raise RuntimeError("canonical_one_row_authority_provenance_invalid")
        if self.max_rows != CANONICAL_ONE_ROW_MAX_ROWS:
            raise RuntimeError("canonical_one_row_max_rows_must_be_one")


def _plan_binding_ref(
    candidate: CanonicalOneRowCandidate,
    *,
    candidate_ref: str,
    target_ref: str,
    expected_git_head: str,
    expected_branch: str,
    key: PersistentAuditKey,
) -> str:
    payload = json.dumps({
        "candidate_ref": candidate_ref,
        "target_ref": target_ref,
        "source_identity": candidate.identity,
        "classification": candidate.transaction_kind,
        "import_status": candidate.import_status,
        "max_rows": candidate.max_rows,
        "expected_git_head": expected_git_head,
        "expected_branch": expected_branch,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hmac.new(
        key.secret, f"canonical-one-row\x00{payload}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:32]
    return f"canonical-one-row-v1:{digest}"


def create_canonical_one_row_manifest(
    candidate: CanonicalOneRowCandidate,
    *,
    binding: TargetBinding,
    audit_key: PersistentAuditKey,
    expected_git_head: str,
    expected_branch: str,
    created_at: datetime,
    run_id: str,
) -> CanonicalOneRowManifest:
    candidate = validate_canonical_one_row_candidate(candidate)
    binding.validate()
    audit_key.validate()
    candidate_ref = canonical_candidate_reference_v2(candidate, audit_key)
    target_ref = target_binding_reference(binding, audit_key)
    manifest = CanonicalOneRowManifest(
        run_id=run_id,
        created_at=created_at,
        selected_source_identity=candidate.identity,
        candidate_ref=candidate_ref,
        target_ref=target_ref,
        plan_binding_ref=_plan_binding_ref(
            candidate,
            candidate_ref=candidate_ref,
            target_ref=target_ref,
            expected_git_head=expected_git_head,
            expected_branch=expected_branch,
            key=audit_key,
        ),
        expected_git_head=expected_git_head,
        expected_branch=expected_branch,
    )
    manifest.validate()
    return manifest


def validate_canonical_one_row_manifest(
    candidate: CanonicalOneRowCandidate,
    manifest: CanonicalOneRowManifest,
    *,
    binding: TargetBinding,
    audit_key: PersistentAuditKey,
) -> None:
    manifest.validate()
    expected = create_canonical_one_row_manifest(
        candidate,
        binding=binding,
        audit_key=audit_key,
        expected_git_head=manifest.expected_git_head,
        expected_branch=manifest.expected_branch,
        created_at=manifest.created_at,
        run_id=manifest.run_id,
    )
    if manifest != expected:
        raise RuntimeError("canonical_one_row_manifest_binding_mismatch")


@dataclass(frozen=True)
class CanonicalFiveRowManifest:
    run_id: str
    created_at: datetime
    candidate_refs: tuple[str, ...] = field(repr=False)
    batch_ref: str
    target_ref: str
    plan_binding_ref: str
    expected_git_head: str
    expected_branch: str
    income_count: int
    expense_count: int
    authority_provenance: str = "phase7_exact_five_source_identities"
    min_rows: int = CANONICAL_BOUNDED_BATCH_ROWS
    max_rows: int = CANONICAL_BOUNDED_BATCH_ROWS
    schema_version: int = CANONICAL_ONE_ROW_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != CANONICAL_ONE_ROW_SCHEMA_VERSION:
            raise RuntimeError("canonical_batch_schema_invalid")
        try:
            UUID(self.run_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("canonical_batch_run_id_invalid") from exc
        _aware(self.created_at, "canonical_batch_created_at_timezone_required")
        if (
            len(self.candidate_refs) != self.max_rows
            or len(set(self.candidate_refs)) != self.max_rows
        ):
            raise RuntimeError("canonical_batch_requires_exact_candidate_refs")
        if any(
            not re.fullmatch(r"canonical-item-v2:[0-9a-f]{32}", item)
            for item in self.candidate_refs
        ):
            raise RuntimeError("canonical_batch_candidate_ref_invalid")
        if not re.fullmatch(r"canonical-item-v2:[0-9a-f]{32}", self.batch_ref):
            raise RuntimeError("canonical_batch_ref_invalid")
        if not re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", self.target_ref):
            raise RuntimeError("canonical_batch_target_ref_invalid")
        plan_namespace = (
            "canonical-five-row"
            if self.max_rows == CANONICAL_BOUNDED_BATCH_ROWS
            else "canonical-initial-backfill"
        )
        if not re.fullmatch(
            rf"{plan_namespace}-v1:[0-9a-f]{{32}}", self.plan_binding_ref,
        ):
            raise RuntimeError("canonical_batch_plan_ref_invalid")
        if not _HEAD.fullmatch(self.expected_git_head):
            raise RuntimeError("canonical_batch_git_head_invalid")
        if self.expected_branch != "agent/bank-csv-ingestion":
            raise RuntimeError("canonical_batch_branch_invalid")
        if (
            self.income_count < 0 or self.expense_count < 0
            or self.income_count + self.expense_count != self.max_rows
            or (
                self.max_rows == CANONICAL_BOUNDED_BATCH_ROWS
                and (self.income_count != 2 or self.expense_count != 3)
            )
        ):
            raise RuntimeError("canonical_batch_classification_mix_changed")
        expected_provenance = (
            "phase7_exact_five_source_identities"
            if self.max_rows == CANONICAL_BOUNDED_BATCH_ROWS
            else "phase9_exact_remaining_initial_backfill"
        )
        if self.authority_provenance != expected_provenance:
            raise RuntimeError("canonical_batch_authority_provenance_invalid")
        if (
            self.min_rows != self.max_rows
            or self.max_rows not in {
                CANONICAL_BOUNDED_BATCH_ROWS, CANONICAL_INITIAL_BACKFILL_ROWS,
            }
        ):
            raise RuntimeError("canonical_batch_row_bound_invalid")


def _canonical_batch_refs(
    batch: CanonicalFiveRowBatch,
    key: PersistentAuditKey,
) -> tuple[tuple[str, ...], str]:
    batch = validate_canonical_five_row_batch(batch)
    candidate_refs = tuple(
        canonical_candidate_reference_v2(candidate, key)
        for candidate in batch.candidates
    )
    payload = json.dumps({
        "candidate_refs": candidate_refs,
        "classifications": tuple(
            candidate.transaction_kind for candidate in batch.candidates
        ),
        "min_rows": batch.min_rows,
        "max_rows": batch.max_rows,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hmac.new(
        key.secret, f"canonical-five-row-batch\x00{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return candidate_refs, f"canonical-item-v2:{digest}"


def _five_row_plan_binding_ref(
    *,
    batch_ref: str,
    target_ref: str,
    expected_git_head: str,
    expected_branch: str,
    key: PersistentAuditKey,
    row_count: int,
) -> str:
    payload = json.dumps({
        "batch_ref": batch_ref,
        "target_ref": target_ref,
        "expected_git_head": expected_git_head,
        "expected_branch": expected_branch,
        "min_rows": row_count,
        "max_rows": row_count,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    namespace = (
        "canonical-five-row"
        if row_count == CANONICAL_BOUNDED_BATCH_ROWS
        else "canonical-initial-backfill"
    )
    digest = hmac.new(
        key.secret, f"{namespace}-plan\x00{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{namespace}-v1:{digest}"


def create_canonical_five_row_manifest(
    batch: CanonicalFiveRowBatch,
    *,
    binding: TargetBinding,
    audit_key: PersistentAuditKey,
    expected_git_head: str,
    expected_branch: str,
    created_at: datetime,
    run_id: str,
) -> CanonicalFiveRowManifest:
    batch = validate_canonical_five_row_batch(batch)
    binding.validate()
    audit_key.validate()
    candidate_refs, batch_ref = _canonical_batch_refs(batch, audit_key)
    target_ref = target_binding_reference(binding, audit_key)
    manifest = CanonicalFiveRowManifest(
        run_id=run_id,
        created_at=created_at,
        candidate_refs=candidate_refs,
        batch_ref=batch_ref,
        target_ref=target_ref,
        plan_binding_ref=_five_row_plan_binding_ref(
            batch_ref=batch_ref,
            target_ref=target_ref,
            expected_git_head=expected_git_head,
            expected_branch=expected_branch,
            key=audit_key,
            row_count=batch.max_rows,
        ),
        expected_git_head=expected_git_head,
        expected_branch=expected_branch,
        income_count=sum(
            candidate.transaction_kind == "income" for candidate in batch.candidates
        ),
        expense_count=sum(
            candidate.transaction_kind == "expense" for candidate in batch.candidates
        ),
        authority_provenance=(
            "phase7_exact_five_source_identities"
            if batch.max_rows == CANONICAL_BOUNDED_BATCH_ROWS
            else "phase9_exact_remaining_initial_backfill"
        ),
        min_rows=batch.min_rows,
        max_rows=batch.max_rows,
    )
    manifest.validate()
    return manifest


def validate_canonical_five_row_manifest(
    batch: CanonicalFiveRowBatch,
    manifest: CanonicalFiveRowManifest,
    *,
    binding: TargetBinding,
    audit_key: PersistentAuditKey,
) -> None:
    manifest.validate()
    expected = create_canonical_five_row_manifest(
        batch,
        binding=binding,
        audit_key=audit_key,
        expected_git_head=manifest.expected_git_head,
        expected_branch=manifest.expected_branch,
        created_at=manifest.created_at,
        run_id=manifest.run_id,
    )
    if manifest != expected:
        raise RuntimeError("canonical_batch_manifest_binding_mismatch")


@dataclass(frozen=True)
class GitCheckpoint:
    branch: str
    head: str
    upstream_head: str
    ahead: int
    behind: int
    clean: bool


def validate_git_checkpoint(
    checkpoint: GitCheckpoint,
    *,
    expected_head: str,
    expected_branch: str,
) -> None:
    if not checkpoint.clean:
        raise RuntimeError("production_canary_dirty_worktree")
    if checkpoint.branch != expected_branch:
        raise RuntimeError("production_canary_branch_mismatch")
    if checkpoint.head != expected_head:
        raise RuntimeError("production_canary_head_mismatch")
    if checkpoint.upstream_head != checkpoint.head or checkpoint.ahead or checkpoint.behind:
        raise RuntimeError("production_canary_unpushed_head")


class GitCheckpointGuard:
    def __init__(self, repo_root: str | Path):
        self.repo_root = Path(repo_root).resolve()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo_root,
            check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def inspect(self) -> GitCheckpoint:
        branch = self._git("branch", "--show-current")
        head = self._git("rev-parse", "HEAD")
        upstream_head = self._git("rev-parse", "@{upstream}")
        counts = self._git(
            "rev-list", "--left-right", "--count", "HEAD...@{upstream}",
        ).split()
        return GitCheckpoint(
            branch=branch,
            head=head,
            upstream_head=upstream_head,
            ahead=int(counts[0]),
            behind=int(counts[1]),
            clean=not bool(self._git("status", "--porcelain")),
        )

    def validate(self, *, expected_head: str, expected_branch: str) -> GitCheckpoint:
        checkpoint = self.inspect()
        validate_git_checkpoint(
            checkpoint, expected_head=expected_head, expected_branch=expected_branch,
        )
        return checkpoint


def _load_key(provider: ProtectedAuditKeyProvider) -> PersistentAuditKey:
    if type(provider) is not ProtectedAuditKeyProvider:
        raise RuntimeError("protected_audit_key_provider_required")
    key = provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    key.validate()
    return key


def issue_canonical_one_row_capability(
    candidate: CanonicalOneRowCandidate,
    manifest: CanonicalOneRowManifest,
    *,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider: ProtectedAuditKeyProvider,
    journal: SqliteAttemptJournal,
    capability_store: SqliteCapabilityStore,
    approval_provider: ProtectedCanaryApprovalProvider,
    clock: Callable[[], datetime],
) -> ProductionWriteCapability:
    candidate = validate_canonical_one_row_candidate(candidate)
    now = _aware(clock(), "canonical_one_row_clock_timezone_required")
    if type(approval_provider) is not ProtectedCanaryApprovalProvider:
        raise RuntimeError("protected_canary_approval_required")
    if type(journal) is not SqliteAttemptJournal:
        raise RuntimeError("durable_attempt_journal_required")
    if type(capability_store) is not SqliteCapabilityStore:
        raise RuntimeError("durable_capability_store_required")
    key = _load_key(key_provider)
    validate_canonical_one_row_manifest(
        candidate, manifest, binding=binding, audit_key=key,
    )
    approval: CanaryApproval = approval_provider.load()
    if approval.batch_size != CANONICAL_ONE_ROW_MAX_ROWS:
        raise RuntimeError("canonical_one_row_max_rows_must_be_one")
    expires_at = _aware(approval.expires_at, "canonical_one_row_expiry_timezone_required")
    ttl = (expires_at - now).total_seconds()
    if ttl <= 0:
        raise RuntimeError("production_capability_expired")
    if ttl > MAX_CANARY_CAPABILITY_TTL_SECONDS:
        raise RuntimeError("production_capability_ttl_too_long")
    if approval.candidate_ref != manifest.candidate_ref:
        raise RuntimeError("approved_candidate_mismatch")
    if approval.target_ref != manifest.target_ref:
        raise RuntimeError("approved_target_mismatch")
    if not approval.approval_reference.strip():
        raise RuntimeError("human_approval_reference_required")
    if not journal.ready(manifest.run_id):
        raise RuntimeError("attempt_journal_unavailable")
    if journal.has_events(manifest.run_id):
        raise RuntimeError("attempt_journal_replay")
    if not capability_store.ready():
        raise RuntimeError("capability_store_unavailable")
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    capability = ProductionWriteCapability(
        capability_id=str(uuid4()),
        run_id=manifest.run_id,
        plan_binding_ref=manifest.plan_binding_ref,
        target_ref=manifest.target_ref,
        candidate_ref=manifest.candidate_ref,
        approval_reference=approval.approval_reference.strip(),
        issued_at=now,
        expires_at=expires_at,
        token=hashlib.sha256(os.urandom(32)).hexdigest(),
    )
    capability_store.issue(capability)
    return capability


def issue_canonical_five_row_capability(
    batch: CanonicalFiveRowBatch,
    manifest: CanonicalFiveRowManifest,
    *,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider: ProtectedAuditKeyProvider,
    journal: SqliteAttemptJournal,
    capability_store: SqliteCapabilityStore,
    approval_provider: ProtectedCanaryApprovalProvider,
    clock: Callable[[], datetime],
) -> ProductionWriteCapability:
    """Issue one short-lived capability bound to one exact ordered five-row batch."""
    batch = validate_canonical_five_row_batch(batch)
    now = _aware(clock(), "canonical_batch_clock_timezone_required")
    if type(approval_provider) is not ProtectedCanaryApprovalProvider:
        raise RuntimeError("protected_canary_approval_required")
    if type(journal) is not SqliteAttemptJournal:
        raise RuntimeError("durable_attempt_journal_required")
    if type(capability_store) is not SqliteCapabilityStore:
        raise RuntimeError("durable_capability_store_required")
    key = _load_key(key_provider)
    validate_canonical_five_row_manifest(
        batch, manifest, binding=binding, audit_key=key,
    )
    approval: CanaryApproval = approval_provider.load()
    if approval.batch_size != batch.max_rows:
        if batch.max_rows == CANONICAL_BOUNDED_BATCH_ROWS:
            raise RuntimeError("canonical_batch_requires_exactly_five_rows")
        raise RuntimeError("canonical_batch_requires_exact_authorized_rows")
    expires_at = _aware(approval.expires_at, "canonical_batch_expiry_timezone_required")
    ttl = (expires_at - now).total_seconds()
    if ttl <= 0:
        raise RuntimeError("production_capability_expired")
    if ttl > MAX_CANARY_CAPABILITY_TTL_SECONDS:
        raise RuntimeError("production_capability_ttl_too_long")
    if approval.candidate_ref != manifest.batch_ref:
        raise RuntimeError("approved_candidate_mismatch")
    if approval.target_ref != manifest.target_ref:
        raise RuntimeError("approved_target_mismatch")
    if not approval.approval_reference.strip():
        raise RuntimeError("human_approval_reference_required")
    if not journal.ready(manifest.run_id):
        raise RuntimeError("attempt_journal_unavailable")
    if journal.has_events(manifest.run_id):
        raise RuntimeError("attempt_journal_replay")
    if not capability_store.ready():
        raise RuntimeError("capability_store_unavailable")
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    capability = ProductionWriteCapability(
        capability_id=str(uuid4()),
        run_id=manifest.run_id,
        plan_binding_ref=manifest.plan_binding_ref,
        target_ref=manifest.target_ref,
        candidate_ref=manifest.batch_ref,
        approval_reference=approval.approval_reference.strip(),
        issued_at=now,
        expires_at=expires_at,
        token=hashlib.sha256(os.urandom(32)).hexdigest(),
    )
    capability_store.issue(capability)
    return capability


@dataclass(frozen=True, init=False)
class CanonicalDispatchPermit:
    capability_id: str
    run_id: str
    attempt_id: str
    candidate_ref: str
    target_ref: str
    max_rows: int
    _authority: object = field(repr=False)

    def __new__(cls, *args, **kwargs):
        raise TypeError("canonical_dispatch_permit_is_executor_only")

    @classmethod
    def _create(
        cls,
        capability: ProductionWriteCapability,
        *,
        attempt_id: str,
        max_rows: int = CANONICAL_ONE_ROW_MAX_ROWS,
    ) -> "CanonicalDispatchPermit":
        if max_rows not in CANONICAL_TRANSPORT_ROW_BOUNDS:
            raise RuntimeError("canonical_dispatch_row_bound_invalid")
        permit = object.__new__(cls)
        for name, value in {
            "capability_id": capability.capability_id,
            "run_id": capability.run_id,
            "attempt_id": attempt_id,
            "candidate_ref": capability.candidate_ref,
            "target_ref": capability.target_ref,
            "max_rows": max_rows,
            "_authority": _DISPATCH_AUTHORITY,
        }.items():
            object.__setattr__(permit, name, value)
        return permit


def _validate_dispatch_permit(
    permit: object,
    *,
    candidate_ref: str,
    target_ref: str,
    max_rows: int = CANONICAL_ONE_ROW_MAX_ROWS,
) -> CanonicalDispatchPermit:
    if type(permit) is not CanonicalDispatchPermit:
        raise TypeError("canonical_dispatch_permit_required")
    if getattr(permit, "_authority", None) is not _DISPATCH_AUTHORITY:
        raise TypeError("unauthorized_canonical_dispatch_permit")
    if max_rows not in CANONICAL_TRANSPORT_ROW_BOUNDS or permit.max_rows != max_rows:
        raise RuntimeError("canonical_dispatch_row_bound_invalid")
    if permit.candidate_ref != candidate_ref or permit.target_ref != target_ref:
        raise RuntimeError("canonical_dispatch_permit_binding_mismatch")
    return permit


class SealedCanonicalOneRowTransport:
    """Append pre-authorized canonical rows under an explicit supported bound."""

    synthetic_only = False
    max_rows = CANONICAL_ONE_ROW_MAX_ROWS

    def __init__(
        self,
        db,
        *,
        binding: TargetBinding,
        inspector: TargetInspector,
        key_provider: ProtectedAuditKeyProvider,
        journal: SqliteAttemptJournal,
        clock: Callable[[], datetime],
        max_rows: int = CANONICAL_ONE_ROW_MAX_ROWS,
    ):
        if max_rows not in CANONICAL_TRANSPORT_ROW_BOUNDS:
            raise RuntimeError("canonical_transport_row_bound_invalid")
        self._db = db
        self._binding = binding
        self._inspector = inspector
        self._key_provider = key_provider
        self._journal = journal
        self._clock = clock
        self.max_rows = max_rows
        self.invocation_count = 0
        self.last_row: tuple | None = None
        self.last_rows: tuple[tuple, ...] | None = None

    def prepare_rows(
        self,
        candidates: tuple[CanonicalOneRowCandidate, ...],
        *,
        imported_at: datetime | None = None,
    ) -> tuple[tuple, ...]:
        """Materialize a bounded canonical block without invoking Sheets."""
        if len(candidates) != self.max_rows:
            raise RuntimeError("canonical_transport_exact_batch_size")
        timestamp = self._clock() if imported_at is None else imported_at
        rows = tuple(
            tuple(validate_canonical_one_row_candidate(candidate).to_import_row(
                imported_at=timestamp,
            ))
            for candidate in candidates
        )
        if any(len(row) != len(HEADERS["取込データ"]) for row in rows):
            raise RuntimeError("canonical_transport_row_schema_invalid")
        return rows

    def write_once(
        self,
        candidate: CanonicalOneRowCandidate,
        permit: CanonicalDispatchPermit,
    ) -> WriteRequestResult:
        if self.max_rows != CANONICAL_ONE_ROW_MAX_ROWS:
            raise RuntimeError("canonical_one_row_transport_bound_invalid")
        candidate = validate_canonical_one_row_candidate(candidate)
        key = _load_key(self._key_provider)
        validate_target_binding(
            self._binding, self._inspector.inspect(self._binding.expected_worksheet),
        )
        candidate_ref = canonical_candidate_reference_v2(candidate, key)
        target_ref = target_binding_reference(self._binding, key)
        permit = _validate_dispatch_permit(
            permit, candidate_ref=candidate_ref, target_ref=target_ref,
        )
        history = self._journal.history(permit.run_id, permit.attempt_id)
        if (
            not history
            or history[-1].stage != JournalStage.WRITE_ATTEMPTED
            or history[-1].canonical_identity != candidate.identity
        ):
            raise RuntimeError("write_attempt_journal_required")
        row = self.prepare_rows((candidate,))[0]
        self.last_row = tuple(row)
        self.invocation_count += 1
        self._db.svc.spreadsheets().values().append(
            spreadsheetId=self._binding.expected_spreadsheet_id,
            range=f"{self._binding.expected_worksheet}!A:L",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return WriteRequestResult(WriteDisposition.ACKNOWLEDGED, "sheets_append_ack")

    def write_batch_once(
        self,
        batch: CanonicalFiveRowBatch,
        permit: CanonicalDispatchPermit,
    ) -> WriteRequestResult:
        if self.max_rows not in {
            CANONICAL_BOUNDED_BATCH_ROWS, CANONICAL_INITIAL_BACKFILL_ROWS,
        }:
            raise RuntimeError("canonical_batch_transport_bound_invalid")
        batch = validate_canonical_five_row_batch(batch)
        key = _load_key(self._key_provider)
        validate_target_binding(
            self._binding, self._inspector.inspect(self._binding.expected_worksheet),
        )
        _, batch_ref = _canonical_batch_refs(batch, key)
        target_ref = target_binding_reference(self._binding, key)
        permit = _validate_dispatch_permit(
            permit,
            candidate_ref=batch_ref,
            target_ref=target_ref,
            max_rows=batch.max_rows,
        )
        history = self._journal.history(permit.run_id, permit.attempt_id)
        if (
            not history
            or history[-1].stage != JournalStage.WRITE_ATTEMPTED
            or history[-1].canonical_identity != batch_ref
        ):
            raise RuntimeError("write_attempt_journal_required")
        rows = self.prepare_rows(batch.candidates)
        self.last_rows = rows
        self.invocation_count += 1
        self._db.svc.spreadsheets().values().append(
            spreadsheetId=self._binding.expected_spreadsheet_id,
            range=f"{self._binding.expected_worksheet}!A:L",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [list(row) for row in rows]},
        ).execute()
        return WriteRequestResult(WriteDisposition.ACKNOWLEDGED, "sheets_append_ack")


def _normalize_row(raw) -> tuple[str, ...]:
    row = list(raw) + [""] * max(0, 12 - len(raw))
    return tuple(str(value) for value in row[:12])


@dataclass(frozen=True)
class CanonicalRowsSnapshot:
    rows: tuple[tuple[str, ...], ...]


class CanonicalRowsReader(Protocol):
    def read_rows(self) -> CanonicalRowsSnapshot: ...


class SheetsCanonicalRowsReader:
    def __init__(self, db):
        self._db = db

    def read_rows(self) -> CanonicalRowsSnapshot:
        return CanonicalRowsSnapshot(tuple(
            _normalize_row(row) for row in self._db.get("取込データ!A2:L")
        ))


@dataclass(frozen=True)
class CanonicalOneRowExecutionResult:
    status: str
    final_state: CandidateState
    write_request_count: int
    actual_new_rows: int
    exact_canonical_match: bool
    duplicate_rows: int
    unexpected_mutations: int
    journal_committed: bool
    capability_state: str
    lease_released: bool
    readback_count: int
    recovered_from_ambiguous: bool
    reason_code: str

    def summary(self) -> dict:
        return {
            "status": self.status,
            "final_state": self.final_state.value,
            "write_request_count": self.write_request_count,
            "actual_new_rows": self.actual_new_rows,
            "exact_canonical_match": self.exact_canonical_match,
            "duplicate_rows": self.duplicate_rows,
            "unexpected_mutations": self.unexpected_mutations,
            "journal_committed": self.journal_committed,
            "capability_state": self.capability_state,
            "lease_released": self.lease_released,
            "readback_count": self.readback_count,
            "recovered_from_ambiguous": self.recovered_from_ambiguous,
            "reason_code": self.reason_code,
        }


def _append_journal(
    journal: SqliteAttemptJournal,
    *,
    manifest: CanonicalOneRowManifest,
    candidate: CanonicalOneRowCandidate,
    attempt_id: str,
    batch_id: str,
    stage: JournalStage,
    state: CandidateState,
    reason: str,
    clock: Callable[[], datetime],
) -> None:
    journal.append(JournalEvent(
        run_id=manifest.run_id,
        canonical_identity=candidate.identity,
        attempt_id=attempt_id,
        batch_id=batch_id,
        stage=stage,
        state=state,
        timestamp=_aware(clock(), "canonical_one_row_journal_clock_invalid"),
        reason_code=reason,
    ))


def _pre_read_state(observation: IdentityRead | None) -> tuple[CandidateState, str]:
    if observation is None or not observation.readable:
        return CandidateState.FAILED, "canonical_one_row_preread_unavailable"
    if not observation.records:
        return CandidateState.VERIFIED_NEW, "still_new"
    if len(observation.records) == 1:
        return CandidateState.ALREADY_PRESENT, "already_present_exact_or_conflict"
    return CandidateState.CONFLICT, "duplicate_identity_rows_present"


def execute_canonical_one_row_canary(
    candidate: CanonicalOneRowCandidate,
    manifest: CanonicalOneRowManifest,
    *,
    capability: ProductionWriteCapability,
    capability_store: SqliteCapabilityStore,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider: ProtectedAuditKeyProvider,
    journal: SqliteAttemptJournal,
    leases: SqliteLeaseManager,
    identity_reader: CanonicalIdentityReader,
    rows_reader: CanonicalRowsReader,
    transport,
    readback_policy: ReadBackPolicy,
    git_guard: GitCheckpointGuard | None,
    owner_id: str,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    synthetic: bool = False,
) -> CanonicalOneRowExecutionResult:
    """Consume one capability once; never retry the write request."""
    candidate = validate_canonical_one_row_candidate(candidate)
    key = _load_key(key_provider)
    validate_canonical_one_row_manifest(
        candidate, manifest, binding=binding, audit_key=key,
    )
    readback_policy.validate()
    if type(capability_store) is not SqliteCapabilityStore:
        raise RuntimeError("durable_capability_store_required")
    if type(journal) is not SqliteAttemptJournal:
        raise RuntimeError("durable_attempt_journal_required")
    if type(leases) is not SqliteLeaseManager:
        raise RuntimeError("durable_execution_lease_required")
    if synthetic:
        if not getattr(transport, "synthetic_only", False):
            raise RuntimeError("synthetic_transport_required")
    elif type(transport) is not SealedCanonicalOneRowTransport:
        raise RuntimeError("sealed_canonical_one_row_transport_required")
    if manifest.max_rows != 1 or candidate.max_rows != 1:
        raise RuntimeError("canonical_one_row_max_rows_must_be_one")
    if capability_store.state(capability.capability_id) != "issued":
        raise RuntimeError("production_capability_reused")
    try:
        if capability.run_id != manifest.run_id:
            raise RuntimeError("capability_run_mismatch")
        if capability.plan_binding_ref != manifest.plan_binding_ref:
            raise RuntimeError("capability_plan_mismatch")
        if capability.candidate_ref != manifest.candidate_ref:
            raise RuntimeError("candidate_not_in_capability")
        if capability.target_ref != manifest.target_ref:
            raise RuntimeError("capability_target_mismatch")
        if _aware(clock(), "canonical_one_row_clock_timezone_required") >= capability.expires_at:
            raise RuntimeError("production_capability_expired")
        if not journal.ready(manifest.run_id):
            raise RuntimeError("attempt_journal_unavailable")
        if not synthetic:
            if type(git_guard) is not GitCheckpointGuard:
                raise RuntimeError("production_git_checkpoint_guard_required")
            git_guard.validate(
                expected_head=manifest.expected_git_head,
                expected_branch=manifest.expected_branch,
            )
        validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    except Exception:
        try:
            capability_store.reseal(capability, "pre_dispatch_validation_failed")
        except Exception:
            pass
        raise

    lease = None
    lease_released = False
    attempt_id = str(uuid4())
    batch_id = f"canonical-one-row-{manifest.run_id}"
    final_state = CandidateState.FAILED
    reason = "canonical_one_row_execution_failed"
    write_count = 0
    actual_new_rows = 0
    exact_match = False
    duplicate_rows = 0
    unexpected_mutations = 0
    readback_count = 0
    recovered = False
    pre_rows = CanonicalRowsSnapshot(())
    try:
        lease = leases.acquire(manifest.target_ref, owner_id, manifest.run_id, 300)
        if lease is None:
            raise RuntimeError("writer_lock_unavailable")
        renewed = leases.renew(lease, 300)
        if renewed is None:
            raise RuntimeError("writer_lease_lost")
        lease = renewed
        pre_rows = rows_reader.read_rows()
        try:
            observations: Mapping[str, IdentityRead] = identity_reader.read_identities(
                (candidate.identity,),
            )
            observation = observations.get(candidate.identity)
        except Exception:
            observation = None
        pre_state, pre_reason = _pre_read_state(observation)
        if (
            pre_state == CandidateState.ALREADY_PRESENT
            and observation is not None
            and len(observation.records) == 1
            and observation.records[0] != ExistingCanonicalRecord.from_candidate(candidate)
        ):
            pre_state = CandidateState.CONFLICT
            pre_reason = "conflicting_existing_identity"
        _append_journal(
            journal, manifest=manifest, candidate=candidate,
            attempt_id=attempt_id, batch_id=batch_id,
            stage=JournalStage.PRE_READ, state=pre_state,
            reason=pre_reason, clock=clock,
        )
        if pre_state != CandidateState.VERIFIED_NEW:
            final_state = pre_state
            reason = pre_reason
            _append_journal(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.FINAL, state=final_state,
                reason=reason, clock=clock,
            )
        else:
            capability_store.claim(capability, attempt_id, clock())
            _append_journal(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.WRITE_ATTEMPTED,
                state=CandidateState.WRITE_ATTEMPTED,
                reason="write_request_about_to_send", clock=clock,
            )
            capability_store.authorize_dispatch(capability, attempt_id, clock())
            permit = CanonicalDispatchPermit._create(
                capability, attempt_id=attempt_id,
            )
            write_count = 1
            try:
                request_result = transport.write_once(candidate, permit)
            except Exception:
                request_result = WriteRequestResult(
                    WriteDisposition.OUTCOME_UNKNOWN,
                    "write_transport_outcome_unknown",
                )
            request_state = (
                CandidateState.OUTCOME_UNKNOWN
                if request_result.disposition == WriteDisposition.OUTCOME_UNKNOWN
                else CandidateState.WRITE_ATTEMPTED
            )
            _append_journal(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.WRITE_RESULT, state=request_state,
                reason=request_result.reason_code, clock=clock,
            )

            post_state = CandidateState.OUTCOME_UNKNOWN
            reason = "post_write_readback_unavailable"
            saw_absence = False
            for index in range(readback_policy.max_attempts):
                readback_count += 1
                try:
                    current = identity_reader.read_identities(
                        (candidate.identity,),
                    ).get(candidate.identity)
                except Exception:
                    current = None
                if current is not None and current.readable:
                    if not current.records:
                        saw_absence = True
                    elif (
                        len(current.records) == 1
                        and current.records[0] == ExistingCanonicalRecord.from_candidate(candidate)
                    ):
                        try:
                            post_rows = rows_reader.read_rows()
                            actual_new_rows = len(post_rows.rows) - len(pre_rows.rows)
                            expected_row = getattr(transport, "last_row", None)
                            expected = (
                                pre_rows.rows + (_normalize_row(expected_row),)
                                if expected_row is not None else ()
                            )
                            exact_match = post_rows.rows == expected
                            duplicate_rows = max(0, sum(
                                row[0] == candidate.identity for row in post_rows.rows
                            ) - 1)
                            if exact_match and actual_new_rows == 1 and duplicate_rows == 0:
                                post_state = CandidateState.WRITE_CONFIRMED
                                reason = "post_write_exact_confirmed"
                            else:
                                post_state = CandidateState.CONFLICT
                                reason = "post_write_row_set_mismatch"
                                unexpected_mutations = 1
                        except Exception:
                            post_state = CandidateState.OUTCOME_UNKNOWN
                            reason = "post_write_row_snapshot_unavailable"
                        break
                    else:
                        post_state = CandidateState.CONFLICT
                        reason = "post_write_identity_conflict"
                        duplicate_rows = max(0, len(current.records) - 1)
                        unexpected_mutations = 1
                        break
                if index < len(readback_policy.delays_seconds):
                    sleeper(readback_policy.delays_seconds[index])

            if post_state == CandidateState.WRITE_CONFIRMED:
                final_state = CandidateState.WRITE_CONFIRMED
                recovered = request_result.disposition == WriteDisposition.OUTCOME_UNKNOWN
                if recovered:
                    reason = "ambiguous_write_recovered_by_exact_readback"
            elif post_state == CandidateState.CONFLICT:
                final_state = CandidateState.CONFLICT
            elif saw_absence and request_result.disposition == WriteDisposition.OUTCOME_UNKNOWN:
                final_state = CandidateState.FAILED
                reason = "write_absence_confirmed_failure"
            else:
                final_state = CandidateState.OUTCOME_UNKNOWN
            _append_journal(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.POST_READ, state=post_state,
                reason=(reason if post_state != CandidateState.FAILED
                        else "post_write_readback_unavailable"),
                clock=clock,
            )
            _append_journal(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.FINAL, state=final_state,
                reason=reason, clock=clock,
            )
    finally:
        try:
            if lease is not None:
                leases.release(lease)
                lease_released = True
        finally:
            capability_store.reseal(capability, reason)

    history = journal.history(manifest.run_id, attempt_id)
    journal_committed = bool(
        history and history[-1].stage == JournalStage.FINAL
        and history[-1].state == final_state
    )
    return CanonicalOneRowExecutionResult(
        status=("canary_complete"
                if final_state == CandidateState.WRITE_CONFIRMED
                else "canary_stopped"),
        final_state=final_state,
        write_request_count=write_count,
        actual_new_rows=actual_new_rows,
        exact_canonical_match=exact_match,
        duplicate_rows=duplicate_rows,
        unexpected_mutations=unexpected_mutations,
        journal_committed=journal_committed,
        capability_state=capability_store.state(capability.capability_id) or "missing",
        lease_released=lease_released,
        readback_count=readback_count,
        recovered_from_ambiguous=recovered,
        reason_code=reason,
    )


@dataclass(frozen=True)
class CanonicalFiveRowExecutionResult:
    status: str
    final_state: CandidateState
    requested_rows: int
    write_request_count: int
    actual_new_rows: int
    exact_canonical_match: bool
    matched_identity_count: int
    duplicate_rows: int
    unexpected_mutations: int
    journal_committed: bool
    capability_state: str
    lease_released: bool
    readback_count: int
    recovered_from_ambiguous: bool
    partial_write_outcome: bool
    unknown_write_outcome: bool
    reason_code: str

    def summary(self) -> dict:
        return {
            "status": self.status,
            "final_state": self.final_state.value,
            "requested_rows": self.requested_rows,
            "write_request_count": self.write_request_count,
            "actual_new_rows": self.actual_new_rows,
            "exact_canonical_match": self.exact_canonical_match,
            "matched_identity_count": self.matched_identity_count,
            "duplicate_rows": self.duplicate_rows,
            "unexpected_mutations": self.unexpected_mutations,
            "journal_committed": self.journal_committed,
            "capability_state": self.capability_state,
            "lease_released": self.lease_released,
            "readback_count": self.readback_count,
            "recovered_from_ambiguous": self.recovered_from_ambiguous,
            "partial_write_outcome": self.partial_write_outcome,
            "unknown_write_outcome": self.unknown_write_outcome,
            "reason_code": self.reason_code,
        }


def _append_batch_journal(
    journal: SqliteAttemptJournal,
    *,
    manifest: CanonicalFiveRowManifest,
    attempt_id: str,
    batch_id: str,
    stage: JournalStage,
    state: CandidateState,
    reason: str,
    clock: Callable[[], datetime],
) -> None:
    journal.append(JournalEvent(
        run_id=manifest.run_id,
        canonical_identity=manifest.batch_ref,
        attempt_id=attempt_id,
        batch_id=batch_id,
        stage=stage,
        state=state,
        timestamp=_aware(clock(), "canonical_batch_journal_clock_invalid"),
        reason_code=reason,
    ))


def execute_canonical_five_row_batch(
    batch: CanonicalFiveRowBatch,
    manifest: CanonicalFiveRowManifest,
    *,
    capability: ProductionWriteCapability,
    capability_store: SqliteCapabilityStore,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider: ProtectedAuditKeyProvider,
    journal: SqliteAttemptJournal,
    leases: SqliteLeaseManager,
    identity_reader: CanonicalIdentityReader,
    rows_reader: CanonicalRowsReader,
    transport,
    readback_policy: ReadBackPolicy,
    git_guard: GitCheckpointGuard | None,
    owner_id: str,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    synthetic: bool = False,
) -> CanonicalFiveRowExecutionResult:
    """Consume one exact supported batch capability; the append is never retried."""
    batch = validate_canonical_five_row_batch(batch)
    batch_rows = batch.max_rows
    key = _load_key(key_provider)
    validate_canonical_five_row_manifest(
        batch, manifest, binding=binding, audit_key=key,
    )
    readback_policy.validate()
    if type(capability_store) is not SqliteCapabilityStore:
        raise RuntimeError("durable_capability_store_required")
    if type(journal) is not SqliteAttemptJournal:
        raise RuntimeError("durable_attempt_journal_required")
    if type(leases) is not SqliteLeaseManager:
        raise RuntimeError("durable_execution_lease_required")
    if synthetic:
        if not getattr(transport, "synthetic_only", False):
            raise RuntimeError("synthetic_transport_required")
    elif type(transport) is not SealedCanonicalOneRowTransport:
        raise RuntimeError("sealed_canonical_one_row_transport_required")
    if (
        manifest.min_rows != batch_rows or manifest.max_rows != batch_rows
        or batch.min_rows != batch_rows
        or batch_rows not in {
            CANONICAL_BOUNDED_BATCH_ROWS, CANONICAL_INITIAL_BACKFILL_ROWS,
        }
        or getattr(transport, "max_rows", None) != batch_rows
    ):
        raise RuntimeError("canonical_batch_requires_exact_authorized_rows")
    if capability_store.state(capability.capability_id) != "issued":
        raise RuntimeError("production_capability_reused")
    try:
        if capability.run_id != manifest.run_id:
            raise RuntimeError("capability_run_mismatch")
        if capability.plan_binding_ref != manifest.plan_binding_ref:
            raise RuntimeError("capability_plan_mismatch")
        if capability.candidate_ref != manifest.batch_ref:
            raise RuntimeError("candidate_not_in_capability")
        if capability.target_ref != manifest.target_ref:
            raise RuntimeError("capability_target_mismatch")
        if _aware(clock(), "canonical_batch_clock_timezone_required") >= capability.expires_at:
            raise RuntimeError("production_capability_expired")
        if not journal.ready(manifest.run_id):
            raise RuntimeError("attempt_journal_unavailable")
        if not synthetic:
            if type(git_guard) is not GitCheckpointGuard:
                raise RuntimeError("production_git_checkpoint_guard_required")
            git_guard.validate(
                expected_head=manifest.expected_git_head,
                expected_branch=manifest.expected_branch,
            )
        validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    except Exception:
        try:
            capability_store.reseal(capability, "pre_dispatch_validation_failed")
        except Exception:
            pass
        raise

    identities = tuple(candidate.identity for candidate in batch.candidates)
    expected_records = {
        candidate.identity: ExistingCanonicalRecord.from_candidate(candidate)
        for candidate in batch.candidates
    }
    lease = None
    lease_released = False
    attempt_id = str(uuid4())
    batch_id = f"canonical-bounded-{manifest.run_id}"
    final_state = CandidateState.FAILED
    reason = "canonical_batch_execution_failed"
    write_count = 0
    actual_new_rows = 0
    exact_match = False
    matched_identity_count = 0
    duplicate_rows = 0
    unexpected_mutations = 0
    readback_count = 0
    recovered = False
    partial = False
    unknown = False
    pre_rows = CanonicalRowsSnapshot(())
    try:
        lease = leases.acquire(manifest.target_ref, owner_id, manifest.run_id, 300)
        if lease is None:
            raise RuntimeError("writer_lock_unavailable")
        renewed = leases.renew(lease, 300)
        if renewed is None:
            raise RuntimeError("writer_lease_lost")
        lease = renewed
        pre_rows = rows_reader.read_rows()
        try:
            observations: Mapping[str, IdentityRead] = identity_reader.read_identities(
                identities,
            )
        except Exception:
            observations = {}
        if any(
            identity not in observations or not observations[identity].readable
            for identity in identities
        ):
            pre_state = CandidateState.FAILED
            pre_reason = "canonical_batch_preread_unavailable"
        elif any(observations[identity].records for identity in identities):
            pre_state = CandidateState.CONFLICT
            pre_reason = "canonical_batch_existing_identity"
        else:
            pre_state = CandidateState.VERIFIED_NEW
            pre_reason = "all_rows_still_new"
        _append_batch_journal(
            journal, manifest=manifest, attempt_id=attempt_id, batch_id=batch_id,
            stage=JournalStage.PRE_READ, state=pre_state,
            reason=pre_reason, clock=clock,
        )
        if pre_state != CandidateState.VERIFIED_NEW:
            final_state = pre_state
            reason = pre_reason
            _append_batch_journal(
                journal, manifest=manifest, attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.FINAL, state=final_state,
                reason=reason, clock=clock,
            )
        else:
            capability_store.claim(capability, attempt_id, clock())
            _append_batch_journal(
                journal, manifest=manifest, attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.WRITE_ATTEMPTED,
                state=CandidateState.WRITE_ATTEMPTED,
                reason="write_request_about_to_send", clock=clock,
            )
            capability_store.authorize_dispatch(capability, attempt_id, clock())
            permit = CanonicalDispatchPermit._create(
                capability, attempt_id=attempt_id,
                max_rows=batch_rows,
            )
            write_count = 1
            try:
                request_result = transport.write_batch_once(batch, permit)
            except Exception:
                request_result = WriteRequestResult(
                    WriteDisposition.OUTCOME_UNKNOWN,
                    "write_transport_outcome_unknown",
                )
            request_state = (
                CandidateState.OUTCOME_UNKNOWN
                if request_result.disposition == WriteDisposition.OUTCOME_UNKNOWN
                else CandidateState.WRITE_ATTEMPTED
            )
            _append_batch_journal(
                journal, manifest=manifest, attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.WRITE_RESULT, state=request_state,
                reason=request_result.reason_code, clock=clock,
            )

            post_state = CandidateState.OUTCOME_UNKNOWN
            reason = "post_write_readback_unavailable"
            saw_all_absent = False
            for index in range(readback_policy.max_attempts):
                readback_count += 1
                try:
                    current = identity_reader.read_identities(identities)
                except Exception:
                    current = {}
                if all(
                    identity in current and current[identity].readable
                    for identity in identities
                ):
                    matched_identity_count = sum(
                        len(current[identity].records) == 1
                        and current[identity].records[0] == expected_records[identity]
                        for identity in identities
                    )
                    present_count = sum(
                        bool(current[identity].records) for identity in identities
                    )
                    duplicate_rows = sum(
                        max(0, len(current[identity].records) - 1)
                        for identity in identities
                    )
                    conflicting = any(
                        current[identity].records
                        and not (
                            len(current[identity].records) == 1
                            and current[identity].records[0] == expected_records[identity]
                        )
                        for identity in identities
                    )
                    if matched_identity_count == batch_rows and not conflicting:
                        try:
                            post_rows = rows_reader.read_rows()
                            actual_new_rows = len(post_rows.rows) - len(pre_rows.rows)
                            written_rows = getattr(transport, "last_rows", None)
                            expected_rows = (
                                pre_rows.rows + tuple(_normalize_row(row) for row in written_rows)
                                if written_rows is not None else ()
                            )
                            exact_match = post_rows.rows == expected_rows
                            if (
                                exact_match and actual_new_rows == batch_rows
                                and duplicate_rows == 0
                            ):
                                post_state = CandidateState.WRITE_CONFIRMED
                                reason = "post_write_exact_confirmed"
                            else:
                                post_state = CandidateState.CONFLICT
                                reason = "post_write_row_set_mismatch"
                                unexpected_mutations = 1
                        except Exception:
                            post_state = CandidateState.OUTCOME_UNKNOWN
                            reason = "post_write_row_snapshot_unavailable"
                        break
                    if 1 <= present_count < batch_rows:
                        post_state = CandidateState.CONFLICT
                        reason = "partial_write_outcome"
                        partial = True
                        break
                    if conflicting or duplicate_rows:
                        post_state = CandidateState.CONFLICT
                        reason = "post_write_identity_conflict"
                        unexpected_mutations = 1
                        break
                    if present_count == 0:
                        saw_all_absent = True
                if index < len(readback_policy.delays_seconds):
                    sleeper(readback_policy.delays_seconds[index])

            if post_state == CandidateState.WRITE_CONFIRMED:
                final_state = CandidateState.WRITE_CONFIRMED
                recovered = request_result.disposition == WriteDisposition.OUTCOME_UNKNOWN
                if recovered:
                    reason = "ambiguous_write_recovered_by_exact_readback"
            elif post_state == CandidateState.CONFLICT:
                final_state = CandidateState.CONFLICT
            elif saw_all_absent:
                final_state = CandidateState.FAILED
                reason = "write_absence_confirmed_failure"
            else:
                final_state = CandidateState.OUTCOME_UNKNOWN
                unknown = True
            _append_batch_journal(
                journal, manifest=manifest, attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.POST_READ,
                state=(post_state if post_state != CandidateState.FAILED
                       else CandidateState.OUTCOME_UNKNOWN),
                reason=reason, clock=clock,
            )
            _append_batch_journal(
                journal, manifest=manifest, attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.FINAL, state=final_state,
                reason=reason, clock=clock,
            )
    finally:
        try:
            if lease is not None:
                leases.release(lease)
                lease_released = True
        finally:
            capability_store.reseal(capability, reason)

    history = journal.history(manifest.run_id, attempt_id)
    journal_committed = bool(
        history and history[-1].stage == JournalStage.FINAL
        and history[-1].state == final_state
    )
    if final_state == CandidateState.WRITE_CONFIRMED:
        status = "batch_complete"
    elif partial:
        status = "partial_write_outcome"
    elif unknown:
        status = "unknown_write_outcome"
    else:
        status = "batch_stopped"
    return CanonicalFiveRowExecutionResult(
        status=status,
        final_state=final_state,
        requested_rows=batch_rows,
        write_request_count=write_count,
        actual_new_rows=actual_new_rows,
        exact_canonical_match=exact_match,
        matched_identity_count=matched_identity_count,
        duplicate_rows=duplicate_rows,
        unexpected_mutations=unexpected_mutations,
        journal_committed=journal_committed,
        capability_state=capability_store.state(capability.capability_id) or "missing",
        lease_released=lease_released,
        readback_count=readback_count,
        recovered_from_ambiguous=recovered,
        partial_write_outcome=partial,
        unknown_write_outcome=unknown,
        reason_code=reason,
    )
