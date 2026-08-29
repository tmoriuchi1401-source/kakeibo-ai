from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import tempfile

from .drive_receipts import normalize_folder_id
from .google_clients import download_drive_file, payroll_read_only_drive_service
from .payroll_ocr import EncryptedPayrollPdfError
from .payroll_statement_parser import preview_payroll_file


SUPPORTED = {
    ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image",
}


@dataclass(frozen=True)
class PayrollSource:
    drive_file_id: str
    content_sha256: str


@contextmanager
def temporary_payroll_file(data: bytes, suffix: str):
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            path = Path(handle.name)
            handle.write(data)
        yield path
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _suffix(file: dict) -> str | None:
    suffix = Path(str(file.get("name", ""))).suffix.lower()
    mime = str(file.get("mimeType", "")).lower()
    if suffix in SUPPORTED:
        return suffix
    return {
        "application/pdf": ".pdf", "image/png": ".png",
        "image/jpeg": ".jpg",
    }.get(mime)


class DrivePayrollPreview:
    """Read-only Drive adapter. It has no Sheets dependency or Drive mutation path."""

    def __init__(self, folder_id: str, *, service=None, downloader=None, parser=None):
        self.folder_id = normalize_folder_id(folder_id)
        self.service = service or payroll_read_only_drive_service()
        self.downloader = downloader or (
            lambda file_id: download_drive_file(file_id, service=self.service)
        )
        self.parser = parser or preview_payroll_file
        self.sources: list[PayrollSource] = []

    def _files(self) -> list[dict]:
        query = f"'{self.folder_id}' in parents and trashed=false"
        return self.service.files().list(
            q=query, fields="files(id,name,mimeType)", orderBy="createdTime",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get("files", [])

    @staticmethod
    def _empty_detail(file_type: str, status: str, *, source_id: bool,
                      content_hash: bool) -> dict:
        return {
            "source_id_present": source_id, "content_hash_present": content_hash,
            "file_type": file_type, "company_present": False,
            "pay_period": None, "pay_date": None,
            "gross_pay": None, "total_deductions": None, "net_pay": None,
            "item_count": 0, "value_resolved_count": 0, "value_none_count": 0,
            "unknown_count": 0, "needs_review_count": 0, "parse_status": status,
        }

    @staticmethod
    def _public_detail(result, source: PayrollSource) -> dict:
        items = result.items
        return {
            "source_id_present": bool(source.drive_file_id),
            "content_hash_present": bool(source.content_sha256),
            "file_type": result.file_type,
            "company_present": result.company_present,
            "pay_period": result.pay_period,
            "pay_date": result.pay_date,
            "gross_pay": result.gross_pay,
            "total_deductions": result.total_deductions,
            "net_pay": result.net_pay,
            "item_count": len(items),
            "value_resolved_count": sum(item.value is not None for item in items),
            "value_none_count": sum(item.value is None for item in items),
            "unknown_count": sum(item.standard_item_candidate is None for item in items),
            "needs_review_count": sum(item.needs_review for item in items),
            "parse_status": result.parse_status,
        }

    def preview(self) -> dict:
        files = self._files()
        details = []
        parsed = unsupported = errors = 0
        for file in files:
            suffix = _suffix(file)
            source = None
            if suffix is None:
                unsupported += 1
                details.append(self._empty_detail(
                    "unsupported", "unsupported", source_id=bool(file.get("id")),
                    content_hash=False,
                ))
                continue
            try:
                data = self.downloader(file["id"])
                source = PayrollSource(file["id"], hashlib.sha256(data).hexdigest())
                self.sources.append(source)
                with temporary_payroll_file(data, suffix) as path:
                    result = self.parser(path)
                parsed += 1
                details.append(self._public_detail(result, source))
            except EncryptedPayrollPdfError:
                unsupported += 1
                details.append(self._empty_detail(
                    "pdf", "unsupported", source_id=bool(file.get("id")),
                    content_hash=source is not None,
                ))
            except Exception:
                errors += 1
                details.append(self._empty_detail(
                    SUPPORTED.get(suffix, "unsupported"), "error",
                    source_id=bool(file.get("id")), content_hash=source is not None,
                ))
        return {
            "files_found": len(files), "parsed": parsed,
            "payroll_detected": parsed,
            "success": sum(item.get("parse_status") == "success" for item in details),
            "needs_review": sum(item.get("needs_review_count", 0) > 0 or
                                item.get("parse_status") == "partial" for item in details),
            "unsupported": unsupported, "errors": errors, "files": details,
        }
