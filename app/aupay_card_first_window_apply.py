"""Shared owner-dispatched Apply safety path using the existing writers.

No standalone audit, Preview, expense writer, replay, or rollback entry exists.
Only aggregate results leave main(); all original evidence stays private.
"""
from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
import logging
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from .aupay_card_window_preview import (
    CHECKPOINT, END, FLAGS, REPO, ROOT, SOURCE, START, GitHub, ReadOnlyState,
    Stop, checkpoint, digest, observe, require,
)
from .aupay_card_window_evidence import CapturedGmail, GetOnly, ObservedStore
from .aupay_card_recurring import (
    SqliteRecurringRunState, _StrictPreviewGmail, _batch_reader,
    _classify_candidates, build_explicit_preview_window,
)
from .aupay_card_apply_plan import build_canonical_apply_plan, validate_canonical_apply_plan
from .aupay_card_batch import (
    ProtectedRecurringAuthorityProvider, SealedSheetsBatchTransport,
    SqliteBatchCapabilityStore, SqliteBatchJournal, SqliteBatchManifestStore,
    create_batch_manifest, create_batch_projection, execute_production_batch_once,
    issue_recurring_batch_capability,
)
from .aupay_card_production import ProtectedAuditKeyProvider, SqliteLeaseManager
from .aupay_card_writer import ReadOnlySheetsTargetInspector, TargetBinding, full_plan_binding_reference_v2
from .amazon_money_runtime import _catchup_batch, _configured_writer, _require_closed_money
from .amazon_money_review import MoneyReviews, record_from_entry
from .amazon_money_writer import MoneyLedger
from .aupay_card_preview_totals import build_totals, metric, money_totals, public_totals
from .aupay_mail_pipeline import AuPayCardMailPipeline
from .transaction_plan import reconcile_transactions, normalize_card_transaction
from .aupay_card_executor import ExistingCanonicalRecord, SheetsCanonicalIdentityReader

from .aupay_card_first_window_guard import WORKFLOW, CONFIRM, controls

JST = ZoneInfo("Asia/Tokyo")


class ReviewLedger(MoneyLedger):
    def append(self, *_args, **_kwargs):
        raise Stop("review_financial_write_forbidden")


def exact_review(book, decision):
    record = decision.record
    entry = book["records"].get(record.money_id)
    return bool(entry and entry.get("state") == "review"
        and entry.get("fingerprint") == record.fingerprint and entry.get("reason") == decision.reason
        and record_from_entry(entry) == record and record.original_url
        and not entry.get("expense_ids") and not entry.get("import_row") and not entry.get("expense_rows")
        and entry.get("related_id", "") == decision.related_id)


def check_reviews(writer, decisions, *, all_saved=False, fixed=True):
    book = writer._book()
    _require_closed_money(book)
    if fixed:
        require(len(decisions) == 4 and sum(d.record.amount for d in decisions) == 4334,
                "held_baseline_changed")
    require(writer.preview([d.record for d in decisions]) == decisions, "review_classification_changed")
    for decision in decisions:
        record = decision.record
        require(decision.action == "review" and record.source == "au_pay_card" and record.original_url
                and not decision.expense_ids, "review_only_required")
        entry = book["records"].get(record.money_id)
        require((entry is None and not all_saved) or exact_review(book, decision), "saved_review_mismatch")
    inbox = MoneyReviews(writer).read()
    require(all(isinstance(r, dict) and r.get("state") in {"applied", "failed"}
                for r in inbox["requests"].values()),
            "review_request_pending")
    return book


