from datetime import datetime

import pytest

from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import StateBinding, StateError
from app.state_transfer import export_state, inspect_bundle, restore_bundle


def test_offline_migration_roundtrip_is_source_bound_and_never_overwrites(tmp_path):
    binding = StateBinding("amazon_gmail", "sheet", "private-folder", "file")
    root = tmp_path / "native"
    state = SqliteRecurringRunState(root / "recurring.sqlite3", repo_root=tmp_path / "repo")
    state.record({"run_id": "synthetic", "status": "noop", "source_window_start": "2026-09-14T00:00:00+09:00",
                  "source_window_end": "2026-09-15T00:00:00+09:00"}, advance_checkpoint=True)
    bundle = tmp_path / "private-bundle.json"
    report = export_state(root, binding, bundle)
    assert report["last_successful_window_end"] == "2026-09-15T00:00:00+09:00"
    assert "sheet" not in report.values()
    assert report == inspect_bundle(bundle.read_bytes(), binding)
    with pytest.raises(FileExistsError):
        export_state(root, binding, bundle)
    restore_bundle(bundle.read_bytes(), binding, tmp_path / "restored")
    restored = SqliteRecurringRunState(tmp_path / "restored/recurring.sqlite3", repo_root=tmp_path / "repo")
    assert restored.successful_window_end() == datetime.fromisoformat("2026-09-15T00:00:00+09:00")


def test_missing_checkpoint_is_not_silent_initialization(tmp_path):
    binding = StateBinding("bank_pdf_drive", "sheet", "private-folder", "file")
    root = tmp_path / "native"
    SqliteRecurringRunState(root / binding.checkpoint_name, repo_root=tmp_path / "repo")
    bundle = tmp_path / "initial.json"
    with pytest.raises(StateError, match="explicit_bootstrap"):
        export_state(root, binding, bundle)
    assert not bundle.exists()
    assert export_state(root, binding, bundle, bootstrap=True)["last_successful_window_end"] is None


def test_ledger_initialization_is_explicit_offline_and_exclusive(tmp_path):
    from app.state_transfer import initialize_ledger
    binding = StateBinding("production_run", "sheet", "folder", "file")
    path = tmp_path / "initial-ledger.json"
    with pytest.raises(StateError):
        initialize_ledger(binding, path, bootstrap=False)
    assert not path.exists()
    report = initialize_ledger(binding, path, bootstrap=True)
    assert report["pending_sources"] == []
    assert all(value is None for value in report["last_success"].values())
    with pytest.raises(FileExistsError):
        initialize_ledger(binding, path, bootstrap=True)
