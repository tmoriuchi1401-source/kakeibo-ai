from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import tempfile

from .drive_receipts import normalize_folder_id
from .google_clients import download_drive_file, read_only_drive_service
from .paypay_operational_coverage import (
    PayPayOperationalEvidence,
    classify_operational_evidence,
    preview_operational_evidence,
)


@contextmanager
def _temporary_named_csv(data: bytes, filename: str):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / Path(filename).name
        path.write_bytes(data)
        yield path


class PayPayDriveInboxPreview:
    """Read-only preview for CSV files shared directly into a Drive inbox."""

    def __init__(self, folder_id: str, *, service=None, downloader=None):
        self.folder_id = normalize_folder_id(folder_id)
        self.service = service or read_only_drive_service()
        self.downloader = downloader or (
            lambda file_id: download_drive_file(file_id, service=self.service)
        )

    def _files(self) -> list[dict]:
        query = f"'{self.folder_id}' in parents and trashed=false"
        return self.service.files().list(
            q=query, fields="files(id,name,mimeType)", orderBy="createdTime",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get("files", [])

    @staticmethod
    def _is_csv(file: dict) -> bool:
        return str(file.get("name", "")).lower().endswith(".csv")

    def _inspect(self, file: dict) -> PayPayOperationalEvidence:
        filename = str(file.get("name", ""))
        try:
            data = self.downloader(file["id"])
            with _temporary_named_csv(data, filename) as path:
                return preview_operational_evidence(path)
        except Exception as exc:
            return PayPayOperationalEvidence(
                None, None, None, False, filename, None, None,
                None, None, None, None, None,
                "rejected", "csv_unreadable", str(exc),
            )

    def preview(self) -> dict:
        csv_files = [file for file in self._files() if self._is_csv(file)]
        evidences = [self._inspect(file) for file in csv_files]
        classified, duplicates, conflicts = classify_operational_evidence(evidences)
        details = []
        for file, evidence in zip(csv_files, classified):
            detail = asdict(evidence)
            detail.update({
                "drive_file_id": file.get("id"),
                "filename": evidence.csv_filename,
                "parse_status": "failed" if evidence.parse_error else "success",
                "completion_status": "unknown",
                "completeness_proven": False,
            })
            details.append(detail)
        return {
            "read_only": True,
            "completion_status": "unknown",
            "completeness_proven": False,
            "files_found": len(csv_files),
            "duplicate_count": duplicates,
            "conflict_count": conflicts,
            "usable_count": sum(
                item.operational_coverage == "usable" for item in classified
            ),
            "needs_confirmation_count": sum(
                item.operational_coverage == "needs_confirmation" for item in classified
            ),
            "rejected_count": sum(
                item.operational_coverage == "rejected" for item in classified
            ),
            "files": details,
        }