def prepare(*, gmail, db, writer, policy, state, now, key, sleeper=None, expected_checkpoint=CHECKPOINT,
            incremental=False):
    """One complete Gmail read; no persistence or writer capability issued."""
    cp = state.successful_window_end()
    require(cp is not None and cp.isoformat() == expected_checkpoint, "checkpoint_changed")
    require(policy.source == SOURCE and policy.expected_spreadsheet_id == db.sid
            and policy.expected_worksheet == "取込データ", "authority_target_mismatch")
    require((policy.overlap_seconds, policy.max_window_seconds, policy.max_messages, policy.max_batch_size)
            == (172800, 1209600, 500, 100), "authority_policy_changed")
    window = build_explicit_preview_window(policy, state, now,
        cp - timedelta(seconds=policy.overlap_seconds) if incremental else datetime.fromisoformat(START),
        now.replace(microsecond=0) if incremental else datetime.fromisoformat(END))
    _require_closed_money(writer._book())
    capture = CapturedGmail(gmail, window=window)
    strict = _StrictPreviewGmail(capture, policy.max_messages)
    records, notices = [], []
    transactions, collection = AuPayCardMailPipeline._collect(strict, window.query_representation,
        policy.max_messages, money_records=records, money_notices=notices,
        sleeper=sleeper, request_interval=0 if sleeper else 0.25)
    require(collection["collection_complete"] and strict.listed == strict.fetched
            and collection["found"] == collection["fetched"] == len(capture.inventory) == len(strict.listed),
            "gmail_incomplete")
    require(not notices and not collection["parser_review_mail_count"] and not collection["review_line_items"],
            "unresolved_candidates")
    existing = deepcopy(db.get("取込データ!A2:L"))
    # Reconciliation counts source IDs as duplicates. Verify their contents too;
    # a corrupted row with the same ID must not silently count as a duplicate.
    normalized = {tx.identity: tx for tx in map(normalize_card_transaction, transactions)}
    identities = SheetsCanonicalIdentityReader(db).read_identities(tuple(normalized))
    require(set(identities) == set(normalized) and all(v.readable for v in identities.values()),
            "existing_identity_unreadable")
    require((incremental or sum(bool(v.records) for v in identities.values()) == 6) and all(
        not v.records or v.records == (ExistingCanonicalRecord.from_candidate(normalized[k]),)
        for k, v in identities.items()), "existing_identity_content_mismatch")
    reconciliation = reconcile_transactions(transactions, existing)
    plan = validate_canonical_apply_plan(build_canonical_apply_plan(collection, reconciliation))
    statuses, amazon_review = _classify_candidates(plan, db)
    require(not amazon_review and set(statuses.values()).issubset(policy.allowed_statuses),
            "classification_not_authorized")
    input_count = len(records)
    require(len(statuses) <= policy.max_batch_size, "candidate_limit_exceeded")
    records, _ = _catchup_batch(writer, records, policy.max_batch_size - len(statuses))
    decisions = writer.preview(records)
    check_reviews(writer, decisions, fixed=not incremental)
    require(len(statuses) + len(decisions) <= policy.max_batch_size, "candidate_limit_exceeded")
    totals = public_totals(build_totals(plan, reconciliation, statuses, collection,
        money=money_totals(decisions), money_input_count=input_count, money_notice_count=0))
    if incremental:
        require(totals["ordinary"]["held"] == metric()
                and all(totals["by_classification"][g] == metric()
                        for g in ("excluded", "linked", "transfer", "supplement"))
                and totals["planned_financial_writes"]["expense_rows"] == metric()
                and all(v["count"] == 0 for k, v in totals["unquantified"].items()
                        if k not in {"noncanonical_resends", "money_repeated_inputs"}),
                "unsupported_candidate")
    else:
        fixed_totals(totals, capture)
    origins = {}
    for message in capture.inventory:
        for identity in message["source_identities"]:
            origins.setdefault(identity, []).append(message["gmail_id"])
    require(all(identity in origins for d in plan.item_decisions for identity in d.source_identities)
            and all(d.record.source_id in origins and d.record.original_url in {
                "https://mail.google.com/mail/u/0/#all/" + g for g in origins[d.record.source_id]}
                for d in decisions), "source_evidence_mismatch")
    projection = (create_batch_projection(plan, candidate_identities=tuple(statuses), statuses=statuses,
        binding=TargetBinding(expected_spreadsheet_id=db.sid), audit_key=key) if statuses else None)
    require(incremental or (projection.batch_size == 33
            and sum(c.amount_yen for c in projection.plan.candidates) == 149218), "import_limit_changed")
    return dict(window=window, transactions=transactions, collection=collection, plan=plan,
        statuses=statuses, projection=projection, decisions=decisions, existing=existing,
        evidence=capture.inventory, totals=totals, money_book=deepcopy(writer._book()),
        incremental=incremental, checkpoint=expected_checkpoint,
        full_plan_ref=full_plan_binding_reference_v2(plan, key))


