import base64
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.aupay_card_executor import CandidateState, SheetsCanonicalIdentityReader
from app.aupay_card_production import (
    ProtectedAuditKeyProvider,
    ProtectedCanaryApprovalProvider,
    SqliteAttemptJournal,
    SqliteCapabilityStore,
    SqliteLeaseManager,
)
from app.aupay_card_writer import (
    JournalEvent,
    JournalStage,
    PersistentAuditKey,
    ReadBackPolicy,
    ReadOnlySheetsTargetInspector,
    TargetBinding,
    WriteDisposition,
    WriteRequestResult,
)
from app.bank_canary import BankBatchAuthority, build_bank_batch_plan
from app.bank_pdf_pipeline import BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.canonical_one_row_production import (
    CanonicalDispatchPermit,
    CanonicalRowsSnapshot,
    SealedCanonicalOneRowTransport,
    SheetsCanonicalRowsReader,
    create_canonical_five_row_manifest,
    execute_canonical_five_row_batch,
    issue_canonical_five_row_capability,
    project_bank_initial_backfill_batch,
    project_bank_loan_repayment_batch,
    project_bank_five_row_batch,
    validate_canonical_five_row_batch,
)
from app.sheets import HEADERS


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
KEY = PersistentAuditKey("bank-phase8-test", b"b" * 32)


def corrupt_frozen(value, **changes):
    clone = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(
            clone, item.name, changes.get(item.name, getattr(value, item.name)),
        )
    return clone


def bank(identity, amount):
    return NormalizedBankTransaction(
        source="auじぶん銀行PDF",
        account_alias="test-account",
        transaction_date="2026-09-01",
        description=(
            f"給与 匿名勤務先 {identity}"
            if amount > 0 else f"口座振替 公共サービス {identity}"
        ),
        signed_amount=amount,
        source_page=1,
        source_row=1,
        source_row_identity=identity,
        source_row_hash=(identity.encode().hex() * 4)[:64],
        transaction_kind="deposit" if amount > 0 else "withdrawal",
    )


def transactions(row_count=5):
    if row_count == 4:
        return tuple(
            NormalizedBankTransaction(
                source="auじぶん銀行PDF",
                account_alias="test-account",
                transaction_date="2026-09-01",
                description="約定返済",
                signed_amount=-(index + 1) * 100,
                source_page=1,
                source_row=index + 1,
                source_row_identity=f"bank:loan-{index}",
                source_row_hash=(f"loan-{index}".encode().hex() * 8)[:64],
                transaction_kind="withdrawal",
            )
            for index in range(4)
        )
    if row_count == 5:
        return (
            bank("bank:expense-1", -1000),
            bank("bank:expense-2", -2000),
            bank("bank:expense-3", -3000),
            bank("bank:income-1", 4000),
            bank("bank:income-2", 5000),
        )
    expenses = tuple(
        bank(f"bank:expense-{index}", -(index + 1) * 100)
        for index in range(26)
    )
    incomes = tuple(
        bank(f"bank:income-{index}", (index + 1) * 100)
        for index in range(25)
    )
    return expenses + incomes


def batch_plan(row_count=5):
    items = transactions(row_count)
    parsed = BankPdfResult(
        pages=1,
        candidate_rows=row_count,
        transactions=items,
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=tuple(item.to_canonical() for item in items),
    )
    shadow = build_bank_shadow_result(parsed, [])
    preview = build_bank_preview_plan(shadow, [])
    return build_bank_batch_plan(
        shadow,
        preview,
        BankBatchAuthority(
            selected_source_identities=tuple(
                item.source_row_identity for item in items
            ),
            target_spreadsheet_id="sheet-id",
            min_rows=row_count,
            max_rows=row_count,
            authority_mode=(
                "bank_loan_repayment_preparation" if row_count == 4
                else (
                    "bank_initial_backfill" if row_count == 51
                    else "bank_batch_preparation"
                )
            ),
        ),
    )


class FakeDB:
    sid = "sheet-id"

    def __init__(self, rows=(), header=None):
        self.rows = [list(row) for row in rows]
        self.header = list(HEADERS["取込データ"] if header is None else header)
        self.svc = AppendRecorder()

    def get(self, range_name):
        if range_name == "取込データ!A1:L1":
            return [self.header]
        if range_name == "取込データ!A2:L":
            return self.rows
        raise AssertionError(range_name)


