"""Manual Actions migration under a frozen ledger and matching backup.

The existing production workflow lock supplies exclusion. Only precreated
money documents and explicit daily cutover surfaces may change. Financial
values, source checkpoints and accounting are never written by this module.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from cryptography.hazmat.primitives import serialization

from .amazon_money import MoneyError, digest
from .amazon_money_migration import MigrationReader, _snapshot, build_manifest, initialize, statement_bindings
from .drive_run_state import StateError
from .private_state_bindings import unwrap
from .projection_store import DriveProjectionStore


BACKUP_VARIABLE = "KAKEIBO_MIGRATION_BACKUP_ID"
OPERATIONS = ("key-info", "inspect", "initialize", "prepare-daily", "inspect-receipts")
SAFE_ERRORS = frozenset({
    "compact_category_snapshot_changed", "compact_category_reference_requires_mapping",
    "compact_category_metadata_reference_requires_mapping", "compact_category_workflow_markers_invalid",
    "compact_category_helper_not_owned", "compact_category_input_readback_changed",
    "compact_category_readback_failed", "compact_category_validation_readback_failed",
    "daily_sheet_contract_changed", "daily_source_binding_mismatch",
    "daily_sharing_mismatch", "daily_output_readback_failed",
    "migration_validated_main_required", "migration_writer_freeze_required",
    "migration_mode_must_be_inactive", "migration_backup_required",
    "migration_backup_sharing_mismatch", "migration_backup_snapshot_mismatch",
    "migration_backup_permissions_unavailable",
    "migration_backup_attestation_invalid",
    "migration_commitment_required", "migration_commitment_mismatch",
    "migration_money_initialization_required", "migration_daily_binding_required",
    "money_migration_snapshot_changed", "money_migration_manifest_conflict",
    "money_migration_book_exists", "money_migration_already_posted",
    "money_migration_refund_identity_required", "private_binding_decode_failed",
    "projection_precreated_file_required", "projection_folder_sharing_mismatch",
    "projection_file_sharing_mismatch", "projection_file_binding_mismatch",
    "projection_file_invalid", "projection_drive_read_failed", "projection_drive_write_unknown",
    "projection_state_changed", "projection_readback_failed",
})


def failure_report(exc):
    """Expose static code locations, never exception text or financial values."""
    code = str(exc)
    report = {"success": False, "error": code if code in SAFE_ERRORS else "ledger_daily_migration_failed"}
    trace = exc.__traceback__
    app_dir = Path(__file__).resolve().parent
    while trace is not None:
        path = Path(trace.tb_frame.f_code.co_filename).resolve()
        if path.parent == app_dir:
            report["failure_location"] = f"{path.name}:{trace.tb_lineno}"
        trace = trace.tb_next
    from googleapiclient.errors import HttpError
    if isinstance(exc, HttpError):
        report["http_status"] = int(exc.resp.status)
    return report


class MigrationReadPacer:
    """One read budget shared by source, backup and daily during migration."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep, self.next_read = clock, sleep, 0.0

    def __call__(self):
        delay = self.next_read - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.next_read = self.clock() + 1.1