def fixed_totals(totals, capture):
    expected = {"new": (33, 149218), "duplicate": (6, 110493), "held": (4, 4334)}
    require(totals["candidates"] == metric(43, 264045) and all(
        value == metric(*expected.get(group, (0, 0)))
        for group, value in totals["by_classification"].items()), "preview_aggregates_changed")
    require(totals["ordinary"]["new"] == metric(33, 149218)
            and totals["ordinary"]["held"] == metric()
            and totals["money"]["held"] == metric(4, 4334)
            and totals["planned_financial_writes"]["expense_rows"] == metric(), "unsupported_candidate")
    require(all(v["count"] == (39 if k == "noncanonical_resends" else 0)
                for k, v in totals["unquantified"].items())
            and len(capture.inventory) == 22, "collection_baseline_changed")


def recheck(prepared, db, writer, key):
    require(db.get("取込データ!A2:L") == prepared["existing"], "existing_rows_changed")
    reconciliation = reconcile_transactions(prepared["transactions"], db.get("取込データ!A2:L"))
    plan = validate_canonical_apply_plan(build_canonical_apply_plan(prepared["collection"], reconciliation))
    statuses, reviews = _classify_candidates(plan, db)
    projection = (create_batch_projection(plan, candidate_identities=tuple(statuses), statuses=statuses,
        binding=TargetBinding(expected_spreadsheet_id=db.sid), audit_key=key) if statuses else None)
    require(not reviews and projection == prepared["projection"]
            and full_plan_binding_reference_v2(plan, key) == prepared["full_plan_ref"],
            "fresh_identity_or_classification_changed")
    check_reviews(writer, prepared["decisions"], fixed=not prepared["incremental"])


class ImportOnlySheets:
    """The existing batch transport gets one append to the exact import range."""
    def __init__(self, service, sid, predispatch, *, projection=None):
        self.service, self.sid, self.predispatch = service, sid, predispatch
        self.calls, self.rows = 0, ()
        self.projection = projection
    def spreadsheets(self): return self
    def values(self): return self
    def get(self, **kwargs):
        api = self.service.spreadsheets()
        return (api.values() if "range" in kwargs else api).get(**kwargs)
    def append(self, **kwargs):
        rows = kwargs.get("body", {}).get("values", [])
        size = self.projection.batch_size if self.projection is not None else 33
        amount = sum(c.amount_yen for c in self.projection.plan.candidates) if self.projection is not None else 149218
        require(self.calls == 0 and kwargs.get("spreadsheetId") == self.sid
            and kwargs.get("range") == "取込データ!A:L" and kwargs.get("valueInputOption") == "RAW"
            and kwargs.get("insertDataOption") == "INSERT_ROWS" and 1 <= len(rows) == size <= 100
            and all(len(r) == 12 for r in rows) and sum(r[6] for r in rows) == amount,
            "financial_scope_forbidden")
        self.predispatch()
        self.calls += 1
        self.rows = deepcopy(rows)
        return self.service.spreadsheets().values().append(**kwargs)


def financial_data(service, sid):
    """Whole-table values AND formulas, including sparse tails and extra columns."""
    from .aupay_card_window_preview import FINANCIAL_SHEETS
    api = service.spreadsheets()
    meta = api.get(spreadsheetId=sid, fields="spreadsheetId,sheets(properties)").execute(num_retries=0)
    require(meta.get("spreadsheetId") == sid, "financial_target_mismatch")
    titles = [s["properties"]["title"] for s in meta["sheets"] if s["properties"]["title"] in FINANCIAL_SHEETS]
    require(set(FINANCIAL_SHEETS[:3]).issubset(titles) and len(titles) == len(set(titles)), "financial_sheet_missing")
    result = {}
    for render in ("UNFORMATTED_VALUE", "FORMULA"):
        ranges = ["'" + t + "'" for t in titles]
        value = api.values().batchGet(spreadsheetId=sid, ranges=ranges,
            valueRenderOption=render, dateTimeRenderOption="SERIAL_NUMBER").execute(num_retries=0)
        require(value.get("spreadsheetId") == sid and len(value.get("valueRanges", [])) == len(titles),
                "financial_collection_incomplete")
        for title, item in zip(titles, value["valueRanges"]):
            require(any(item.get("range", "").startswith(p + "!") for p in (title, "'" + title + "'")),
                    "financial_range_mismatch")
            rows = deepcopy(item.get("values", []))
            # Ignore API-omitted trailing empty cells only, never interior rows.
            for row in rows:
                while row and row[-1] == "": row.pop()
            while rows and not rows[-1]: rows.pop()
            result[(title, render)] = rows
    require(sum(len(json.dumps(v)) for v in result.values()) <= 64 * 1024 * 1024, "financial_snapshot_too_large")
    return result


