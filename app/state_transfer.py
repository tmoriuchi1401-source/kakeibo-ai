"""Offline-only migration/inspection of recurring state. No Google connection."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

from .drive_run_state import (
    DurableState, StateBinding, StateError, _external_directory, _validate_file,
    envelope, snapshot, validate,
)


def load_binding(path: Path) -> StateBinding:
    _external_directory(path.parent)
    value = json.loads(path.read_bytes())
    if set(value) != {"source", "spreadsheet_id", "folder_id", "file_id", "schema"}:
        raise StateError("state_binding_invalid")
    return StateBinding(**value)


def inspect_bundle(payload: bytes, binding: StateBinding) -> dict:
    if binding.source == "production_run":
        from .production_ledger import validate_ledger
        value = validate_ledger(payload, binding)
        return {"source": binding.source, "schema": value["schema"], "generation": value["generation"],
                "pending_sources": [source for source, item in value["sources"].items() if item["phase"] == "pending"],
                "last_success": {source: item["last_success"] for source, item in value["sources"].items()},
                "digest": hashlib.sha256(payload).hexdigest()}
    value = validate(payload, binding)
    checkpoint = _validate_file(binding.source, binding.checkpoint_name,
                                base64.b64decode(value["files"][binding.checkpoint_name]["data"]))
    return {"source": binding.source, "schema": value["schema"], "phase": value["phase"],
            "generation": value["generation"], "files": len(value["files"]),
            "last_successful_window_end": checkpoint, "digest": hashlib.sha256(payload).hexdigest()}


def export_state(directory: Path, binding: StateBinding, output: Path, *, bootstrap: bool = False) -> dict:
    _external_directory(output.parent)
    payload = envelope(binding, snapshot(directory, binding))
    report = inspect_bundle(payload, binding)
    if report["last_successful_window_end"] is None and not bootstrap:
        raise StateError("state_migration_requires_checkpoint_or_explicit_bootstrap")
    with output.open("xb") as stream:
        stream.write(payload)
    output.chmod(0o600)
    if output.read_bytes() != payload:
        raise StateError("state_export_readback_failed")
    return report


def restore_bundle(payload: bytes, binding: StateBinding, directory: Path) -> None:
    class ReadOnlyBundle:
        def read(self): return payload
        def write(self, data): raise StateError("offline_bundle_is_read_only")
    DurableState(ReadOnlyBundle(), binding).restore(directory)


def initialize_ledger(binding: StateBinding, output: Path, *, bootstrap: bool) -> dict:
    from .production_ledger import initial_ledger
    if not bootstrap or binding.source != "production_run":
        raise StateError("ledger_initialization_requires_explicit_bootstrap")
    _external_directory(output.parent)
    payload = initial_ledger(binding)
    with output.open("xb") as stream:
        stream.write(payload)
    output.chmod(0o600)
    if output.read_bytes() != payload:
        raise StateError("state_export_readback_failed")
    return inspect_bundle(payload, binding)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("export", "inspect", "restore", "ledger-init"))
    parser.add_argument("--binding-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bootstrap", action="store_true",
                        help="Explicit approved first initialization with no prior checkpoint; never recovery")
    args = parser.parse_args()
    try:
        binding = load_binding(args.binding_file)
        _external_directory(args.bundle.parent)
        if args.operation == "ledger-init":
            report = initialize_ledger(binding, args.bundle, bootstrap=args.bootstrap)
        elif args.operation == "export":
            if args.state_dir is None:
                raise StateError("state_directory_required")
            report = export_state(args.state_dir, binding, args.bundle, bootstrap=args.bootstrap)
        else:
            if args.bootstrap:
                raise StateError("bootstrap_is_not_recovery")
            payload = args.bundle.read_bytes()
            report = inspect_bundle(payload, binding)
            if args.operation == "restore":
                if args.state_dir is None:
                    raise StateError("state_directory_required")
                restore_bundle(payload, binding, args.state_dir)
        print(json.dumps(report, sort_keys=True))
    except Exception:
        print('{"error":"state_transfer_failed"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
