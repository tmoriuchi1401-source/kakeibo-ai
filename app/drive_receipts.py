from __future__ import annotations
import mimetypes
from .google_clients import drive_service, download_drive_file
from .receipt_pipeline import ReceiptPipeline

def process_inbox(folder_id:str,pipeline:ReceiptPipeline,processed_folder_id:str=""):
    svc=drive_service()
    q=f"'{folder_id}' in parents and trashed=false"
    files=svc.files().list(q=q,fields="files(id,name,mimeType,webViewLink,parents)",orderBy="createdTime").execute().get("files",[])
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
