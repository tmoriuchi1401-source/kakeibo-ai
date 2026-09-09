from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import inspect
from uuid import uuid4

import pytest

from app import cli
from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.aupay_card_executor import (
    CandidateState,
    ExistingCanonicalRecord,
    IdentityRead,
)
from app.aupay_card_writer import (
    ABSOLUTE_MAX_BATCH_SIZE,
    BatchPolicy,
    FixedSourceWindow,
    InMemoryAttemptJournal,
    InMemoryLeaseManager,
    JournalEvent,
    JournalStage,
    Lease,
    PersistentAuditKey,
    ProductionRunManifest,
    ReadBackPolicy,
    TargetBinding,
    TargetSnapshot,
    WriteDisposition,
    WriteRequestResult,
    create_run_manifest,
    execute_synthetic_write,
    preflight_writer,
)
from app.sheets import HEADERS
from app.transaction_plan import reconcile_transactions


AUDIT_KEY = PersistentAuditKey(
    key_id="unit-key-v1",
    secret=b"unit-test-persistent-audit-key-material-32-plus",
)
NOW = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)
SHEET_ID = "synthetic-sheet-id"


def mail_id(char, number=1):
    return f"aupaycard-mail:{char * 24}:{number:03d}"


def row(identity, *, amount=1000, merchant="店", number=1):
    return {
        "import_id": identity,
        "date": "2026-08-08",
        "merchant": merchant,
        "amount": amount,
        "transaction_kind": "purchase",
        "payment_type": "メール通知",
        "member": "本会員",
        "memo": f"メール明細No.{number:03d}",
    }


def make_plan(count=1):
    rows = [
        row(mail_id(chr(ord("a") + index), index + 1), amount=1000 + index, number=index + 1)
        for index in range(count)
    ]
    reconciliation = reconcile_transactions(rows, [])
    return build_canonical_apply_plan({
        "collection_complete": True,
        "listing_complete": True,
        "collection_truncated": False,
        "gmail_list_failed": 0,
        "gmail_read_failed": 0,
        "needs_review": 0,
    }, reconciliation)


def fixed_window(query=None):
    return FixedSourceWindow(
        start=datetime(2025, 9, 9, tzinfo=timezone.utc),
        end=datetime(2026, 9, 9, tzinfo=timezone.utc),
        timezone_name="Asia/Tokyo",
        query_representation=query or (
            'in:anywhere from:kddi-fs.com subject:"card" '
            "after:2025/09/09 before:2026/09/09"
        ),
    )


class KeyProvider:
    def __init__(self, key=AUDIT_KEY):
        self.key = key

    def load(self):
        return self.key


class Inspector:
    def __init__(self, *, spreadsheet_id=SHEET_ID, worksheet="取込データ", header=None):
        self.snapshot = TargetSnapshot(
            spreadsheet_id=spreadsheet_id,
            worksheet=worksheet,
            header=tuple(header if header is not None else HEADERS["取込データ"]),
        )

    def inspect(self, _worksheet):
        return self.snapshot


class SequenceReader:
    def __init__(self, observations=None, *, fail_after=None):
        self.observations = list(observations or [])
        self.last = {}
        self.calls = 0
        self.fail_after = fail_after

    def read_identities(self, identities):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise TimeoutError("synthetic read timeout")
        if self.observations:
            self.last = self.observations.pop(0)
        return {
            identity: self.last.get(identity, IdentityRead())
            for identity in identities
        }


class Transport:
    synthetic_only = True

    def __init__(self, result=None, *, raises=None, on_write=None):
        self.result = result or WriteRequestResult(
            WriteDisposition.ACKNOWLEDGED, "synthetic_ack",
        )
        self.raises = raises
        self.on_write = on_write
        self.calls = []

    def write_once(self, candidate):
        self.calls.append(candidate.identity)
        if self.on_write:
            self.on_write(candidate)
        if self.raises:
            raise self.raises
        return self.result


class DenyLeaseManager:
    def __init__(self):
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self, *_args):
        self.acquire_calls += 1
        return None

    def release(self, _lease):
        self.release_calls += 1


def manifest(plan, *, window=None, mode="synthetic_test"):
    return create_run_manifest(
        plan,
        source_window=window or fixed_window(),
        plan_created_at=NOW,
        run_id=str(uuid4()),
        audit_key=AUDIT_KEY,
        authority_mode=mode,
    )


