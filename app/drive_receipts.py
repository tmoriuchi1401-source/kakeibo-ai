from __future__ import annotations
import re
from datetime import datetime, timezone
from .google_clients import drive_service, download_drive_file
from .receipt_pipeline import ReceiptPipeline
from .medical_receipt_privacy import Classification


def normalize_folder_id(value:str)->str:
    value=(value or "").strip()
    match=re.search(r"/folders/([A-Za-z0-9_-]+)",value)
    if match:value=match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}",value):
        raise ValueError("DriveフォルダIDが不正です。フォルダURLまたはfolders/以降のIDを設定してください")
    return value


def should_archive_result(result: dict) -> bool:
    """Return whether an inbox image was safely recorded and can be archived."""
    status = result.get("status")
    return status in {"imported", "needs_review"} or (
        status == "skipped" and result.get("reason") == "already_imported"
    )


def is_supported_receipt_mime(mime_type: str) -> bool:
    return mime_type.startswith("image/") or mime_type == "application/pdf"


def process_inbox(folder_id:str,pipeline:ReceiptPipeline,processed_folder_id:str="", *,
                  known_source_classification: Classification | None = None, approved_sources=None,
                  progress=None):
    # Optional in-memory diagnostics; never includes source identities or values.
    progress = progress or (lambda stage, result=None: None)
    folder_id=normalize_folder_id(folder_id)
    processed_folder_id=normalize_folder_id(processed_folder_id) if processed_folder_id else ""
    svc=drive_service()
    q=f"'{folder_id}' in parents and trashed=false"
    response=svc.files().list(
        q=q,fields="nextPageToken,files(id,name,mimeType,version,webViewLink,parents,appProperties)",orderBy="createdTime",pageSize=100,
        supportsAllDrives=True,includeItemsFromAllDrives=True,
    ).execute()
    if response.get("nextPageToken"):
        raise RuntimeError("receipt_inbox_collection_incomplete")
    files=response.get("files",[])
    results=[]
    approved=None if approved_sources is None else {x['source_id']:x for x in approved_sources}
    for f in files:
        if not is_supported_receipt_mime(f["mimeType"]): continue
        progress('receipt_preflight')
        if approved is not None:
            if f['id'] not in approved:continue
            entry=approved[f['id']]
            if f.get('version')!=entry['version'] or f['mimeType']!=entry['mime_type']:
                raise RuntimeError('receipt_preflight_source_changed')
            from pathlib import Path
            from hashlib import sha256
            data=Path(entry['path']).read_bytes()
            if sha256(data).hexdigest()!=entry['sha256']:raise RuntimeError('receipt_preflight_content_changed')
        else:
            data=download_drive_file(f["id"])
        source_policy = ({"known_source_classification": known_source_classification}
                         if known_source_classification is not None else {})
        try:
            progress('receipt_processing')
            res=pipeline.process_bytes(data,f["mimeType"],f["id"],f.get("webViewLink",""),**source_policy)
        except Exception as error:
            from google.genai.errors import APIError
            # These errors originate from extraction, before accounting starts.
            # Preserve the inbox for the next scheduled run. Never catch a
            # Sheets/Drive write error or reset an uncertain accounting write.
            if isinstance(error,APIError) and getattr(error,'code',None) in {429,503}:
                remaining=[x for x in files[files.index(f):] if is_supported_receipt_mime(x['mimeType'])
                           and (approved is None or x['id'] in approved)]
                for x in remaining:
                    deferred={'status':'deferred','reason':'ai_temporarily_unavailable'}
                    results.append((x['name'],deferred))
                    progress('receipt_processing', deferred)
                break
            raise
        results.append((f["name"],res))
        progress('receipt_processing', {key: res[key] for key in
            ('status', 'reason', 'medical_shadow_status') if key in res})
        if processed_folder_id and should_archive_result(res):
            progress('receipt_archive')
            prev=",".join(f.get("parents",[]))
            properties = dict(f.get("appProperties", {}))
            # Only a fresh result passed through the normal privacy gate proves
            # provenance. A legacy already-imported marker alone cannot do so.
            if res.get("status") in {"imported", "needs_review"}:
                properties["kakeiboReceiptClass"] = "normal"
            svc.files().update(
                fileId=f["id"],
                addParents=processed_folder_id,
                removeParents=prev,
                body={"appProperties":{
                    **properties,
                    "kakeiboProcessedAt":datetime.now(timezone.utc).isoformat(),
                }},
                fields="id,parents",
                supportsAllDrives=True,
            ).execute()
    return results
