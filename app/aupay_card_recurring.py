"""Bounded, new-item-only recurring production flow for au PAY card Gmail."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from .aupay_card_apply_plan import build_canonical_apply_plan, production_import_status
from .aupay_card_batch import (
    ProtectedRecurringAuthorityProvider,
    RecurringAuthorityPolicy,
    SealedSheetsBatchTransport,
    SqliteBatchCapabilityStore,
    SqliteBatchJournal,
    SqliteBatchManifestStore,
    create_batch_manifest,
    create_batch_projection,
    execute_production_batch_once,
    issue_recurring_batch_capability,
)
from .aupay_card_contract import is_amazon_merchant
from .aupay_card_pipeline import AuPayCardPipeline, _amazon_extended_eligible
from .aupay_card_production import ProtectedAuditKeyProvider, SqliteLeaseManager
from .aupay_card_writer import (
    FixedSourceWindow,
    ReadOnlySheetsTargetInspector,
    TargetBinding,
    validate_target_binding,
)
from .aupay_mail_pipeline import AuPayCardMailPipeline
from .transaction_plan import reconcile_transactions


JST = ZoneInfo("Asia/Tokyo")
SOURCE_QUERY = 'in:anywhere from:kddi-fs.com subject:"【ご利用詳細】au PAY カード"'


class SqliteRecurringRunState:
    """Cached checkpoint plus append-only summaries of all attempted runs."""

    def __init__(self, path, *, repo_root):
        self.path = Path(path).expanduser().resolve()
        root = Path(repo_root).resolve()
        try:
            self.path.relative_to(root)
        except ValueError:
            pass
        else:
            raise RuntimeError("recurring_state_must_be_outside_repository")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS recurring_checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    successful_window_end TEXT NOT NULL,
                    run_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recurring_runs (
                    run_id TEXT PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS recurring_run_no_update
                BEFORE UPDATE ON recurring_runs BEGIN SELECT RAISE(ABORT,'append_only_run'); END;
                CREATE TRIGGER IF NOT EXISTS recurring_run_no_delete
                BEFORE DELETE ON recurring_runs BEGIN SELECT RAISE(ABORT,'append_only_run'); END;
            """)

    def successful_window_end(self) -> datetime | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT successful_window_end FROM recurring_checkpoint WHERE singleton=1"
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def record(self, summary: Mapping[str, object], *, advance_checkpoint: bool) -> None:
        payload = json.dumps(dict(summary), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO recurring_runs VALUES (?,?,?,?,?,?)",
                (summary["run_id"], datetime.now(JST).isoformat(), summary["status"],
                 summary["source_window_start"], summary["source_window_end"], payload),
            )
            if advance_checkpoint:
                connection.execute(
                    "INSERT INTO recurring_checkpoint VALUES (1,?,?) "
                    "ON CONFLICT(singleton) DO UPDATE SET "
                    "successful_window_end=excluded.successful_window_end,run_id=excluded.run_id",
                    (summary["source_window_end"], summary["run_id"]),
                )
            connection.commit()


def build_incremental_window(
    policy: RecurringAuthorityPolicy, state: SqliteRecurringRunState, now: datetime,
) -> FixedSourceWindow:
    policy.validate()
    end = now.astimezone(JST).replace(microsecond=0)
    checkpoint = state.successful_window_end()
    boundary = checkpoint.astimezone(JST) if checkpoint else policy.initial_start.astimezone(JST)
    start = boundary - timedelta(seconds=policy.overlap_seconds)
    if start >= end:
        raise RuntimeError("incremental_window_not_started")
    if (end - start).total_seconds() > policy.max_window_seconds:
        raise RuntimeError("incremental_window_exceeds_authority")
    query = f"{SOURCE_QUERY} after:{int(start.timestamp())} before:{int(end.timestamp())}"
    window = FixedSourceWindow(start, end, "Asia/Tokyo", query)
    window.validate()
    return window


def _batch_reader(db) -> Callable[[tuple[str, ...]], Mapping[str, tuple[tuple, ...]]]:
    def read(identities: tuple[str, ...]) -> Mapping[str, tuple[tuple, ...]]:
        wanted = set(identities)
        found: dict[str, list[tuple]] = {identity: [] for identity in identities}
        for raw in db.get("取込データ!A2:L"):
            row = tuple((list(raw) + [""] * 12)[:12])
            identity = str(row[0]).strip()
            if identity in wanted:
                found[identity].append(row)
        return {identity: tuple(rows) for identity, rows in found.items()}
    return read


def _classify_candidates(plan, db) -> tuple[dict[str, str], int]:
    statuses: dict[str, str] = {}
    review_count = 0
    classifier = AuPayCardPipeline(db)
    amazon_candidates = None
    for candidate in plan.candidates:
        amazon_status = None
        if is_amazon_merchant(candidate.merchant):
            if amazon_candidates is None:
                amazon_candidates = classifier._amazon_candidates()
            amazon_status, _, _, _ = classifier._classify_amazon_details(
                candidate.transaction_date, candidate.amount_yen, amazon_candidates,
                allow_extended=_amazon_extended_eligible(
                    candidate.merchant, candidate.payment_method, candidate.memo,
                ),
            )
        status = production_import_status(candidate, amazon_status=amazon_status)
        if status in {"amazon_needs_review", "amazon_unmatched"}:
            review_count += 1
        else:
            statuses[candidate.identity] = status
    return statuses, review_count


