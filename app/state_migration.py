"""One-time fixed-file migration; no transaction runner, Sheets writer or cache save."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .drive_run_state import DriveStateTransport, StateBinding, StateError, _external_directory, snapshot
from .production_ledger import ProductionLedger
from .state_cache_inspect import SOURCES, _inventory, check_closed_state, stage_native
from .state_transfer import export_state, initialize_ledger, inspect_bundle, restore_bundle


FILE_VARIABLES = {
    "amazon_gmail": "AMAZON_STATE_FILE_ID", "au_pay_card_gmail": "AUPAY_CARD_STATE_FILE_ID",
    "bank_pdf_drive": "BANK_STATE_FILE_ID", "production_run": "KAKEIBO_RUN_LEDGER_FILE_ID",
}


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def bindings_from_environment(env: dict) -> dict[str, StateBinding]:
    bindings = {source: StateBinding(source, env.get("SPREADSHEET_ID", ""),
                                     env.get("KAKEIBO_STATE_FOLDER_ID", ""), env.get(variable, ""))
                for source, variable in FILE_VARIABLES.items()}
    if len({binding.file_id for binding in bindings.values()}) != 4:
        raise StateError("migration_distinct_fixed_files_required")
    return bindings


def preparation_payload(source: str) -> bytes:
    return encoded({"purpose": "connectivity_check_only", "status": "UNINITIALIZED_NOT_PRODUCTION_STATE",
                    "source_label": source, "test_value": "sa_update_verified"})


def destination_snapshot(service, bindings: dict, sa_email: str) -> dict:
    """Stable identity/permissions commitment, computed privately before dispatch.

    The owner-side My Drive root check remains in the existing private setup
    evidence. SA metadata sees the shared folder and every child's exact parent.
    """
    if set(bindings) != set(FILE_VARIABLES):
        raise StateError("migration_bindings_required")
    folders = {binding.folder_id for binding in bindings.values()}
    if len(folders) != 1 or len({binding.spreadsheet_id for binding in bindings.values()}) != 1:
        raise StateError("migration_binding_mismatch")
    folder_id = next(iter(folders))
    targets = {"folder": folder_id, **{source: binding.file_id for source, binding in bindings.items()}}
    result = {}
    owner = None
    for label, file_id in targets.items():
        meta = service.files().get(fileId=file_id, supportsAllDrives=True,
            fields="id,parents,owners(emailAddress),mimeType,trashed,driveId,capabilities(canEdit,canDownload)").execute(num_retries=0)
        owners = meta.get("owners", [])
        if (meta.get("id") != file_id or meta.get("trashed") or meta.get("driveId") or len(owners) != 1
                or not owners[0].get("emailAddress") or owners[0]["emailAddress"] == sa_email
                or not meta.get("capabilities", {}).get("canEdit")):
            raise StateError("migration_destination_identity_invalid")
        if owner is None:
            owner = owners[0]["emailAddress"]
        if owners != [{"emailAddress": owner}]:
            raise StateError("migration_owner_mismatch")
        mime = "application/vnd.google-apps.folder" if label == "folder" else "application/json"
        if meta.get("mimeType") != mime or (label != "folder" and (
                meta.get("parents") != [folder_id] or not meta["capabilities"].get("canDownload"))):
            raise StateError("migration_destination_parent_or_type_invalid")
        permissions = service.permissions().list(fileId=file_id, supportsAllDrives=True, pageSize=100,
            fields="nextPageToken,permissions(id,type,role,emailAddress,deleted,expirationTime)").execute(num_retries=0)
        entries = permissions.get("permissions", [])
        if (permissions.get("nextPageToken") or len(entries) != 2 or any(p.get("deleted") or p.get("expirationTime") for p in entries)
                or sorted((p.get("type"), p.get("role"), p.get("emailAddress")) for p in entries)
                != sorted([("user", "owner", owner), ("user", "writer", sa_email)])):
            raise StateError("migration_destination_sharing_invalid")
        result[label] = {"id": file_id, "parents": meta.get("parents", []), "owner": owner, "mimeType": mime,
                         "permissions": sorted(entries, key=lambda p: p["id"])}
    return result


def restore_verified(payload: bytes, binding: StateBinding, target: Path) -> None:
    if binding.source == "production_run":
        class ReadOnlyLedger:
            def read(self): return payload
            def write(self, _): raise StateError("migration_read_only_restore")
        ledger = ProductionLedger(ReadOnlyLedger(), binding)
        if any(item["phase"] != "ready" or item["last_success"] is not None for item in ledger.value["sources"].values()):
            raise StateError("migration_new_ledger_invalid")
    else:
        restore_bundle(payload, binding, target)
        original = json.loads(payload)["files"]
        if snapshot(target, binding) != original:
            raise StateError("migration_restore_mismatch")


def prepare_bundles(runner_temp: Path, bindings: dict, work: Path) -> tuple[dict, dict]:
    """Finish all four exports/restores before returning any writable payload."""
    payloads, reports = {}, {}
    for source, binding in bindings.items():
        output = work / (source + ".json")
        if source == "production_run":
            report = initialize_ledger(binding, output, bootstrap=True)
        else:
            root = runner_temp / SOURCES[source]
            before = _inventory(root)
            try:
                staged = work / (source + "-staged")
                stage_native(root, source, staged)
                check_closed_state(snapshot(staged, binding), binding)
                report = export_state(staged, binding, output)  # Never bootstrap a history source.
            finally:
                if _inventory(root) != before:
                    raise StateError("migration_source_changed")
        payload = output.read_bytes()
        restore_verified(payload, binding, work / (source + "-restored"))
        payloads[source], reports[source] = payload, report
    return payloads, reports


def migrate(service, bindings: dict, sa_email: str, runner_temp: Path, expected_destinations: str,
            *, apply: bool = False, report: dict | None = None) -> dict:
    result = report if report is not None else {}
    result.update(operation="migrate" if apply else "inspect", success=False, prepared=False, sources={})
    if not re.fullmatch(r"[0-9a-f]{64}", expected_destinations):
        raise StateError("migration_destination_commitment_required")
    baseline = destination_snapshot(service, bindings, sa_email)
    if digest(encoded(baseline)) != expected_destinations:
        raise StateError("migration_destination_commitment_mismatch")
    transports = {source: DriveStateTransport(service, binding) for source, binding in bindings.items()}
    old_payloads = {source: transport.read() for source, transport in transports.items()}
    if any(old_payloads[source] != preparation_payload(source) for source in bindings):
        raise StateError("migration_requires_preparation_files")  # Formal ledger is never reinitialized.
    temporary_root = _external_directory(runner_temp)
    with tempfile.TemporaryDirectory(prefix="state-migration-", dir=temporary_root) as temporary:
        payloads, reports = prepare_bundles(temporary_root, bindings, Path(temporary))
        result["prepared"] = True
        result["sources"] = {source: {"prepared": True, "updated": False, "readback_verified": False,
                                      "restore_verified": True, "digest": digest(payloads[source]),
                                      "checkpoint": reports[source].get("last_successful_window_end")}
                             for source in bindings}
        for source, transport in transports.items():
            if destination_snapshot(service, bindings, sa_email) != baseline:
                raise StateError("migration_destination_changed")
            if transport.read() != old_payloads[source]:
                raise StateError("migration_preparation_changed")
            if apply:
                result["sources"][source]["updated"] = None  # Attempted; outcome not yet known.
                try:
                    transport.write(payloads[source])
                except StateError:
                    # Unknown response is reconciled by GET, never by another update.
                    if transport.read() != payloads[source]:
                        raise StateError("migration_write_not_confirmed") from None
                if transport.read() != payloads[source]:
                    raise StateError("migration_readback_mismatch")
                result["sources"][source].update(updated=True, readback_verified=True)
                if destination_snapshot(service, bindings, sa_email) != baseline:
                    raise StateError("migration_post_write_metadata_changed")
        # Final read of every fixed ID, including earlier writes in a partial sequence.
        for source, transport in transports.items():
            expected = payloads[source] if apply else old_payloads[source]
            if transport.read() != expected:
                raise StateError("migration_final_readback_mismatch")
            if apply:
                inspect_bundle(expected, bindings[source])
    result["success"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "migrate"), nargs="?", default="inspect")
    parser.add_argument("--expected-destinations", required=True)
    args = parser.parse_args()
    report = {"success": False, "sources": {}}
    try:
        env = dict(os.environ)
        if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REF") != "refs/heads/main"
                or env.get("GITHUB_REPOSITORY") != "tmoriuchi1401-source/kakeibo-ai"
                or env.get("GITHUB_SHA") != env.get("KAKEIBO_VALIDATED_MAIN_SHA")
                or env.get("KAKEIBO_PRODUCTION_ENABLED") == "true"
                or env.get("KAKEIBO_SCHEDULE_ENABLED") == "true"
                or (args.operation == "migrate" and env.get("KAKEIBO_LEGACY_DISABLED") != "true")):
            raise StateError("migration_execution_boundary_invalid")
        from .google_clients import credentials, DRIVE_READ_ONLY_SCOPES
        from .private_state_bindings import decode_environment
        env, _ = decode_environment(env)
        from googleapiclient.discovery import build
        scopes = ["https://www.googleapis.com/auth/drive"] if args.operation == "migrate" else DRIVE_READ_ONLY_SCOPES
        creds = credentials(scopes)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        migrate(service, bindings_from_environment(env), creds.service_account_email, Path(env["RUNNER_TEMP"]),
                args.expected_destinations, apply=args.operation == "migrate", report=report)
    except Exception:
        report.update(success=False, error="state_migration_failed")
    print(json.dumps(report, sort_keys=True))  # Counts, phases and digests only.
    if not report["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
