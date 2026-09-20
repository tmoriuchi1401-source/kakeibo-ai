"""Read-only evidence for an operator reconciling a stopped receipt stage.

No OCR, AI, original-file download, write, or pending release. Existing private
bindings select exactly the confirmation file and production ledger.
"""
import hashlib
import json

from .amazon_money import digest
from .amazon_money_migration import _snapshot
from .drive_run_state import DriveStateTransport, StateBinding, StateError
from .private_state_bindings import unwrap
from .production_ledger import ProductionLedger
from .receipt_confirmation import ReceiptConfirmation
from .receipt_reimport_production import ReimportStore, ResultBinding


def audit_rows(raw, receipt_rows, confirmation, ledger):
    def unique(rows):
        result = {}
        for row in rows:
            if not any(v not in (None, "") for v in row):
                continue
            if not row[0] or row[0] in result:
                raise StateError("receipt_audit_identity_invalid")
            result[row[0]] = list(row)
        return result
    receipts = unique(receipt_rows)
    imports = unique(raw["imports"])
    expenses = unique(raw["expenses"])
    markers = {key: row for key, row in imports.items() if key.startswith("receipt:")}
    missing = sum(not key.startswith("R-") or "receipt:" + key[2:] not in markers for key in receipts)
    missing += sum("R-" + key[8:] not in receipts for key in markers)
    orphan = 0
    for row in expenses.values():
        if len(row) > 10 and row[8] == "receipt" and row[0].startswith("R-"):
            orphan += int(row[9] not in receipts or row[10] not in markers
                          or row[10] != "receipt:" + str(row[9])[2:])
    pending = sum(item["status"] == "pending" for item in confirmation.items.values())
    requested = sum(item["phase"] == "requested" for item in confirmation.store.value["records"].values())
    counts = {"receipt_rows": len(receipts), "receipt_markers": len(markers),
        "receipt_missing_counterparts": missing, "receipt_orphan_parts": orphan,
        "confirmation_pending": pending, "analysis_requested": requested,
        "production_receipts_pending": int(ledger.value["sources"]["receipts"]["phase"] == "pending")}
    evidence = digest({"finance": _snapshot(raw), "receipts": receipts,
        "confirmation": hashlib.sha256(confirmation.store.payload).hexdigest(),
        "ledger": hashlib.sha256(ledger.payload).hexdigest(), "counts": counts})
    return {"counts": counts, "evidence_sha256": evidence,
            "ledger_sha256": hashlib.sha256(ledger.payload).hexdigest()}


def run(env, key, service, db, reader):
    sid = db.sid
    folder = unwrap("KAKEIBO_STATE_FOLDER_ID", env.get("KAKEIBO_STATE_FOLDER_ID", ""), key)
    ledger_id = unwrap("KAKEIBO_RUN_LEDGER_FILE_ID", env.get("KAKEIBO_RUN_LEDGER_FILE_ID", ""), key)
    config = json.loads(env.get("RECEIPT_CONFIRMATION_BINDING", ""))
    confirmation_id = unwrap("RECEIPT_REIMPORT_FILE_ID", config["file"], key)
    if confirmation_id == ledger_id:
        raise StateError("confirmation_store_must_be_separate")
    store = ReimportStore(DriveStateTransport(service, ResultBinding(folder, confirmation_id)), config["manifest"], sid)
    confirmation = ReceiptConfirmation(store, db, lambda *args: None)
    binding = StateBinding("production_run", sid, folder, ledger_id)
    ledger = ProductionLedger(DriveStateTransport(service, binding), binding)
    meta = db._execute_sheet_read(lambda: db.svc.spreadsheets().get(spreadsheetId=sid,
        fields="sheets(properties(title,gridProperties(rowCount)))"))
    extent = [s["properties"]["gridProperties"]["rowCount"] for s in meta["sheets"]
              if s["properties"]["title"] == "レシート"]
    if len(extent) != 1:
        raise StateError("receipt_audit_sheet_missing")
    rows = []
    for first in range(2, extent[0] + 1, 2000):
        rows.extend(db.get_raw(f"'レシート'!A{first}:I{min(extent[0], first + 1999)}"))
    report = audit_rows(reader(), rows, confirmation, ledger)
    if store.transport.read() != store.payload or ledger.transport.read() != ledger.payload:
        raise StateError("receipt_audit_state_changed")
    return report
