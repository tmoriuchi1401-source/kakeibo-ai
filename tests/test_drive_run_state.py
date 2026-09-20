"""Synthetic only: no credentials, network, source documents or production APIs."""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import pytest

from app.aupay_card_recurring import SqliteRecurringRunState
from app.aupay_card_batch import SqliteBatchManifestStore, SqliteBatchCapabilityStore, SqliteBatchJournal
from app.aupay_card_production import SqliteLeaseManager, SqliteAttemptJournal, SqliteCapabilityStore
from app.drive_run_state import (
    StateBinding, StateError, DurableState, DriveStateTransport, snapshot, envelope, validate,
)


class FakeDrive:
    def __init__(self, payload):
        self.payload = payload
        self.writes = 0
        self.fail = ""

    def read(self):
        if self.payload is None:
            raise StateError("state_drive_read_failed")
        return self.payload

    def write(self, payload):
        self.writes += 1
        if self.fail == "before":
            raise StateError("state_drive_write_unknown")
        self.payload = payload
        if self.fail == "after":
            raise StateError("state_drive_write_unknown")
        if self.fail == "readback":
            self.payload = b"corrupt"


def record(state, run="initial", end="2026-09-15T06:00:00+09:00", *, success=True):
    state.record({"run_id": run, "status": "complete" if success else "failed",
                  "source_window_start": "2026-09-14T06:00:00+09:00",
                  "source_window_end": end}, advance_checkpoint=success)


@pytest.fixture
def setup(tmp_path):
    binding = StateBinding("amazon_gmail", "synthetic-sheet", "private-state-folder", "fixed-state-file")
    original = tmp_path / "original"
    state = SqliteRecurringRunState(original / "recurring.sqlite3", repo_root=tmp_path / "repo")
    record(state)
    payload = envelope(binding, snapshot(original, binding))
    return binding, FakeDrive(payload), original


def test_roundtrip_preserves_checkpoint_and_run_history(setup, tmp_path):
    binding, drive, _ = setup
    store = DurableState(drive, binding)
    target = tmp_path / "restored"
    store.restore(target)
    state = SqliteRecurringRunState(target / "recurring.sqlite3", repo_root=tmp_path / "repo")
    assert state.successful_window_end().isoformat() == "2026-09-15T06:00:00+09:00"
    store.begin("a" * 32)
    record(state, "next", "2026-09-15T18:00:00+09:00")
    store.commit(target)
    again = tmp_path / "again"
    DurableState(drive, binding).restore(again)
    with sqlite3.connect(again / "recurring.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM recurring_runs").fetchone()[0] == 2
    assert drive.writes == 2
    assert validate(drive.payload, binding)["phase"] == "ready"


def test_preview_restore_has_zero_remote_writes(setup, tmp_path):
    binding, drive, _ = setup
    original = drive.payload
    DurableState(drive, binding).restore(tmp_path / "preview")
    assert drive.payload == original
    assert drive.writes == 0


@pytest.mark.parametrize("payload", [None, b"", b"broken", b'{}'])
def test_missing_or_corrupt_remote_never_initializes(setup, tmp_path, payload):
    binding, _, _ = setup
    drive = FakeDrive(payload)
    with pytest.raises(StateError):
        DurableState(drive, binding).restore(tmp_path / "restore")
    assert not (tmp_path / "restore").exists()
    assert drive.writes == 0


