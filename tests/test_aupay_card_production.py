from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import inspect
import json
import re
import sqlite3
from uuid import uuid4

import pytest

from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.aupay_card_executor import (
    CandidateState,
    ExistingCanonicalRecord,
    IdentityRead,
)
from app.aupay_card_production import (
    CanaryApproval,
    CapabilitySealChecks,
    PRODUCTION_CAPABILITY_ENABLED,
    ProtectedAuditKeyProvider,
    ProtectedCanaryApprovalProvider,
    SealedSheetsCandidateTransport,
    SqliteAttemptJournal,
    SqliteCapabilityStore,
    SqliteLeaseManager,
    SqliteRunManifestStore,
    build_precanary_manifest,
    canonical_candidate_reference,
    evaluate_capability_seal,
    execute_synthetic_one_shot_canary,
    execute_production_one_shot_canary,
    issue_production_write_capability,
    recover_interrupted_attempt,
    target_binding_reference,
    validate_production_approval_preflight,
)
from app.aupay_card_writer import (
    BatchPolicy,
    FixedSourceWindow,
    JournalEvent,
    JournalStage,
    PersistentAuditKey,
    ReadBackPolicy,
    TargetBinding,
    TargetSnapshot,
    canonical_candidate_reference_v2,
    create_one_candidate_projection,
    create_production_canary_manifest,
    create_run_manifest,
    execute_synthetic_write,
    validate_target_binding,
)
from app.sheets import HEADERS
from app.transaction_plan import reconcile_transactions


NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
SHEET_ID = "production-target-unit-id"
KEY = PersistentAuditKey("production-key-v1", b"p" * 32)


def mail_id(char, number=1):
    return f"aupaycard-mail:{char * 24}:{number:03d}"


def make_plan(count=1):
    rows = [{
        "import_id": mail_id(chr(ord("a") + index), index + 1),
        "date": "2026-08-08",
        "merchant": f"秘密店舗{index}",
        "amount": 1000 + index,
        "transaction_kind": "purchase",
        "payment_type": "メール通知",
        "member": "本会員",
        "memo": f"メール明細No.{index + 1:03d}",
    } for index in range(count)]
    reconciliation = reconcile_transactions(rows, [])
    return build_canonical_apply_plan({
        "collection_complete": True,
        "listing_complete": True,
        "collection_truncated": False,
        "gmail_list_failed": 0,
        "gmail_read_failed": 0,
        "needs_review": 0,
    }, reconciliation)


def make_single_plan(**changes):
    row = {
        "import_id": mail_id("a"), "date": "2026-08-08",
        "merchant": "秘密店舗", "amount": 1000,
        "transaction_kind": "purchase", "payment_type": "メール通知",
        "member": "本会員", "memo": "メール明細No.001",
    }
    row.update(changes)
    reconciliation = reconcile_transactions([row], [])
    return build_canonical_apply_plan({
        "collection_complete": True, "listing_complete": True,
        "collection_truncated": False, "gmail_list_failed": 0,
        "gmail_read_failed": 0, "needs_review": 0,
    }, reconciliation)


def source_window(query=None):
    return FixedSourceWindow(
        NOW - timedelta(days=365), NOW, "Asia/Tokyo",
        query or "from:kddi-fs.com after:2025/09/09 before:2026/09/09",
    )


def run_manifest(plan, run_id=None, *, mode="synthetic_test"):
    return create_run_manifest(
        plan, source_window=source_window(), plan_created_at=NOW,
        run_id=run_id or str(uuid4()), audit_key=KEY,
        authority_mode=mode,
    )


class KeyProvider:
    def load(self):
        return KEY


class Inspector:
    def __init__(self, spreadsheet_id=SHEET_ID, worksheet="取込データ", header=None):
        self.snapshot = TargetSnapshot(
            spreadsheet_id, worksheet,
            tuple(header if header is not None else HEADERS["取込データ"]),
        )

    def inspect(self, _worksheet):
        return self.snapshot


class StaticReader:
    def __init__(self, observation=None, *, fail=False):
        self.observation = observation or IdentityRead()
        self.fail = fail
        self.calls = 0

    def read_identities(self, identities):
        self.calls += 1
        if self.fail:
            raise TimeoutError("synthetic read unavailable")
        return {identity: self.observation for identity in identities}


class FakeDB:
    def __init__(self):
        self.append_calls = []

    def append(self, sheet, rows):
        self.append_calls.append((sheet, rows))


class CountingTransport:
    synthetic_only = True

    def __init__(self):
        self.calls = 0

    def write_once(self, _candidate):
        self.calls += 1
        raise AssertionError("transport must not be called")


class CanaryTransport:
    synthetic_only = True

    def __init__(self, *, raises=None):
        self.calls = []
        self.raises = raises

    def write_once(self, candidate, *, attempt_id):
        self.calls.append((candidate.identity, attempt_id))
        if self.raises:
            raise self.raises
        from app.aupay_card_writer import WriteDisposition, WriteRequestResult
        return WriteRequestResult(WriteDisposition.ACKNOWLEDGED, "synthetic_ack")


class SequenceReader:
    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = 0

    def read_identities(self, identities):
        self.calls += 1
        observation = self.observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return {identity: observation for identity in identities}


def journal_event(manifest, candidate, attempt_id, stage, state, reason):
    return JournalEvent(
        manifest.run_id, candidate.identity, attempt_id, "writer-batch-v1:test",
        stage, state, NOW, reason,
    )


def repo_guard(path):
    return path.parent / "repository"