def prepare_daily(store, reader, backup_reader, source, daily, env, writer_email,
                  *, cutover_day, expected_commitment):
    """Explicit category/protection migration; reuses existing tested builders."""
    from .compact_categories import CompactCategoryMigration, compact_helper, category_rows
    from .daily_edit_cutover import cutover_requests, verify_cutover
    from .daily_runtime import refresh_daily
    from .monthly_projection_sheets import SheetsLedgerReader
    from .projection_refresh import ProjectionRefresh, load_catalog

    if not re.fullmatch(r"[0-9a-f]{64}", expected_commitment):
        raise StateError("migration_commitment_required")
    report = migrate(store, reader, backup_reader, cutover_day=cutover_day,
                     expected_commitment=expected_commitment)
    if store.read("money-migration") is None or store.read("money") is None:
        raise StateError("migration_money_initialization_required")
    if daily is None:
        raise StateError("migration_daily_binding_required")
    daily.verify()
    projection = ProjectionRefresh(store, SheetsLedgerReader(source))
    counts = projection.bootstrap(source.categories())
    catalog = load_catalog(store.read("catalog"))
    categories = CompactCategoryMigration(source)
    snapshot = categories.snapshot()
    if not compact_helper(snapshot["metadata"]):
        from .compact_categories import migration_plan
        counts.update(categories.apply(migration_plan(snapshot, catalog), catalog))
    elif snapshot.get("helper_values") != category_rows(catalog):
        raise StateError("compact_category_readback_failed")
    # Fresh native metadata, after category migration, before protecting edits.
    def metadata():
        return source._execute_sheet_read(lambda: source.svc.spreadsheets().get(
            spreadsheetId=source.sid, includeGridData=False))
    owner_email = getattr(daily, "source_owner_email", "")
    requests = cutover_requests(metadata(), daily.db.sid, writer_email, owner_email=owner_email)
    if requests:
        source.svc.spreadsheets().batchUpdate(spreadsheetId=source.sid,
            body={"requests": requests}).execute(num_retries=0)
        source._invalidate_sheet_metadata()
    verify_cutover(metadata(), daily.db.sid, writer_email, owner_email=owner_email)
    counts["daily_cutover_write_requests"] = int(bool(requests))
    counts.update(refresh_daily(source, store, env))
    # Recheck all financial IDs/values and the independent backup after writes.
    migrate(store, reader, backup_reader, cutover_day=cutover_day,
            expected_commitment=expected_commitment)
    return {"manifest_sha256": report["manifest_sha256"], "counts": {
        **counts, "expense_rows_written": 0, "import_rows_written": 0,
        "projection_drive_reads": store.metrics["reads"],
        "projection_drive_writes": store.metrics["writes"]}}


def validate_boundary(env, head, expected_sha, operation):
    if (operation not in OPERATIONS or not re.fullmatch(r"[0-9a-f]{40}", expected_sha)
            or env.get("GITHUB_ACTIONS") != "true"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_REF") != "refs/heads/main"
            or env.get("GITHUB_REPOSITORY") != "tmoriuchi1401-source/kakeibo-ai"
            or head != expected_sha or env.get("GITHUB_SHA") != head
            or env.get("KAKEIBO_VALIDATED_MAIN_SHA") != head
            or env.get("RUNNER_DEBUG") == "1"):
        raise StateError("migration_validated_main_required")
    if operation == "key-info":
        return
    if (env.get("KAKEIBO_PRODUCTION_ENABLED") == "true"
            or env.get("KAKEIBO_SCHEDULE_ENABLED") == "true"
            or env.get("KAKEIBO_LEGACY_DISABLED") != "true"):
        raise StateError("migration_writer_freeze_required")
    if env.get("KAKEIBO_AMAZON_MONEY_MODE") or env.get("KAKEIBO_DAILY_CORRECTIONS_MODE"):
        raise StateError("migration_mode_must_be_inactive")


def public_key_fingerprint(private_key_pem):
    """Match Google's published certificate locally; no key bytes in output."""
    key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    public = key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(public).hexdigest()


