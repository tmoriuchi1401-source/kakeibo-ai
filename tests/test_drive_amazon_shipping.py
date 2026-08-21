from pathlib import Path

import pytest

from app.drive_amazon_shipping import DriveAmazonShippingPipeline, select_latest_csv
from app.google_clients import DRIVE_READ_ONLY_SCOPES, SHIPPING_BACKFILL_SHEETS_SCOPES
from app.sheets import SheetsDB


class Request:
    def __init__(self, result): self.result = result
    def execute(self): return self.result


class FakeFiles:
    def __init__(self, files): self.items = files; self.list_args = None
    def list(self, **kwargs):
        self.list_args = kwargs
        return Request({"files": self.items})


class FakeDrive:
    def __init__(self, files): self.resource = FakeFiles(files)
    def files(self): return self.resource


class FakeValues:
    def __init__(self): self.batch = None
    def batchUpdate(self, **kwargs):
        self.batch = kwargs
        return Request({})


class FakeSpreadsheets:
    def __init__(self): self.resource = FakeValues()
    def values(self): return self.resource


class FakeSheets:
    def __init__(self): self.resource = FakeSpreadsheets()
    def spreadsheets(self): return self.resource


class FakeDB:
    def __init__(self): self.updated = []
    def get(self, rng):
        assert rng == "Amazon注文!A2:O"
        return [["ORDER-1|ASIN-1", "ORDER-1", "ASIN-1"]]
    def update_shipping_fields(self, updates): self.updated.extend(updates)


CSV = (
    "Order ID,ASIN,Ship Date,Original Quantity,Carrier Name & Tracking Number\n"
    "ORDER-1,ASIN-1,2026-08-20,1,tracking\n"
).encode()


def drive_file(file_id, name, modified):
    return {"id": file_id, "name": name, "modifiedTime": modified,
            "createdTime": modified, "mimeType": "text/csv"}


def test_latest_csv_is_selected_and_preview_is_read_only():
    files = [
        drive_file("old", "Order History old.csv", "2026-08-20T00:00:00Z"),
        drive_file("new", "Order History latest.CSV", "2026-08-21T00:00:00Z"),
        drive_file("txt", "notes.txt", "2026-08-22T00:00:00Z"),
    ]
    downloaded = []
    preview = DriveAmazonShippingPipeline(
        "1234567890folder", FakeDB(), FakeDrive(files),
        downloader=lambda file_id: downloaded.append(file_id) or CSV,
    ).preview()
    assert downloaded == ["new"]
    assert preview["csv_file"] == "Order History latest.CSV"
    assert preview["csv_rows"] == 1
    assert preview["matched_amazon_rows"] == 1


def test_apply_uses_latest_csv_and_only_shipping_field_update_method():
    db = FakeDB()
    downloaded = []
    result = DriveAmazonShippingPipeline(
        "1234567890folder", db,
        FakeDrive([drive_file("new", "Order History.csv", "2026-08-21T00:00:00Z")]),
        downloader=lambda file_id: downloaded.append(file_id) or CSV,
    ).apply()
    assert downloaded == ["new"]
    assert result["csv_file"] == "Order History.csv"
    assert result["updated_rows"] == 1
    assert len(db.updated) == 1


def test_sheets_update_is_limited_to_ship_date_and_shipment_count_columns():
    service = FakeSheets()
    row = [f"value-{index}" for index in range(15)]
    SheetsDB("sheet-id", service=service).update_shipping_fields([(7, row)])
    assert service.resource.resource.batch == {
        "spreadsheetId": "sheet-id",
        "body": {"valueInputOption": "USER_ENTERED", "data": [{
            "range": "Amazon注文!N7:O7", "values": [["value-13", "value-14"]],
        }]},
    }


def test_apply_credentials_have_only_required_write_scope():
    assert SHIPPING_BACKFILL_SHEETS_SCOPES == [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    assert DRIVE_READ_ONLY_SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]


def test_no_csv_stops_with_clear_error():
    with pytest.raises(RuntimeError, match="Amazon Order HistoryのCSVがありません"):
        select_latest_csv([drive_file("txt", "notes.txt", "2026-08-22T00:00:00Z")])


def test_workflow_uses_required_secrets_and_read_only_command():
    workflow = Path(".github/workflows/amazon-shipping-backfill-preview.yml").read_text()
    assert "AMAZON_ORDER_HISTORY_FOLDER_ID: ${{ secrets.AMAZON_ORDER_HISTORY_FOLDER_ID }}" in workflow
    assert "GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "amazon-shipping-backfill-drive-preview" in workflow


def test_apply_workflow_requires_explicit_confirmation():
    workflow = Path(".github/workflows/amazon-shipping-backfill.yml").read_text()
    assert "confirm == 'APPLY'" in workflow
    assert "amazon-shipping-backfill-drive-apply" in workflow
    assert "GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}" in workflow
