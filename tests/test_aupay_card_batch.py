from datetime import datetime, timedelta, timezone
import base64
import json
import re
from uuid import uuid4

import pytest

from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.aupay_card_batch import (
    ProtectedBatchApprovalProvider,
    SealedSheetsBatchTransport,
    SqliteBatchCapabilityStore,
    SqliteBatchJournal,
    SqliteBatchManifestStore,
    create_batch_manifest,
    create_batch_projection,
    execute_production_batch_once,
    issue_batch_capability,
)
from app.aupay_card_production import ProtectedAuditKeyProvider, SqliteLeaseManager
from app.aupay_card_writer import (
    FixedSourceWindow,
    PersistentAuditKey,
    ReadOnlySheetsTargetInspector,
    TargetBinding,
)
from app.sheets import HEADERS
from app.transaction_plan import reconcile_transactions


NOW = datetime(2026, 9, 10, 1, 2, 3, tzinfo=timezone.utc)
KEY = PersistentAuditKey("batch-key-v1", b"b" * 32)


def mail_id(index):
    return f"aupaycard-mail:{chr(ord('a') + index) * 24}:{index + 1:03d}"


def make_plan(count=5):
    rows = [{
        "import_id": mail_id(index), "date": f"2026-08-{index + 1:02d}",
        "merchant": f"店舗{index}", "amount": 1000 + index,
        "transaction_kind": "purchase", "payment_type": "メール通知",
        "member": "本会員", "memo": f"メール明細No.{index + 1:03d}",
    } for index in range(count)]
    return build_canonical_apply_plan({
        "collection_complete": True, "listing_complete": True,
        "collection_truncated": False, "gmail_list_failed": 0,
        "gmail_read_failed": 0, "needs_review": 0,
    }, reconcile_transactions(rows, []))


def window():
    return FixedSourceWindow(
        datetime(2025, 9, 10, tzinfo=timezone.utc),
        datetime(2026, 9, 10, tzinfo=timezone.utc),
        "UTC", "after:2025/09/10 before:2026/09/10",
    )


class Values:
    def __init__(self, db):
        self.db = db
        self.body = None

    def append(self, **kwargs):
        self.body = kwargs["body"]
        return self

    def execute(self):
        self.db.calls += 1
        for row in self.body["values"]:
            self.db.rows.append(list(row))
        return {"updates": {"updatedRows": len(self.body["values"])}}


class FailingValues(Values):
    def execute(self):
        self.db.calls += 1
        raise TimeoutError("unknown transport outcome")


class Spreadsheets:
    def __init__(self, db):
        self.db = db
        self.api = Values(db)

    def values(self):
        return self.api


class Service:
    def __init__(self, db):
        self.api = Spreadsheets(db)

    def spreadsheets(self):
        return self.api


class DB:
    def __init__(self, sid="sheet-id"):
        self.sid = sid
        self.rows = []
        self.calls = 0
        self.svc = Service(self)

    def get(self, rng):
        if rng == "取込データ!A1:L1":
            return [HEADERS["取込データ"]]
        if rng == "取込データ!A2:L":
            return self.rows
        raise AssertionError(rng)


def reader(db):
    def read(identities):
        result = {identity: [] for identity in identities}
        for raw in db.rows:
            row = tuple((list(raw) + [""] * 12)[:12])
            if row[0] in result:
                result[row[0]].append(row)
        return {key: tuple(value) for key, value in result.items()}
    return read


def components(tmp_path, count=5):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    key_path = state / "key.json"
    key_path.write_text(json.dumps({
        "key_id": KEY.key_id,
        "key_b64": base64.b64encode(KEY.secret).decode("ascii"),
    }), encoding="utf-8")
    key_provider = ProtectedAuditKeyProvider(key_path, repo_root=repo)
    full = make_plan(count)
    identities = tuple(candidate.identity for candidate in full.candidates)
    statuses = {identity: "auto_expense" for identity in identities}
    binding = TargetBinding(expected_spreadsheet_id="sheet-id")
    projection = create_batch_projection(
        full, candidate_identities=identities, statuses=statuses,
        binding=binding, audit_key=KEY,
    )
    manifest = create_batch_manifest(
        projection, source_window=window(), created_at=NOW,
        run_id=str(uuid4()), audit_key=KEY, binding=binding,
    )
    manifest_store = SqliteBatchManifestStore(state / "manifest.sqlite3", repo_root=repo)
    manifest_store.save(manifest)
    approval_path = state / "approval.json"
    approval_path.write_text(json.dumps({
        "approval_reference": "test-approved-batch",
        "batch_ref": projection.batch_ref, "target_ref": projection.target_ref,
        "batch_size": count, "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
    }), encoding="utf-8")
    approval = ProtectedBatchApprovalProvider(approval_path, repo_root=repo)
    store = SqliteBatchCapabilityStore(state / "capability.sqlite3", repo_root=repo)
    journal = SqliteBatchJournal(state / "journal.sqlite3", repo_root=repo)
    leases = SqliteLeaseManager(state / "leases.sqlite3", repo_root=repo, clock=lambda: NOW)
    db = DB()
    inspector = ReadOnlySheetsTargetInspector(db)
    capability = issue_batch_capability(
        projection, manifest, binding=binding, inspector=inspector,
        key_provider=key_provider, approval_provider=approval, store=store, now=NOW,
    )
    transport = SealedSheetsBatchTransport(
        db, projection=projection, manifest=manifest, capability=capability,
        store=store, journal=journal, binding=binding, inspector=inspector,
        key_provider=key_provider, clock=lambda: NOW,
    )
    return (repo, projection, manifest, manifest_store, key_provider, store,
            journal, leases, db, inspector, binding, capability, transport)