def writer_args(plan, *, reader=None, transport=None, journal=None, leases=None,
                inspector=None, key_provider=None, batch_size=1, read_attempts=3):
    return {
        "binding": TargetBinding(expected_spreadsheet_id=SHEET_ID),
        "inspector": inspector or Inspector(),
        "key_provider": key_provider or KeyProvider(),
        "journal": journal or InMemoryAttemptJournal(),
        "leases": leases or InMemoryLeaseManager(clock=lambda: NOW),
        "reader": reader or SequenceReader(),
        "transport": transport or Transport(),
        "batch_policy": BatchPolicy(requested_size=batch_size),
        "readback_policy": ReadBackPolicy(
            max_attempts=read_attempts,
            delays_seconds=(0.0,) * (read_attempts - 1),
        ),
        "owner_id": "synthetic-owner",
        "clock": lambda: NOW,
        "sleeper": lambda _delay: None,
    }


def test_fixed_source_window_and_run_manifest_are_immutable_and_deterministic():
    plan = make_plan()
    run_id = str(uuid4())
    first = create_run_manifest(
        plan, source_window=fixed_window(), plan_created_at=NOW,
        run_id=run_id, audit_key=AUDIT_KEY,
    )
    second = create_run_manifest(
        plan, source_window=fixed_window(), plan_created_at=NOW,
        run_id=run_id, audit_key=AUDIT_KEY,
    )

    assert first == second
    assert first.plan_binding_ref == second.plan_binding_ref
    with pytest.raises(FrozenInstanceError):
        first.run_id = str(uuid4())


@pytest.mark.parametrize("query", [
    'from:kddi-fs.com newer_than:1y after:2025/09/09 before:2026/09/09',
    'from:kddi-fs.com newer:2025/09/09 before:2026/09/09',
])
def test_relative_window_production_manifest_is_rejected(query):
    with pytest.raises(ValueError, match="relative_source_window_forbidden"):
        manifest(make_plan(), window=fixed_window(query))


def test_absolute_query_must_match_manifest_boundaries():
    mismatched = fixed_window(
        'from:kddi-fs.com after:2025/09/08 before:2026/09/09',
    )
    with pytest.raises(ValueError, match="source_window_query_mismatch"):
        manifest(make_plan(), window=mismatched)


@pytest.mark.parametrize("inspector, reason", [
    (Inspector(spreadsheet_id="wrong"), "target_spreadsheet_mismatch"),
    (Inspector(worksheet="wrong"), "target_worksheet_mismatch"),
    (Inspector(header=("wrong",)), "target_schema_mismatch"),
])
def test_target_binding_mismatch_stops_before_write(inspector, reason):
    plan = make_plan()
    transport = Transport()
    args = writer_args(plan, inspector=inspector, transport=transport)

    with pytest.raises(RuntimeError, match=reason):
        execute_synthetic_write(plan, manifest(plan), **args)

    assert transport.calls == []


def test_missing_or_ephemeral_persistent_key_stops_before_write():
    plan = make_plan()
    transport = Transport()
    args = writer_args(plan, transport=transport, key_provider=KeyProvider(None))
    with pytest.raises(RuntimeError, match="persistent_audit_key_required"):
        execute_synthetic_write(plan, manifest(plan), **args)

    ephemeral = PersistentAuditKey("ephemeral", b"x" * 32, persistent=False)
    args = writer_args(plan, transport=transport, key_provider=KeyProvider(ephemeral))
    with pytest.raises(RuntimeError, match="persistent_audit_key_required"):
        execute_synthetic_write(plan, manifest(plan), **args)
    assert transport.calls == []


def test_journal_unavailable_stops_before_write():
    plan = make_plan()
    transport = Transport()
    args = writer_args(
        plan, transport=transport,
        journal=InMemoryAttemptJournal(available=False),
    )
    with pytest.raises(RuntimeError, match="attempt_journal_unavailable"):
        execute_synthetic_write(plan, manifest(plan), **args)
    assert transport.calls == []


def test_lock_acquisition_failure_and_concurrent_run_stop_before_write():
    plan = make_plan()
    transport = Transport()
    denied = DenyLeaseManager()
    args = writer_args(plan, transport=transport, leases=denied)
    with pytest.raises(RuntimeError, match="writer_lock_unavailable"):
        execute_synthetic_write(plan, manifest(plan), **args)
    assert denied.acquire_calls == 1
    assert transport.calls == []


def test_stale_lease_can_be_replaced_but_live_lease_cannot():
    current = [NOW]
    leases = InMemoryLeaseManager(clock=lambda: current[0])
    first = leases.acquire("target", "owner-a", "run-a", 10)
    assert first is not None
    assert leases.acquire("target", "owner-b", "run-b", 10) is None

    current[0] += timedelta(seconds=11)
    replacement = leases.acquire("target", "owner-b", "run-b", 10)
    assert replacement is not None
    assert replacement.token != first.token
    leases.release(first)
    assert leases.acquire("target", "owner-c", "run-c", 10) is None


