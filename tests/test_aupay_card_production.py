from dataclasses import replace
from datetime import datetime, timedelta, timezone
import base64
import inspect
import json
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
    CapabilitySealChecks,
    PRODUCTION_CAPABILITY_ENABLED,
    ProtectedAuditKeyProvider,
    SealedSheetsCandidateTransport,
    SqliteAttemptJournal,
    SqliteLeaseManager,
    SqliteRunManifestStore,
    build_precanary_manifest,
    evaluate_capability_seal,
    issue_production_write_capability,
    recover_interrupted_attempt,
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


def source_window(query=None):
    return FixedSourceWindow(
        NOW - timedelta(days=365), NOW, "Asia/Tokyo",
        query or "from:kddi-fs.com after:2025/09/09 before:2026/09/09",
    )


def run_manifest(plan, run_id=None):
    return create_run_manifest(
        plan, source_window=source_window(), plan_created_at=NOW,
        run_id=run_id or str(uuid4()), audit_key=KEY,
        authority_mode="synthetic_test",
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
    with pytest.raises(RuntimeError, match="production_capability_issuance_disabled"):
        issue_production_write_capability()
    with pytest.raises(RuntimeError, match="production_capability_issuance_disabled"):
        transport.write_once(plan.candidates[0])
    with pytest.raises(AttributeError):
        transport.write_range("arbitrary!A1", [["forbidden"]])
    assert transport.invocation_count == 0
    assert db.append_calls == []


def test_capability_seal_always_includes_disabled_blocker_and_cli_has_no_wiring():
    all_preconditions = CapabilitySealChecks(
        explicit_capability_flag=True,
        persistent_key_available=True,
        durable_journal_healthy=True,
        lease_acquired=True,
        target_binding_verified=True,
        fixed_source_window_valid=True,
        executor_authority_valid=True,
        explicit_apply_authority=True,
    )
    assert evaluate_capability_seal(all_preconditions) == (
        "production_capability_disabled",
    )

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
