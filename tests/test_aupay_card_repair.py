from datetime import datetime, timedelta, timezone
import base64
import json
import sqlite3

import pytest

from app.aupay_card_production import (
    ProtectedAuditKeyProvider,
    SqliteLeaseManager,
    target_binding_reference,
)
from app.aupay_card_repair import (
    ProtectedRepairApprovalProvider,
    SealedSheetsRowRepairTransport,
    SqliteRepairCapabilityStore,
    SqliteRepairJournal,
    create_repair_preview,
    execute_repair_once,
    issue_repair_capability,
    repair_reference,
)
from app.aupay_card_writer import PersistentAuditKey, TargetBinding, TargetSnapshot
from app.sheets import HEADERS


NOW = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
IDENTITY = "aupaycard-mail:" + "a" * 24 + ":002"
KEY = PersistentAuditKey("repair-test-key", b"r" * 32)


def before_row():
    return (
        IDENTITY, "", "au PAYカード", IDENTITY, "2025-09-10",
        "フィットネス会費(FIT365)", 3278, "メール通知",
        "unclassified_card", "", "8" * 24, "メール明細No.002",
    )


class Inspector:
    def __init__(self, binding):
        self.snapshot = TargetSnapshot(
            binding.expected_spreadsheet_id, binding.expected_worksheet,
            tuple(HEADERS["取込データ"]),
        )

    def inspect(self, _worksheet):
        return self.snapshot


class Request:
    def execute(self):
        return {"updated": True}


class FailingRequest:
    def execute(self):
        raise TimeoutError("unknown transport outcome")


class Values:
    def __init__(self, reader):
        self.reader = reader
        self.calls = []

    def batchUpdate(self, **kwargs):
        self.calls.append(kwargs)
        row = list(self.reader.rows[0][1])
        indexes = {"B": 1, "H": 7, "I": 8, "K": 10}
        for item in kwargs["body"]["data"]:
            column = item["range"].split("!")[1][0]
            row[indexes[column]] = item["values"][0][0]
        self.reader.rows = [(423, tuple(row))]
        return Request()


class FailingValues(Values):
    def batchUpdate(self, **kwargs):
        self.calls.append(kwargs)
        return FailingRequest()


class Service:
    def __init__(self, reader):
        self.api = Values(reader)

    def spreadsheets(self):
        return self

    def values(self):
        return self.api


class DB:
    def __init__(self, reader):
        self.svc = Service(reader)


class Reader:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return self.rows


def components(tmp_path):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    key_path = state / "audit-key.json"
    key_path.write_text(json.dumps({
        "key_id": KEY.key_id,
        "key_b64": base64.b64encode(KEY.secret).decode("ascii"),
    }), encoding="utf-8")
    key_provider = ProtectedAuditKeyProvider(key_path, repo_root=repo)
    binding = TargetBinding(expected_spreadsheet_id="repair-sheet")
    preview = create_repair_preview(
        run_id="6dbce034-73d0-44d8-a7c5-663508f76063",
        row_number=423,
        identity=IDENTITY,
        reason="repair_legacy_canary_materialization_contract",
        candidate_ref="canonical-item-v2:" + "c" * 32,
        target_ref=target_binding_reference(binding, KEY),
        before_row=before_row(),
        corrected_timestamp="2026-09-10 18:58:00",
        corrected_source_hash="f" * 64,
    )
    approval_path = state / "repair-approval.json"
    approval_path.write_text(json.dumps({
        "approval_reference": "user-approved-canary-contract-repair",
        "repair_ref": repair_reference(preview, KEY),
        "target_ref": preview.target_ref,
        "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
    }), encoding="utf-8")
    approval = ProtectedRepairApprovalProvider(approval_path, repo_root=repo)
    store = SqliteRepairCapabilityStore(state / "capabilities.sqlite3", repo_root=repo)
    journal = SqliteRepairJournal(state / "journal.sqlite3", repo_root=repo)
    leases = SqliteLeaseManager(state / "leases.sqlite3", repo_root=repo, clock=lambda: NOW)
    capability = issue_repair_capability(
        preview, key_provider=key_provider, approval_provider=approval,
        store=store, now=NOW,
    )
    reader = Reader([(423, preview.before_row)])
    db = DB(reader)
    transport = SealedSheetsRowRepairTransport(
        db, capability=capability, store=store, binding=binding,
        inspector=Inspector(binding), key_provider=key_provider, clock=lambda: NOW,
    )
    return preview, key_provider, store, journal, leases, capability, reader, db, transport