class AppendRecorder:
    def __init__(self):
        self.requests = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def append(self, **request):
        self.requests.append(request)
        return self

    def execute(self):
        return {"updates": {"updatedRows": 5}}


class SyntheticBatchTransport:
    synthetic_only = True

    def __init__(self, db, *, mode="ack", max_rows=5):
        self.db = db
        self.mode = mode
        self.max_rows = max_rows
        self.invocation_count = 0
        self.last_rows = None

    def write_batch_once(self, batch, _permit):
        self.invocation_count += 1
        rows = tuple(
            tuple(candidate.to_import_row(imported_at=NOW))
            for candidate in batch.candidates
        )
        self.last_rows = rows
        if self.mode == "partial":
            self.db.rows.extend([list(row) for row in rows[:2]])
            raise TimeoutError("synthetic timeout")
        if self.mode == "absent":
            raise TimeoutError("synthetic timeout")
        appended = [list(row) for row in rows]
        if self.mode == "mismatch":
            appended[-1][8] = "unexpected_status"
        self.db.rows.extend(appended)
        if self.mode == "ambiguous_present":
            raise TimeoutError("synthetic timeout")
        return WriteRequestResult(WriteDisposition.ACKNOWLEDGED, "synthetic_ack")


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def unissued_parts(tmp_path, *, approval_batch_size=None, header=None, row_count=5):
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    db = FakeDB(header=header)
    plan = batch_plan(row_count)
    batch = (
        project_bank_loan_repayment_batch(plan)
        if row_count == 4 else (
            project_bank_initial_backfill_batch(plan)
            if row_count == 51 else project_bank_five_row_batch(plan)
        )
    )
    binding = TargetBinding(expected_spreadsheet_id=db.sid)
    manifest = create_canonical_five_row_manifest(
        batch,
        binding=binding,
        audit_key=KEY,
        expected_git_head="a" * 40,
        expected_branch="agent/bank-csv-ingestion",
        created_at=NOW,
        run_id="11111111-1111-4111-8111-111111111111",
    )
    key_file = tmp_path / "audit-key.json"
    write_json(key_file, {
        "key_id": KEY.key_id,
        "key_b64": base64.b64encode(KEY.secret).decode("ascii"),
    })
    approval_file = tmp_path / "approval.json"
    write_json(approval_file, {
        "approval_reference": "phase8-test-approval",
        "candidate_ref": manifest.batch_ref,
        "target_ref": manifest.target_ref,
        "batch_size": row_count if approval_batch_size is None else approval_batch_size,
        "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
    })
    state = tmp_path / "state.sqlite3"
    journal = SqliteAttemptJournal(state, repo_root=repo_root)
    store = SqliteCapabilityStore(state, repo_root=repo_root)
    leases = SqliteLeaseManager(state, repo_root=repo_root, clock=lambda: NOW)
    key_provider = ProtectedAuditKeyProvider(key_file, repo_root=repo_root)
    approval_provider = ProtectedCanaryApprovalProvider(
        approval_file, repo_root=repo_root,
    )
    return {
        "repo_root": repo_root,
        "db": db,
        "batch": batch,
        "binding": binding,
        "manifest": manifest,
        "journal": journal,
        "store": store,
        "leases": leases,
        "key_provider": key_provider,
        "approval_provider": approval_provider,
        "inspector": ReadOnlySheetsTargetInspector(db),
    }


def issue(parts):
    capability = issue_canonical_five_row_capability(
        parts["batch"],
        parts["manifest"],
        binding=parts["binding"],
        inspector=parts["inspector"],
        key_provider=parts["key_provider"],
        journal=parts["journal"],
        capability_store=parts["store"],
        approval_provider=parts["approval_provider"],
        clock=lambda: NOW,
    )
    parts["capability"] = capability
    return capability