def verify_financial(before, after, rows):
    expected = deepcopy(before)
    for render in ("UNFORMATTED_VALUE", "FORMULA"):
        target = expected[("取込データ", render)]
        for row in deepcopy(rows):
            while row and row[-1] == "": row.pop()
            target.append(row)
    require(after == expected, "financial_exact_delta_failed")


class ReviewStore:
    """Restrict the existing MoneyWriter to new review records in its book."""
    def __init__(self, backing, decisions, guard, expected):
        self.backing, self.decisions, self.guard = backing, decisions, guard
        self.expected = deepcopy(expected)
    def read(self, key): return self.backing.read(key)
    def write(self, key, value):
        self.guard()
        require(key == "money", "review_document_forbidden")
        before = self.backing.read(key)
        require(before == self.expected, "review_book_changed")
        require({k: v for k, v in before.items() if k != "records"}
                == {k: v for k, v in value.items() if k != "records"}, "review_book_changed")
        require(all(value["records"].get(k) == v for k, v in before["records"].items()),
                "saved_review_changed")
        added = set(value["records"]) - set(before["records"])
        by_id = {d.record.money_id: d for d in self.decisions}
        require(len(added) == 1 and added.issubset(by_id)
                and all(exact_review(value, by_id[k]) for k in added), "review_insert_invalid")
        self.backing.write(key, value)
        self.expected = deepcopy(value)


