from __future__ import annotations
import io, json, os
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from .settings import service_account_source

SCOPES=[
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def credentials():
    path, info = service_account_source()
    if info:
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if not path or not os.path.exists(path):
        raise RuntimeError(f"サービスアカウントJSONが見つかりません: {path}")
    return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)

def sheets_service(): return build("sheets","v4",credentials=credentials(),cache_discovery=False)
def drive_service(): return build("drive","v3",credentials=credentials(),cache_discovery=False)

def download_drive_file(file_id:str) -> bytes:
    svc=drive_service(); req=svc.files().get_media(fileId=file_id); buf=io.BytesIO(); dl=MediaIoBaseDownload(buf,req)
    done=False
    while not done: _,done=dl.next_chunk()
    return buf.getvalue()
