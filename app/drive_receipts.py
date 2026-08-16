from __future__ import annotations
import mimetypes
import re
from .google_clients import drive_service, download_drive_file
from .receipt_pipeline import ReceiptPipeline

def normalize_folder_id(value:str)->str:
    value=(value or "").strip()
    match=re.search(r"/folders/([A-Za-z0-9_-]+)",value)
    if match:value=match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,}",value):
        raise ValueError("DriveフォルダIDが不正です。フォルダURLまたはfolders/以降のIDを設定してください")
    return value

def process_inbox(folder_id:str,pipeline:ReceiptPipeline,processed_folder_id:str=""):
    folder_id=normalize_folder_id(folder_id)
    processed_folder_id=normalize_folder_id(processed_folder_id) if processed_folder_id else ""
    svc=drive_service()
    q=f"'{folder_id}' in parents and trashed=false"
    files=svc.files().list(
        q=q,fields="files(id,name,mimeType,webViewLink,parents)",orderBy="createdTime",
        supportsAllDrives=True,includeItemsFromAllDrives=True,
    ).execute().get("files",[])
    results=[]
    for f in files:
        if not f["mimeType"].startswith("image/"): continue
        data=download_drive_file(f["id"])
        res=pipeline.process_bytes(data,f["mimeType"],f["id"],f.get("webViewLink",""))
        results.append((f["name"],res))
        if res.get("status")=="imported" and processed_folder_id:
            prev=",".join(f.get("parents",[]))
            svc.files().update(fileId=f["id"],addParents=processed_folder_id,removeParents=prev,fields="id,parents").execute()
    return results