def execute(parts, transport):
    return execute_canonical_five_row_batch(
        parts["batch"],
        parts["manifest"],
        capability=parts["capability"],
        capability_store=parts["store"],
        binding=parts["binding"],
        inspector=parts["inspector"],
        key_provider=parts["key_provider"],
        journal=parts["journal"],
        leases=parts["leases"],
        identity_reader=SheetsCanonicalIdentityReader(parts["db"]),
        rows_reader=SheetsCanonicalRowsReader(parts["db"]),
        transport=transport,
        readback_policy=ReadBackPolicy(1, ()),
        git_guard=None,
        owner_id="synthetic-owner",
        clock=lambda: NOW,
        sleeper=lambda _delay: None,
        synthetic=True,
    )


def test_exact_five_succeeds_in_one_request_and_seals_authority(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"])

    result = execute(parts, transport)

    assert result.status == "batch_complete"
    assert result.requested_rows == 5
    assert result.write_request_count == 1
    assert result.actual_new_rows == 5
    assert result.matched_identity_count == 5
    assert result.exact_canonical_match
    assert result.duplicate_rows == 0
    assert result.unexpected_mutations == 0
    assert result.journal_committed
    assert result.capability_state == "sealed"
    assert result.lease_released
    assert transport.invocation_count == 1


def test_exact_51_backfill_succeeds_in_one_request(tmp_path):
    parts = unissued_parts(tmp_path, row_count=51)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"], max_rows=51)

    result = execute(parts, transport)

    assert result.status == "batch_complete"
    assert result.requested_rows == 51
    assert result.write_request_count == 1
    assert result.actual_new_rows == 51
    assert result.matched_identity_count == 51
    assert result.exact_canonical_match
    assert result.capability_state == "sealed"
    assert result.lease_released
    assert transport.invocation_count == 1


def test_exact_four_loan_batch_succeeds_and_seals_authority(tmp_path):
    parts = unissued_parts(tmp_path, row_count=4)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"], max_rows=4)

    result = execute(parts, transport)

    assert result.status == "batch_complete"
    assert result.requested_rows == 4
    assert result.write_request_count == 1
    assert result.actual_new_rows == 4
    assert result.matched_identity_count == 4
    assert result.exact_canonical_match
    assert result.duplicate_rows == 0
    assert result.unexpected_mutations == 0
    assert result.journal_committed
    assert result.capability_state == "sealed"
    assert result.lease_released
    assert transport.invocation_count == 1


@pytest.mark.parametrize("row_count", [4, 5, 51])
def test_sealed_transport_sends_one_exact_bounded_append(tmp_path, row_count):
    parts = unissued_parts(tmp_path, row_count=row_count)
    capability = issue(parts)
    attempt_id = "22222222-2222-4222-8222-222222222222"
    parts["store"].claim(capability, attempt_id, NOW)
    parts["journal"].append(JournalEvent(
        run_id=parts["manifest"].run_id,
        canonical_identity=parts["manifest"].batch_ref,
        attempt_id=attempt_id,
        batch_id="canonical-five-row-test",
        stage=JournalStage.PRE_READ,
        state=CandidateState.VERIFIED_NEW,
        timestamp=NOW,
        reason_code="all_five_still_new",
    ))
    parts["journal"].append(JournalEvent(
        run_id=parts["manifest"].run_id,
        canonical_identity=parts["manifest"].batch_ref,
        attempt_id=attempt_id,
        batch_id="canonical-five-row-test",
        stage=JournalStage.WRITE_ATTEMPTED,
        state=CandidateState.WRITE_ATTEMPTED,
        timestamp=NOW,
        reason_code="write_request_about_to_send",
    ))
    parts["store"].authorize_dispatch(capability, attempt_id, NOW)
    permit = CanonicalDispatchPermit._create(
        capability, attempt_id=attempt_id, max_rows=row_count,
    )
    transport = SealedCanonicalOneRowTransport(
        parts["db"],
        binding=parts["binding"],
        inspector=parts["inspector"],
        key_provider=parts["key_provider"],
        journal=parts["journal"],
        clock=lambda: NOW,
        max_rows=row_count,
    )

    result = transport.write_batch_once(parts["batch"], permit)

    assert result.disposition == WriteDisposition.ACKNOWLEDGED
    assert transport.invocation_count == 1
    assert len(parts["db"].svc.requests) == 1
    assert len(parts["db"].svc.requests[0]["body"]["values"]) == row_count
    assert all(len(row) == 12 for row in parts["db"].svc.requests[0]["body"]["values"])


def test_non_five_approval_and_corrupted_batch_are_rejected(tmp_path):
    parts = unissued_parts(tmp_path, approval_batch_size=4)
    with pytest.raises(RuntimeError, match="exactly_five_rows"):
        issue(parts)

    changed = corrupt_frozen(parts["batch"], candidates=parts["batch"].candidates[:4])
    with pytest.raises(RuntimeError, match="exactly_five_rows"):
        validate_canonical_five_row_batch(changed)


@pytest.mark.parametrize("row_count", [50, 52])
def test_initial_backfill_rejects_non_51_authority(row_count):
    authority = BankBatchAuthority(
        selected_source_identities=tuple(
            f"bank:{index}" for index in range(row_count)
        ),
        target_spreadsheet_id="sheet-id",
        min_rows=row_count,
        max_rows=row_count,
        authority_mode="bank_initial_backfill",
    )

    with pytest.raises(RuntimeError, match="row_bound_invalid"):
        authority.validate()


def test_existing_one_of_five_stops_whole_batch_before_dispatch(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    parts["db"].rows.append(
        parts["batch"].candidates[0].to_import_row(imported_at=NOW)
    )
    transport = SyntheticBatchTransport(parts["db"])

    result = execute(parts, transport)

    assert result.status == "batch_stopped"
    assert result.write_request_count == 0
    assert result.reason_code == "canonical_batch_existing_identity"
    assert transport.invocation_count == 0


def test_partial_outcome_stops_without_retry_or_cleanup(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"], mode="partial")

    result = execute(parts, transport)

    assert result.status == "partial_write_outcome"
    assert result.matched_identity_count == 2
    assert result.write_request_count == 1
    assert len(parts["db"].rows) == 2
    assert transport.invocation_count == 1


def test_51_row_partial_outcome_stops_without_retry(tmp_path):
    parts = unissued_parts(tmp_path, row_count=51)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"], mode="partial", max_rows=51)

    result = execute(parts, transport)

    assert result.status == "partial_write_outcome"
    assert result.matched_identity_count == 2
    assert result.write_request_count == 1
    assert len(parts["db"].rows) == 2
    assert transport.invocation_count == 1


def test_ambiguous_outcome_recovers_only_when_all_five_match(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"], mode="ambiguous_present")

    result = execute(parts, transport)

    assert result.status == "batch_complete"
    assert result.recovered_from_ambiguous
    assert result.matched_identity_count == 5
    assert result.write_request_count == 1
    assert transport.invocation_count == 1


def test_ambiguous_absence_is_failure_without_retry(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"], mode="absent")

    result = execute(parts, transport)

    assert result.status == "batch_stopped"
    assert result.reason_code == "write_absence_confirmed_failure"
    assert result.write_request_count == 1
    assert transport.invocation_count == 1


def test_post_read_row_mismatch_is_conflict(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticBatchTransport(parts["db"], mode="mismatch")

    result = execute(parts, transport)

    assert result.status == "batch_stopped"
    assert result.final_state.value == "conflict"
    assert result.unexpected_mutations == 1
    assert result.write_request_count == 1


def test_capability_replay_and_lease_conflict_reject(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    held = parts["leases"].acquire(
        parts["manifest"].target_ref, "other", "other-run", 300,
    )
    transport = SyntheticBatchTransport(parts["db"])
    with pytest.raises(RuntimeError, match="writer_lock_unavailable"):
        execute(parts, transport)
    assert held is not None
    assert transport.invocation_count == 0
    with pytest.raises(RuntimeError, match="production_capability_reused"):
        execute(parts, transport)


def test_header_mismatch_rejects_capability_issue(tmp_path):
    parts = unissued_parts(tmp_path, header=["wrong"])
    with pytest.raises(RuntimeError, match="target_schema_mismatch"):
        issue(parts)


def test_rows_snapshot_type_remains_source_independent():
    assert CanonicalRowsSnapshot(()).rows == ()