def _base_summary(run_id: str, window: FixedSourceWindow) -> dict[str, object]:
    return {
        "schema_version": 1, "run_id": run_id, "status": "started",
        "source_window_start": window.start.isoformat(),
        "source_window_end": window.end.isoformat(),
        "source_timezone": window.timezone_name,
        "source_query": window.query_representation,
        "found": 0, "fetched": 0, "new_eligible": 0,
        "already_present": 0, "withheld": 0, "review": 0,
        "written": 0, "duplicate": 0, "failure": 0,
        "manifest_final": "not_created", "capability_final": "not_issued",
        "journal_final": "not_started", "lease_final": "not_acquired",
        "write_requests": 0,
    }


def run_recurring_ingestion(
    *, gmail_service, db, state: SqliteRecurringRunState,
    key_provider: ProtectedAuditKeyProvider,
    authority_provider: ProtectedRecurringAuthorityProvider,
    state_dir, repo_root, now: datetime, dry_run: bool = False,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, object]:
    """Collect, plan, and optionally apply exactly one bounded fresh batch."""
    policy = authority_provider.load()
    if not (policy.valid_from <= now < policy.expires_at):
        raise RuntimeError("recurring_authority_expired_or_not_started")
    key = key_provider.load()
    if key is None:
        raise RuntimeError("persistent_audit_key_required")
    binding = TargetBinding(expected_spreadsheet_id=policy.expected_spreadsheet_id)
    inspector = ReadOnlySheetsTargetInspector(db)
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    window = build_incremental_window(policy, state, now)
    run_id = str(uuid4())
    summary = _base_summary(run_id, window)
    state_path = Path(state_dir)
    capability = None
    capability_store = None
    journal = None
    execution = None
    try:
        transactions, collection = AuPayCardMailPipeline._collect(
            gmail_service, window.query_representation, policy.max_messages,
            sleeper=sleeper, request_interval=0 if sleeper is not None else 0.25,
        )
        summary.update(found=collection["found"], fetched=collection["fetched"])
        if not collection["collection_complete"]:
            raise RuntimeError("incremental_gmail_collection_incomplete")
        existing = db.get("取込データ!A2:L")
        plan = build_canonical_apply_plan(collection, reconcile_transactions(transactions, existing))
        statuses, amazon_review = _classify_candidates(plan, db)
        summary.update(
            new_eligible=len(statuses),
            already_present=plan.duplicate_existing_identity_count,
            duplicate=plan.duplicate_existing_identity_count + collection["duplicate_gmail_message"],
            withheld=(plan.withheld_return_count + plan.withheld_ambiguous_count
                      + plan.invalid_item_count + plan.global_withheld_count),
            review=(plan.parser_review_line_item_count + plan.withheld_review_count + amazon_review),
        )
        if len(statuses) > policy.max_batch_size:
            raise RuntimeError("new_eligible_exceeds_bounded_batch")
        if not statuses:
            summary["status"] = "dry_run_noop" if dry_run else "noop"
            summary["journal_final"] = "not_needed"
            summary["lease_final"] = "not_needed"
            state.record(summary, advance_checkpoint=not dry_run)
            return summary
        if dry_run:
            summary["status"] = "dry_run_ready"
            summary["journal_final"] = "not_started"
            summary["lease_final"] = "not_acquired"
            state.record(summary, advance_checkpoint=False)
            return summary

        identities = tuple(statuses)
        projection = create_batch_projection(
            plan, candidate_identities=identities, statuses=statuses,
            binding=binding, audit_key=key,
        )
        manifest = create_batch_manifest(
            projection, source_window=window, created_at=now,
            run_id=run_id, audit_key=key, binding=binding,
        )
        manifest_store = SqliteBatchManifestStore(
            state_path / "manifests.sqlite3", repo_root=repo_root,
        )
        manifest_store.save(manifest)
        summary["manifest_final"] = "persisted"
        capability_store = SqliteBatchCapabilityStore(
            state_path / "capabilities.sqlite3", repo_root=repo_root,
        )
        journal = SqliteBatchJournal(state_path / "journal.sqlite3", repo_root=repo_root)
        leases = SqliteLeaseManager(
            state_path / "leases.sqlite3", repo_root=repo_root, clock=lambda: now,
        )
        capability = issue_recurring_batch_capability(
            projection, manifest, binding=binding, inspector=inspector,
            key_provider=key_provider, authority_provider=authority_provider,
            store=capability_store, now=now,
        )
        summary["capability_final"] = "issued"
        transport = SealedSheetsBatchTransport(
            db, projection=projection, manifest=manifest, capability=capability,
            store=capability_store, journal=journal, binding=binding,
            inspector=inspector, key_provider=key_provider,
            clock=lambda: now,
        )
        execution = execute_production_batch_once(
            projection, manifest, capability=capability, store=capability_store,
            journal=journal, leases=leases, reader=_batch_reader(db), transport=transport,
            binding=binding, inspector=inspector, key_provider=key_provider,
            owner_id=f"recurring-{run_id}", clock=lambda: now,
            sleeper=sleeper or __import__("time").sleep,
        )
        summary.update(
            status="complete" if execution.exact else "failed",
            written=execution.confirmed_count,
            write_requests=execution.write_request_count,
            failure=0 if execution.exact else 1,
            capability_final=capability_store.state(capability.capability_id),
            journal_final="confirmed" if execution.exact else "outcome_unknown",
            lease_final="released",
        )
        state.record(summary, advance_checkpoint=execution.exact)
        return summary
    except Exception as exc:
        summary.update(status="failed", failure=1)
        summary["failure_reason"] = str(exc)
        if capability is not None and capability_store is not None:
            summary["capability_final"] = capability_store.state(capability.capability_id)
        if execution is not None:
            summary["write_requests"] = execution.write_request_count
        summary["lease_final"] = "released_or_expired"
        if journal is not None:
            summary["journal_final"] = "failed_closed"
        state.record(summary, advance_checkpoint=False)
        return summary