def verify_backup_access(service, source_id, backup_id, *, reader_email="", owner_attestation=""):
    if not source_id or not backup_id or backup_id == source_id:
        raise StateError("migration_backup_required")
    source = service.files().get(fileId=source_id, supportsAllDrives=True,
        fields="owners(emailAddress),permissions(id,type,role,emailAddress)").execute(num_retries=0)
    backup = service.files().get(fileId=backup_id, supportsAllDrives=True,
        fields="mimeType,trashed,owners(emailAddress),permissionIds,permissions(id,type)").execute(num_retries=0)
    allowed = {p["id"] for p in source.get("permissions", []) if p.get("type") in {"user", "group"}}
    grants = backup.get("permissions")
    # Permission IDs identify the same principals across files. Match every ID
    # to a user/group verified on the source; unknown/public IDs cannot pass.
    # Unlike the full ACL, files.permissionIds does not require canShare.
    if grants is None and "permissionIds" in backup:
        ids = backup["permissionIds"]
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in allowed for i in ids):
            raise StateError("migration_backup_sharing_mismatch")
        grants = [{"id": i, "type": "user"} for i in ids]
    # files.permissions is omitted when the caller cannot share the file.
    # Keep backup access read-only and retrieve its ACL through permissions.list.
    if grants is None:
        grants, token, seen = [], None, set()
        while True:
            try:
                page = service.permissions().list(fileId=backup_id, supportsAllDrives=True,
                    pageSize=100, pageToken=token,
                    fields="nextPageToken,permissions(id,type)").execute(num_retries=0)
            except Exception:
                if any(p.get("type") not in {"user", "group"} or p.get("id") not in allowed for p in grants):
                    raise StateError("migration_backup_sharing_mismatch") from None
                if not owner_attestation:
                    raise StateError("migration_backup_permissions_unavailable") from None
                # A Drive reader can be denied every ACL endpoint. The manual
                # operator then supplies a fresh owner-side ACL observation,
                # bound to this source, backup and exactly owner + SA reader.
                # This never authorizes a share or grants additional access.
                try:
                    proof = json.loads(owner_attestation)
                    owners = [p["emailAddress"] for p in source["owners"]]
                    if (len(owners) != 1 or not reader_email or reader_email == owners[0]
                            or [p["emailAddress"] for p in backup["owners"]] != owners
                            or not any(p.get("emailAddress") == reader_email and p.get("type") == "user"
                                and p.get("role") == "writer" for p in source["permissions"])
                            or set(proof) != {"sha256", "verified_at"}
                            or type(proof["verified_at"]) is not int
                            or not 0 <= time.time() - proof["verified_at"] <= 900):
                        raise ValueError()
                    expected = digest({"source": source_id, "backup": backup_id, "permissions": sorted([
                        ["user", "owner", owners[0]], ["user", "reader", reader_email]])})
                    if proof["sha256"] != expected:
                        raise ValueError()
                except Exception:
                    raise StateError("migration_backup_attestation_invalid") from None
                grants = [{"id": p["id"], "type": "user"} for p in source["permissions"]
                          if p.get("emailAddress") in {owners[0], reader_email}]
                break
            grants.extend(page.get("permissions", []))
            token = page.get("nextPageToken")
            if not token:
                break
            if token in seen:
                raise StateError("migration_backup_permissions_unavailable")
            seen.add(token)
    if (backup.get("mimeType") != "application/vnd.google-apps.spreadsheet" or backup.get("trashed")
            or not grants or any(p.get("type") not in {"user", "group"}
                                 or p.get("id") not in allowed for p in grants)):
        raise StateError("migration_backup_sharing_mismatch")


def migrate(store, reader, backup_reader, *, cutover_day, expected_commitment="", apply=False):
    """Compare both full finance tables before even the migration-intent write."""
    if apply and not re.fullmatch(r"[0-9a-f]{64}", expected_commitment):
        raise StateError("migration_commitment_required")
    raw, backup = reader(), backup_reader()
    if not backup.get("spreadsheet_id") or backup["spreadsheet_id"] == raw["spreadsheet_id"]:
        raise StateError("migration_backup_required")
    source_snapshot = _snapshot(raw)
    if _snapshot(dict(backup, spreadsheet_id=raw["spreadsheet_id"])) != source_snapshot:
        raise StateError("migration_backup_snapshot_mismatch")
    bindings = statement_bindings(raw)
    manifest = build_manifest(raw, cutover_day=cutover_day, bindings=bindings)
    # Bind the exact private backup too; only this irreversible digest is logged.
    commitment = digest({"manifest": manifest, "backup_binding": digest(backup["spreadsheet_id"])})
    if expected_commitment and expected_commitment != commitment:
        raise StateError("migration_commitment_mismatch")
    previous, current = store.read("money-migration"), store.read("money")
    if previous is not None and previous != manifest:
        raise MoneyError("money_migration_manifest_conflict")
    if current is not None and (previous is None or current != manifest["book"]):
        raise MoneyError("money_migration_book_exists")
    counts = {"money_initialized": 0, "expense_rows_written": 0, "import_rows_written": 0}
    if apply:
        counts = initialize(store, reader, manifest, cutover_day=cutover_day, bindings=bindings)
    # Manual edits can occur outside the workflow lock. Detect late changes;
    # never undo a saved intent or overwrite the canonical ledger on failure.
    if _snapshot(reader()) != source_snapshot:
        raise MoneyError("money_migration_snapshot_changed")
    final_backup = backup_reader()
    if (final_backup.get("spreadsheet_id") != backup["spreadsheet_id"]
            or _snapshot(dict(final_backup, spreadsheet_id=raw["spreadsheet_id"])) != source_snapshot):
        raise StateError("migration_backup_snapshot_mismatch")
    return {"manifest_sha256": commitment, "counts": counts}