def execute(parts):
    (_, projection, manifest, _, key_provider, store, journal, leases,
     db, inspector, binding, capability, transport) = parts
    return execute_production_batch_once(
        projection, manifest, capability=capability, store=store,
        journal=journal, leases=leases, reader=reader(db), transport=transport,
        binding=binding, inspector=inspector, key_provider=key_provider,
        owner_id="batch-test", clock=lambda: NOW, sleeper=lambda _delay: None,
    )


def test_batch_projection_and_manifest_bind_all_candidates_and_exact_target(tmp_path):
    parts = components(tmp_path)
    projection, manifest, manifest_store = parts[1:4]
    assert projection.batch_size == 5
    assert len(set(projection.candidate_refs)) == 5
    assert re.fullmatch(r"writer-target-v1:[0-9a-f]{32}", projection.target_ref)
    assert manifest.candidate_refs == projection.candidate_refs
    assert manifest.statuses == ("auto_expense",) * 5
    assert manifest_store.load(manifest.run_id) == manifest


def test_five_item_batch_is_one_request_and_full_row_readback_exact(tmp_path):
    parts = components(tmp_path)
    result = execute(parts)
    projection, store, journal, db, capability, transport = (
        parts[1], parts[5], parts[6], parts[8], parts[11], parts[12],
    )
    assert result.status == "batch_complete"
    assert result.confirmed_count == 5
    assert result.write_request_count == 1
    assert db.calls == 1
    assert transport.invocation_count == 1
    assert len(db.rows) == 5
    assert all(row[1] == "2026-09-10 10:02:03" for row in db.rows)
    assert all(row[7] == "通常払い" and row[8] == "auto_expense" for row in db.rows)
    assert all(re.fullmatch(r"[0-9a-f]{64}", row[10]) for row in db.rows)
    assert store.state(capability.capability_id) == "sealed"
    assert [row[0] for row in journal.history(parts[2].run_id, result.attempt_id)] == [
        "pre_read", "write_attempted", "write_result", "post_read", "final",
    ]


def test_existing_identity_stops_before_request_and_seals(tmp_path):
    parts = components(tmp_path)
    db, projection, capability, store = parts[8], parts[1], parts[11], parts[5]
    candidate = projection.plan.candidates[0]
    db.rows.append(candidate.to_import_row(imported_at=NOW, status="auto_expense"))
    with pytest.raises(RuntimeError, match="not_all_absent"):
        execute(parts)
    assert db.calls == 0
    assert store.state(capability.capability_id) == "sealed"


def test_capability_cannot_dispatch_twice(tmp_path):
    parts = components(tmp_path)
    execute(parts)
    manifest, journal, transport = parts[2], parts[6], parts[12]
    second_attempt = str(uuid4())
    journal.append(manifest, second_attempt, "pre_read", "verified", "all_identities_absent", NOW)
    journal.append(manifest, second_attempt, "write_attempted", "attempted", "batch_about_to_append", NOW)
    with pytest.raises(RuntimeError, match="reused_or_expired"):
        transport.write_once(attempt_id=second_attempt)
    assert transport.invocation_count == 1


def test_transport_timeout_is_not_retried_and_finishes_outcome_unknown(tmp_path):
    parts = components(tmp_path)
    db, journal, manifest, transport = parts[8], parts[6], parts[2], parts[12]
    db.svc.api.api = FailingValues(db)

    result = execute(parts)

    assert result.status == "batch_stopped"
    assert result.write_request_count == 1
    assert not result.exact
    assert db.calls == 1
    assert transport.invocation_count == 1
    assert journal.history(manifest.run_id, result.attempt_id)[-1][:3] == (
        "final", "outcome_unknown", "batch_readback_unresolved",
    )


def test_dispatch_precondition_failure_cannot_be_misclassified_as_exact(tmp_path):
    parts = list(components(tmp_path))
    transport = parts[12]
    transport.projection = create_batch_projection(
        parts[1].source_plan,
        candidate_identities=tuple(reversed(parts[1].candidate_identities)),
        statuses={identity: "auto_expense" for identity in parts[1].candidate_identities},
        binding=parts[10], audit_key=KEY,
    )

    result = execute(tuple(parts))

    assert result.status == "batch_stopped"
    assert result.write_request_count == 0
    assert not result.exact
