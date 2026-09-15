"""Offline inspection of exact legacy cache copies before a maintenance stop.

No credentials, Google client, cache save, bootstrap, or production binding.
Diagnostic bundles use synthetic bindings and never leave temporary storage.
Success is a prerequisite for migration, not evidence of a final source cache.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from .drive_run_state import StateBinding, StateError, _expected_tables, _external_directory, snapshot
from .state_transfer import export_state, restore_bundle


SOURCES = {
    "amazon_gmail": "amazon-production-state",
    "au_pay_card_gmail": "aupay-card-production-state",
    "bank_pdf_drive": "bank-pdf-recurring-state",
}
SAFE_STATUSES = {"noop", "complete", "fully_applied", "dry_run_noop", "dry_run_ready"}


def cache_key(source: str, run_id: str, attempt: str) -> str:
    if source not in SOURCES or not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise StateError("cache_reference_invalid")
    return f"{SOURCES[source]}-{run_id}-{attempt}"


def _allowed(source: str, name: str) -> bool:
    native = name[:-4] if name.endswith(("-wal", "-shm")) else name
    return _expected_tables(source, native) is not None or (
        source == "bank_pdf_drive" and re.fullmatch(
            r"bank-pdf-batch-[0-9a-f]{16}/exact-steady-state-manifest\.json", name
        ) is not None
    )


def _inventory(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise StateError("cache_directory_missing")
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise StateError("cache_symlink_forbidden")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def stage_native(root: Path, source: str, destination: Path) -> None:
    """Copy the adapter's allowlist, including WAL, without opening the originals."""
    _external_directory(root)
    _external_directory(destination)
    inventory = _inventory(root)
    destination.mkdir(mode=0o700, exist_ok=False)
    for name in inventory:
        if _allowed(source, name):
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
            target.chmod(0o600)


def check_closed_state(files: dict, binding: StateBinding) -> str:
    """Reject unresolved native runs, capabilities, journals and leases; never edit."""
    latest_status = ""
    for name, item in files.items():
        tables = _expected_tables(binding.source, name)
        if tables is None:
            continue
        with closing(sqlite3.connect(":memory:")) as db:
            db.deserialize(base64.b64decode(item["data"], validate=True))
            db.execute("PRAGMA trusted_schema=OFF")
            if "recurring_runs" in tables:
                rows = db.execute("SELECT status,summary_json FROM recurring_runs ORDER BY rowid DESC LIMIT 1").fetchall()
                if not rows:
                    raise StateError("cache_run_history_missing")
                latest_status, raw = rows[0]
                summary = json.loads(raw)
                if latest_status not in SAFE_STATUSES or summary.get("status") != latest_status or summary.get("failure", 0):
                    raise StateError("cache_latest_run_unresolved")
            if "writer_leases" in tables and db.execute("SELECT 1 FROM writer_leases LIMIT 1").fetchone():
                raise StateError("cache_lease_unresolved")
            for table in ("batch_capabilities", "production_capabilities"):
                if table in tables and db.execute(f"SELECT 1 FROM {table} WHERE state != 'sealed' LIMIT 1").fetchone():
                    raise StateError("cache_capability_unresolved")
            for table, group, confirmed in (
                ("batch_events", "run_id,attempt_id", "confirmed"),
                ("journal_events", "run_id,canonical_identity,attempt_id", "write_confirmed"),
            ):
                if table in tables:
                    rows = db.execute(f"SELECT stage,state FROM {table} WHERE seq IN (SELECT MAX(seq) FROM {table} GROUP BY {group})").fetchall()
                    if any(stage != "final" or state != confirmed for stage, state in rows):
                        raise StateError("cache_journal_unresolved")
    return latest_status


def inspect_cache(root: Path, source: str, temporary_root: Path) -> dict:
    if source not in SOURCES:
        raise StateError("cache_source_invalid")
    root = _external_directory(root)
    temporary_root = _external_directory(temporary_root)
    if temporary_root == root or temporary_root.is_relative_to(root):
        raise StateError("cache_temporary_directory_overlaps_source")
    before = _inventory(root)
    binding = StateBinding(source, "diagnostic-only", "diagnostic-only", "diagnostic-only")
    try:
        with tempfile.TemporaryDirectory(prefix="cache-inspect-", dir=temporary_root) as temporary:
            work = Path(temporary)
            staged = work / "staged"
            stage_native(root, source, staged)
            files = snapshot(staged, binding)
            latest_status = check_closed_state(files, binding)
            report = export_state(staged, binding, work / "bundle.json")  # no bootstrap
            restored = work / "restored"
            restore_bundle((work / "bundle.json").read_bytes(), binding, restored)
            if snapshot(restored, binding) != files:
                raise StateError("cache_restore_mismatch")
            return {"source": source, "result": "inspect_passed", "files": report["files"],
                    "checkpoint": report["last_successful_window_end"], "latest_status": latest_status,
                    "restore_verified": True, "final_migration_source": False}
    finally:
        if _inventory(root) != before:
            raise StateError("cache_source_changed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-temp", type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for source, name in SOURCES.items():
        try:
            report = inspect_cache(args.runner_temp / name, source, args.runner_temp)
        except StateError as exc:
            # Only our fixed codes, never SQLite/JSON/path/transaction details.
            code = str(exc)
            if not re.fullmatch(r"[a-z_]+", code):
                code = "cache_inspection_failed"
            report = {"source": source, "result": "blocked", "error": code}
        except Exception:
            report = {"source": source, "result": "blocked", "error": "cache_inspection_failed"}
        reports.append(report)
    print(json.dumps({"operation": "inspect", "sources": reports}, sort_keys=True))
    if any(report["result"] != "inspect_passed" for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
