"""Production orchestration for bounded approved bank writes."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .aupay_card_executor import SheetsCanonicalIdentityReader
from .aupay_card_production import (
    ProtectedAuditKeyProvider,
    ProtectedCanaryApprovalProvider,
    SqliteAttemptJournal,
    SqliteCapabilityStore,
    SqliteLeaseManager,
)
from .aupay_card_writer import (
    ReadBackPolicy,
    ReadOnlySheetsTargetInspector,
    TargetBinding,
)
from .bank_canary import (
    BankBatchAuthority,
    BankCanaryAuthority,
    BankCanaryPreparationPipeline,
    build_bank_batch_plan,
    build_bank_canary_plan,
    dry_run_bank_batch,
    dry_run_bank_canary,
)
from .canonical_one_row_production import (
    GitCheckpointGuard,
    SealedCanonicalOneRowTransport,
    SheetsCanonicalRowsReader,
    create_canonical_five_row_manifest,
    create_canonical_one_row_manifest,
    execute_canonical_five_row_batch,
    execute_canonical_one_row_canary,
    issue_canonical_five_row_capability,
    issue_canonical_one_row_capability,
    project_bank_five_row_batch,
    project_bank_canary_candidate,
)


def run_bank_production_canary(
    db,
    pdf_path: str | Path,
    *,
    selected_source_identity: str,
    approved_target_spreadsheet_id: str,
    expected_git_head: str,
    repo_root: str | Path,
    state_dir: str | Path,
    audit_key_file: str | Path,
    approval_file: str | Path,
    account_alias: str,
    confirmed_internal_transfers: frozenset[tuple[str, str]],
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> dict:
    """Execute one exact append and stop; every mutable authority is one-shot."""
    repo = Path(repo_root).resolve()
    if str(db.sid) != approved_target_spreadsheet_id:
        raise RuntimeError("approved_target_mismatch")
    git_guard = GitCheckpointGuard(repo)
    checkpoint = git_guard.validate(
        expected_head=expected_git_head,
        expected_branch="agent/bank-csv-ingestion",
    )

    titles = tuple(db.sheet_titles())
    if "取込データ" not in titles:
        raise RuntimeError("target_worksheet_mismatch")
    pipeline = BankCanaryPreparationPipeline(db)
    shadow, preview = pipeline._context(
        pdf_path,
        account_alias=account_alias,
        confirmed_internal_transfers=confirmed_internal_transfers,
    )
    authority = BankCanaryAuthority(
        selected_source_identity=selected_source_identity,
        target_spreadsheet_id=approved_target_spreadsheet_id,
    )
    plan = build_bank_canary_plan(shadow, preview, authority)
    preflight = dry_run_bank_canary(plan, db, imported_at=clock())
    if (
        preflight.planned != 1
        or preflight.authorized != 1
        or preflight.existing_duplicate
        or preflight.ambiguous_collision
        or preflight.external_write_count
        or preview.candidate_identities.count(selected_source_identity) != 1
    ):
        raise RuntimeError("bank_canary_preflight_not_exactly_one")
    candidate = project_bank_canary_candidate(plan)
    binding = TargetBinding(
        expected_spreadsheet_id=approved_target_spreadsheet_id,
        expected_worksheet="取込データ",
    )
    key_provider = ProtectedAuditKeyProvider(audit_key_file, repo_root=repo)
    audit_key = key_provider.load()
    if audit_key is None:
        raise RuntimeError("persistent_audit_key_required")
    audit_key.validate()
    now = clock()
    manifest = create_canonical_one_row_manifest(
        candidate,
        binding=binding,
        audit_key=audit_key,
        expected_git_head=checkpoint.head,
        expected_branch=checkpoint.branch,
        created_at=now,
        run_id=str(uuid4()),
    )
    state_path = Path(state_dir).expanduser().resolve() / "bank-canary.sqlite3"
    journal = SqliteAttemptJournal(state_path, repo_root=repo)
    capability_store = SqliteCapabilityStore(state_path, repo_root=repo)
    leases = SqliteLeaseManager(state_path, repo_root=repo, clock=clock)
    inspector = ReadOnlySheetsTargetInspector(db)
    approval_provider = ProtectedCanaryApprovalProvider(
        approval_file, repo_root=repo,
    )
    capability = issue_canonical_one_row_capability(
        candidate,
        manifest,
        binding=binding,
        inspector=inspector,
        key_provider=key_provider,
        journal=journal,
        capability_store=capability_store,
        approval_provider=approval_provider,
        clock=clock,
    )
    transport = SealedCanonicalOneRowTransport(
        db,
        binding=binding,
        inspector=inspector,
        key_provider=key_provider,
        journal=journal,
        clock=clock,
    )
    execution = execute_canonical_one_row_canary(
        candidate,
        manifest,
        capability=capability,
        capability_store=capability_store,
        binding=binding,
        inspector=inspector,
        key_provider=key_provider,
        journal=journal,
        leases=leases,
        identity_reader=SheetsCanonicalIdentityReader(db),
        rows_reader=SheetsCanonicalRowsReader(db),
        transport=transport,
        readback_policy=ReadBackPolicy(3, (0.5, 1.0)),
        git_guard=git_guard,
        owner_id=f"bank-canary-{manifest.run_id}",
        clock=clock,
        sleeper=sleeper,
    )
    summary = execution.summary()
    summary.update({
        "branch": checkpoint.branch,
        "head": checkpoint.head,
        "upstream_equal": checkpoint.head == checkpoint.upstream_head,
        "ahead": checkpoint.ahead,
        "behind": checkpoint.behind,
        "target_sheet": "取込データ",
        "header_valid": preflight.target_header_valid,
        "planned_rows": preflight.planned,
        "authorized_rows": preflight.authorized,
        "existing_duplicate_before_write": preflight.existing_duplicate,
        "collision_before_write": preflight.ambiguous_collision,
        "capability_max_rows": candidate.max_rows,
        "parsed": preview.parsed,
        "eligible_before_dedupe": preview.eligible_before_dedupe,
        "withheld_by_classification": preview.withheld_by_classification,
    })
    return summary


def run_bank_production_batch(
    db,
    pdf_path: str | Path,
    *,
    selected_source_identities: tuple[str, ...],
    phase6_canary_identity: str,
    approved_target_spreadsheet_id: str,
    expected_git_head: str,
    repo_root: str | Path,
    state_dir: str | Path,
    audit_key_file: str | Path,
    approval_file: str | Path,
    account_alias: str,
    confirmed_internal_transfers: frozenset[tuple[str, str]],
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> dict:
    """Execute one exact five-row append and stop without fallback or retry."""
    repo = Path(repo_root).resolve()
    selected = tuple(selected_source_identities)
    if str(db.sid) != approved_target_spreadsheet_id:
        raise RuntimeError("approved_target_mismatch")
    git_guard = GitCheckpointGuard(repo)
    checkpoint = git_guard.validate(
        expected_head=expected_git_head,
        expected_branch="agent/bank-csv-ingestion",
    )

    titles = tuple(db.sheet_titles())
    if "取込データ" not in titles:
        raise RuntimeError("target_worksheet_mismatch")
    pipeline = BankCanaryPreparationPipeline(db)
    shadow, preview = pipeline._context(
        pdf_path,
        account_alias=account_alias,
        confirmed_internal_transfers=confirmed_internal_transfers,
    )
    authority = BankBatchAuthority(
        selected_source_identities=selected,
        target_spreadsheet_id=approved_target_spreadsheet_id,
    )
    plan = build_bank_batch_plan(shadow, preview, authority)
    preflight = dry_run_bank_batch(plan, db, imported_at=clock())
    canary_replay = pipeline.replay_status(
        pdf_path,
        selected_source_identity=phase6_canary_identity,
        account_alias=account_alias,
        confirmed_internal_transfers=confirmed_internal_transfers,
    )
    if (
        preflight.selected != 5
        or preflight.planned != 5
        or preflight.authorized != 5
        or preflight.max_writes != 5
        or preflight.income != 2
        or preflight.expense != 3
        or preflight.existing_duplicate
        or preflight.ambiguous_collision
        or preflight.withheld
        or preflight.external_write_count
        or any(preview.candidate_identities.count(identity) != 1 for identity in selected)
        or canary_replay["selected_existing_duplicate"] != 1
        or canary_replay["selected_new_plan_candidate"] != 0
    ):
        raise RuntimeError("bank_batch_preflight_not_exactly_five")

    batch = project_bank_five_row_batch(plan)
    binding = TargetBinding(
        expected_spreadsheet_id=approved_target_spreadsheet_id,
        expected_worksheet="取込データ",
    )
    key_provider = ProtectedAuditKeyProvider(audit_key_file, repo_root=repo)
    audit_key = key_provider.load()
    if audit_key is None:
        raise RuntimeError("persistent_audit_key_required")
    audit_key.validate()
    now = clock()
    manifest = create_canonical_five_row_manifest(
        batch,
        binding=binding,
        audit_key=audit_key,
        expected_git_head=checkpoint.head,
        expected_branch=checkpoint.branch,
        created_at=now,
        run_id=str(uuid4()),
    )
    state_path = Path(state_dir).expanduser().resolve() / "bank-five-row.sqlite3"
    journal = SqliteAttemptJournal(state_path, repo_root=repo)
    capability_store = SqliteCapabilityStore(state_path, repo_root=repo)
    leases = SqliteLeaseManager(state_path, repo_root=repo, clock=clock)
    inspector = ReadOnlySheetsTargetInspector(db)
    approval_provider = ProtectedCanaryApprovalProvider(
        approval_file, repo_root=repo,
    )
    capability = issue_canonical_five_row_capability(
        batch,
        manifest,
        binding=binding,
        inspector=inspector,
        key_provider=key_provider,
        journal=journal,
        capability_store=capability_store,
        approval_provider=approval_provider,
        clock=clock,
    )
    transport = SealedCanonicalOneRowTransport(
        db,
        binding=binding,
        inspector=inspector,
        key_provider=key_provider,
        journal=journal,
        clock=clock,
        max_rows=5,
    )
    execution = execute_canonical_five_row_batch(
        batch,
        manifest,
        capability=capability,
        capability_store=capability_store,
        binding=binding,
        inspector=inspector,
        key_provider=key_provider,
        journal=journal,
        leases=leases,
        identity_reader=SheetsCanonicalIdentityReader(db),
        rows_reader=SheetsCanonicalRowsReader(db),
        transport=transport,
        readback_policy=ReadBackPolicy(3, (0.5, 1.0)),
        git_guard=git_guard,
        owner_id=f"bank-five-row-{manifest.run_id}",
        clock=clock,
        sleeper=sleeper,
    )
    summary = execution.summary()
    summary.update({
        "branch": checkpoint.branch,
        "head": checkpoint.head,
        "upstream_equal": checkpoint.head == checkpoint.upstream_head,
        "ahead": checkpoint.ahead,
        "behind": checkpoint.behind,
        "target_sheet": "取込データ",
        "header_valid": preflight.target_header_valid,
        "selected": preflight.selected,
        "planned_rows": preflight.planned,
        "authorized_rows": preflight.authorized,
        "income": preflight.income,
        "expense": preflight.expense,
        "existing_duplicate_before_write": preflight.existing_duplicate,
        "collision_before_write": preflight.ambiguous_collision,
        "withheld_before_write": preflight.withheld,
        "capability_max_rows": batch.max_rows,
        "phase6_canary_existing_duplicate": canary_replay[
            "selected_existing_duplicate"
        ],
        "parsed": preview.parsed,
        "eligible_before_dedupe": preview.eligible_before_dedupe,
        "withheld_by_classification": preview.withheld_by_classification,
    })
    return summary