def execute_fixed(*, prepared, db, writer, readback_writer, policy, key_provider,
                  authority_provider, state, state_dir, repo_root, durable, ledger,
                  guard, financial_read, documents_check, final_read, clock=None):
    """All writes in order; any exception leaves pending state, without retry."""
    clock = clock or (lambda: datetime.now(JST))
    key = key_provider.load()
    guard()
    recheck(prepared, db, writer, key)
    require(writer._book() == prepared["money_book"], "money_book_changed_before_write")
    documents_check(writer._book())
    before = financial_read()
    binding = TargetBinding(expected_spreadsheet_id=db.sid)
    projection = prepared["projection"]
    fixed = not prepared["incremental"]
    count = projection.batch_size if projection else 0
    held = len(prepared["decisions"])
    require(state.successful_window_end().isoformat() == prepared["checkpoint"], "checkpoint_changed")
    run_id = str(uuid4())
    manifest = (create_batch_manifest(projection, source_window=prepared["window"], created_at=clock(),
        run_id=run_id, audit_key=key, binding=binding) if count else None)
    # Durable parent/native pending markers are confirmed before any review or
    # financial write. Failure anywhere from here requires manual reconciliation.
    ledger.begin("aupay_card", uuid4().hex)
    durable.begin(uuid4().hex)

    def pending_guard():
        guard()
        require(durable.transport.read() == durable._pending
                and ledger.transport.read() == ledger.payload, "pending_state_changed")

    writer.store = ReviewStore(writer.store, prepared["decisions"], pending_guard, prepared["money_book"])
    check_reviews(writer, prepared["decisions"], fixed=fixed)
    stats = writer.apply([d.record for d in prepared["decisions"]], limit=max(1, held), review_only=True)
    require(stats["money_review"] == held and all(v == 0 for k, v in stats.items() if k != "money_review"),
            "review_saved_financial_activity")
    saved_book = check_reviews(readback_writer, prepared["decisions"], all_saved=True, fixed=fixed)
    require(saved_book == writer.store.expected, "review_book_changed")
    documents_check(saved_book)
    verify_financial(before, financial_read(), [])

    def predispatch():
        pending_guard()
        recheck(prepared, db, readback_writer, key)
        check_reviews(readback_writer, prepared["decisions"], all_saved=True, fixed=fixed)
        require(readback_writer._book() == saved_book, "saved_review_changed")
        documents_check(saved_book)
        verify_financial(before, financial_read(), [])

    predispatch()
    path = Path(state_dir)
    rows = ()
    if count:
        manifests = SqliteBatchManifestStore(path / "manifests.sqlite3", repo_root=repo_root)
        capabilities = SqliteBatchCapabilityStore(path / "capabilities.sqlite3", repo_root=repo_root)
        journal = SqliteBatchJournal(path / "journal.sqlite3", repo_root=repo_root)
        leases = SqliteLeaseManager(path / "leases.sqlite3", repo_root=repo_root, clock=clock)
        manifests.save(manifest)
        inspector = ReadOnlySheetsTargetInspector(db)
        capability = issue_recurring_batch_capability(projection, manifest, binding=binding,
            inspector=inspector, key_provider=key_provider, authority_provider=authority_provider,
            store=capabilities, now=clock())
        # Wrap the service, retaining the exact existing transport/writer types.
        original_service = db.svc
        bounded = ImportOnlySheets(original_service, db.sid, predispatch, projection=projection)
        db.svc = bounded
        try:
            transport = SealedSheetsBatchTransport(db, projection=projection, manifest=manifest,
                capability=capability, store=capabilities, journal=journal, binding=binding,
                inspector=inspector, key_provider=key_provider, clock=clock)
            execution = execute_production_batch_once(projection, manifest, capability=capability,
                store=capabilities, journal=journal, leases=leases, reader=_batch_reader(db),
                transport=transport, binding=binding, inspector=inspector, key_provider=key_provider,
                owner_id="first-window-" + run_id, readback_attempts=1, readback_delays=(), clock=clock)
        finally:
            db.svc = original_service
        require(execution.exact and execution.confirmed_count == count and execution.write_request_count == 1
                and bounded.calls == 1, "write_unknown_stop_no_retry")
        # Unlike the general batch writer, this one-use entry rejects even an
        # ambiguous transport response followed by an exact read. No checkpoint.
        history = journal.history(run_id, execution.attempt_id)
        require(any(h[0:2] == ("write_result", "acknowledged") for h in history)
                and history[-1][0:2] == ("final", "confirmed"), "write_unknown_stop_no_retry")
        require(manifests.load(run_id) == manifest and capabilities.state(capability.capability_id) == "sealed",
                "native_manifest_capability_unconfirmed")
        with closing(sqlite3.connect(leases._path)) as connection:
            require(connection.execute("SELECT COUNT(*) FROM writer_leases").fetchone()[0] == 0,
                    "native_lease_unreleased")
        rows = bounded.rows
    pending_guard()
    check_reviews(readback_writer, prepared["decisions"], all_saved=True, fixed=fixed)
    documents_check(saved_book)
    verify_financial(before, financial_read(), rows)
    end = prepared["window"].end.isoformat()
    duplicate = prepared["totals"]["ordinary"]["duplicate"]["count"]
    result = dict(status="complete" if count else "noop",
        found=prepared["collection"]["found"], fetched=prepared["collection"]["fetched"],
        new_eligible=count, written=count, duplicate=duplicate, money_review=held, failure=0,
        write_requests=int(bool(count)), manifest_final="persisted" if count else "not_created",
        capability_final="sealed" if count else "not_issued", journal_final="confirmed" if count else "not_needed",
        lease_final="released" if count else "not_needed",
        source_window_start=prepared["window"].start.isoformat(), source_window_end=end, run_id=run_id)
    # Checkpoint is last in the native state, after all accounting proofs.
    state.record(result, advance_checkpoint=True)
    durable.commit(path)
    final_read(card_only=True)
    ledger.complete("aupay_card", result, 0)
    final_read(card_only=False)
    verify_financial(before, financial_read(), rows)
    require(readback_writer._book() == saved_book, "saved_review_changed")
    return dict(written=count, amount_yen=sum(c.amount_yen for c in projection.plan.candidates) if count else 0,
        held_protected=held, duplicate=duplicate, financial_write_requests=int(bool(count)),
        expense_rows_written=0, checkpoint=end, parent_ledger="complete")


