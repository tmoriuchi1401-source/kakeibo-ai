"""Sealed production-writer safety contracts for au PAY card ingestion.

This phase intentionally exposes no real Sheets write transport and no CLI
entry point.  The orchestration below accepts only a transport explicitly
marked synthetic, allowing the state machine to be tested without granting
production capability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import base64
import hashlib
import hmac
import os
import re
import time
from typing import Callable, Mapping, Protocol, Sequence
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .aupay_card_apply_plan import (
    CanonicalApplyCandidate,
    CanonicalApplyPlan,
    validate_canonical_apply_plan,
)
from .aupay_card_executor import (
    CandidateState,
    CanonicalExecutionResult,
    CanonicalIdentityReader,
    ExecutionSubset,
    IdentityRead,
    execute_canonical_apply_plan,
    select_canonical_candidates,
    verify_post_write,
)
from .sheets import HEADERS


WRITER_SAFETY_SCHEMA_VERSION = 2
TARGET_BINDING_VERSION = 1
ABSOLUTE_MAX_BATCH_SIZE = 100
_RELATIVE_QUERY = re.compile(r"(?:newer_than|older_than|newer|older):", re.IGNORECASE)
_ABSOLUTE_QUERY = re.compile(r"(?:after|before):\d{4}[/-]\d{1,2}[/-]\d{1,2}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(reason)
    return value


@dataclass(frozen=True)
class FixedSourceWindow:
    start: datetime
    end: datetime
    timezone_name: str
    query_representation: str

    def validate(self) -> None:
        start = _aware(self.start, "source_window_start_timezone_required")
        end = _aware(self.end, "source_window_end_timezone_required")
        if start >= end:
            raise ValueError("source_window_invalid")
        try:
            source_timezone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("source_window_timezone_invalid") from exc
        if _RELATIVE_QUERY.search(self.query_representation):
            raise ValueError("relative_source_window_forbidden")
        if len(_ABSOLUTE_QUERY.findall(self.query_representation)) < 2:
            raise ValueError("absolute_source_query_required")
        expected_after = start.astimezone(source_timezone).strftime("after:%Y/%m/%d")
        expected_before = end.astimezone(source_timezone).strftime("before:%Y/%m/%d")
        normalized_query = self.query_representation.replace("-", "/")
        if expected_after not in normalized_query or expected_before not in normalized_query:
            raise ValueError("source_window_query_mismatch")


@dataclass(frozen=True)
class PersistentAuditKey:
    key_id: str
    secret: bytes = field(repr=False)
    persistent: bool = True

    def validate(self) -> None:
        if not self.persistent:
            raise RuntimeError("persistent_audit_key_required")
        if not self.key_id or len(self.secret) < 32:
            raise RuntimeError("persistent_audit_key_invalid")


class AuditKeyProvider(Protocol):
    def load(self) -> PersistentAuditKey | None: ...


class EnvironmentAuditKeyProvider:
    """Load a protected base64 key without logging or persisting its value."""

    def __init__(self, key_env: str = "AUPAY_WRITER_AUDIT_KEY_B64",
                 key_id_env: str = "AUPAY_WRITER_AUDIT_KEY_ID"):
        self._key_env = key_env
        self._key_id_env = key_id_env

    def load(self) -> PersistentAuditKey | None:
        encoded = os.getenv(self._key_env, "").strip()
        key_id = os.getenv(self._key_id_env, "").strip()
        if not encoded or not key_id:
            return None
        try:
            secret = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("persistent_audit_key_invalid") from exc
        return PersistentAuditKey(key_id=key_id, secret=secret)


def _keyed_ref(key: bytes, namespace: str, *parts: str) -> str:
    body = "\x00".join((namespace, *parts)).encode("utf-8")
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()[:32]
    return f"{namespace}-v1:{digest}"


def _plan_binding_ref(plan: CanonicalApplyPlan, key: bytes) -> str:
    identities = "\n".join(sorted(candidate.identity for candidate in plan.candidates))
    return _keyed_ref(
        key, "writer-plan", str(plan.schema_version), plan.status,
        str(plan.canonical_transaction_count), identities,
    )


@dataclass(frozen=True)
class ProductionRunManifest:
    schema_version: int
    run_id: str
    source_window: FixedSourceWindow
    plan_created_at: datetime
    plan_binding_ref: str
    audit_key_id: str
    target_binding_version: int
    canonical_count: int
    candidate_count: int
    withheld_count: int
    authority_mode: str = "read_only_preflight"

    def validate(self) -> None:
        if self.schema_version != WRITER_SAFETY_SCHEMA_VERSION:
            raise RuntimeError("run_manifest_schema_invalid")
        try:
            UUID(self.run_id)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("run_identifier_invalid") from exc
        self.source_window.validate()
        _aware(self.plan_created_at, "plan_created_at_timezone_required")
        if self.authority_mode not in {
            "read_only_preflight", "synthetic_test", "production_canary",
        }:
            raise RuntimeError("production_run_authority_disabled")
        if self.target_binding_version != TARGET_BINDING_VERSION:
            raise RuntimeError("run_manifest_target_binding_invalid")
        if (
            self.canonical_count < 0
            or self.candidate_count < 0
            or self.withheld_count < 0
            or self.candidate_count + self.withheld_count != self.canonical_count
        ):
            raise RuntimeError("run_manifest_accounting_invalid")


def create_run_manifest(
    plan: CanonicalApplyPlan,
    *,
    source_window: FixedSourceWindow,
    plan_created_at: datetime,
    run_id: str,
    audit_key: PersistentAuditKey,
    target_binding_version: int = TARGET_BINDING_VERSION,
    authority_mode: str = "read_only_preflight",
) -> ProductionRunManifest:
    validate_canonical_apply_plan(plan)
    audit_key.validate()
    manifest = ProductionRunManifest(
        schema_version=WRITER_SAFETY_SCHEMA_VERSION,
        run_id=run_id,
        source_window=source_window,
        plan_created_at=plan_created_at,
        plan_binding_ref=_plan_binding_ref(plan, audit_key.secret),
        audit_key_id=audit_key.key_id,
        target_binding_version=target_binding_version,
        canonical_count=plan.canonical_transaction_count,
        candidate_count=len(plan.candidates),
        withheld_count=plan.canonical_transaction_count - len(plan.candidates),
        authority_mode=authority_mode,
    )
    manifest.validate()
    return manifest


@dataclass(frozen=True)
class TargetBinding:
    expected_spreadsheet_id: str = field(repr=False)
    expected_worksheet: str = "取込データ"
    expected_header: tuple[str, ...] = tuple(HEADERS["取込データ"])
    binding_version: int = TARGET_BINDING_VERSION

    def validate(self) -> None:
        if self.binding_version != TARGET_BINDING_VERSION:
            raise RuntimeError("target_binding_version_invalid")
        if not self.expected_spreadsheet_id:
            raise RuntimeError("target_spreadsheet_id_required")
        if not self.expected_worksheet or not self.expected_header:
            raise RuntimeError("target_schema_required")


@dataclass(frozen=True)
class TargetSnapshot:
    spreadsheet_id: str = field(repr=False)
    worksheet: str
    header: tuple[str, ...]


class TargetInspector(Protocol):
    def inspect(self, worksheet: str) -> TargetSnapshot: ...


class ReadOnlySheetsTargetInspector:
    """Inspect target identity/schema using only SheetsDB.get."""

    def __init__(self, db):
        self._db = db

    def inspect(self, worksheet: str) -> TargetSnapshot:
        rows = self._db.get(f"{worksheet}!A1:L1")
        header = tuple(rows[0]) if rows else ()
        return TargetSnapshot(
            spreadsheet_id=str(self._db.sid), worksheet=worksheet, header=header,
        )


def validate_target_binding(binding: TargetBinding, snapshot: TargetSnapshot) -> None:
    binding.validate()
    if snapshot.spreadsheet_id != binding.expected_spreadsheet_id:
        raise RuntimeError("target_spreadsheet_mismatch")
    if snapshot.worksheet != binding.expected_worksheet:
        raise RuntimeError("target_worksheet_mismatch")
    if snapshot.header != binding.expected_header:
        raise RuntimeError("target_schema_mismatch")


class JournalStage(str, Enum):
    PRE_READ = "pre_read"
    WRITE_ATTEMPTED = "write_attempted"
    WRITE_RESULT = "write_result"
    POST_READ = "post_read"
    FINAL = "final"


@dataclass(frozen=True)
class JournalEvent:
    run_id: str
    canonical_identity: str
    attempt_id: str
    batch_id: str
    stage: JournalStage
    state: CandidateState
    timestamp: datetime
    reason_code: str


class AttemptJournal(Protocol):
    persistent: bool

    def ready(self, run_id: str) -> bool: ...
    def append(self, event: JournalEvent) -> None: ...


class InMemoryAttemptJournal:
    """Strict synthetic journal used to verify production transition rules."""

    persistent = True

    def __init__(self, *, available: bool = True):
        self.available = available
        self.events: list[JournalEvent] = []
        self._last: dict[tuple[str, str], JournalEvent] = {}

    def ready(self, run_id: str) -> bool:
        return self.available and bool(run_id)

    def append(self, event: JournalEvent) -> None:
        if not self.available:
            raise RuntimeError("attempt_journal_unavailable")
        _aware(event.timestamp, "journal_timestamp_timezone_required")
        try:
            UUID(event.run_id)
            UUID(event.attempt_id)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("journal_identifier_invalid") from exc
        if (
            not event.canonical_identity
            or not event.batch_id
            or not re.fullmatch(r"[a-z0-9_]{1,64}", event.reason_code)
        ):
            raise RuntimeError("journal_event_content_invalid")
        key = (event.run_id, event.attempt_id)
        prior = self._last.get(key)
        allowed = {
            None: {JournalStage.PRE_READ},
            JournalStage.PRE_READ: {JournalStage.WRITE_ATTEMPTED, JournalStage.FINAL},
            JournalStage.WRITE_ATTEMPTED: {JournalStage.WRITE_RESULT},
            JournalStage.WRITE_RESULT: {JournalStage.POST_READ},
            JournalStage.POST_READ: {JournalStage.FINAL},
            JournalStage.FINAL: set(),
        }
        if event.stage not in allowed[prior.stage if prior else None]:
            raise RuntimeError("invalid_journal_state_transition")
        if prior and event.timestamp < prior.timestamp:
            raise RuntimeError("journal_timestamp_regression")
        self.events.append(event)
        self._last[key] = event


@dataclass(frozen=True)
class Lease:
    target_ref: str
    owner_id: str
    run_id: str
    token: str
    expires_at: datetime


class LeaseManager(Protocol):
    def acquire(self, target_ref: str, owner_id: str, run_id: str,
                lease_seconds: int) -> Lease | None: ...
    def renew(self, lease: Lease, lease_seconds: int) -> Lease | None: ...
    def release(self, lease: Lease) -> None: ...


class InMemoryLeaseManager:
    """Synthetic exclusive lease with safe stale-lock replacement."""

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now):
        self._clock = clock
        self._leases: dict[str, Lease] = {}

    def acquire(self, target_ref: str, owner_id: str, run_id: str,
                lease_seconds: int) -> Lease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_duration_invalid")
        now = _aware(self._clock(), "lease_clock_timezone_required")
        existing = self._leases.get(target_ref)
        if existing and existing.expires_at > now:
            return None
        lease = Lease(
            target_ref=target_ref,
            owner_id=owner_id,
            run_id=run_id,
            token=str(uuid4()),
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        self._leases[target_ref] = lease
        return lease

    def renew(self, lease: Lease, lease_seconds: int) -> Lease | None:
        if lease_seconds <= 0:
            raise ValueError("lease_duration_invalid")
        now = _aware(self._clock(), "lease_clock_timezone_required")
        existing = self._leases.get(lease.target_ref)
        if (
            existing is None
            or existing.token != lease.token
            or existing.expires_at <= now
        ):
            return None
        renewed = Lease(
            target_ref=lease.target_ref,
            owner_id=lease.owner_id,
            run_id=lease.run_id,
            token=lease.token,
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        self._leases[lease.target_ref] = renewed
        return renewed

    def release(self, lease: Lease) -> None:
        existing = self._leases.get(lease.target_ref)
        if existing and existing.token == lease.token:
            del self._leases[lease.target_ref]


@dataclass(frozen=True)
class BatchPolicy:
    requested_size: int
    max_batch_size: int = ABSOLUTE_MAX_BATCH_SIZE

    def validate(self) -> None:
        if self.max_batch_size <= 0 or self.max_batch_size > ABSOLUTE_MAX_BATCH_SIZE:
            raise ValueError("batch_upper_bound_invalid")
        if self.requested_size <= 0 or self.requested_size > self.max_batch_size:
            raise ValueError("batch_size_exceeds_bound")


@dataclass(frozen=True)
class ReadBackPolicy:
    max_attempts: int = 3
    delays_seconds: tuple[float, ...] = (0.0, 0.0)

    def validate(self) -> None:
        if self.max_attempts <= 0 or self.max_attempts > 10:
            raise ValueError("readback_attempt_bound_invalid")
        if len(self.delays_seconds) != self.max_attempts - 1:
            raise ValueError("readback_delay_schedule_invalid")
        if any(delay < 0 or delay > 30 for delay in self.delays_seconds):
            raise ValueError("readback_delay_invalid")


class WriteDisposition(str, Enum):
    ACKNOWLEDGED = "acknowledged"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class WriteRequestResult:
    disposition: WriteDisposition
    reason_code: str


class SyntheticWriteTransport(Protocol):
    synthetic_only: bool

    def write_once(self, candidate: CanonicalApplyCandidate) -> WriteRequestResult: ...


@dataclass(frozen=True)
class WriterPreflightResult:
    status: str
    run_id: str
    selected_count: int
    would_attempt_count: int
    already_present_count: int
    conflict_count: int
    failed_review_count: int
    target_binding_valid: bool
    fixed_window_valid: bool
    persistent_key_available: bool
    journal_ready: bool
    lock_ready: bool
    reason_codes: tuple[str, ...]
    dry_run: CanonicalExecutionResult | None = field(default=None, repr=False)


@dataclass(frozen=True)
class WriterExecutionResult:
    status: str
    run_id: str
    selected_count: int
    write_request_count: int
    confirmed_count: int
    already_present_count: int
    retry_eligible_count: int
    outcome_unknown_count: int
    conflict_count: int
    failed_count: int
    readback_count: int
    reason_codes: tuple[str, ...]
    final_states: tuple[tuple[str, CandidateState], ...]


def _load_and_validate_key(provider: AuditKeyProvider) -> PersistentAuditKey:
    key = provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    key.validate()
    return key


def _validate_manifest(plan: CanonicalApplyPlan, manifest: ProductionRunManifest,
                       key: PersistentAuditKey) -> None:
    manifest.validate()
    if manifest.audit_key_id != key.key_id:
        raise RuntimeError("run_manifest_audit_key_mismatch")
    if manifest.plan_binding_ref != _plan_binding_ref(plan, key.secret):
        raise RuntimeError("run_manifest_plan_mismatch")


def _target_ref(binding: TargetBinding, key: bytes) -> str:
    return _keyed_ref(
        key, "writer-target", binding.expected_spreadsheet_id,
        binding.expected_worksheet, str(binding.binding_version),
    )


def preflight_writer(
    plan: CanonicalApplyPlan,
    manifest: ProductionRunManifest,
    *,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider: AuditKeyProvider,
    journal: AttemptJournal,
    leases: LeaseManager,
    reader: CanonicalIdentityReader,
    batch_policy: BatchPolicy,
    owner_id: str,
    lease_seconds: int = 300,
) -> WriterPreflightResult:
    """Run every write-adjacent check and release the probe lease; never write."""
    validate_canonical_apply_plan(plan)
    key = _load_and_validate_key(key_provider)
    _validate_manifest(plan, manifest, key)
    batch_policy.validate()
    if not getattr(journal, "persistent", False) or not journal.ready(manifest.run_id):
        raise RuntimeError("attempt_journal_unavailable")
    snapshot = inspector.inspect(binding.expected_worksheet)
    validate_target_binding(binding, snapshot)
    target_ref = _target_ref(binding, key.secret)
    lease = leases.acquire(target_ref, owner_id, manifest.run_id, lease_seconds)
    if lease is None:
        raise RuntimeError("writer_lock_unavailable")
    try:
        dry_run = execute_canonical_apply_plan(
            plan,
            reader,
            apply=False,
            audit_key=key.secret,
            audit_ref_scope="persistent_protected_key",
            subset=ExecutionSubset(limit=batch_policy.requested_size),
        )
    finally:
        leases.release(lease)
    ready = dry_run.execution_status in {"dry_run_ready", "dry_run_noop"}
    return WriterPreflightResult(
        status="preflight_ready" if ready else "preflight_stopped",
        run_id=manifest.run_id,
        selected_count=dry_run.selected_candidate_count,
        would_attempt_count=dry_run.would_write_count,
        already_present_count=dry_run.already_present_count,
        conflict_count=dry_run.conflict_count,
        failed_review_count=dry_run.failed_review_count,
        target_binding_valid=True,
        fixed_window_valid=True,
        persistent_key_available=True,
        journal_ready=True,
        lock_ready=True,
        reason_codes=dry_run.reason_codes,
        dry_run=dry_run,
    )


def _journal_event(
    journal: AttemptJournal,
    *,
    manifest: ProductionRunManifest,
    candidate: CanonicalApplyCandidate,
    attempt_id: str,
    batch_id: str,
    stage: JournalStage,
    state: CandidateState,
    reason_code: str,
    clock: Callable[[], datetime],
) -> None:
    journal.append(JournalEvent(
        run_id=manifest.run_id,
        canonical_identity=candidate.identity,
        attempt_id=attempt_id,
        batch_id=batch_id,
        stage=stage,
        state=state,
        timestamp=_aware(clock(), "journal_clock_timezone_required"),
        reason_code=reason_code,
    ))


def _bounded_readback(
    plan: CanonicalApplyPlan,
    candidate: CanonicalApplyCandidate,
    reader: CanonicalIdentityReader,
    policy: ReadBackPolicy,
    key: bytes,
    sleeper: Callable[[float], None],
) -> tuple[CandidateState, str, int, bool]:
    policy.validate()
    reads = 0
    saw_readable_absence = False
    for attempt in range(policy.max_attempts):
        reads += 1
        try:
            result: Mapping[str, IdentityRead] = reader.read_identities((candidate.identity,))
            observation = result.get(candidate.identity)
        except Exception:
            observation = None
        if observation is not None and observation.readable:
            verified = verify_post_write(
                plan, candidate.identity, observation, audit_key=key,
            )
            if verified.state == CandidateState.WRITE_CONFIRMED:
                return verified.state, verified.reason_code, reads, saw_readable_absence
            if verified.state == CandidateState.CONFLICT:
                return verified.state, verified.reason_code, reads, saw_readable_absence
            if not observation.records:
                saw_readable_absence = True
        if attempt < len(policy.delays_seconds):
            sleeper(policy.delays_seconds[attempt])
    return (
        CandidateState.OUTCOME_UNKNOWN,
        "post_write_consistency_window_exhausted",
        reads,
        saw_readable_absence,
    )


def execute_synthetic_write(
    plan: CanonicalApplyPlan,
    manifest: ProductionRunManifest,
    *,
    binding: TargetBinding,
    inspector: TargetInspector,
    key_provider: AuditKeyProvider,
    journal: AttemptJournal,
    leases: LeaseManager,
    reader: CanonicalIdentityReader,
    transport: SyntheticWriteTransport,
    batch_policy: BatchPolicy,
    readback_policy: ReadBackPolicy,
    owner_id: str,
    lease_seconds: int = 300,
    clock: Callable[[], datetime] = _utc_now,
    sleeper: Callable[[float], None] = time.sleep,
) -> WriterExecutionResult:
    """Exercise the writer state machine against a synthetic transport only."""
    if not getattr(transport, "synthetic_only", False):
        raise RuntimeError("production_writer_capability_disabled")
    validate_canonical_apply_plan(plan)
    key = _load_and_validate_key(key_provider)
    _validate_manifest(plan, manifest, key)
    batch_policy.validate()
    readback_policy.validate()
    if not getattr(journal, "persistent", False) or not journal.ready(manifest.run_id):
        raise RuntimeError("attempt_journal_unavailable")
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    target_ref = _target_ref(binding, key.secret)
    lease = leases.acquire(target_ref, owner_id, manifest.run_id, lease_seconds)
    if lease is None:
        raise RuntimeError("writer_lock_unavailable")

    write_count = confirmed = already = retryable = unknown = conflicts = failed = 0
    readback_count = 0
    reasons: list[str] = []
    final_states: list[tuple[str, CandidateState]] = []
    try:
        selection = ExecutionSubset(limit=batch_policy.requested_size)
        candidates = select_canonical_candidates(plan.candidates, selection, key.secret)
        preflight = execute_canonical_apply_plan(
            plan, reader, apply=False, audit_key=key.secret,
            audit_ref_scope="persistent_protected_key", subset=selection,
        )
        if preflight.execution_status not in {"dry_run_ready", "dry_run_noop"}:
            return WriterExecutionResult(
                status="preflight_stopped", run_id=manifest.run_id,
                selected_count=len(candidates), write_request_count=0,
                confirmed_count=0, already_present_count=preflight.already_present_count,
                retry_eligible_count=0, outcome_unknown_count=0,
                conflict_count=preflight.conflict_count,
                failed_count=preflight.failed_review_count,
                readback_count=0, reason_codes=preflight.reason_codes,
                final_states=tuple(
                    (result.audit_item_ref, result.state)
                    for result in preflight.candidate_results
                ),
            )

        batch_id = _keyed_ref(key.secret, "writer-batch", manifest.run_id, "1")
        stop_after_index: int | None = None
        for index, (candidate, pre_result) in enumerate(
            zip(candidates, preflight.candidate_results)
        ):
            renewed_lease = leases.renew(lease, lease_seconds)
            if renewed_lease is None:
                failed += 1
                reasons.append("writer_lease_lost")
                for remaining in candidates[index:]:
                    final_states.append((
                        _keyed_ref(key.secret, "canonical-item", remaining.identity),
                        CandidateState.NOT_STARTED,
                    ))
                break
            lease = renewed_lease
            attempt_id = str(uuid4())
            _journal_event(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.PRE_READ, state=pre_result.state,
                reason_code=pre_result.reason_code, clock=clock,
            )
            if pre_result.state == CandidateState.ALREADY_PRESENT:
                already += 1
                final_state = CandidateState.ALREADY_PRESENT
                reason = "already_present_exact"
                _journal_event(
                    journal, manifest=manifest, candidate=candidate,
                    attempt_id=attempt_id, batch_id=batch_id,
                    stage=JournalStage.FINAL, state=final_state,
                    reason_code=reason, clock=clock,
                )
                final_states.append((pre_result.audit_item_ref, final_state))
                reasons.append(reason)
                continue
            if pre_result.state != CandidateState.VERIFIED_NEW:
                conflicts += pre_result.state == CandidateState.CONFLICT
                failed += pre_result.state != CandidateState.CONFLICT
                final_states.append((pre_result.audit_item_ref, pre_result.state))
                reasons.append(pre_result.reason_code)
                continue

            write_count += 1
            _journal_event(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.WRITE_ATTEMPTED,
                state=CandidateState.WRITE_ATTEMPTED,
                reason_code="write_request_about_to_send", clock=clock,
            )
            try:
                request_result = transport.write_once(candidate)
            except Exception:
                request_result = WriteRequestResult(
                    WriteDisposition.OUTCOME_UNKNOWN, "write_transport_outcome_unknown",
                )
            request_state = (
                CandidateState.OUTCOME_UNKNOWN
                if request_result.disposition == WriteDisposition.OUTCOME_UNKNOWN
                else CandidateState.WRITE_ATTEMPTED
            )
            _journal_event(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.WRITE_RESULT, state=request_state,
                reason_code=request_result.reason_code, clock=clock,
            )

            post_state, reason, reads, saw_absence = _bounded_readback(
                plan, candidate, reader, readback_policy, key.secret, sleeper,
            )
            readback_count += reads
            if post_state == CandidateState.WRITE_CONFIRMED:
                final_state = CandidateState.WRITE_CONFIRMED
                confirmed += 1
            elif post_state == CandidateState.CONFLICT:
                final_state = CandidateState.CONFLICT
                conflicts += 1
            elif (
                saw_absence
                and request_result.disposition in {
                    WriteDisposition.OUTCOME_UNKNOWN,
                    WriteDisposition.DEFINITELY_NOT_SENT,
                }
            ):
                final_state = CandidateState.RETRY_ELIGIBLE
                retryable += 1
                reason = "write_absence_confirmed_retry_eligible"
            else:
                final_state = CandidateState.OUTCOME_UNKNOWN
                unknown += 1
            _journal_event(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.POST_READ, state=post_state,
                reason_code=reason, clock=clock,
            )
            _journal_event(
                journal, manifest=manifest, candidate=candidate,
                attempt_id=attempt_id, batch_id=batch_id,
                stage=JournalStage.FINAL, state=final_state,
                reason_code=reason, clock=clock,
            )
            final_states.append((pre_result.audit_item_ref, final_state))
            reasons.append(reason)
            if final_state in {
                CandidateState.OUTCOME_UNKNOWN,
                CandidateState.CONFLICT,
                CandidateState.RETRY_ELIGIBLE,
                CandidateState.FAILED,
            }:
                stop_after_index = index
                break
        if stop_after_index is not None:
            for candidate in candidates[stop_after_index + 1:]:
                final_states.append((
                    _keyed_ref(key.secret, "canonical-item", candidate.identity),
                    CandidateState.NOT_STARTED,
                ))
    finally:
        leases.release(lease)

    stopped = bool(unknown or conflicts or failed)
    return WriterExecutionResult(
        status="synthetic_stopped" if stopped else "synthetic_complete",
        run_id=manifest.run_id,
        selected_count=len(final_states),
        write_request_count=write_count,
        confirmed_count=confirmed,
        already_present_count=already,
        retry_eligible_count=retryable,
        outcome_unknown_count=unknown,
        conflict_count=conflicts,
        failed_count=failed,
        readback_count=readback_count,
        reason_codes=tuple(sorted(set(reasons))),
        final_states=tuple(final_states),
    )