def create_interrupted_journal(path, plan, manifest):
    candidate = plan.candidates[0]
    attempt_id = str(uuid4())
    journal = SqliteAttemptJournal(path, repo_root=repo_guard(path))
    journal.append(journal_event(
        manifest, candidate, attempt_id, JournalStage.PRE_READ,
        CandidateState.VERIFIED_NEW, "still_new",
    ))
    journal.append(journal_event(
        manifest, candidate, attempt_id, JournalStage.WRITE_ATTEMPTED,
        CandidateState.WRITE_ATTEMPTED, "write_request_about_to_send",
    ))
    return attempt_id


def test_protected_key_missing_malformed_and_weak_fail_without_leak(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    missing = ProtectedAuditKeyProvider(
        tmp_path / "missing.json", repo_root=repo_root,
    )
    assert missing.load() is None
    assert "missing.json" not in repr(missing)

    secret_text = "super-sensitive-production-key-text"
    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text(json.dumps({
        "key_id": "key-v1", "key_b64": secret_text,
    }), encoding="utf-8")
    malformed = ProtectedAuditKeyProvider(malformed_path, repo_root=repo_root)
    with pytest.raises(RuntimeError, match="protected_audit_key_invalid") as exc:
        malformed.load()
    assert secret_text not in str(exc.value)

    weak_path = tmp_path / "weak.json"
    weak_path.write_text(json.dumps({
        "key_id": "key-v1",
        "key_b64": base64.b64encode(b"weak").decode(),
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="protected_audit_key_invalid"):
        ProtectedAuditKeyProvider(weak_path, repo_root=repo_root).load()

    unsafe_id_path = tmp_path / "unsafe-id.json"
    unsafe_id_path.write_text(json.dumps({
        "key_id": "secret-like key id", "key_b64": base64.b64encode(b"x" * 32).decode(),
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="protected_audit_key_invalid"):
        ProtectedAuditKeyProvider(unsafe_id_path, repo_root=repo_root).load()


def test_protected_key_file_inside_repository_is_rejected(tmp_path):
    key_path = tmp_path / "key.json"
    with pytest.raises(RuntimeError, match="outside_repository"):
        ProtectedAuditKeyProvider(key_path, repo_root=tmp_path)


def test_persistence_requires_absolute_repo_external_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="must_be_absolute"):
        SqliteAttemptJournal("relative.db", repo_root=tmp_path / "repository")
    with pytest.raises(RuntimeError, match="outside_repository"):
        SqliteAttemptJournal(
            tmp_path / "repository" / "state.db", repo_root=tmp_path / "repository",
        )
    with pytest.raises(RuntimeError, match="repository_root_required"):
        SqliteAttemptJournal(tmp_path / "state.db", repo_root=None)


def test_durable_journal_reopens_and_preserves_order(tmp_path):
    plan = make_plan()
    manifest = run_manifest(plan)
    attempt_id = create_interrupted_journal(tmp_path / "state.db", plan, manifest)

    path = tmp_path / "state.db"
    reopened = SqliteAttemptJournal(path, repo_root=repo_guard(path))
    history = reopened.history(manifest.run_id, attempt_id)

    assert [event.stage for event in history] == [
        JournalStage.PRE_READ, JournalStage.WRITE_ATTEMPTED,
    ]
    assert history[-1].canonical_identity == plan.candidates[0].identity


def test_durable_journal_invalid_transition_and_corruption_fail_closed(tmp_path):
    plan = make_plan()
    manifest = run_manifest(plan)
    path = tmp_path / "state.db"
    journal = SqliteAttemptJournal(path, repo_root=repo_guard(path))
    attempt_id = str(uuid4())
    with pytest.raises(RuntimeError, match="invalid_journal_state_transition"):
        journal.append(journal_event(
            manifest, plan.candidates[0], attempt_id,
            JournalStage.FINAL, CandidateState.FAILED, "invalid_transition",
        ))

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER journal_no_update")
        connection.execute("UPDATE journal_events SET event_hash='corrupt'")
    # No rows existed after the rejected transition, so inject an invalid chain.
    with sqlite3.connect(path) as connection:
        connection.execute("""INSERT INTO journal_events
            (run_id, canonical_identity, attempt_id, batch_id, stage, state,
             timestamp, reason_code, prev_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            manifest.run_id, plan.candidates[0].identity, str(uuid4()), "batch",
            "pre_read", "verified_new", NOW.isoformat(), "still_new", "", "bad",
        ))
    assert not SqliteAttemptJournal(
        path, repo_root=repo_guard(path),
    ).ready(manifest.run_id)


def test_durable_journal_rejects_stage_state_and_attempt_binding_changes(tmp_path):
    plan = make_plan(2)
    manifest = run_manifest(plan)
    path = tmp_path / "state.db"
    journal = SqliteAttemptJournal(path, repo_root=repo_guard(path))
    attempt_id = str(uuid4())
    first = plan.candidates[0]
    second = plan.candidates[1]

    with pytest.raises(RuntimeError, match="stage_state"):
        journal.append(journal_event(
            manifest, first, attempt_id, JournalStage.PRE_READ,
            CandidateState.WRITE_CONFIRMED, "invalid_stage_state",
        ))

    journal.append(journal_event(
        manifest, first, attempt_id, JournalStage.PRE_READ,
        CandidateState.VERIFIED_NEW, "still_new",
    ))
    with pytest.raises(RuntimeError, match="attempt_binding_changed"):
        journal.append(journal_event(
            manifest, second, attempt_id, JournalStage.WRITE_ATTEMPTED,
            CandidateState.WRITE_ATTEMPTED, "write_request_about_to_send",
        ))


def test_journal_persist_failure_prevents_transport_call(tmp_path):
    plan = make_plan()
    manifest = run_manifest(plan)
    transport = CountingTransport()
    path = tmp_path / "state.db"
    journal = SqliteAttemptJournal(
        path, repo_root=repo_guard(path), fail_writes=True,
    )

    with pytest.raises(RuntimeError, match="attempt_journal_persist_failed"):
        execute_synthetic_write(
            plan, manifest,
            binding=TargetBinding(expected_spreadsheet_id=SHEET_ID),
            inspector=Inspector(), key_provider=KeyProvider(), journal=journal,
            leases=SqliteLeaseManager(
                path, repo_root=repo_guard(path), clock=lambda: NOW,
            ),
            reader=StaticReader(), transport=transport,
            batch_policy=BatchPolicy(1), readback_policy=ReadBackPolicy(1, ()),
            owner_id="owner", clock=lambda: NOW, sleeper=lambda _delay: None,
        )
    assert transport.calls == 0


def test_sqlite_lease_blocks_concurrent_process_and_allows_stale_takeover(tmp_path):
    current = [NOW]
    path = tmp_path / "state.db"
    first_backend = SqliteLeaseManager(
        path, repo_root=repo_guard(path), clock=lambda: current[0],
    )
    second_backend = SqliteLeaseManager(
        path, repo_root=repo_guard(path), clock=lambda: current[0],
    )

    first = first_backend.acquire("target", "owner-a", "run-a", 10)
    assert first is not None
    assert second_backend.acquire("target", "owner-b", "run-b", 10) is None

    current[0] += timedelta(seconds=11)
    second = second_backend.acquire("target", "owner-b", "run-b", 10)
    assert second is not None
    assert second.token != first.token
    with pytest.raises(RuntimeError, match="lease_owner_token_mismatch"):
        first_backend.release(first)
    second_backend.release(second)


def test_lease_backend_failure_prevents_transport_call(tmp_path):
    plan = make_plan()
    manifest = run_manifest(plan)
    transport = CountingTransport()
    with pytest.raises(RuntimeError, match="lease_backend_unavailable"):
        execute_synthetic_write(
            plan, manifest,
            binding=TargetBinding(expected_spreadsheet_id=SHEET_ID),
            inspector=Inspector(), key_provider=KeyProvider(),
            journal=SqliteAttemptJournal(
                tmp_path / "state.db", repo_root=tmp_path / "repository",
            ),
            leases=SqliteLeaseManager(
                tmp_path / "state.db", repo_root=tmp_path / "repository",
                fail_operations=True,
            ),
            reader=StaticReader(), transport=transport,
            batch_policy=BatchPolicy(1), readback_policy=ReadBackPolicy(1, ()),
            owner_id="owner", clock=lambda: NOW, sleeper=lambda _delay: None,
        )
    assert transport.calls == 0


def test_real_sheets_transport_is_sealed_and_has_no_arbitrary_range_api():
    plan = make_plan()
    db = FakeDB()
    binding = TargetBinding(expected_spreadsheet_id=SHEET_ID)
    transport = SealedSheetsCandidateTransport(
        db, binding=binding, inspector=Inspector(),
    )

    assert not PRODUCTION_CAPABILITY_ENABLED
    with pytest.raises(RuntimeError, match="production_capability_required"):
        transport.write_once(plan.candidates[0])
    with pytest.raises(AttributeError):
        transport.write_range("arbitrary!A1", [["forbidden"]])
    assert transport.invocation_count == 0
    assert db.append_calls == []


def test_capability_seal_requires_every_scoped_gate_and_cli_has_no_wiring():
    all_preconditions = CapabilitySealChecks(
        explicit_capability_flag=True,
        persistent_key_available=True,
        durable_journal_healthy=True,
        lease_acquired=True,
        target_binding_verified=True,
        fixed_source_window_valid=True,
        executor_authority_valid=True,
        explicit_apply_authority=True,
        human_approval_present=True,
        exact_candidate_bound=True,
        exact_target_bound=True,
        batch_size_one=True,
        short_ttl_valid=True,
        one_shot_store_ready=True,
    )
    assert evaluate_capability_seal(all_preconditions) == ()

    import app.cli as cli
    source = inspect.getsource(cli)
    assert "aupay_card_production" not in source
    assert "SealedSheetsCandidateTransport" not in source


def test_wrong_spreadsheet_binding_fails_before_any_adapter_write():
    binding = TargetBinding(expected_spreadsheet_id=SHEET_ID)
    with pytest.raises(RuntimeError, match="target_spreadsheet_mismatch"):
        validate_target_binding(
            binding, Inspector(spreadsheet_id="wrong").snapshot,
        )


def test_fixed_manifest_persists_reopens_and_mutation_is_rejected(tmp_path):
    plan = make_plan(2)
    manifest = run_manifest(plan)
    path = tmp_path / "state.db"
    store = SqliteRunManifestStore(path, repo_root=repo_guard(path))
    store.save(manifest)
    store.save(manifest)

    reopened = SqliteRunManifestStore(path, repo_root=repo_guard(path))
    assert reopened.load(manifest.run_id) == manifest
    assert reopened.load(manifest.run_id).candidate_count == 2

    mutated = replace(manifest, candidate_count=1, withheld_count=1)
    with pytest.raises(RuntimeError, match="immutable_conflict"):
        reopened.save(mutated)


def test_relative_query_cannot_be_persisted_as_manifest(tmp_path):
    plan = make_plan()
    relative = source_window(
        "from:kddi-fs.com newer_than:1y after:2025/09/09 before:2026/09/09",
    )
    with pytest.raises(ValueError, match="relative_source_window_forbidden"):
        create_run_manifest(
            plan, source_window=relative, plan_created_at=NOW,
            run_id=str(uuid4()), audit_key=KEY,
        )
    path = tmp_path / "state.db"
    assert SqliteRunManifestStore(
        path, repo_root=repo_guard(path),
    ).load(str(uuid4())) is None


def test_precanary_manifest_is_deterministic_across_restart_and_privacy_safe(tmp_path):
    plan = make_plan(3)
    manifest = run_manifest(plan)
    path = tmp_path / "state.db"
    store = SqliteRunManifestStore(path, repo_root=repo_guard(path))
    store.save(manifest)
    binding = TargetBinding(expected_spreadsheet_id=SHEET_ID)

    first = build_precanary_manifest(
        plan, manifest, binding=binding, inspector=Inspector(),
        key_provider=KeyProvider(),
        journal=SqliteAttemptJournal(path, repo_root=repo_guard(path)),
        leases=SqliteLeaseManager(
            path, repo_root=repo_guard(path), clock=lambda: NOW,
        ),
        reader=StaticReader(), owner_id="owner-a",
    )
    restored = SqliteRunManifestStore(
        path, repo_root=repo_guard(path),
    ).load(manifest.run_id)
    second = build_precanary_manifest(
        plan, restored, binding=binding, inspector=Inspector(),
        key_provider=KeyProvider(),
        journal=SqliteAttemptJournal(path, repo_root=repo_guard(path)),
        leases=SqliteLeaseManager(
            path, repo_root=repo_guard(path), clock=lambda: NOW,
        ),
        reader=StaticReader(), owner_id="owner-b",
    )

    assert first.selected_candidate_count == second.selected_candidate_count == 1
    assert first.selection_audit_ref == second.selection_audit_ref
    assert first.pre_read_classification == "verified_new"
    assert first.writer_safety_ready
    rendered = repr(first)
    assert "秘密店舗" not in rendered
    assert SHEET_ID not in rendered
    assert first.external_write_count == 0


@pytest.mark.parametrize("mode", ["found", "absent", "unknown"])
def test_restart_recovery_never_resends_write(tmp_path, mode):
    plan = make_plan()
    manifest = run_manifest(plan)
    path = tmp_path / f"{mode}.db"
    attempt_id = create_interrupted_journal(path, plan, manifest)
    candidate = plan.candidates[0]
    if mode == "found":
        observation = IdentityRead((ExistingCanonicalRecord.from_candidate(candidate),))
        reader = StaticReader(observation)
        expected = CandidateState.WRITE_CONFIRMED
    elif mode == "absent":
        reader = StaticReader(IdentityRead())
        expected = CandidateState.RETRY_ELIGIBLE
    else:
        reader = StaticReader(fail=True)
        expected = CandidateState.OUTCOME_UNKNOWN

    reopened = SqliteAttemptJournal(path, repo_root=repo_guard(path))
    result = recover_interrupted_attempt(
        plan, manifest, attempt_id=attempt_id, journal=reopened,
        reader=reader, key_provider=KeyProvider(),
        readback_policy=ReadBackPolicy(2, (0.0,)),
        clock=lambda: NOW, sleeper=lambda _delay: None,
    )

    assert result.state == expected
    assert result.transport_invocation_count == 0
    assert reopened.history(manifest.run_id, attempt_id)[-1].stage == JournalStage.FINAL


def _write_approval(path, approval):
    path.write_text(json.dumps({
        "approval_reference": approval.approval_reference,
        "candidate_ref": approval.candidate_ref,
        "target_ref": approval.target_ref,
        "batch_size": approval.batch_size,
        "expires_at": approval.expires_at.isoformat(),
    }), encoding="utf-8")
    return ProtectedCanaryApprovalProvider(path, repo_root=repo_guard(path))


def canary_components(tmp_path, *, count=1, expires_at=None,
                      approval_ref="change-42", approval_changes=None):
    full_plan = make_plan(count)
    binding = TargetBinding(expected_spreadsheet_id=SHEET_ID)
    projection = create_one_candidate_projection(
        full_plan, candidate_identity=full_plan.candidates[0].identity,
        binding=binding, audit_key=KEY,
    )
    manifest = create_production_canary_manifest(
        projection, source_window=source_window(), plan_created_at=NOW,
        run_id=str(uuid4()), audit_key=KEY, binding=binding,
    )
    path = tmp_path / "canary-state.db"
    journal = SqliteAttemptJournal(path, repo_root=repo_guard(path))
    store = SqliteCapabilityStore(path, repo_root=repo_guard(path))
    leases = SqliteLeaseManager(path, repo_root=repo_guard(path), clock=lambda: NOW)
    key_path = tmp_path / "canary-key.json"
    key_path.write_text(json.dumps({
        "key_id": KEY.key_id,
        "key_b64": base64.b64encode(KEY.secret).decode("ascii"),
    }), encoding="utf-8")
    key_provider = ProtectedAuditKeyProvider(key_path, repo_root=repo_guard(key_path))
    approval = CanaryApproval(
        approval_reference=approval_ref,
        candidate_ref=canonical_candidate_reference_v2(
            projection.plan.candidates[0], KEY,
        ),
        target_ref=target_binding_reference(binding, KEY),
        batch_size=1,
        expires_at=expires_at or NOW + timedelta(seconds=60),
    )
    if approval_changes:
        approval = replace(approval, **approval_changes)
    provider = _write_approval(tmp_path / "canary-approval.json", approval)
    return projection, manifest, binding, journal, store, leases, key_provider, provider, approval


def issue_canary(parts, *, inspector=None, clock=lambda: NOW):
    projection, manifest, binding, journal, store, _leases, key_provider, provider, _approval = parts
    return issue_production_write_capability(
        projection, manifest, binding=binding, inspector=inspector or Inspector(),
        key_provider=key_provider, journal=journal, capability_store=store,
        approval_provider=provider, clock=clock,
    )


def validate_canary(parts, *, manifest=None, key_provider=None,
                    approval_provider=None, expected_run_id=None,
                    expected_source_window=None, clock=lambda: NOW):
    projection, default_manifest, binding, _journal, _store, _leases, default_key, \
        default_approval, _approval = parts
    selected_manifest = manifest or default_manifest
    return validate_production_approval_preflight(
        projection, selected_manifest, binding=binding, inspector=Inspector(),
        key_provider=key_provider or default_key,
        approval_provider=approval_provider or default_approval,
        expected_run_id=expected_run_id or default_manifest.run_id,
        expected_source_window=(
            expected_source_window or default_manifest.source_window
        ),
        clock=clock,
    )


def test_valid_approval_dry_validation_has_zero_authority_side_effects(tmp_path):
    parts = canary_components(tmp_path)
    result = validate_canary(parts)

    assert result.valid
    assert result.run_id == parts[1].run_id
    assert result.candidate_ref == parts[-1].candidate_ref
    assert result.target_ref == parts[-1].target_ref
    assert result.batch_size == 1
    assert result.external_write_count == 0
    assert result.capability_issued_count == 0
    assert result.lease_acquired_count == 0
    assert result.journal_write_count == 0
    assert result.transport_invocation_count == 0
    assert parts[0].plan.candidates[0].merchant not in repr(result)


def test_production_manifest_v3_round_trips_all_bindings(tmp_path):
    parts = canary_components(tmp_path)
    manifest_path = tmp_path / "manifest-v3.db"
    store = SqliteRunManifestStore(
        manifest_path, repo_root=repo_guard(manifest_path),
    )

    store.save(parts[1])

    assert store.load(parts[1].run_id) == parts[1]


def test_candidate_reference_v2_is_deterministic_and_content_bound():
    baseline = make_single_plan().candidates[0]
    baseline_ref = canonical_candidate_reference_v2(baseline, KEY)

    assert baseline_ref == canonical_candidate_reference_v2(baseline, KEY)
    assert baseline_ref.startswith("canonical-item-v2:")
    for changed_plan in (
        make_single_plan(merchant="別店舗"),
        make_single_plan(date="2026-08-09"),
        make_single_plan(amount=1001),
        make_single_plan(import_id=mail_id("b")),
    ):
        assert canonical_candidate_reference_v2(changed_plan.candidates[0], KEY) != baseline_ref


def test_approval_v1_reference_is_not_accepted_by_v2_contract(tmp_path):
    parts = canary_components(
        tmp_path, approval_changes={
            "candidate_ref": "canonical-item-v1:" + "0" * 32,
        },
    )
    with pytest.raises(RuntimeError, match="protected_canary_approval_invalid"):
        validate_canary(parts)


def test_dry_validation_rejects_wrong_key_and_manifest_bindings(tmp_path):
    parts = canary_components(tmp_path)
    wrong_key_path = tmp_path / "wrong-key.json"
    wrong_key_path.write_text(json.dumps({
        "key_id": KEY.key_id,
        "key_b64": base64.b64encode(b"q" * 32).decode("ascii"),
    }), encoding="utf-8")
    wrong_key = ProtectedAuditKeyProvider(
        wrong_key_path, repo_root=repo_guard(wrong_key_path),
    )
    with pytest.raises(RuntimeError, match="projection_binding_mismatch"):
        validate_canary(parts, key_provider=wrong_key)

    for field_name in ("full_plan_binding_ref", "projection_binding_ref"):
        bad = replace(parts[1], **{
            field_name: (
                "writer-full-plan-v2:" if field_name.startswith("full")
                else "writer-projection-v2:"
            ) + "0" * 32,
        })
        with pytest.raises(RuntimeError, match="manifest_binding_mismatch"):
            validate_canary(parts, manifest=bad)


def test_old_approval_cannot_authorize_same_identity_with_changed_content(tmp_path):
    parts = canary_components(tmp_path)
    changed_full_plan = make_single_plan(merchant="同一IDの変更後店舗")
    changed_projection = create_one_candidate_projection(
        changed_full_plan,
        candidate_identity=changed_full_plan.candidates[0].identity,
        binding=parts[2], audit_key=KEY,
    )
    changed_manifest = create_production_canary_manifest(
        changed_projection, source_window=source_window(), plan_created_at=NOW,
        run_id=parts[1].run_id, audit_key=KEY, binding=parts[2],
    )

    with pytest.raises(RuntimeError, match="approved_candidate_mismatch"):
        validate_production_approval_preflight(
            changed_projection, changed_manifest, binding=parts[2],
            inspector=Inspector(), key_provider=parts[6],
            approval_provider=parts[7], expected_run_id=changed_manifest.run_id,
            expected_source_window=changed_manifest.source_window,
            clock=lambda: NOW,
        )


def test_manifest_cannot_be_reused_for_changed_full_plan(tmp_path):
    parts = canary_components(tmp_path, count=2)
    changed_full_plan = make_plan(3)
    changed_projection = create_one_candidate_projection(
        changed_full_plan,
        candidate_identity=parts[0].selected_candidate_identity,
        binding=parts[2], audit_key=KEY,
    )

    with pytest.raises(RuntimeError, match="manifest_binding_mismatch"):
        validate_production_approval_preflight(
            changed_projection, parts[1], binding=parts[2],
            inspector=Inspector(), key_provider=parts[6],
            approval_provider=parts[7], expected_run_id=parts[1].run_id,
            expected_source_window=parts[1].source_window,
            clock=lambda: NOW,
        )


def test_approval_dry_validation_rejects_missing_approval_file(tmp_path):
    parts = canary_components(tmp_path)
    missing = ProtectedCanaryApprovalProvider(
        tmp_path / "missing-approval.json",
        repo_root=repo_guard(tmp_path / "missing-approval.json"),
    )
    with pytest.raises(RuntimeError, match="protected_canary_approval_invalid"):
        validate_canary(parts, approval_provider=missing)


def test_approval_dry_validation_rejects_missing_audit_key(tmp_path):
    parts = canary_components(tmp_path)
    missing = ProtectedAuditKeyProvider(
        tmp_path / "missing-key.json",
        repo_root=repo_guard(tmp_path / "missing-key.json"),
    )
    with pytest.raises(RuntimeError, match="persistent_audit_key_required"):
        validate_canary(parts, key_provider=missing)


@pytest.mark.parametrize("failure, reason", [
    ("reference", "human_approval_reference_required"),
    ("candidate", "approved_candidate_mismatch"),
    ("target", "approved_target_mismatch"),
    ("batch", "production_canary_batch_size_must_be_one"),
    ("expired", "production_capability_expired"),
    ("ttl", "production_capability_ttl_too_long"),
])
def test_approval_dry_validation_rejects_approval_mismatch(tmp_path, failure, reason):
    changes = {
        "reference": {"approval_reference": ""},
        "candidate": {"candidate_ref": "canonical-item-v2:" + "0" * 32},
        "target": {"target_ref": "writer-target-v1:" + "0" * 32},
        "batch": {"batch_size": 2},
        "expired": {"expires_at": NOW},
        "ttl": {"expires_at": NOW + timedelta(seconds=301)},
    }
    parts = canary_components(tmp_path, approval_changes=changes[failure])
    with pytest.raises(RuntimeError, match=reason):
        validate_canary(parts)


def test_approval_dry_validation_recomputes_candidate_reference(tmp_path):
    parts = canary_components(
        tmp_path, approval_changes={
            "candidate_ref": "canonical-item-v2:" + "f" * 32,
        },
    )
    with pytest.raises(RuntimeError, match="approved_candidate_mismatch"):
        validate_canary(parts)


def test_approval_dry_validation_rejects_manifest_run_mismatch(tmp_path):
    parts = canary_components(tmp_path)
    with pytest.raises(RuntimeError, match="production_preflight_run_mismatch"):
        validate_canary(parts, expected_run_id=str(uuid4()))


def test_approval_dry_validation_rejects_source_window_mismatch(tmp_path):
    parts = canary_components(tmp_path)
    different_window = FixedSourceWindow(
        NOW - timedelta(days=364), NOW, "Asia/Tokyo",
        "from:kddi-fs.com after:2025/09/10 before:2026/09/09",
    )
    with pytest.raises(RuntimeError, match="production_preflight_source_window_mismatch"):
        validate_canary(parts, expected_source_window=different_window)


def test_approval_dry_validation_leaves_sqlite_and_transport_unchanged(tmp_path):
    parts = canary_components(tmp_path)
    path = tmp_path / "canary-state.db"
    with sqlite3.connect(path) as connection:
        before_counts = tuple(connection.execute(query).fetchone()[0] for query in (
            "SELECT COUNT(*) FROM production_capabilities",
            "SELECT COUNT(*) FROM writer_leases",
            "SELECT COUNT(*) FROM journal_events",
        ))
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    transport = CanaryTransport()

    result = validate_canary(parts)

    after_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path) as connection:
        after_counts = tuple(connection.execute(query).fetchone()[0] for query in (
            "SELECT COUNT(*) FROM production_capabilities",
            "SELECT COUNT(*) FROM writer_leases",
            "SELECT COUNT(*) FROM journal_events",
        ))
    assert result.valid
    assert before_counts == after_counts == (0, 0, 0)
    assert before_hash == after_hash
    assert transport.calls == []


def run_synthetic_canary(parts, capability, reader, transport, *, clock=lambda: NOW):
    projection, manifest, binding, journal, store, leases, key_provider, _provider, _approval = parts
    return execute_synthetic_one_shot_canary(
        projection, manifest, capability=capability, capability_store=store,
        binding=binding, inspector=Inspector(), key_provider=key_provider,
        journal=journal, leases=leases, reader=reader, transport=transport,
        readback_policy=ReadBackPolicy(1, ()), owner_id="canary-owner",
        clock=clock, sleeper=lambda _delay: None,
    )


def test_valid_one_shot_capability_issuance_is_exact_and_short_lived(tmp_path):
    parts = canary_components(tmp_path)
    capability = issue_canary(parts)
    projection, manifest, binding, _journal, store, _leases, _key, _provider, approval = parts

    assert capability.run_id == manifest.run_id
    assert capability.candidate_ref == approval.candidate_ref
    assert capability.target_ref == target_binding_reference(binding, KEY)
    assert capability.expires_at - capability.issued_at == timedelta(seconds=60)
    assert store.state(capability.capability_id) == "issued"
    assert projection.plan.candidates[0].merchant not in repr(capability)


@pytest.mark.parametrize("failure", ["candidate", "target", "batch", "approval", "expired"])
def test_capability_issuance_rejects_wrong_or_missing_approval_bounds(tmp_path, failure):
    changes = {
        "candidate": {"candidate_ref": "canonical-item-v2:" + "0" * 32},
        "target": {"target_ref": "writer-target-v1:" + "0" * 32},
        "batch": {"batch_size": 2},
        "approval": {"approval_reference": ""},
        "expired": {"expires_at": NOW},
    }
    parts = canary_components(tmp_path, approval_changes=changes[failure])
    with pytest.raises(RuntimeError):
        issue_canary(parts)
    assert parts[4].state("missing") is None


def test_projection_structurally_excludes_second_candidate(tmp_path):
    parts = canary_components(tmp_path, count=2)
    assert len(parts[0].source_plan.candidates) == 2
    assert len(parts[0].plan.candidates) == 1
    issue_canary(parts)


def test_expired_issued_capability_is_rejected_and_resealed(tmp_path):
    parts = canary_components(tmp_path)
    capability = issue_canary(parts)
    transport = CanaryTransport()
    with pytest.raises(RuntimeError, match="production_capability_expired"):
        run_synthetic_canary(
            parts, capability, StaticReader(), transport,
            clock=lambda: NOW + timedelta(seconds=61),
        )
    assert transport.calls == []
    assert parts[4].state(capability.capability_id) == "sealed"


def test_successful_synthetic_one_shot_reseals_and_second_write_is_impossible(tmp_path):
    parts = canary_components(tmp_path)
    capability = issue_canary(parts)
    candidate = parts[0].plan.candidates[0]
    reader = SequenceReader([
        IdentityRead(),
        IdentityRead((ExistingCanonicalRecord.from_candidate(candidate),)),
    ])
    transport = CanaryTransport()
    result = run_synthetic_canary(parts, capability, reader, transport)

    assert result.status == "canary_complete"
    assert result.write_request_count == result.confirmed_count == 1
    assert len(transport.calls) == 1
    assert parts[4].state(capability.capability_id) == "sealed"

    with pytest.raises(RuntimeError, match="production_capability_reused"):
        run_synthetic_canary(
            parts, capability,
            SequenceReader([IdentityRead()]), transport,
        )
    assert len(transport.calls) == 1


def test_fresh_preread_failure_duplicate_and_identity_mismatch_write_zero(tmp_path):
    failure_parts = canary_components(tmp_path / "failure")
    failure_capability = issue_canary(failure_parts)
    failure_transport = CanaryTransport()
    failure_result = run_synthetic_canary(
        failure_parts, failure_capability,
        SequenceReader([TimeoutError("read failed")]), failure_transport,
    )
    assert failure_result.write_request_count == 0
    assert failure_transport.calls == []
    assert failure_parts[4].state(failure_capability.capability_id) == "sealed"

    duplicate_parts = canary_components(tmp_path / "duplicate")
    duplicate_capability = issue_canary(duplicate_parts)
    duplicate_candidate = duplicate_parts[0].plan.candidates[0]
    duplicate_transport = CanaryTransport()
    duplicate_result = run_synthetic_canary(
        duplicate_parts, duplicate_capability,
        SequenceReader([IdentityRead((
            ExistingCanonicalRecord.from_candidate(duplicate_candidate),
        ))]), duplicate_transport,
    )
    assert duplicate_result.already_present_count == 1
    assert duplicate_result.write_request_count == 0
    assert duplicate_transport.calls == []

    conflict_parts = canary_components(tmp_path / "conflict")
    conflict_capability = issue_canary(conflict_parts)
    other_candidate = make_plan(2).candidates[1]
    conflict_transport = CanaryTransport()
    conflict_result = run_synthetic_canary(
        conflict_parts, conflict_capability,
        SequenceReader([IdentityRead((
            ExistingCanonicalRecord.from_candidate(other_candidate),
        ))]), conflict_transport,
    )
    assert conflict_result.conflict_count == 1
    assert conflict_result.write_request_count == 0
    assert conflict_transport.calls == []

    mismatch_parts = canary_components(tmp_path / "mismatch")
    mismatch_capability = issue_canary(mismatch_parts)
    other_parts = canary_components(tmp_path / "other")
    mismatch_transport = CanaryTransport()
    with pytest.raises(RuntimeError, match="capability_run_mismatch|run_manifest_plan_mismatch"):
        execute_synthetic_one_shot_canary(
            other_parts[0], other_parts[1], capability=mismatch_capability,
            capability_store=mismatch_parts[4], binding=mismatch_parts[2],
            inspector=Inspector(), key_provider=mismatch_parts[6], journal=mismatch_parts[3],
            leases=mismatch_parts[5], reader=StaticReader(),
            transport=mismatch_transport, readback_policy=ReadBackPolicy(1, ()),
            owner_id="canary-owner", clock=lambda: NOW,
        )
    assert mismatch_transport.calls == []
    assert mismatch_parts[4].state(mismatch_capability.capability_id) == "sealed"


def test_transport_failure_releases_lease_and_reseals(tmp_path):
    parts = canary_components(tmp_path)
    capability = issue_canary(parts)
    transport = CanaryTransport(raises=TimeoutError("transport timeout"))
    result = run_synthetic_canary(
        parts, capability,
        SequenceReader([IdentityRead(), IdentityRead()]), transport,
    )
    assert result.retry_eligible_count == 1
    assert parts[4].state(capability.capability_id) == "sealed"
    target_ref = target_binding_reference(parts[2], KEY)
    probe = parts[5].acquire(target_ref, "probe", parts[1].run_id, 30)
    assert probe is not None
    parts[5].release(probe)


def test_real_transport_cannot_be_reached_without_claimed_capability(tmp_path):
    parts = canary_components(tmp_path)
    capability = issue_canary(parts)
    db = FakeDB()
    transport = SealedSheetsCandidateTransport(
        db, binding=parts[2], inspector=Inspector(), capability=capability,
        capability_store=parts[4], journal=parts[3],
        key_provider=parts[6], clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="write_attempt_journal_required"):
        transport.write_once(parts[0].plan.candidates[0], attempt_id=str(uuid4()))
    assert transport.invocation_count == 0
    assert db.append_calls == []


def test_second_capability_for_same_approved_canary_is_rejected(tmp_path):
    parts = canary_components(tmp_path)
    issue_canary(parts)
    with pytest.raises(RuntimeError, match="canary_capability_already_issued"):
        issue_canary(parts)


def test_protected_approval_must_be_repo_external(tmp_path):
    approval_path = tmp_path / "repo" / "approval.json"
    approval_path.parent.mkdir()
    with pytest.raises(RuntimeError, match="outside_repository"):
        ProtectedCanaryApprovalProvider(approval_path, repo_root=approval_path.parent)


class FailingSheetsRequest:
    def execute(self):
        raise TimeoutError("fake Sheets timeout")


class FakeSheetsValues:
    def __init__(self):
        self.append_calls = 0
        self.append_arguments = []

    def append(self, **kwargs):
        self.append_calls += 1
        self.append_arguments.append(kwargs)
        return FailingSheetsRequest()


class FakeSheetsService:
    def __init__(self):
        self.values_api = FakeSheetsValues()

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_api


class ProductionShapedFakeDB:
    def __init__(self):
        self.svc = FakeSheetsService()


def test_real_transport_fake_timeout_is_one_shot_released_and_resealed(tmp_path):
    parts = canary_components(tmp_path)
    capability = issue_canary(parts)
    db = ProductionShapedFakeDB()
    transport = SealedSheetsCandidateTransport(
        db, binding=parts[2], inspector=Inspector(), capability=capability,
        capability_store=parts[4], journal=parts[3],
        key_provider=parts[6], clock=lambda: NOW,
    )
    result = execute_production_one_shot_canary(
        parts[0], parts[1], capability=capability, capability_store=parts[4],
        binding=parts[2], inspector=Inspector(), key_provider=parts[6],
        journal=parts[3], leases=parts[5],
        reader=SequenceReader([IdentityRead(), IdentityRead()]),
        transport=transport, readback_policy=ReadBackPolicy(1, ()),
        owner_id="canary-owner", clock=lambda: NOW,
    )
    assert result.retry_eligible_count == 1
    assert transport.invocation_count == 1
    assert db.svc.values_api.append_calls == 1
    materialized = db.svc.values_api.append_arguments[0]["body"]["values"][0]
    assert materialized[1] == "2026-09-09 09:00:00"
    assert materialized[7] == "通常払い"
    assert materialized[8] == "auto_expense"
    assert re.fullmatch(r"[0-9a-f]{64}", materialized[10])
    assert materialized[10] != parts[0].plan.candidates[0].business_fingerprint
    assert parts[4].state(capability.capability_id) == "sealed"
    probe = parts[5].acquire(
        target_binding_reference(parts[2], KEY), "probe", parts[1].run_id, 30,
    )
    assert probe is not None
    parts[5].release(probe)


def test_synthetic_and_real_authority_are_separate_and_reseal_on_rejection(tmp_path):
    synthetic_parts = canary_components(tmp_path / "synthetic-entry")
    synthetic_capability = issue_canary(synthetic_parts)
    real_transport = SealedSheetsCandidateTransport(
        ProductionShapedFakeDB(), binding=synthetic_parts[2], inspector=Inspector(),
        capability=synthetic_capability, capability_store=synthetic_parts[4],
        journal=synthetic_parts[3], key_provider=synthetic_parts[6], clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="synthetic_transport_required"):
        execute_synthetic_one_shot_canary(
            synthetic_parts[0], synthetic_parts[1], capability=synthetic_capability,
            capability_store=synthetic_parts[4], binding=synthetic_parts[2],
            inspector=Inspector(), key_provider=synthetic_parts[6],
            journal=synthetic_parts[3], leases=synthetic_parts[5],
            reader=StaticReader(), transport=real_transport,
            readback_policy=ReadBackPolicy(1, ()), owner_id="owner",
            clock=lambda: NOW,
        )
    assert synthetic_parts[4].state(synthetic_capability.capability_id) == "sealed"
    assert real_transport.invocation_count == 0

    real_parts = canary_components(tmp_path / "real-entry")
    real_capability = issue_canary(real_parts)
    synthetic_transport = CanaryTransport()
    with pytest.raises(RuntimeError, match="sealed_real_transport_required"):
        execute_production_one_shot_canary(
            real_parts[0], real_parts[1], capability=real_capability,
            capability_store=real_parts[4], binding=real_parts[2],
            inspector=Inspector(), key_provider=real_parts[6], journal=real_parts[3],
            leases=real_parts[5], reader=StaticReader(),
            transport=synthetic_transport, readback_policy=ReadBackPolicy(1, ()),
            owner_id="owner", clock=lambda: NOW,
        )
    assert real_parts[4].state(real_capability.capability_id) == "sealed"
    assert synthetic_transport.calls == []


def test_key_load_exception_still_reseals_without_write(tmp_path):
    parts = canary_components(tmp_path)
    capability = issue_canary(parts)
    missing_key = ProtectedAuditKeyProvider(
        tmp_path / "missing-key.json", repo_root=repo_guard(tmp_path / "missing-key.json"),
    )
    transport = CanaryTransport()
    with pytest.raises(RuntimeError, match="persistent_audit_key_required"):
        execute_synthetic_one_shot_canary(
            parts[0], parts[1], capability=capability, capability_store=parts[4],
            binding=parts[2], inspector=Inspector(), key_provider=missing_key,
            journal=parts[3], leases=parts[5], reader=StaticReader(),
            transport=transport, readback_policy=ReadBackPolicy(1, ()),
            owner_id="owner", clock=lambda: NOW,
        )
    assert parts[4].state(capability.capability_id) == "sealed"
    assert transport.calls == []
