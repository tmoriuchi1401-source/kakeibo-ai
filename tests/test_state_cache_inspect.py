import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import StateBinding, StateError, snapshot
from app.state_cache_inspect import SOURCES, cache_key, check_closed_state, inspect_cache, stage_native


def native(tmp_path, source="amazon_gmail", *, checkpoint=True, status="noop"):
    root = tmp_path / SOURCES[source]
    binding = StateBinding(source, "sheet", "folder", "file")
    state = SqliteRecurringRunState(root / binding.checkpoint_name, repo_root=tmp_path / "repo")
    state.record({"run_id": "synthetic", "status": status,
                  "source_window_start": "2026-09-14T00:00:00+09:00",
                  "source_window_end": "2026-09-15T00:00:00+09:00"}, advance_checkpoint=checkpoint)
    return root, state, binding


@pytest.mark.parametrize("source", SOURCES)
def test_inspection_restores_all_native_sources_without_modifying_input(tmp_path, source):
    root, state, _ = native(tmp_path, source)
    before = hashlib.sha256(state.path.read_bytes()).hexdigest()
    report = inspect_cache(root, source, tmp_path)
    assert report["checkpoint"] == "2026-09-15T00:00:00+09:00"
    assert report["restore_verified"] and not report["final_migration_source"]
    assert hashlib.sha256(state.path.read_bytes()).hexdigest() == before
    assert not list(tmp_path.glob("cache-inspect-*"))


def test_preview_history_without_checkpoint_is_not_bootstrapped(tmp_path):
    root, state, _ = native(tmp_path, "bank_pdf_drive", checkpoint=False, status="dry_run_ready")
    before = state.path.read_bytes()
    with pytest.raises(StateError, match="requires_checkpoint"):
        inspect_cache(root, "bank_pdf_drive", tmp_path)
    assert state.path.read_bytes() == before
    assert state.successful_window_end() is None


def test_later_failed_run_cannot_be_hidden_by_earlier_checkpoint(tmp_path):
    root, state, _ = native(tmp_path)
    state.record({"run_id": "later-failure", "status": "failed", "failure": 1,
                  "source_window_start": "2026-09-15T00:00:00+09:00",
                  "source_window_end": "2026-09-15T01:00:00+09:00"}, advance_checkpoint=False)
    with pytest.raises(StateError, match="latest_run_unresolved"):
        inspect_cache(root, "amazon_gmail", tmp_path)


def test_staging_excludes_credentials_and_includes_committed_wal(tmp_path):
    root, state, binding = native(tmp_path)
    (root / "credentials.json").write_text("PRIVATE-CREDENTIAL-SENTINEL")
    with sqlite3.connect(state.path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO recurring_runs VALUES (?,?,?,?,?,?)", (
            "wal-only", "2026-09-15T00:01:00+09:00", "dry_run_noop", "start", "end",
            json.dumps({"status": "dry_run_noop"}),
        ))
        connection.commit()
        stage_native(root, binding.source, tmp_path / "staged")
        assert (tmp_path / "staged/recurring.sqlite3-wal").exists()
        assert not (tmp_path / "staged/credentials.json").exists()
        assert check_closed_state(snapshot(tmp_path / "staged", binding), binding) == "dry_run_noop"


def test_missing_cache_and_overlapping_destination_fail_closed(tmp_path):
    with pytest.raises(StateError, match="missing"):
        inspect_cache(tmp_path / "missing", "amazon_gmail", tmp_path)
    root, _, _ = native(tmp_path)
    with pytest.raises(StateError, match="overlaps"):
        inspect_cache(root, "amazon_gmail", root)


@pytest.mark.parametrize("run_id,attempt", [("12\n", "1"), ("12", "0"), ("../12", "1"), ("12-1", "1")])
def test_exact_cache_key_rejects_prefix_and_untrusted_reference(run_id, attempt):
    with pytest.raises(StateError):
        cache_key("amazon_gmail", run_id, attempt)


def test_workflow_is_manual_main_inspect_only_with_exact_restore_and_no_google():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/state-cache-inspect.yml"
    raw = path.read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["operation"]["default"] == "inspect"
    assert inputs["operation"]["options"] == ["inspect"]
    job = workflow["jobs"]["inspect"]
    assert "github.sha == inputs.expected_sha" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "secrets." not in raw
    assert "restore-keys" not in raw
    assert "cache/save" not in raw and "upload-artifact" not in raw and "app.cli" not in raw
    restores = [step for step in job["steps"] if step.get("uses", "").startswith("actions/cache/restore")]
    assert len(restores) == 3
    assert all(step["with"]["fail-on-cache-miss"] == "true" for step in restores)
    final = job["steps"][-1]
    assert all("cache-hit == 'true'" in value and "cache-matched-key == inputs." in value for value in final["env"].values())
