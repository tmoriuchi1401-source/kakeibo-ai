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
READ_ONLY_SCOPES=[
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]
SHIPPING_BACKFILL_SHEETS_SCOPES=[
    "https://www.googleapis.com/auth/spreadsheets",
]
DRIVE_READ_ONLY_SCOPES=["https://www.googleapis.com/auth/drive.readonly"]

def credentials(scopes=None):
    scopes = scopes or SCOPES
    path, info = service_account_source()
    if info:
        return service_account.Credentials.from_service_account_info(info, scopes=scopes)
    if not path or not os.path.exists(path):
        raise RuntimeError(f"サービスアカウントJSONが見つかりません: {path}")
    return service_account.Credentials.from_service_account_file(path, scopes=scopes)

def sheets_service(): return build("sheets","v4",credentials=credentials(),cache_discovery=False)
def drive_service(): return build("drive","v3",credentials=credentials(),cache_discovery=False)
def read_only_sheets_service():
    return build("sheets", "v4", credentials=credentials(READ_ONLY_SCOPES), cache_discovery=False)
def read_only_drive_service():
    return build("drive", "v3", credentials=credentials(READ_ONLY_SCOPES), cache_discovery=False)
def shipping_backfill_sheets_service():
    return build(
        "sheets", "v4", credentials=credentials(SHIPPING_BACKFILL_SHEETS_SCOPES),
        cache_discovery=False,
    )
def shipping_backfill_drive_service():
    return build(
        "drive", "v3", credentials=credentials(DRIVE_READ_ONLY_SCOPES),
        cache_discovery=False,
    )
def payroll_read_only_drive_service():
    return build(
        "drive", "v3", credentials=credentials(DRIVE_READ_ONLY_SCOPES),
        cache_discovery=False,
    )

def download_drive_file(file_id:str, service=None) -> bytes:
    svc=service or drive_service(); req=svc.files().get_media(fileId=file_id); buf=io.BytesIO(); dl=MediaIoBaseDownload(buf,req)
    done=False
    while not done: _,done=dl.next_chunk()
    return buf.getvalue()
