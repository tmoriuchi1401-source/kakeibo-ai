from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import StateBinding, StateError
from app.production_ledger import initial_ledger
from app.state_cache_inspect import SOURCES, _inventory
from app.state_migration import FILE_VARIABLES, destination_snapshot, digest, encoded, migrate, preparation_payload


OWNER, SA = "owner@example.invalid", "sa@example.invalid"


class Request:
    def __init__(self, fn): self.fn = fn
    def execute(self, **kwargs):
        assert kwargs == {"num_retries": 0}
        return deepcopy(self.fn())


class Google:
    def __init__(self, bindings):
        self.writes, self.fail = [], ""
        self.payloads = {b.file_id: preparation_payload(s) for s, b in bindings.items()}
        self.metadata = {"folder": self.meta("folder", folder=True),
                         **{b.file_id: self.meta(b.file_id) for b in bindings.values()}}
        self.grants = {key: [{"id": "owner-permission", "type": "user", "role": "owner", "emailAddress": OWNER},
                             {"id": "sa-permission", "type": "user", "role": "writer", "emailAddress": SA}]
                       for key in self.metadata}

    def meta(self, file_id, *, folder=False):
        return {"id": file_id, "parents": [] if folder else ["folder"], "owners": [{"emailAddress": OWNER}],
                "mimeType": "application/vnd.google-apps.folder" if folder else "application/json",
                "trashed": False, "capabilities": {"canEdit": True, "canDownload": True}}

    def files(self): return self
    def permissions(self): return self
    def get(self, **kw): return Request(lambda: self.metadata[kw["fileId"]])
    def list(self, **kw): return Request(lambda: {"permissions": self.grants[kw["fileId"]]})
    def get_media(self, **kw): return Request(lambda: self.payloads[kw["fileId"]])
    def update(self, **kw):
        def write():
            self.writes.append(kw["fileId"])
            if self.fail == "before": raise RuntimeError("private-transport-error")
            media = kw["media_body"]
            self.payloads[kw["fileId"]] = media.getbytes(0, media.size())
            if self.fail == "corrupt": self.payloads[kw["fileId"]] = b"corrupt"
            if self.fail == "metadata": self.metadata[kw["fileId"]]["owners"] = [{"emailAddress": "different@example.invalid"}]
            if self.fail == "after": raise RuntimeError("private-response-lost")
            return {"id": kw["fileId"]}
        return Request(write)


@pytest.fixture
def prepared(tmp_path):
    bindings = {source: StateBinding(source, "sheet", "folder", "fixed-" + source) for source in FILE_VARIABLES}
    for source, directory in SOURCES.items():
        state = SqliteRecurringRunState(tmp_path / directory / bindings[source].checkpoint_name, repo_root=tmp_path / "repo")
        state.record({"run_id": "initial", "status": "noop", "source_window_start": "2026-09-14T00:00:00+09:00",
                      "source_window_end": "2026-09-15T00:00:00+09:00"}, advance_checkpoint=True)
    google = Google(bindings)
    commitment = digest(encoded(destination_snapshot(google, bindings, SA)))
    return bindings, google, commitment


def test_four_bundles_restored_before_updates_and_original_caches_unchanged(prepared, tmp_path):
    bindings, google, commitment = prepared
    before = {s: _inventory(tmp_path / path) for s, path in SOURCES.items()}
    report = migrate(google, bindings, SA, tmp_path, commitment, apply=True)
    assert report["success"] and report["prepared"]
    assert len(google.writes) == len(set(google.writes)) == 4
    assert all(item["updated"] and item["readback_verified"] and item["restore_verified"] for item in report["sources"].values())
    assert {s: _inventory(tmp_path / path) for s, path in SOURCES.items()} == before
    ledger = json.loads(google.payloads[bindings["production_run"].file_id])
    assert all(item["last_success"] is None and item["phase"] == "ready" for item in ledger["sources"].values())


def test_inspect_default_has_no_external_updates(prepared, tmp_path):
    bindings, google, commitment = prepared
    before = deepcopy(google.payloads)
    assert migrate(google, bindings, SA, tmp_path, commitment)["success"]
    assert not google.writes and google.payloads == before


def test_last_native_bundle_failure_prevents_every_drive_update(prepared, tmp_path):
    bindings, google, commitment = prepared
    import sqlite3
    with sqlite3.connect(tmp_path / SOURCES["bank_pdf_drive"] / "bank-recurring.sqlite3") as db:
        db.execute("DELETE FROM recurring_checkpoint")
    with pytest.raises(StateError, match="requires_checkpoint"):
        migrate(google, bindings, SA, tmp_path, commitment, apply=True)
    assert not google.writes


def test_existing_formal_ledger_is_never_reinitialized(prepared, tmp_path):
    bindings, google, commitment = prepared
    google.payloads[bindings["production_run"].file_id] = initial_ledger(bindings["production_run"])
    with pytest.raises(StateError, match="requires_preparation"):
        migrate(google, bindings, SA, tmp_path, commitment, apply=True)
    assert not google.writes


def test_unknown_update_response_is_confirmed_by_get_without_second_update(prepared, tmp_path):
    bindings, google, commitment = prepared
    google.fail = "after"
    assert migrate(google, bindings, SA, tmp_path, commitment, apply=True)["success"]
    assert len(google.writes) == len(set(google.writes)) == 4


@pytest.mark.parametrize("failure", ["before", "corrupt", "metadata"])
def test_unknown_or_changed_destination_stops_remaining_updates(prepared, tmp_path, failure):
    bindings, google, commitment = prepared
    google.fail = failure
    report = {}
    with pytest.raises(StateError):
        migrate(google, bindings, SA, tmp_path, commitment, apply=True, report=report)
    assert len(google.writes) == 1
    assert not report["success"]
    assert all(not item["updated"] for s, item in report["sources"].items() if s != "amazon_gmail")


def test_destination_identity_and_sharing_must_match_private_commitment(prepared, tmp_path):
    bindings, google, commitment = prepared
    with pytest.raises(StateError, match="commitment_mismatch"):
        migrate(google, bindings, SA, tmp_path, "0" * 64, apply=True)
    google.grants["folder"].append({"id": "public", "type": "anyone", "role": "reader"})
    with pytest.raises(StateError, match="sharing_invalid"):
        migrate(google, bindings, SA, tmp_path, commitment, apply=True)
    assert not google.writes


def test_migration_and_parent_use_encrypted_bindings_and_reject_debug():
    root = Path(__file__).resolve().parents[1]
    for filename in ("kakeibo-production.yml", "state-migration.yml"):
        raw = (root / ".github/workflows" / filename).read_text(encoding="utf-8")
        assert "vars.KAKEIBO_STATE_FOLDER_ID" in raw
        assert all("vars." + name in raw for name in FILE_VARIABLES.values())
        assert "runner.debug == '1'" in raw
        assert "synthetic-test:" not in raw
    raw = (root / ".github/workflows/state-migration.yml").read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["operation"]["default"] == "inspect"
    assert "cache/save" not in raw and "restore-keys" not in raw and "upload-artifact" not in raw
    assert "GMAIL" not in raw and "GEMINI" not in raw and "app.cli" not in raw
    guard = workflow["jobs"]["migrate"]["if"]
    assert "vars.KAKEIBO_LEGACY_DISABLED == 'true'" in guard
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in guard
    for filename in ("production_flow.py", "state_migration.py"):
        code = (root / "app" / filename).read_text(encoding="utf-8")
        assert "decode_environment(env" in code
        assert "GITHUB_ENV" not in code