def test_expired_lease_cannot_be_renewed():
    current = [NOW]
    leases = InMemoryLeaseManager(clock=lambda: current[0])
    lease = leases.acquire("target", "owner", "run", 10)
    current[0] += timedelta(seconds=10)

    assert leases.renew(lease, 10) is None


def test_batch_upper_bound_and_deterministic_canary_one():
    plan = make_plan(4)
    with pytest.raises(ValueError, match="batch_size_exceeds_bound"):
        BatchPolicy(
            requested_size=ABSOLUTE_MAX_BATCH_SIZE + 1,
        ).validate()

    common = {
        "binding": TargetBinding(expected_spreadsheet_id=SHEET_ID),
        "inspector": Inspector(),
        "key_provider": KeyProvider(),
        "journal": InMemoryAttemptJournal(),
        "reader": SequenceReader(),
        "batch_policy": BatchPolicy(requested_size=1),
        "owner_id": "owner",
    }
    first = preflight_writer(
        plan, manifest(plan), leases=InMemoryLeaseManager(clock=lambda: NOW), **common,
    )
    second = preflight_writer(
        plan, manifest(plan), leases=InMemoryLeaseManager(clock=lambda: NOW), **common,
    )

    assert first.selected_count == second.selected_count == 1
    assert first.dry_run.candidate_results == second.dry_run.candidate_results


def test_pre_read_exact_existing_and_conflict_never_write():
    plan = make_plan()
    candidate = plan.candidates[0]
    exact = ExistingCanonicalRecord.from_candidate(candidate)
    transport = Transport()

    exact_result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(
            plan,
            reader=SequenceReader([{candidate.identity: IdentityRead((exact,))}]),
            transport=transport,
        ),
    )
    assert exact_result.already_present_count == 1
    assert exact_result.write_request_count == 0

    conflict = ExistingCanonicalRecord(**{**exact.__dict__, "amount_yen": 999})
    conflict_result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(
            plan,
            reader=SequenceReader([{candidate.identity: IdentityRead((conflict,))}]),
            transport=transport,
        ),
    )
    assert conflict_result.status == "preflight_stopped"
    assert conflict_result.conflict_count == 1
    assert transport.calls == []


def test_success_response_requires_exact_post_write_readback():
    plan = make_plan()
    candidate = plan.candidates[0]
    exact = ExistingCanonicalRecord.from_candidate(candidate)
    transport = Transport()
    reader = SequenceReader([
        {},
        {candidate.identity: IdentityRead()},
        {candidate.identity: IdentityRead((exact,))},
    ])

    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=reader, transport=transport),
    )

    assert result.write_request_count == 1
    assert result.confirmed_count == 1
    assert result.readback_count == 2
    assert len(transport.calls) == 1


def test_success_response_without_visible_row_is_outcome_unknown():
    plan = make_plan()
    transport = Transport()
    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=SequenceReader([{}, {}, {}, {}]), transport=transport),
    )

    assert result.confirmed_count == 0
    assert result.outcome_unknown_count == 1
    assert result.readback_count == 3
    assert len(transport.calls) == 1


def test_timeout_then_found_confirms_without_blind_write_retry():
    plan = make_plan()
    candidate = plan.candidates[0]
    exact = ExistingCanonicalRecord.from_candidate(candidate)
    transport = Transport(raises=TimeoutError("synthetic timeout"))
    reader = SequenceReader([{}, {candidate.identity: IdentityRead((exact,))}])

    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=reader, transport=transport),
    )

    assert result.confirmed_count == 1
    assert result.write_request_count == 1
    assert len(transport.calls) == 1


def test_timeout_then_definite_absence_is_retry_eligible():
    plan = make_plan()
    transport = Transport(raises=ConnectionError("synthetic drop"))
    reader = SequenceReader([{}, {}, {}, {}])

    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=reader, transport=transport),
    )

    assert result.retry_eligible_count == 1
    assert result.outcome_unknown_count == 0
    assert len(transport.calls) == 1


def test_timeout_then_read_failure_remains_stopped_outcome_unknown():
    plan = make_plan()
    transport = Transport(raises=TimeoutError("synthetic timeout"))
    reader = SequenceReader([{}], fail_after=1)

    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=reader, transport=transport),
    )

    assert result.status == "synthetic_stopped"
    assert result.outcome_unknown_count == 1
    assert result.retry_eligible_count == 0
    assert len(transport.calls) == 1