def test_preview_changes_only_b_h_i_k_and_binds_before_after_digests(tmp_path):
    preview = components(tmp_path)[0]
    assert preview.changed_columns == ("B", "H", "I", "K")
    assert preview.after_row[1] == "2026-09-10 18:58:00"
    assert preview.after_row[7:9] == ("通常払い", "auto_expense")
    assert preview.after_row[10] == "f" * 64
    assert all(
        preview.before_row[index] == preview.after_row[index]
        for index in (0, 2, 3, 4, 5, 6, 9, 11)
    )


def test_formal_repair_is_one_request_in_place_and_reseals(tmp_path):
    preview, key_provider, store, journal, leases, capability, reader, db, transport = components(tmp_path)
    result = execute_repair_once(
        preview, capability=capability, store=store, journal=journal,
        leases=leases, reader=reader, transport=transport,
        key_provider=key_provider, owner_id="repair-test", clock=lambda: NOW,
    )

    assert result.status == "repair_complete"
    assert result.mutation_count == 1
    assert result.row_count == 1
    assert result.exact
    assert len(db.svc.api.calls) == 1
    assert [item["range"] for item in db.svc.api.calls[0]["body"]["data"]] == [
        "取込データ!B423", "取込データ!H423", "取込データ!I423", "取込データ!K423",
    ]
    assert reader.rows == [(423, preview.after_row)]
    assert store.state(capability.capability_id) == "sealed"
    assert [item[0] for item in journal.history(preview.run_id, result.attempt_id)] == [
        "pre_read", "write_attempted", "write_result", "post_read", "final",
    ]
    with pytest.raises(RuntimeError, match="repair_capability_reused"):
        transport.write_once(preview, attempt_id=result.attempt_id)
    assert len(db.svc.api.calls) == 1


def test_fresh_row_mismatch_stops_before_mutation_and_releases(tmp_path):
    preview, key_provider, store, journal, leases, capability, reader, db, transport = components(tmp_path)
    reader.rows = []
    with pytest.raises(RuntimeError, match="fresh_pre_read_mismatch"):
        execute_repair_once(
            preview, capability=capability, store=store, journal=journal,
            leases=leases, reader=reader, transport=transport,
            key_provider=key_provider, owner_id="repair-test", clock=lambda: NOW,
        )
    assert db.svc.api.calls == []
    assert store.state(capability.capability_id) == "sealed"
    probe = leases.acquire(preview.target_ref, "probe", preview.run_id, 30)
    assert probe is not None
    leases.release(probe)


def test_transport_failure_is_journaled_without_second_request(tmp_path):
    preview, key_provider, store, journal, leases, capability, reader, db, transport = components(tmp_path)
    db.svc.api = FailingValues(reader)
    with pytest.raises(TimeoutError, match="unknown transport outcome"):
        execute_repair_once(
            preview, capability=capability, store=store, journal=journal,
            leases=leases, reader=reader, transport=transport,
            key_provider=key_provider, owner_id="repair-test", clock=lambda: NOW,
        )
    with sqlite3.connect(journal.path) as connection:
        stages = connection.execute(
            "SELECT stage,state,reason FROM repair_events WHERE run_id=? ORDER BY seq",
            (preview.run_id,),
        ).fetchall()
    assert stages[-1] == (
        "write_result", "outcome_unknown", "repair_transport_outcome_unknown",
    )
    assert len(db.svc.api.calls) == 1
    assert store.state(capability.capability_id) == "sealed"
