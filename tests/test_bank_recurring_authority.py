import base64
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from app.aupay_card_production import (
    ProtectedAuditKeyProvider,
    SqliteAttemptJournal,
    SqliteCapabilityStore,
)
from app.aupay_card_writer import PersistentAuditKey, ReadOnlySheetsTargetInspector, TargetBinding
from app.bank_canary import BankBatchAuthority, build_bank_batch_plan
from app.bank_canary_production import run_bank_production_batch
from app.bank_pdf_pipeline import (
    CHIBA_BANK_SOURCE,
    DOCOMO_SMTB_SOURCE,
    SOURCE,
    BankPdfResult,
    NormalizedBankTransaction,
)
from app.bank_reconciliation import build_bank_preview_plan, build_bank_shadow_result
from app.bank_recurring_authority import (
    BANK_RECURRING_SOURCE,
    ProtectedBankRecurringAuthorityProvider,
    create_bank_recurring_run_context,
    issue_bank_recurring_batch_capability,
)
from app.canonical_one_row_production import (
    create_canonical_five_row_manifest,
    project_bank_bounded_batch,
)
from app.sheets import HEADERS


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
KEY = PersistentAuditKey("bank-recurring-test-v1", b"r" * 32)


class _DB:
    sid = "sheet-id"

    def get(self, range_name):
        if range_name == "取込データ!A1:L1":
            return [HEADERS["取込データ"]]
        if range_name == "取込データ!A2:L":
            return []
        raise AssertionError(range_name)


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _authority_file(tmp_path, **overrides):
    value = {
        "schema_version": 1,
        "policy_id": "bank-pdf-recurring-v1",
        "source": BANK_RECURRING_SOURCE,
        "expected_spreadsheet_id": "sheet-id",
        "expected_drive_folder_id": "A" * 20,
        "expected_worksheet": "取込データ",
        "target_binding_version": 1,
        "canonical_schema_version": 1,
        "supported_bank_sources": [SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE],
        "allowed_classifications": ["expense"],
        "max_files": 20,
        "max_rows": 100,
        "overlap_seconds": 3600,
        "max_window_seconds": 7 * 86400,
        "initial_start": "2026-09-13T00:00:00+09:00",
        "valid_from": "2026-09-13T00:00:00+09:00",
        "expires_at": "2027-09-14T00:00:00+09:00",
        "expected_branch": "main",
    }
    value.update(overrides)
    path = tmp_path / "bank-authority.json"
    _write_json(path, value)
    return path


def _transaction(identity="bank:expense-1", *, source=SOURCE, amount=-1000):
    return NormalizedBankTransaction(
        source=source,
        account_alias="test-account",
        transaction_date="2026-09-14",
        description="口座振替 公共サービス" if amount < 0 else "給与 匿名勤務先",
        signed_amount=amount,
        source_page=1,
        source_row=1,
        source_row_identity=identity,
        source_row_hash=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        transaction_kind="withdrawal" if amount < 0 else "deposit",
    )


def _parts(tmp_path, *, transaction=None, authority_overrides=None):
    tx = transaction or _transaction()
    parsed = BankPdfResult(
        pages=1,
        candidate_rows=1,
        transactions=(tx,),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=(tx.to_canonical(),),
        source=tx.source,
    )
    shadow = build_bank_shadow_result(parsed, [])
    preview = build_bank_preview_plan(shadow, [])
    plan = build_bank_batch_plan(
        shadow,
        preview,
        BankBatchAuthority(
            selected_source_identities=(tx.source_row_identity,),
            target_spreadsheet_id="sheet-id",
            min_rows=1,
            max_rows=1,
            authority_mode="bank_steady_state",
        ),
    )
    batch = project_bank_bounded_batch(plan)
    binding = TargetBinding(expected_spreadsheet_id="sheet-id")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    key_path = tmp_path / "audit.json"
    _write_json(key_path, {
        "key_id": KEY.key_id,
        "key_b64": base64.b64encode(KEY.secret).decode("ascii"),
    })
    key_provider = ProtectedAuditKeyProvider(key_path, repo_root=repo_root)
    manifest = create_canonical_five_row_manifest(
        batch,
        binding=binding,
        audit_key=KEY,
        expected_git_head="a" * 40,
        expected_branch="main",
        created_at=NOW,
        run_id="11111111-1111-4111-8111-111111111111",
    )
    authority_path = _authority_file(tmp_path, **(authority_overrides or {}))
    provider = ProtectedBankRecurringAuthorityProvider(authority_path, repo_root=repo_root)
    context = create_bank_recurring_run_context(
        authority_provider=provider,
        run_id="22222222-2222-4222-8222-222222222222",
        now=NOW,
        spreadsheet_id="sheet-id",
        drive_folder_id="A" * 20,
        files_seen=1,
        candidate_batches=((tx.source_row_identity,),),
    )
    state_path = tmp_path / "state.sqlite3"
    return {
        "db": _DB(),
        "batch": batch,
        "binding": binding,
        "manifest": manifest,
        "key_provider": key_provider,
        "journal": SqliteAttemptJournal(state_path, repo_root=repo_root),
        "store": SqliteCapabilityStore(state_path, repo_root=repo_root),
        "context": context,
        "provider": provider,
        "repo_root": repo_root,
    }


def _issue(parts):
    return issue_bank_recurring_batch_capability(
        parts["batch"],
        parts["manifest"],
        context=parts["context"],
        binding=parts["binding"],
        inspector=ReadOnlySheetsTargetInspector(parts["db"]),
        key_provider=parts["key_provider"],
        journal=parts["journal"],
        capability_store=parts["store"],
        clock=lambda: NOW,
    )


