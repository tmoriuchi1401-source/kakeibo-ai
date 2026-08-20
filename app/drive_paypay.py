from __future__ import annotations

from datetime import datetime, timezone
import tempfile

from .drive_receipts import normalize_folder_id
from .google_clients import download_drive_file, drive_service
from .paypay_pipeline import PayPayPipeline


PROCESSED_PROPERTY = "kakeiboPayPayProcessedAt"


def is_csv_file(file: dict) -> bool:
    return str(file.get("name", "")).lower().endswith(".csv")


class DrivePayPayPipeline:
    def __init__(self, folder_id: str, db=None, processed_folder_id: str = "",
                 service=None, downloader=None):
        self.folder_id = normalize_folder_id(folder_id)
        self.processed_folder_id = (
            normalize_folder_id(processed_folder_id) if processed_folder_id else ""
        )
        self.db = db
        self.service = service or drive_service()
        self.downloader = downloader or download_drive_file

    def _files(self) -> list[dict]:
        query = f"'{self.folder_id}' in parents and trashed=false"
        return self.service.files().list(
            q=query,
            fields="files(id,name,mimeType,webViewLink,parents,appProperties)",
            orderBy="createdTime",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute().get("files", [])

    @staticmethod
    def _processed(file: dict) -> bool:
        return bool(file.get("appProperties", {}).get(PROCESSED_PROPERTY))

    def _inspect(self, file: dict) -> tuple[dict, bytes | None]:
        result = {
            "name": file.get("name", ""), "rows": 0, "payments": 0,
            "payment_total": 0, "processable": False, "skip_reason": "",
        }
        if not is_csv_file(file):
            result["skip_reason"] = "CSV以外"
            return result, None
        if self._processed(file):
            result["skip_reason"] = "処理済み"
            return result, None
        try:
            data = self.downloader(file["id"])
            with tempfile.NamedTemporaryFile(suffix=".csv") as handle:
                handle.write(data)
                handle.flush()
                preview = PayPayPipeline().preview(handle.name, sample_limit=0)["summary"]
            result.update({
                "rows": preview["rows"], "payments": preview["payments"],
                "payment_total": preview["payment_total"], "processable": True,
            })
            return result, data
        except Exception as exc:
            result["skip_reason"] = f"PayPay CSVとして読み込めません: {exc}"
            return result, None

    def preview(self) -> dict:
        files = self._files()
        details = [self._inspect(file)[0] for file in files]
        return {
            "target_csvs": sum(is_csv_file(file) for file in files),
            "processable_csvs": sum(item["processable"] for item in details),
            "files": details,
        }

    def _mark_processed(self, file: dict) -> None:
        properties = {
            **file.get("appProperties", {}),
            PROCESSED_PROPERTY: datetime.now(timezone.utc).isoformat(),
        }
        kwargs = {
            "fileId": file["id"], "body": {"appProperties": properties},
            "fields": "id,parents,appProperties", "supportsAllDrives": True,
        }
        if self.processed_folder_id:
            kwargs["addParents"] = self.processed_folder_id
            kwargs["removeParents"] = ",".join(file.get("parents", []))
        self.service.files().update(**kwargs).execute()

    def apply(self) -> dict:
        if self.db is None:
            raise ValueError("drive-paypay applyにはSheetsDBが必要です")
        files = self._files()
        details = []
        for file in files:
            inspected, data = self._inspect(file)
            if not inspected["processable"] or data is None:
                inspected["result"] = (
                    "error" if is_csv_file(file) and not self._processed(file) else "skipped"
                )
                details.append(inspected)
                continue
            try:
                with tempfile.NamedTemporaryFile(suffix=".csv") as handle:
                    handle.write(data)
                    handle.flush()
                    imported = PayPayPipeline(self.db).import_csv(handle.name)
                self._mark_processed(file)
                inspected["result"] = "imported"
                inspected["import"] = imported
            except Exception as exc:
                inspected["result"] = "error"
                inspected["skip_reason"] = f"取込エラー: {exc}"
            details.append(inspected)
        return {
            "target_csvs": sum(is_csv_file(file) for file in files),
            "imported_files": sum(item.get("result") == "imported" for item in details),
            "skipped_files": sum(item.get("result") == "skipped" for item in details),
            "failed_files": sum(item.get("result") == "error" for item in details),
            "files": details,
        }
