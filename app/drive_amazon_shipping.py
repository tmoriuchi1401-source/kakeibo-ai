from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile

from .amazon_shipping import AmazonShippingBackfillPipeline
from .drive_receipts import normalize_folder_id
from .google_clients import download_drive_file


def _is_csv(file: dict) -> bool:
    return str(file.get("name", "")).lower().endswith(".csv")


def select_latest_csv(files: list[dict]) -> dict:
    candidates = [file for file in files if _is_csv(file)]
    if not candidates:
        raise RuntimeError("Google DriveフォルダにAmazon Order HistoryのCSVがありません")
    return max(candidates, key=lambda file: (
        file.get("modifiedTime", ""), file.get("createdTime", ""),
        file.get("name", ""), file.get("id", ""),
    ))


@contextmanager
def _temporary_csv(data: bytes):
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as handle:
            path = handle.name
            handle.write(data)
        yield path
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


class DriveAmazonShippingPipeline:
    def __init__(self, folder_id: str, db, service, downloader=None):
        self.folder_id = normalize_folder_id(folder_id)
        self.db = db
        self.service = service
        self.downloader = downloader or (
            lambda file_id: download_drive_file(file_id, service=self.service)
        )

    def _files(self) -> list[dict]:
        query = f"'{self.folder_id}' in parents and trashed=false"
        return self.service.files().list(
            q=query,
            fields="files(id,name,mimeType,modifiedTime,createdTime)",
            orderBy="modifiedTime desc,createdTime desc,name",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])

    def preview(self) -> dict:
        selected = select_latest_csv(self._files())
        with _temporary_csv(self.downloader(selected["id"])) as path:
            summary = AmazonShippingBackfillPipeline(self.db).preview(path)
        return {"csv_file": selected["name"], **summary}

    def apply(self) -> dict:
        selected = select_latest_csv(self._files())
        with _temporary_csv(self.downloader(selected["id"])) as path:
            summary = AmazonShippingBackfillPipeline(self.db).apply(path)
        return {"csv_file": selected["name"], **summary}


DriveAmazonShippingPreview = DriveAmazonShippingPipeline