def _corrupt(value, **changes):
    clone = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(clone, item.name, changes.get(item.name, getattr(value, item.name)))
    return clone


def test_valid_bank_recurring_authority_mints_short_lived_capability(tmp_path):
    parts = _parts(tmp_path)

    capability = _issue(parts)

    assert capability.approval_reference.startswith("recurring:bank-recurring-v1:")
    assert capability.expires_at - capability.issued_at == timedelta(seconds=120)
    assert parts["store"].state(capability.capability_id) == "issued"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("supported_bank_sources", [SOURCE, "unsupported-bank"]),
        ("allowed_classifications", ["expense", "income"]),
        ("max_files", 21),
        ("max_rows", 101),
        ("expected_worksheet", "別シート"),
    ),
)
def test_bank_recurring_authority_rejects_scope_expansion(tmp_path, field, value):
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path, **{field: value}),
        repo_root=tmp_path / "repo",
    )

    with pytest.raises(RuntimeError, match="protected_bank_recurring_authority_invalid"):
        provider.load()


def test_bank_recurring_context_rejects_spreadsheet_or_drive_mismatch(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path), repo_root=repo_root,
    )
    base = {
        "authority_provider": provider,
        "run_id": "22222222-2222-4222-8222-222222222222",
        "now": NOW,
        "files_seen": 1,
        "candidate_batches": (("bank:expense-1",),),
    }

    with pytest.raises(RuntimeError, match="scope_mismatch"):
        create_bank_recurring_run_context(
            **base, spreadsheet_id="wrong", drive_folder_id="A" * 20,
        )
    with pytest.raises(RuntimeError, match="scope_mismatch"):
        create_bank_recurring_run_context(
            **base, spreadsheet_id="sheet-id", drive_folder_id="B" * 20,
        )


def test_bank_recurring_context_rejects_total_row_limit(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path), repo_root=repo_root,
    )

    with pytest.raises(RuntimeError, match="scope_mismatch"):
        create_bank_recurring_run_context(
            authority_provider=provider,
            run_id="22222222-2222-4222-8222-222222222222",
            now=NOW,
            spreadsheet_id="sheet-id",
            drive_folder_id="A" * 20,
            files_seen=20,
            candidate_batches=(tuple(f"bank:expense-{index}" for index in range(101)),),
        )


def test_recurring_capability_rejects_unsupported_bank_source(tmp_path):
    parts = _parts(tmp_path)
    candidate = parts["batch"].candidates[0]
    changed_candidate = _corrupt(candidate, source="unsupported-bank")
    changed_batch = _corrupt(parts["batch"], candidates=(changed_candidate,))
    changed_manifest = create_canonical_five_row_manifest(
        changed_batch,
        binding=parts["binding"],
        audit_key=KEY,
        expected_git_head="a" * 40,
        expected_branch="main",
        created_at=NOW,
        run_id="33333333-3333-4333-8333-333333333333",
    )

    with pytest.raises(RuntimeError, match="scope_mismatch"):
        issue_bank_recurring_batch_capability(
            changed_batch,
            changed_manifest,
            context=parts["context"],
            binding=parts["binding"],
            inspector=ReadOnlySheetsTargetInspector(parts["db"]),
            key_provider=parts["key_provider"],
            journal=parts["journal"],
            capability_store=parts["store"],
            clock=lambda: NOW,
        )


def test_recurring_capability_rejects_income(tmp_path):
    parts = _parts(tmp_path, transaction=_transaction("bank:income-1", amount=1000))

    with pytest.raises(RuntimeError, match="scope_mismatch"):
        _issue(parts)


def test_manual_batch_does_not_accept_recurring_context_without_launcher(tmp_path):
    parts = _parts(tmp_path)

    with pytest.raises(RuntimeError, match="bank_recurring_launcher_required"):
        run_bank_production_batch(
            None,
            "unused.pdf",
            selected_source_identities=("bank:expense-1",),
            phase6_canary_identity=None,
            approved_target_spreadsheet_id="sheet-id",
            expected_git_head="a" * 40,
            expected_branch="main",
            repo_root=parts["repo_root"],
            state_dir=tmp_path,
            audit_key_file=tmp_path / "audit.json",
            approval_file=None,
            account_alias="test-account",
            confirmed_internal_transfers=frozenset(),
            clock=lambda: NOW,
            sleeper=lambda _delay: None,
            steady_state=True,
            _recurring_context=parts["context"],
        )


def test_manual_batch_still_requires_candidate_approval(tmp_path):
    with pytest.raises(RuntimeError, match="protected_canary_approval_required"):
        run_bank_production_batch(
            None,
            "unused.pdf",
            selected_source_identities=("bank:expense-1",),
            phase6_canary_identity=None,
            approved_target_spreadsheet_id="sheet-id",
            expected_git_head="a" * 40,
            expected_branch="main",
            repo_root=tmp_path,
            state_dir=tmp_path,
            audit_key_file=tmp_path / "audit.json",
            approval_file=None,
            account_alias="test-account",
            confirmed_internal_transfers=frozenset(),
            clock=lambda: NOW,
            sleeper=lambda _delay: None,
            steady_state=True,
        )


def test_bank_recurring_workflow_uses_standing_authority_not_static_approval():
    workflow = Path(".github/workflows/bank-pdf-recurring.yml").read_text(encoding="utf-8")

    assert "BANK_PDF_RECURRING_AUTHORITY_JSON" in workflow
    assert "BANK_AUDIT_KEY_JSON" in workflow
    assert "BANK_APPROVAL_JSON" not in workflow
    assert "--approval-file" not in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "schedule:" not in workflow
