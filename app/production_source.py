"""Receipt/PayPay source adapters with count-only output for the parent runner."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .cli import make_receipt_pipeline
from .drive_paypay import DrivePayPayPipeline
from .drive_receipts import is_supported_receipt_mime, normalize_folder_id, process_inbox
from .google_clients import download_drive_file, read_only_drive_service, read_only_sheets_service
from .receipt_privacy_gate import evaluate_receipt_privacy
from .settings import Settings
from .sheets import SheetsDB


def receipts(settings, *, apply: bool) -> dict:
    settings.validate(need_drive=True, need_sheet=True)
    db = SheetsDB(settings.spreadsheet_id, service=None if apply else read_only_sheets_service())
    counts = {"found": 0, "written": 0, "needs_review": 0, "unchanged": 0, "failure": 0}
    if apply:
        approved=None
        if os.environ.get('RECEIPT_CONFIRMATION_BINDING'):
            if not os.environ.get('RECEIPT_SCAN_PLAN'):raise RuntimeError('receipt_preflight_required')
            approved=json.loads(Path(os.environ['RECEIPT_SCAN_PLAN']).read_bytes())['sources']
        results = process_inbox(
            settings.receipt_drive_folder_id, make_receipt_pipeline(settings, db, None),
            settings.processed_drive_folder_id, **({'approved_sources':approved} if approved is not None else {}),
        )
        for _, result in results:
            counts["found"] += 1
            status = result.get("status")
            if status == "imported":
                counts["written"] += 1
            elif status in {"needs_review", "privacy_blocked"}:
                counts["needs_review"] += 1
            elif status == "skipped" and result.get("reason") == "already_imported":
                counts["unchanged"] += 1
            else:
                counts["failure"] += 1
            if result.get("medical_shadow_status") == "handoff_failed":
                counts["failure"] += 1
        return counts
    # Read-only preview inspects identities and the existing local privacy gate.
    # AI extraction/write counts cannot be promised before the approved AI run.
    service = read_only_drive_service()
    folder = normalize_folder_id(settings.receipt_drive_folder_id)
    response = service.files().list(
        q=f"'{folder}' in parents and trashed=false", pageSize=100,
        fields="nextPageToken,files(id,mimeType)", orderBy="createdTime",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    if response.get("nextPageToken"):
        raise RuntimeError("receipt_inbox_collection_incomplete")
    identities = db.import_ids()
    counts["new_eligible"] = 0
    for file in response.get("files", []):
        if not is_supported_receipt_mime(file["mimeType"]):
            continue
        counts["found"] += 1
        if f"receipt:{file['id']}" in identities:
            counts["unchanged"] += 1
            continue
        gate = evaluate_receipt_privacy(download_drive_file(file["id"], service=service), file["mimeType"])
        if gate.classification == "normal" and gate.gemini_allowed:
            counts["new_eligible"] += 1
        else:
            counts["needs_review"] += 1
    return counts


def paypay(settings, *, apply: bool) -> dict:
    settings.validate(need_paypay_drive=True, need_sheet=True)
    if apply:
        result = DrivePayPayPipeline(settings.paypay_drive_folder_id, SheetsDB(settings.spreadsheet_id),
                                     settings.processed_drive_folder_id).apply()
        return {key: result[key] for key in ("imported_files", "skipped_files", "failed_files")}
    service = read_only_drive_service()
    result = DrivePayPayPipeline(
        settings.paypay_drive_folder_id, service=service,
        downloader=lambda file_id: download_drive_file(file_id, service=service),
    ).preview()
    return {"found": result["target_csvs"], "new_eligible": result["processable_csvs"],
            "needs_review": sum(not file["processable"] and file["skip_reason"] not in {"CSV以外", "処理済み"}
                                for file in result["files"]), "written": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=("receipts", "paypay"))
    parser.add_argument("mode", choices=("preview", "apply"))
    args = parser.parse_args()
    result = {"failure": 1}
    try:
        result = {"receipts": receipts, "paypay": paypay}[args.source](Settings(), apply=args.mode == "apply")
    except Exception:
        pass  # Raw API errors/OCR values are never emitted to Actions logs.
    print(json.dumps(result, sort_keys=True))
    if result.get("failure") or result.get("failed_files"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
