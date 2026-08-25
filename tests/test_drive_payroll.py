import json
from pathlib import Path

from app.drive_payroll import DrivePayrollPreview
from app.payroll_models import PayrollItem, PayrollPreview
from app.payroll_ocr import EncryptedPayrollPdfError


class Request:
    def __init__(self, result): self.result = result
    def execute(self): return self.result


class ReadOnlyFiles:
    def __init__(self, files):
        self.items = files
        self.list_calls = []
        self.write_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return Request({"files": self.items})

    def update(self, **kwargs):
        self.write_calls.append(("update", kwargs))
        raise AssertionError("Drive write must not be called")

    def delete(self, **kwargs):
        self.write_calls.append(("delete", kwargs))
        raise AssertionError("Drive delete must not be called")

    def create(self, **kwargs):
        self.write_calls.append(("create", kwargs))
        raise AssertionError("Drive create must not be called")


class FakeDrive:
    def __init__(self, files): self.resource = ReadOnlyFiles(files)
    def files(self): return self.resource


def file(file_id, name, mime):
    return {"id": file_id, "name": name, "mimeType": mime}


def parsed(file_type="pdf", review=False):
    return PayrollPreview(
        file_type=file_type, extraction_method="pdf_text" if file_type == "pdf" else "ocr",
        pay_period="2026-08", pay_date=None, gross_pay=300000,
        total_deductions=50000, net_pay=250000, parse_status="success",
        items=[PayrollItem(raw_item_name="独自手当", section="earnings",
                           value=None if review else 10000, needs_review=review)],
    )


def test_multiple_drive_files_preview_is_read_only_and_anonymous(tmp_path):
    files = [
        file("pdf-secret-id", "personal-payroll.pdf", "application/pdf"),
        file("png-secret-id", "screen-person.png", "image/png"),
        file("jpg-secret-id", "photo-person.jpeg", "image/jpeg"),
        file("txt-secret-id", "private-name.txt", "text/plain"),
    ]
    payloads = {item["id"]: item["id"].encode() for item in files[:3]}
    seen_paths = []

    def parser(path):
        seen_paths.append(Path(path))
        return parsed("pdf" if path.suffix == ".pdf" else "image", review=path.suffix == ".png")

    drive = FakeDrive(files)
    pipeline = DrivePayrollPreview("1234567890folder", service=drive,
                                   downloader=lambda file_id: payloads[file_id], parser=parser)
    result = pipeline.preview()

    assert result == {
        "files_found": 4, "parsed": 3, "success": 3, "needs_review": 1,
        "unsupported": 1, "errors": 0, "files": result["files"],
    }
    assert {detail["file_type"] for detail in result["files"]} == {"pdf", "image", "unsupported"}
    assert all(not path.exists() for path in seen_paths)
    assert len(pipeline.sources) == 3
    assert all(source.drive_file_id and len(source.content_sha256) == 64 for source in pipeline.sources)
    public = json.dumps(result, ensure_ascii=False)
    assert "secret-id" not in public
    assert "personal-payroll" not in public
    assert "private-name" not in public
    assert all(detail["source_id_present"] for detail in result["files"])
    assert drive.resource.write_calls == []
    assert "parents" in drive.resource.list_calls[0]["q"]


def test_encrypted_and_failed_file_do_not_stop_batch():
    files = [
        file("encrypted", "locked.pdf", "application/pdf"),
        file("broken", "broken.png", "image/png"),
        file("good", "good.jpg", "image/jpeg"),
    ]
    payloads = {key: key.encode() for key in ("encrypted", "broken", "good")}

    def parser(path):
        marker = path.read_bytes().decode()
        if marker == "encrypted": raise EncryptedPayrollPdfError("locked")
        if marker == "broken": raise ValueError("bad image")
        return parsed("image")

    drive = FakeDrive(files)
    result = DrivePayrollPreview("1234567890folder", service=drive,
                                 downloader=lambda file_id: payloads[file_id], parser=parser).preview()
    assert result["files_found"] == 3
    assert result["parsed"] == 1
    assert result["success"] == 1
    assert result["unsupported"] == 1
    assert result["errors"] == 1
    assert result["files"][0]["parse_status"] == "unsupported"
    assert result["files"][1]["parse_status"] == "error"
    assert result["files"][2]["parse_status"] == "success"
    assert drive.resource.write_calls == []


def test_downloader_failure_still_allows_next_file():
    files = [file("bad", "bad.pdf", "application/pdf"),
             file("good", "good.pdf", "application/pdf")]
    def downloader(file_id):
        if file_id == "bad": raise OSError("download failed")
        return b"good"
    result = DrivePayrollPreview("1234567890folder", service=FakeDrive(files),
                                 downloader=downloader, parser=lambda path: parsed()).preview()
    assert (result["errors"], result["parsed"], result["success"]) == (1, 1, 1)
