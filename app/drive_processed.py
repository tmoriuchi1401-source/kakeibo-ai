"""Move completed inputs without changing unrelated parents or replaying writes."""
from __future__ import annotations

from .drive_receipts import normalize_folder_id


def validate_processed_folder(service, inbox_id: str, processed_id: str) -> str:
    target = normalize_folder_id(processed_id)
    if target == normalize_folder_id(inbox_id):
        raise ValueError("processed_folder_is_inbox")
    folder = service.files().get(
        fileId=target, fields="id,mimeType,trashed,capabilities(canAddChildren)",
        supportsAllDrives=True,
    ).execute()
    if (folder.get("mimeType") != "application/vnd.google-apps.folder"
            or folder.get("trashed")
            or not folder.get("capabilities", {}).get("canAddChildren")):
        raise RuntimeError("processed_folder_unwritable")
    return target


def move_processed(service, file_id: str, inbox_id: str, processed_id: str,
                   properties: dict) -> None:
    """Re-read membership; remove only the configured inbox; verify the result."""
    current = service.files().get(
        fileId=file_id, fields="id,parents,appProperties,trashed",
        supportsAllDrives=True,
    ).execute()
    parents = current.get("parents", [])
    if current.get("trashed"):
        raise RuntimeError("processed_source_trashed")
    if processed_id in parents and inbox_id not in parents:
        return  # The preceding attempt committed but lost its response.
    if inbox_id not in parents:
        raise RuntimeError("processed_source_parent_changed")
    service.files().update(
        fileId=file_id, addParents=processed_id, removeParents=inbox_id,
        body={"appProperties": {**current.get("appProperties", {}), **properties}},
        fields="id,parents", supportsAllDrives=True,
    ).execute()
    actual = service.files().get(
        fileId=file_id, fields="id,parents,trashed", supportsAllDrives=True,
    ).execute()
    expected = (set(parents) - {inbox_id}) | {processed_id}
    if actual.get("trashed") or set(actual.get("parents", [])) != expected:
        raise RuntimeError("processed_move_readback_failed")
