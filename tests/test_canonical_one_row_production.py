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
)
from app.bank_canary import BankCanaryAuthority, build_bank_canary_plan
from app.bank_pdf_pipeline import BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.canonical_one_row_production import (
    CanonicalRowsSnapshot,
    GitCheckpoint,
    SheetsCanonicalRowsReader,
    create_canonical_one_row_manifest,
    execute_canonical_one_row_canary,
    issue_canonical_one_row_capability,
    project_bank_canary_candidate,
    validate_canonical_one_row_candidate,
    validate_git_checkpoint,
)
from app.sheets import HEADERS


NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
KEY = PersistentAuditKey("bank-phase6-test", b"k" * 32)


def corrupt_frozen(value, **changes):
    clone = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(
            clone, item.name, changes.get(item.name, getattr(value, item.name)),
        )
    return clone


def bank(identity="bank:income"):
    return NormalizedBankTransaction(
        source="auじぶん銀行PDF",
        account_alias="test-account",
        transaction_date="2026-09-01",
        description="給与 匿名勤務先",
        signed_amount=1000,
        source_page=1,
        source_row=1,
        source_row_identity=identity,
        source_row_hash="a" * 64,
        transaction_kind="deposit",
    )


def bank_plan(identity="bank:income"):
    transaction = bank(identity)
    parsed = BankPdfResult(
        pages=1,
        candidate_rows=1,
        transactions=(transaction,),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=(transaction.to_canonical(),),
    )
    shadow = build_bank_shadow_result(parsed, [])
    preview = build_bank_preview_plan(shadow, [])
    return build_bank_canary_plan(
        shadow,
        preview,
        BankCanaryAuthority(
            selected_source_identity=identity,
            target_spreadsheet_id="sheet-id",
        ),
    )


class FakeDB:
    sid = "sheet-id"

    def __init__(self, rows=(), header=None):
        self.rows = [list(row) for row in rows]
        self.header = list(HEADERS["取込データ"] if header is None else header)

    def get(self, range_name):
        if range_name == "取込データ!A1:L1":
            return [self.header]
        if range_name == "取込データ!A2:L":
            return self.rows
        raise AssertionError(range_name)


