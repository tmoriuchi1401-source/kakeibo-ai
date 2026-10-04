"""Read-only proof that a displayed nonfinancial original reached processed."""
from datetime import datetime
import hashlib
import re

from .bank_review_groups import digest, validate_snapshot
from .bank_review_source import MAX_FILES, MAX_FILE_BYTES
from .bank_review_ui import PURPOSES, checked, validate_rows
from .drive_receipts import normalize_folder_id
from .google_clients import download_drive_file


class BankReviewRetirementReader:
    def __init__(self, drive, inbox_id, processed_id, *, download=None):
        self.drive = drive
        self.inbox_id = normalize_folder_id(inbox_id)
        self.processed_id = normalize_folder_id(processed_id)
        if self.inbox_id == self.processed_id:
            raise ValueError("bank_review_retirement_folder_conflict")
        self.download = download or (lambda file_id: download_drive_file(file_id, service=drive))

    def _metadata(self, file_id):
        try:
            return self.drive.files().get(fileId=file_id,
                fields="id,mimeType,size,modifiedTime,parents,trashed,appProperties",
                supportsAllDrives=True).execute()
        except Exception as error:
            if getattr(getattr(error, "resp", None), "status", None) == 404:
                return None
            raise ValueError("bank_review_retirement_read_failed") from None

    def __call__(self, prior_rows, source):
        validate_rows(prior_rows)
        current_keys = {group["key"] for group in source.groups}
        expected = {}
        for index in range(0, len(prior_rows), 2):
            primary = prior_rows[index]
            if (primary[6] in current_keys or checked(primary[5])
                    or PURPOSES.get(primary[2]) not in {"transfer", "reimbursement", "other_nonwrite"}):
                continue
            proof = validate_snapshot(primary[11], primary[6], primary[10])
            for member in proof["members"]:
                file_id, sha = member["file_id"], member["pdf_sha256"]
                if (not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id)
                        or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                        or file_id in expected and expected[file_id] != sha):
                    raise ValueError("bank_review_retirement_snapshot_invalid")
                expected[file_id] = sha
        if len(expected) > MAX_FILES:
            raise ValueError("bank_review_retirement_file_bound_exceeded")
        verified = {}
        for file_id, sha in expected.items():
            before = self._metadata(file_id)
            if before is None or not self._processed(before, file_id):
                continue
            try:
                data = self.download(file_id)
            except Exception:
                raise ValueError("bank_review_retirement_read_failed") from None
            if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE_BYTES:
                continue
            if hashlib.sha256(data).hexdigest() != sha:
                continue
            after = self._metadata(file_id)
            if before != after:
                raise ValueError("bank_review_retirement_original_changed")
            verified[file_id] = {"pdf_sha256": sha, "fingerprint": digest(after)}
        return verified

    def _processed(self, item, file_id):
        if (item.get("id") != file_id or item.get("mimeType") != "application/pdf"
                or item.get("trashed") or self.processed_id not in item.get("parents", [])
                or self.inbox_id in item.get("parents", [])):
            return False
        size = item.get("size")
        if size is not None and (not str(size).isdigit() or not 0 < int(size) <= MAX_FILE_BYTES):
            return False
        try:
            for value in (item["modifiedTime"], (item.get("appProperties") or {})["kakeiboBankPdfProcessedAt"]):
                if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
                    return False
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        return True