def run(env, head, github, *, incremental=False):
    started_at = datetime.now(JST).replace(microsecond=0)
    scheduled = incremental and env.get("GITHUB_EVENT_NAME") == "schedule"
    boundary = controls
    if incremental:
        from .aupay_card_incremental_guard import controls as boundary
    recovered_failure = boundary(env, head, github)
    expected_cp = (None if scheduled else env["EXPECTED_CHECKPOINT"]) if incremental else CHECKPOINT
    require(env.get("KAKEIBO_AMAZON_MONEY_MODE") == "confirmed-v1", "money_mode_required")
    from .amazon_gmail_preview import gmail_readonly_service
    from .drive_run_state import DurableState, StateBinding, DriveStateTransport, validate
    from .google_clients import drive_service, sheets_service, read_only_drive_service, read_only_sheets_service
    from .private_state_bindings import unwrap
    from .production_flow import _secret_file
    from .production_ledger import ProductionLedger, validate_ledger
    from .projection_store import store_from_environment
    from .sheets import SheetsDB

    info = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
    names = ("KAKEIBO_STATE_FOLDER_ID", "AUPAY_CARD_STATE_FILE_ID", "KAKEIBO_RUN_LEDGER_FILE_ID")
    decoded = {n: unwrap(n, env[n], info["private_key"]) for n in names}
    sid, folder = env["SPREADSHEET_ID"], decoded["KAKEIBO_STATE_FOLDER_ID"]
    deadline = time.monotonic() + 900
    ro_drive = GetOnly(read_only_drive_service(), deadline)
    ro_sheets = GetOnly(read_only_sheets_service(), deadline)
    card_binding = StateBinding(SOURCE, sid, folder, decoded["AUPAY_CARD_STATE_FILE_ID"])
    parent_binding = StateBinding("production_run", sid, folder, decoded["KAKEIBO_RUN_LEDGER_FILE_ID"])
    card_ro, parent_ro = ReadOnlyState(ro_drive, card_binding), ReadOnlyState(ro_drive, parent_binding)
    amazon_ro = None
    if env.get("AMAZON_STATE_FILE_ID"):
        amazon_id = unwrap("AMAZON_STATE_FILE_ID", env["AMAZON_STATE_FILE_ID"], info["private_key"])
        amazon_ro = ReadOnlyState(ro_drive, StateBinding("amazon_gmail", sid, folder, amazon_id))
    if incremental:
        # Complete protected-state validation before even assembling the money
        # writer/reader or starting Gmail. The scheduled checkpoint stays local.
        from .aupay_card_incremental_recovery import successful_checkpoint, verify_reconciled_state
        native_raw, parent_raw = card_ro.read(), parent_ro.read()
        native_before = validate(native_raw, card_binding)
        initial_parent = validate_ledger(parent_raw, parent_binding)
        if scheduled:
            expected_cp = successful_checkpoint(native_before)
            recovered_failure = boundary(env, head, github, expected_checkpoint=expected_cp)
        require(all(v["phase"] == "ready" and not v["attempt"] and not v["error"]
                    for v in initial_parent["sources"].values()), "parent_pending")
        if recovered_failure:
            verify_reconciled_state(native_before, initial_parent, expected_cp)
    backing = store_from_environment(sid, env)
    readback_store = store_from_environment(sid, env, read_only=True)
    require(backing is not None and readback_store is not None, "money_binding_required")
    observed = ObservedStore(readback_store)
    db = SheetsDB(sid, service=ro_sheets, read_retry_attempts=1)
    reader = _configured_writer(db, observed, env, ledger=ReviewLedger(db))
    before = observe(card_ro, parent_ro, ro_sheets, sid, observed, expected_checkpoint=expected_cp, amazon=amazon_ro)
    if incremental:
        from .aupay_card_incremental_recovery import NO_WRITE_CHECKPOINT, verify_no_write_state
        if datetime.fromisoformat(expected_cp) >= datetime.fromisoformat(NO_WRITE_CHECKPOINT):
            verify_no_write_state(native_before, initial_parent, expected_cp,
                financial=lambda: financial_data(ro_sheets, sid), money=readback_store)
    parent_before = validate_ledger(parent_ro.read(), parent_binding)
    if incremental:
        require(card_ro.read() == native_raw and parent_ro.read() == parent_raw, "preparation_native_changed")
    with tempfile.TemporaryDirectory(prefix="card-first-apply-", dir=env["RUNNER_TEMP"]) as temp:
        private = Path(temp)
        authority = ProtectedRecurringAuthorityProvider(_secret_file(private, "authority.json",
            env["AUPAY_CARD_RECURRING_AUTHORITY_JSON"]), repo_root=ROOT)
        key = ProtectedAuditKeyProvider(_secret_file(private, "key.json", env["AUPAY_CARD_AUDIT_KEY_JSON"]), repo_root=ROOT)
        policy, audit_key = authority.load(), key.load()
        require(audit_key is not None, "audit_key_required")
        drive = drive_service()
        durable = DurableState(DriveStateTransport(drive, card_binding), card_binding)
        ledger = ProductionLedger(DriveStateTransport(drive, parent_binding), parent_binding)
        path = private / "state"
        durable.restore(path)
        state = SqliteRecurringRunState(path / "recurring.sqlite3", repo_root=ROOT)

        def guard():
            if scheduled:
                boundary(env, head, github, expected_checkpoint=expected_cp)
            else:
                boundary(env, head, github)
            now = datetime.now(JST)
            require(authority.load() == policy and policy.valid_from <= now < policy.expires_at,
                    "authority_changed_or_expired")
            require(key.load() == audit_key, "audit_key_changed")
            require(time.monotonic() < deadline, "apply_timeout")

        guard()
        prepared = prepare(gmail=GetOnly(gmail_readonly_service(env["GOOGLE_GMAIL_TOKEN_JSON"]), deadline),
            db=db, writer=reader, policy=policy, state=state, now=started_at, key=audit_key,
            expected_checkpoint=expected_cp, incremental=incremental)
        require(observe(card_ro, parent_ro, ro_sheets, sid, observed, expected_checkpoint=expected_cp, amazon=amazon_ro) == before, "prewrite_state_changed")
        observed.verify()
        writer = _configured_writer(db, backing, env, ledger=ReviewLedger(db))
        initial_book = deepcopy(observed.observed["money"])

        def documents_check(expected_book):
            require(all(readback_store.read(k) == (expected_book if k == "money" else v)
                        for k, v in observed.observed.items()), "review_projection_changed")
            require(all(expected_book["records"].get(k) == v for k, v in initial_book["records"].items()),
                    "existing_money_record_changed")

        def final_read(*, card_only):
            remote = validate(card_ro.read(), card_binding)
            require(checkpoint(remote, prepared["window"].end.isoformat()) == prepared["window"].end.isoformat(), "final_checkpoint_failed")
            require(durable._loaded == card_ro.read(), "final_native_state_changed")
            parent = validate_ledger(parent_ro.read(), parent_binding)
            require(all(parent["sources"][k] == v for k, v in parent_before["sources"].items()
                        if k != "aupay_card"), "other_source_changed")
            if not card_only:
                require(parent_ro.read() == ledger.payload
                        and parent["sources"]["aupay_card"]["phase"] == "ready"
                        and not parent["sources"]["aupay_card"]["attempt"]
                        and parent["sources"]["aupay_card"]["counts"].get("written") == len(prepared["statuses"]),
                        "final_parent_ledger_failed")

        db.svc = sheets_service()
        # Independent reader retains its own read-only Sheets client.
        ro_db = SheetsDB(sid, service=ro_sheets, read_retry_attempts=1)
        independent = _configured_writer(ro_db, readback_store, env, ledger=ReviewLedger(ro_db))
        return execute_fixed(prepared=prepared, db=db, writer=writer, readback_writer=independent,
            policy=policy, key_provider=key, authority_provider=authority, state=state,
            state_dir=path, repo_root=ROOT, durable=durable, ledger=ledger, guard=guard,
            financial_read=lambda: financial_data(ro_sheets, sid), documents_check=documents_check,
            final_read=final_read)


def main(*, incremental=False):
    logging.disable(logging.CRITICAL)
    socket.setdefaulttimeout(30)
    def timeout(*_): raise Stop("apply_timeout")
    signal.signal(signal.SIGTERM, timeout)
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(900)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            env = dict(os.environ)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            github = GitHub(env)
            require(not sys.argv[1:], "arguments_forbidden")
            result = dict(success=True, **(run(env, head, github, incremental=True) if incremental else run(env, head, github)))
    except BaseException:
        result = dict(success=False, error="incremental_apply_stopped_no_retry" if incremental else "first_window_apply_stopped_no_retry",
            automatic_retry=False, rollback=False)
    finally:
        if hasattr(signal, "SIGALRM"): signal.alarm(0)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