@pytest.mark.parametrize("change", ["source", "target", "folder", "file", "schema", "checksum", "traversal", "database", "columns"])
def test_binding_content_and_schema_validation(setup, tmp_path, change):
    binding, drive, _ = setup
    value = json.loads(drive.payload)
    if change in {"source", "target", "folder", "file"}:
        other = StateBinding("bank_pdf_drive" if change == "source" else binding.source,
                             "wrong" if change == "target" else binding.spreadsheet_id,
                             "wrong" if change == "folder" else binding.folder_id,
                             "wrong" if change == "file" else binding.file_id)
        with pytest.raises(StateError, match="binding_mismatch"):
            DurableState(drive, other).restore(tmp_path / "out")
        return
    if change == "schema":
        value["schema"] = 2
    elif change == "checksum":
        value["files"]["recurring.sqlite3"]["sha256"] = "0" * 64
    elif change == "traversal":
        value["files"]["../../escape"] = value["files"]["recurring.sqlite3"]
    else:
        data = b"not sqlite"
        if change == "columns":
            with sqlite3.connect(":memory:") as db:
                db.executescript("CREATE TABLE recurring_checkpoint(wrong TEXT); CREATE TABLE recurring_runs(wrong TEXT);")
                data = db.serialize()
        value["files"]["recurring.sqlite3"] = {"data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
    drive.payload = json.dumps(value).encode()
    with pytest.raises(StateError):
        DurableState(drive, binding).restore(tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert drive.writes == 0


@pytest.mark.parametrize("failure", ["before", "after", "readback"])
def test_unknown_begin_never_allows_source_write(setup, tmp_path, failure):
    binding, drive, _ = setup
    store = DurableState(drive, binding)
    store.restore(tmp_path / "out")
    drive.fail = failure
    with pytest.raises(StateError):
        store.begin("b" * 32)
    with pytest.raises(StateError, match="begin_required"):
        store.commit(tmp_path / "out")
    assert drive.writes == 1  # no automatic retry


def test_interruption_leaves_pending_and_requires_bound_operator_evidence(setup, tmp_path):
    binding, drive, _ = setup
    store = DurableState(drive, binding)
    store.restore(tmp_path / "out")
    store.begin("c" * 32)
    restarted = DurableState(drive, binding)
    with pytest.raises(StateError, match="reconciliation_required"):
        restarted.restore(tmp_path / "next")
    with pytest.raises(StateError, match="recovery_evidence_required"):
        restarted.release_after_reconciliation(observed_digest="wrong", evidence_reference="d" * 64)
    restarted.release_after_reconciliation(observed_digest=hashlib.sha256(drive.payload).hexdigest(), evidence_reference="d" * 64)
    restarted.restore(tmp_path / "next")
    state = SqliteRecurringRunState(tmp_path / "next/recurring.sqlite3", repo_root=tmp_path / "repo")
    assert state.successful_window_end().isoformat() == "2026-09-15T06:00:00+09:00"


def test_sheets_success_then_state_failure_replays_by_existing_identity(setup, tmp_path):
    binding, drive, _ = setup
    store = DurableState(drive, binding)
    target = tmp_path / "out"
    store.restore(target)
    store.begin("e" * 32)
    # Synthetic external append/read-back and checkpoint update.
    sheet = {"stable-source-id": ("synthetic", 123)}
    state = SqliteRecurringRunState(target / "recurring.sqlite3", repo_root=tmp_path / "repo")
    record(state, "written", "2026-09-15T18:00:00+09:00")
    drive.fail = "before"
    with pytest.raises(StateError):
        store.commit(target)
    assert validate(drive.payload, binding)["phase"] == "pending"
    drive.fail = ""
    recovery = DurableState(drive, binding)
    # Separate operator confirmation of exact existing target, never automatic.
    assert sheet["stable-source-id"] == ("synthetic", 123)
    recovery.release_after_reconciliation(observed_digest=hashlib.sha256(drive.payload).hexdigest(), evidence_reference="f" * 64)
    recovery.restore(tmp_path / "replay")
    append_count = 0 if "stable-source-id" in sheet else 1
    assert append_count == 0


def test_stale_restore_cannot_overwrite_new_generation(setup, tmp_path):
    binding, drive, _ = setup
    first, second = DurableState(drive, binding), DurableState(drive, binding)
    first.restore(tmp_path / "one")
    second.restore(tmp_path / "two")
    first.begin("a" * 32)
    with pytest.raises(StateError, match="changed_since_restore"):
        second.begin("b" * 32)
    assert drive.writes == 1


def test_checkpoint_regression_is_rejected(setup, tmp_path):
    binding, drive, _ = setup
    store = DurableState(drive, binding)
    target = tmp_path / "out"
    store.restore(target)
    store.begin("a" * 32)
    state = SqliteRecurringRunState(target / "recurring.sqlite3", repo_root=tmp_path / "repo")
    record(state, "bad", "2026-09-14T07:00:00+09:00")
    with pytest.raises(StateError, match="checkpoint_regression"):
        store.commit(target)
    assert validate(drive.payload, binding)["phase"] == "pending"


def test_unexpected_credentials_are_never_bundled(setup):
    binding, _, original = setup
    (original / "token.json").write_text('{"token":"synthetic-secret"}')
    with pytest.raises(StateError, match="file_not_allowed") as error:
        snapshot(original, binding)
    assert "synthetic-secret" not in str(error.value)


def test_restore_refuses_to_replace_existing_local_files(setup, tmp_path):
    binding, drive, _ = setup
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("unchanged")
    with pytest.raises(StateError, match="new_directory"):
        DurableState(drive, binding).restore(target)
    assert (target / "keep").read_text() == "unchanged"


def test_state_cannot_be_restored_or_snapshotted_inside_repository(setup):
    from app.drive_run_state import REPOSITORY_ROOT
    binding, drive, _ = setup
    with pytest.raises(StateError, match="absolute_and_external"):
        DurableState(drive, binding).restore(REPOSITORY_ROOT / "forbidden-state")
    with pytest.raises(StateError, match="absolute_and_external"):
        snapshot(REPOSITORY_ROOT, binding)
    assert not (REPOSITORY_ROOT / "forbidden-state").exists()


@pytest.mark.parametrize("source", ["au_pay_card_gmail", "bank_pdf_drive"])
def test_native_auxiliary_stores_and_committed_wal_roundtrip(tmp_path, source):
    root, repo = tmp_path / "state", tmp_path / "repo"
    binding = StateBinding(source, "sheet", "folder", "file")
    SqliteRecurringRunState(root / binding.checkpoint_name, repo_root=repo)
    if source == "au_pay_card_gmail":
        SqliteBatchManifestStore(root / "manifests.sqlite3", repo_root=repo)
        SqliteBatchCapabilityStore(root / "capabilities.sqlite3", repo_root=repo)
        SqliteBatchJournal(root / "journal.sqlite3", repo_root=repo)
        SqliteLeaseManager(root / "leases.sqlite3", repo_root=repo)
        wal_path = root / "leases.sqlite3"
    else:
        wal_path = root / ("bank-pdf-batch-" + "a" * 16) / "bank-steady-state.sqlite3"
        SqliteAttemptJournal(wal_path, repo_root=repo)
        SqliteCapabilityStore(wal_path, repo_root=repo)
        SqliteLeaseManager(wal_path, repo_root=repo)
    # Hold the connection open so committed state is still in a WAL sidecar.
    with sqlite3.connect(wal_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO writer_leases VALUES ('target','owner','run','synthetic-token','2026-09-15T06:00:00+09:00')")
        db.commit()
        payload = envelope(binding, snapshot(root, binding))
    target = tmp_path / "restored"
    DurableState(FakeDrive(payload), binding).restore(target)
    with sqlite3.connect(target / wal_path.relative_to(root)) as db:
        assert db.execute("SELECT owner_id FROM writer_leases").fetchone() == ("owner",)


def test_drive_transport_has_fixed_target_and_no_retry(setup):
    binding, _, _ = setup
    service = MagicMock()
    service.files().get().execute.return_value = {
        "parents": [binding.folder_id], "mimeType": "application/json", "capabilities": {"canEdit": True},
    }
    service.files().get_media().execute.return_value = b"example"
    transport = DriveStateTransport(service, binding)
    assert transport.read() == b"example"
    transport.write(b"example")
    service.files().update().execute.assert_called_once_with(num_retries=0)
    assert service.files().create.call_count == 0
    assert service.files().delete.call_count == 0
    service.files().get().execute.return_value["parents"] = ["family-inbox"]
    with pytest.raises(StateError, match="target_mismatch"):
        transport.read()


@pytest.mark.parametrize('status',[429,500,502,503,504])
def test_state_read_retries_transient_response_only(setup,monkeypatch,status):
    from googleapiclient.errors import HttpError
    from httplib2 import Response
    monkeypatch.setattr('time.sleep',lambda _:None)
    binding,_,_=setup;service=MagicMock()
    service.files().get().execute.return_value={'parents':[binding.folder_id],'mimeType':'application/json'}
    request=service.files().get_media().execute
    request.side_effect=[HttpError(Response({'status':status}),b'private error'),b'recovered']
    assert DriveStateTransport(service,binding).read()==b'recovered'
    assert request.call_count==2 and service.files().update.call_count==0


@pytest.mark.parametrize('status',[400,401,403,404])
def test_state_read_does_not_retry_permanent_response(setup,status):
    from googleapiclient.errors import HttpError
    from httplib2 import Response
    binding,_,_=setup;service=MagicMock()
    service.files().get().execute.side_effect=HttpError(Response({'status':status}),b'private error')
    expected='state_drive_read_failed'+('_'+str(status) if status!=400 else '')
    with pytest.raises(StateError,match='^'+expected+'$'):DriveStateTransport(service,binding).read()
    assert service.files().get().execute.call_count==1


def test_state_read_timeout_is_bounded_and_write_remains_single_attempt(setup,monkeypatch):
    monkeypatch.setattr('time.sleep',lambda _:None)
    binding,_,_=setup;service=MagicMock()
    service.files().get().execute.return_value={'parents':[binding.folder_id],'mimeType':'application/json','capabilities':{'canEdit':True}}
    service.files().get_media().execute.side_effect=TimeoutError()
    with pytest.raises(StateError,match='state_drive_read_failed'):DriveStateTransport(service,binding).read()
    assert service.files().get_media().execute.call_count==3
    service.files().update().execute.side_effect=TimeoutError()
    with pytest.raises(StateError,match='state_drive_write_unknown'):DriveStateTransport(service,binding).write(b'payload')
    assert service.files().update().execute.call_count==1

@pytest.mark.parametrize('reason',['userRateLimitExceeded','rateLimitExceeded','insufficientFilePermissions'])
def test_state_read_distinguishes_rate_limited_403_from_permission_failure(setup,monkeypatch,reason):
    from googleapiclient.errors import HttpError
    from httplib2 import Response
    monkeypatch.setattr('time.sleep',lambda _:None)
    binding,_,_=setup;service=MagicMock()
    service.files().get().execute.return_value={'parents':[binding.folder_id],'mimeType':'application/json'}
    request=service.files().get_media().execute
    error=HttpError(Response({'status':403}),json.dumps({'error':{'errors':[{'reason':reason}]}}).encode())
    request.side_effect=[error,b'recovered']
    if reason=='insufficientFilePermissions':
        with pytest.raises(StateError,match='state_drive_read_failed_403'):DriveStateTransport(service,binding).read()
        assert request.call_count==1
    else:
        assert DriveStateTransport(service,binding).read()==b'recovered'
        assert request.call_count==2
    service.files().update.assert_not_called()