class SyntheticTransport:
    synthetic_only = True

    def __init__(self, db, *, mode="ack"):
        self.db = db
        self.mode = mode
        self.invocation_count = 0
        self.last_row = None

    def write_once(self, candidate, _permit):
        self.invocation_count += 1
        row = candidate.to_import_row(imported_at=NOW)
        self.last_row = tuple(row)
        if self.mode == "mismatch":
            row[8] = "unexpected_status"
        if self.mode != "absent":
            self.db.rows.append(row)
        if self.mode in {"ambiguous_present", "absent"}:
            raise TimeoutError("synthetic timeout")
        from app.aupay_card_writer import WriteDisposition, WriteRequestResult
        return WriteRequestResult(WriteDisposition.ACKNOWLEDGED, "synthetic_ack")


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def unissued_parts(tmp_path, *, approval_batch_size=1, header=None):
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    db = FakeDB(header=header)
    plan = bank_plan()
    candidate = project_bank_canary_candidate(plan)
    binding = TargetBinding(expected_spreadsheet_id=db.sid)
    manifest = create_canonical_one_row_manifest(
        candidate,
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
        "approval_reference": "phase6-test-approval",
        "candidate_ref": manifest.candidate_ref,
        "target_ref": manifest.target_ref,
        "batch_size": approval_batch_size,
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
    inspector = ReadOnlySheetsTargetInspector(db)
    return {
        "repo_root": repo_root,
        "db": db,
        "candidate": candidate,
        "binding": binding,
        "manifest": manifest,
        "journal": journal,
        "store": store,
        "leases": leases,
        "key_provider": key_provider,
        "approval_provider": approval_provider,
        "inspector": inspector,
    }


def issue(parts):
    capability = issue_canonical_one_row_capability(
        parts["candidate"],
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
    return execute_canonical_one_row_canary(
        parts["candidate"],
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


def test_exact_one_success_consumes_capability_and_commits_journal(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticTransport(parts["db"])

    result = execute(parts, transport)

    assert result.status == "canary_complete"
    assert result.write_request_count == 1
    assert result.actual_new_rows == 1
    assert result.exact_canonical_match
    assert result.duplicate_rows == 0
    assert result.unexpected_mutations == 0
    assert result.journal_committed
    assert result.capability_state == "sealed"
    assert result.lease_released
    assert transport.invocation_count == 1


def test_capability_max_rows_greater_than_one_is_rejected(tmp_path):
    parts = unissued_parts(tmp_path, approval_batch_size=2)

    with pytest.raises(RuntimeError, match="max_rows_must_be_one"):
        issue(parts)

    changed = corrupt_frozen(parts["candidate"], max_rows=2)
    with pytest.raises(RuntimeError, match="max_rows_must_be_one"):
        validate_canonical_one_row_candidate(changed)


def test_second_invocation_and_same_candidate_reissue_are_rejected(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticTransport(parts["db"])
    execute(parts, transport)

    with pytest.raises(RuntimeError, match="production_capability_reused"):
        execute(parts, transport)
    assert transport.invocation_count == 1

    with pytest.raises(RuntimeError, match="attempt_journal_replay"):
        issue_canonical_one_row_capability(
            parts["candidate"], parts["manifest"],
            binding=parts["binding"], inspector=parts["inspector"],
            key_provider=parts["key_provider"], journal=parts["journal"],
            capability_store=parts["store"],
            approval_provider=parts["approval_provider"], clock=lambda: NOW,
        )


def test_existing_journal_run_rejects_capability_issue(tmp_path):
    parts = unissued_parts(tmp_path)
    parts["journal"].append(JournalEvent(
        run_id=parts["manifest"].run_id,
        canonical_identity=parts["candidate"].identity,
        attempt_id="22222222-2222-4222-8222-222222222222",
        batch_id="canonical-one-row-replay-test",
        stage=JournalStage.PRE_READ,
        state=CandidateState.VERIFIED_NEW,
        timestamp=NOW,
        reason_code="still_new",
    ))

    with pytest.raises(RuntimeError, match="attempt_journal_replay"):
        issue(parts)


def test_lease_conflict_rejects_before_transport(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    held = parts["leases"].acquire(
        parts["manifest"].target_ref, "other-owner", "other-run", 300,
    )
    transport = SyntheticTransport(parts["db"])

    with pytest.raises(RuntimeError, match="writer_lock_unavailable"):
        execute(parts, transport)

    assert held is not None
    assert transport.invocation_count == 0
    assert parts["store"].state(parts["capability"].capability_id) == "sealed"


@pytest.mark.parametrize(("header", "reason"), [
    (["wrong"], "target_schema_mismatch"),
])
def test_target_header_mismatch_rejects_capability_issue(tmp_path, header, reason):
    parts = unissued_parts(tmp_path, header=header)

    with pytest.raises(RuntimeError, match=reason):
        issue(parts)


def test_target_and_git_checkpoint_mismatch_reject(tmp_path):
    checkpoint = GitCheckpoint(
        branch="agent/bank-csv-ingestion",
        head="a" * 40,
        upstream_head="b" * 40,
        ahead=1,
        behind=0,
        clean=True,
    )
    with pytest.raises(RuntimeError, match="unpushed_head"):
        validate_git_checkpoint(
            checkpoint,
            expected_head="a" * 40,
            expected_branch="agent/bank-csv-ingestion",
        )

    dirty = corrupt_frozen(checkpoint, clean=False, upstream_head="a" * 40, ahead=0)
    with pytest.raises(RuntimeError, match="dirty_worktree"):
        validate_git_checkpoint(
            dirty,
            expected_head="a" * 40,
            expected_branch="agent/bank-csv-ingestion",
        )


def test_execution_target_mismatch_reseals_without_dispatch(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    parts["db"].sid = "different-sheet"
    transport = SyntheticTransport(parts["db"])

    with pytest.raises(RuntimeError, match="target_spreadsheet_mismatch"):
        execute(parts, transport)

    assert transport.invocation_count == 0
    assert parts["store"].state(parts["capability"].capability_id) == "sealed"


def test_duplicate_before_write_is_journaled_and_never_dispatched(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    parts["db"].rows.append(parts["candidate"].to_import_row(imported_at=NOW))
    transport = SyntheticTransport(parts["db"])

    result = execute(parts, transport)

    assert result.status == "canary_stopped"
    assert result.final_state.value == "already_present"
    assert result.write_request_count == 0
    assert result.journal_committed
    assert result.capability_state == "sealed"
    assert transport.invocation_count == 0


def test_ambiguous_write_outcome_is_recovered_only_by_exact_readback(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticTransport(parts["db"], mode="ambiguous_present")

    result = execute(parts, transport)

    assert result.status == "canary_complete"
    assert result.recovered_from_ambiguous
    assert result.write_request_count == 1
    assert result.actual_new_rows == 1
    assert transport.invocation_count == 1


def test_ambiguous_write_absence_is_failure_without_retry(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticTransport(parts["db"], mode="absent")

    result = execute(parts, transport)

    assert result.status == "canary_stopped"
    assert result.final_state.value == "failed"
    assert result.reason_code == "write_absence_confirmed_failure"
    assert result.write_request_count == 1
    assert transport.invocation_count == 1


def test_post_read_full_row_mismatch_is_conflict(tmp_path):
    parts = unissued_parts(tmp_path)
    issue(parts)
    transport = SyntheticTransport(parts["db"], mode="mismatch")

    result = execute(parts, transport)

    assert result.status == "canary_stopped"
    assert result.final_state.value == "conflict"
    assert not result.exact_canonical_match
    assert result.unexpected_mutations == 1
    assert result.write_request_count == 1
    assert transport.invocation_count == 1


def test_classification_bypass_and_authority_provenance_are_rejected(tmp_path):
    parts = unissued_parts(tmp_path)
    changed = corrupt_frozen(parts["candidate"], transaction_kind="transfer")
    with pytest.raises(RuntimeError, match="classification_withheld"):
        validate_canonical_one_row_candidate(changed)

    changed_manifest = corrupt_frozen(
        parts["manifest"], authority_provenance="manual_override",
    )
    with pytest.raises(RuntimeError, match="authority_provenance_invalid"):
        changed_manifest.validate()


def test_rows_reader_normalizes_trailing_blank_cells():
    db = FakeDB(rows=([["id", "timestamp"],]))

    snapshot = SheetsCanonicalRowsReader(db).read_rows()

    assert snapshot == CanonicalRowsSnapshot((
        ("id", "timestamp", "", "", "", "", "", "", "", "", "", ""),
    ))
