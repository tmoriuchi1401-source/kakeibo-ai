"""Private, replaceable JSON projection files in a pinned existing Drive folder.

The production Workflow owns exclusion. Compare/readback detects stale callers;
it is not a distributed lock. Never retry writes with an unknown response.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import re

from .monthly_projection import ProjectionError


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def replace_document(store, key, before, after):
    if before == after:
        return
    if store.read(key) != before:
        raise ProjectionError("projection_state_changed")
    store.write(key, after)
    if store.read(key) != after:
        raise ProjectionError("projection_readback_failed")


class DriveProjectionStore:
    def __init__(self, service, folder_id: str, spreadsheet_id: str):
        if not folder_id or not spreadsheet_id:
            raise ProjectionError("projection_binding_missing")
        self.service, self.folder_id, self.spreadsheet_id = service, folder_id, spreadsheet_id
        self.binding = hashlib.sha256(spreadsheet_id.encode()).hexdigest()
        self.metrics = {"reads": 0, "writes": 0, "bytes_read": 0, "bytes_written": 0}

    def _check_destination(self):
        self.metrics["reads"] += 2
        folder = self.service.files().get(fileId=self.folder_id, supportsAllDrives=True,
            fields="mimeType,trashed,capabilities(canAddChildren),permissions(id,type,role)").execute(num_retries=0)
        source = self.service.files().get(fileId=self.spreadsheet_id, supportsAllDrives=True,
            fields="permissions(id,type,role)").execute(num_retries=0)
        allowed = {p["id"] for p in source.get("permissions", []) if p.get("type") in {"user", "group"}}
        grants = folder.get("permissions", [])
        if (folder.get("mimeType") != "application/vnd.google-apps.folder" or folder.get("trashed")
                or not folder.get("capabilities", {}).get("canAddChildren") or not grants
                or any(p.get("type") not in {"user", "group"} or p.get("id") not in allowed for p in grants)):
            raise ProjectionError("projection_folder_sharing_mismatch")
        return allowed

    def _name(self, key):
        if not re.fullmatch(r"catalog|index|journal|summary|corrections|money|month-\d{4}-\d{2}", key):
            raise ProjectionError("projection_key_invalid")
        return f"kakeibo-projection-{key}.json"

    def _file(self, key):
        name = self._name(key)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.folder_id):
            raise ProjectionError("projection_binding_invalid")
        self.metrics["reads"] += 1
        result = self.service.files().list(
            q=f"'{self.folder_id}' in parents and trashed=false and name='{name}'",
            fields="nextPageToken,files(id,mimeType,appProperties)", pageSize=2,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute(num_retries=0)
        files = result.get("files", [])
        if result.get("nextPageToken") or len(files) > 1:
            raise ProjectionError("projection_duplicate_file")
        if files and (files[0].get("mimeType") != "application/json" or
                      files[0].get("appProperties", {}).get("projection_source") != self.binding):
            raise ProjectionError("projection_file_binding_mismatch")
        return files[0]["id"] if files else None

    def read(self, key):
        try:
            file_id = self._file(key)
            if file_id is None:
                return None
            self.metrics["reads"] += 1
            payload = self.service.files().get_media(fileId=file_id, supportsAllDrives=True).execute(num_retries=0)
            self.metrics["bytes_read"] += len(payload)
            if len(payload) > 64 * 1024 * 1024:
                raise ProjectionError("projection_file_too_large")
            value = json.loads(payload)
            if (set(value) != {"schema", "binding", "key", "data"} or value["schema"] != 1
                    or value["binding"] != self.binding or value["key"] != key
                    or not isinstance(value["data"], dict)):
                raise ProjectionError("projection_file_invalid")
            return value["data"]
        except ProjectionError:
            raise
        except Exception:
            raise ProjectionError("projection_drive_read_failed") from None

    def write(self, key, value):
        from googleapiclient.http import MediaIoBaseUpload
        try:
            file_id = self._file(key)
            allowed = self._check_destination()
            if file_id:
                self.metrics["reads"] += 1
                existing = self.service.files().get(fileId=file_id,supportsAllDrives=True,
                    fields="permissions(id,type)").execute(num_retries=0)
                grants=existing.get("permissions",[])
                if not grants or any(p.get("type") not in {"user","group"} or p.get("id") not in allowed for p in grants):
                    raise ProjectionError("projection_file_sharing_mismatch")
            payload = encode({"schema": 1, "binding": self.binding, "key": key, "data": value})
            if len(payload) > 64 * 1024 * 1024:
                raise ProjectionError("projection_file_too_large")
            upload = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False)
            self.metrics["writes"] += 1
            self.metrics["bytes_written"] += len(payload)
            if file_id:
                self.service.files().update(fileId=file_id, media_body=upload,
                                            fields="id", supportsAllDrives=True).execute(num_retries=0)
            else:
                self.service.files().create(body={"name": self._name(key), "parents": [self.folder_id],
                    "mimeType": "application/json", "appProperties": {"projection_source": self.binding}},
                    media_body=upload, fields="id", supportsAllDrives=True).execute(num_retries=0)
        except ProjectionError:
            raise
        except Exception:
            raise ProjectionError("projection_drive_write_unknown") from None


def merge_ranges(ranges):
    merged = []
    for first, last in sorted(ranges):
        if type(first) is not int or type(last) is not int or first < 2 or last < first:
            raise ProjectionError("projection_range_invalid")
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(last, merged[-1][1])
        else:
            merged.append([first, last])
    return merged


def empty_journal():
    return {"generation": 0, "ranges": [], "append": False, "months": []}


class ProjectionJournal:
    """Small outstanding invalidations; cleared only after all derived saves."""
    def __init__(self, store):
        self.store = store

    def read(self):
        from .monthly_projection import month_key
        try:
            value = self.store.read("journal")
            if (not isinstance(value, dict) or set(value) != {"generation", "ranges", "append", "months"}
                    or type(value["generation"]) is not int or value["generation"] < 0
                    or type(value["append"]) is not bool
                    or merge_ranges(value["ranges"]) != value["ranges"]
                    or sorted(set(value["months"])) != value["months"]):
                raise ValueError
            for month in value["months"]:
                month_key(month)
            return value
        except ProjectionError:
            raise
        except Exception:
            raise ProjectionError("projection_journal_invalid") from None

    def mark(self, ranges=(), *, append=False):
        before = self.read()
        after = deepcopy(before)
        after["ranges"] = merge_ranges([*before["ranges"], *ranges])
        after["append"] = before["append"] or append
        if after == before:
            return
        after["generation"] += 1
        replace_document(self.store, "journal", before, after)


def store_from_environment(spreadsheet_id, env=None):
    """Use the existing SA credential; fixed folder ID is encrypted in Actions."""
    import os
    from pathlib import Path
    from .google_clients import drive_service
    from .private_state_bindings import unwrap
    from .settings import service_account_source
    env = os.environ if env is None else env
    value = env.get("KAKEIBO_PROJECTION_FOLDER_ID", "")
    if not value:
        return None
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REF") != "refs/heads/main"
            or env.get("GITHUB_REPOSITORY") != "tmoriuchi1401-source/kakeibo-ai"
            or not env.get("KAKEIBO_VALIDATED_MAIN_SHA")
            or env.get("GITHUB_SHA") != env.get("KAKEIBO_VALIDATED_MAIN_SHA")):
        raise ProjectionError("projection_validated_main_required")
    path, info = service_account_source()
    info = info or json.loads(Path(path).read_bytes())
    folder_id = unwrap("KAKEIBO_PROJECTION_FOLDER_ID", value, info["private_key"])
    return DriveProjectionStore(drive_service(), folder_id, spreadsheet_id)
