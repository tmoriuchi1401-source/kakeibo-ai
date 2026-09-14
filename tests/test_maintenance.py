from datetime import datetime, timezone

from app.maintenance import backup_spreadsheet, cleanup_processed_receipts


FOLDER_ID = "1AbCdEfGhIjKlMnOp"


class Call:
    def __init__(self, result=None):
        self.result = result or {}

    def execute(self):
        return self.result


class FakeFiles:
    def __init__(self, listings):
        self.listings = list(listings)
        self.copies = []
        self.deletes = []

    def list(self, **kwargs):
        return Call(self.listings.pop(0))

    def copy(self, **kwargs):
        self.copies.append(kwargs)
        return Call({"id": "backup-id", "name": kwargs["body"]["name"]})

    def delete(self, **kwargs):
        self.deletes.append(kwargs)
        return Call()


class FakeDrive:
    def __init__(self, listings):
        self.resource = FakeFiles(listings)

    def files(self):
        return self.resource


def test_monthly_backup_is_idempotent():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    drive = FakeDrive([{"files": []}])
    result = backup_spreadsheet("sheet-id", FOLDER_ID, now=now, service=drive)
    assert result["status"] == "created"
    assert result["name"] == "kakeibo-backup-2026-08"
    assert drive.resource.copies[0]["body"]["parents"] == [FOLDER_ID]

    existing = FakeDrive([{"files": [{"id": "existing-id"}]}])
    result = backup_spreadsheet("sheet-id", FOLDER_ID, now=now, service=existing)
    assert result["status"] == "unchanged"
    assert existing.resource.copies == []


def test_cleanup_preview_selects_only_proven_normal_receipts_older_than_retention():
    drive = FakeDrive([{"files": [
        {"id": "old-image", "name": "old.jpg", "mimeType": "image/jpeg",
         "createdTime": "2020-01-01T00:00:00Z", "modifiedTime": "2025-08-17T00:00:00Z",
         "appProperties": {"kakeiboReceiptClass": "normal", "kakeiboProcessedAt": "2025-08-17T00:00:00Z"}},
        {"id": "new-pdf", "name": "new.pdf", "mimeType": "application/pdf",
         "createdTime": "2020-01-01T00:00:00Z",
         "appProperties": {"kakeiboProcessedAt": "2026-01-01T00:00:00+00:00"}},
        {"id": "old-text", "name": "memo.txt", "mimeType": "text/plain",
         "createdTime": "2020-01-01T00:00:00Z", "modifiedTime": "2020-01-01T00:00:00Z"},
    ]}])
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    result = cleanup_processed_receipts(
        FOLDER_ID, now=now, service=drive,
    )
    assert result == {"mode": "preview", "retention_days": 365, "expired": 1, "deleted": 0}
    assert drive.resource.deletes == []


def test_cleanup_preview_never_deletes():
    drive = FakeDrive([{"files": [{
        "id": "old-image", "name": "old.jpg", "mimeType": "image/jpeg",
        "createdTime": "2020-01-01T00:00:00Z", "modifiedTime": "2020-01-01T00:00:00Z",
    }]}])
    result = cleanup_processed_receipts(
        FOLDER_ID, now=datetime(2026, 8, 18, tzinfo=timezone.utc), service=drive,
    )
    assert result["expired"] == 0  # Legacy provenance is not sufficient.
    assert result["deleted"] == 0
    assert drive.resource.deletes == []


def test_sensitive_and_unclassified_originals_are_never_retention_candidates():
    files = [{"id": kind, "mimeType": "application/pdf", "appProperties": {
        "kakeiboReceiptClass": kind, "kakeiboProcessedAt": "2020-01-01T00:00:00Z",
    }} for kind in ("medical", "payroll", "sensitive_unknown", "")]
    drive = FakeDrive([{"files": files}])
    result = cleanup_processed_receipts(FOLDER_ID, service=drive)
    assert result["expired"] == result["deleted"] == 0
    assert drive.resource.deletes == []