def run(env, *, operation, cutover_day="", expected_commitment="", encrypted_backup="", owner_attestation=""):
    from .settings import service_account_source
    path, info = service_account_source()
    info = info or json.loads(Path(path).read_bytes())
    key = info["private_key"]
    if operation == "key-info":
        return {"public_key_sha256": public_key_fingerprint(key)}
    if not encrypted_backup:
        raise StateError("migration_backup_required")
    backup_id = unwrap(BACKUP_VARIABLE, encrypted_backup, key)
    folder_id = unwrap("KAKEIBO_PROJECTION_FOLDER_ID", env.get("KAKEIBO_PROJECTION_FOLDER_ID", ""), key)
    from .google_clients import drive_service, read_only_drive_service, read_only_sheets_service
    from .sheets import SheetsDB
    service = drive_service() if operation in {"initialize", "prepare-daily"} else read_only_drive_service()
    sid = env.get("SPREADSHEET_ID", "")
    verify_backup_access(service, sid, backup_id, reader_email=info.get("client_email", ""),
                         owner_attestation=owner_attestation)
    store = DriveProjectionStore(service, folder_id, sid, precreated=True)
    # Same existing SA, but read-only Sheets credentials for both modes.
    sheets = read_only_sheets_service()
    pacer = MigrationReadPacer()
    def db_for(target, service=None):
        return SheetsDB(target, service=service, read_pacer=pacer, read_retry_base=20)
    reader = MigrationReader(db_for(sid, sheets))
    backup_reader = MigrationReader(db_for(backup_id, sheets))
    if operation == "inspect-receipts":
        from .receipt_recovery_audit import run as audit_receipts
        migrate(store, reader, backup_reader, cutover_day=cutover_day,
                expected_commitment=expected_commitment)
        report = audit_receipts(env, key, service, db_for(sid, sheets), reader)
        migrate(store, reader, backup_reader, cutover_day=cutover_day,
                expected_commitment=expected_commitment)
        return report
    if operation == "prepare-daily":
        from .daily_runtime import daily_from_environment
        from .projection_cache import RollingProjectionStore
        # The backup remains on the read-only service. Only source UI and the
        # separately bound daily copy use writable Sheets credentials.
        source = db_for(sid)
        rolling = RollingProjectionStore(store)
        daily = daily_from_environment(source, rolling, env)
        if daily is not None:
            daily.db._read_pacer = pacer
            daily.db._read_retry_base = 20
        return prepare_daily(rolling, reader, backup_reader, source, daily, env,
            info["client_email"], cutover_day=cutover_day,
            expected_commitment=expected_commitment)
    return migrate(store, reader, backup_reader, cutover_day=cutover_day,
                   expected_commitment=expected_commitment, apply=operation == "initialize")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        env = dict(os.environ)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1], text=True).strip()
        validate_boundary(env, head, args.expected_sha, args.operation)
        result = run(env, operation=args.operation, cutover_day=env.get("MIGRATION_CUTOVER_DAY", ""),
            expected_commitment=env.get("MIGRATION_MANIFEST_SHA256", ""),
            encrypted_backup=env.get("MIGRATION_BACKUP", ""),
            owner_attestation=env.get("MIGRATION_BACKUP_ACL", ""))
        report = {"success": True, "operation": args.operation, **result}
    except Exception as exc:
        report = failure_report(exc)
    print(json.dumps(report, sort_keys=True))
    if not report["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
