from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .drive_receipts import is_supported_receipt_mime, normalize_folder_id
from .google_clients import drive_service

JST = timezone(timedelta(hours=9))
DRIVE_BACKUP_SCOPE = "https://www.googleapis.com/auth/drive"


def authorize_drive_backup(client_secret_file: str, token_output_file: str) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secret_file, scopes=[DRIVE_BACKUP_SCOPE],
    )
    credentials = flow.run_local_server(
        host="localhost", port=0,
        authorization_prompt_message="次のURLをブラウザで開いてDriveアクセスを許可してください:\n{url}",
        success_message="認証が完了しました。このブラウザ画面を閉じてください。",
        open_browser=True,
    )
    with open(token_output_file, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())


def backup_drive_service(token_json: str):
    credentials = Credentials.from_authorized_user_info(
        json.loads(token_json), scopes=[DRIVE_BACKUP_SCOPE],
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _drive_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def backup_spreadsheet(spreadsheet_id: str, folder_id: str,
                       now: datetime | None = None, service=None) -> dict:
    folder_id = normalize_folder_id(folder_id)
    now = now or datetime.now(JST)
    name = f"kakeibo-backup-{now.astimezone(JST):%Y-%m}"
    svc = service or drive_service()
    escaped = name.replace("'", "\\'")
    query = (
        f"'{folder_id}' in parents and trashed=false and name='{escaped}' "
        "and mimeType='application/vnd.google-apps.spreadsheet'"
    )
    existing = svc.files().list(
        q=query, fields="files(id,name,createdTime)", pageSize=1,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])
    if existing:
        return {"status": "unchanged", "name": name, "file_id": existing[0]["id"]}
    copied = svc.files().copy(
        fileId=spreadsheet_id,
        body={"name": name, "parents": [folder_id]},
        fields="id,name,createdTime",
        supportsAllDrives=True,
    ).execute()
    return {"status": "created", "name": copied["name"], "file_id": copied["id"]}


def list_expired_receipts(folder_id: str, *, retention_days: int = 365,
                          now: datetime | None = None, service=None) -> list[dict]:
    folder_id = normalize_folder_id(folder_id)
    now = now or datetime.now(timezone.utc)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=retention_days)
    svc = service or drive_service()
    query = f"'{folder_id}' in parents and trashed=false"
    files = []
    token = None
    while True:
        response = svc.files().list(
            q=query,
            fields="nextPageToken,files(id,name,mimeType,createdTime,modifiedTime,appProperties)",
            pageSize=1000, pageToken=token, orderBy="createdTime",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            break
    expired = []
    for item in files:
        if not is_supported_receipt_mime(item.get("mimeType", "")):
            continue
        properties = item.get("appProperties", {})
        archived_at = properties.get("kakeiboProcessedAt")
        reference = archived_at or item.get("modifiedTime") or item.get("createdTime")
        if reference and _drive_time(reference) <= cutoff:
            expired.append(item)
    return expired


def cleanup_processed_receipts(folder_id: str, *, apply: bool = False,
                               retention_days: int = 365,
                               now: datetime | None = None, service=None) -> dict:
    svc = service or drive_service()
    expired = list_expired_receipts(
        folder_id, retention_days=retention_days, now=now, service=svc,
    )
    if apply:
        for item in expired:
            svc.files().delete(fileId=item["id"], supportsAllDrives=True).execute()
    return {
        "mode": "apply" if apply else "preview",
        "retention_days": retention_days,
        "expired": len(expired),
        "deleted": len(expired) if apply else 0,
    }