def test_post_write_read_retry_is_bounded_and_write_is_never_retried():
    plan = make_plan()
    transport = Transport()
    reader = SequenceReader([{}, {}, {}, {}])
    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(
            plan, reader=reader, transport=transport, read_attempts=3,
        ),
    )

    assert reader.calls == 4  # one pre-read plus three post-write reads
    assert result.readback_count == 3
    assert len(transport.calls) == 1


def test_unknown_outcome_stops_remaining_batch_as_not_started():
    plan = make_plan(3)
    transport = Transport()
    reader = SequenceReader([{}, {}, {}])

    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(
            plan, reader=reader, transport=transport,
            batch_size=3, read_attempts=2,
        ),
    )

    assert result.write_request_count == 1
    assert len(transport.calls) == 1
    assert result.outcome_unknown_count == 1
    assert [state for _ref, state in result.final_states].count(
        CandidateState.NOT_STARTED,
    ) == 2


def test_journal_state_transitions_are_complete_and_invalid_order_is_rejected():
    plan = make_plan()
    candidate = plan.candidates[0]
    exact = ExistingCanonicalRecord.from_candidate(candidate)
    journal = InMemoryAttemptJournal()
    reader = SequenceReader([{}, {candidate.identity: IdentityRead((exact,))}])

    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=reader, journal=journal),
    )
    assert result.confirmed_count == 1
    assert [event.stage for event in journal.events] == [
        JournalStage.PRE_READ,
        JournalStage.WRITE_ATTEMPTED,
        JournalStage.WRITE_RESULT,
        JournalStage.POST_READ,
        JournalStage.FINAL,
    ]
    assert all(event.canonical_identity == candidate.identity for event in journal.events)

    invalid = JournalEvent(
        run_id=str(uuid4()), canonical_identity=candidate.identity,
        attempt_id=str(uuid4()), batch_id="batch",
        stage=JournalStage.FINAL, state=CandidateState.FAILED,
        timestamp=NOW, reason_code="invalid",
    )
    with pytest.raises(RuntimeError, match="invalid_journal_state_transition"):
        journal.append(invalid)


def test_write_attempt_is_journaled_before_transport_call():
    plan = make_plan()
    candidate = plan.candidates[0]
    exact = ExistingCanonicalRecord.from_candidate(candidate)
    journal = InMemoryAttemptJournal()

    def assert_attempt_recorded(_candidate):
        assert journal.events[-1].stage == JournalStage.WRITE_ATTEMPTED
        assert journal.events[-1].state == CandidateState.WRITE_ATTEMPTED

    transport = Transport(on_write=assert_attempt_recorded)
    reader = SequenceReader([{}, {candidate.identity: IdentityRead((exact,))}])
    result = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(
            plan, reader=reader, transport=transport, journal=journal,
        ),
    )

    assert result.confirmed_count == 1


def test_journal_rejects_unstructured_or_sensitive_reason_text():
    journal = InMemoryAttemptJournal()
    event = JournalEvent(
        run_id=str(uuid4()), canonical_identity=mail_id("a"),
        attempt_id=str(uuid4()), batch_id="batch",
        stage=JournalStage.PRE_READ, state=CandidateState.VERIFIED_NEW,
        timestamp=NOW, reason_code="raw upstream response: secret",
    )

    with pytest.raises(RuntimeError, match="journal_event_content_invalid"):
        journal.append(event)


def test_same_plan_rerun_observes_exact_row_and_does_not_write_twice():
    plan = make_plan()
    state = {}
    reader = SequenceReader()
    transport = Transport(on_write=lambda candidate: state.update({
        candidate.identity: IdentityRead((ExistingCanonicalRecord.from_candidate(candidate),)),
    }))

    class StatefulReader:
        def read_identities(self, identities):
            return {identity: state.get(identity, IdentityRead()) for identity in identities}

    stateful = StatefulReader()
    first = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=stateful, transport=transport),
    )
    second = execute_synthetic_write(
        plan, manifest(plan),
        **writer_args(plan, reader=stateful, transport=transport),
    )

    assert first.confirmed_count == 1
    assert second.already_present_count == 1
    assert second.write_request_count == 0
    assert len(transport.calls) == 1


def test_real_transport_and_production_cli_capability_are_disabled():
    plan = make_plan()

    class RealTransport(Transport):
        synthetic_only = False

    transport = RealTransport()
    with pytest.raises(RuntimeError, match="production_writer_capability_disabled"):
        execute_synthetic_write(
            plan, manifest(plan), **writer_args(plan, transport=transport),
        )
    assert transport.calls == []

    cli_source = inspect.getsource(cli)
    assert "execute_synthetic_write" not in cli_source
    assert "production-writer" not in cli_source
