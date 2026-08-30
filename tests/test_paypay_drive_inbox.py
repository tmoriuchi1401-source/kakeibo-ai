import sys

import pytest

from app import cli
from app.paypay_drive_inbox import PayPayDriveInboxPreview


HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)


def csv_bytes(*dates):
    rows = "".join(
        f"{day} 12:00,100,,,,,,支払い,店,PayPay残高,一回払い,本人,TX-{i}\n"
        for i, day in enumerate(dates)
    )
    return (HEADER + rows).encode("utf-8-sig")


class Request:
    def __init__(self, result): self.result = result
    def execute(self): return self.result


class FakeFiles:
    def __init__(self, files): self.items = files
    def list(self, **kwargs): return Request({"files": self.items})


class FakeDrive:
    def __init__(self, files): self.resource = FakeFiles(files)
    def files(self): return self.resource


def drive_file(file_id, name):
    return {"id": file_id, "name": name, "mimeType": "text/csv"}


def preview(files, payloads):
    return PayPayDriveInboxPreview(
        "1234567890folder", service=FakeDrive(files),
        downloader=lambda file_id: payloads[file_id],
    ).preview()


def test_valid_filename_is_only_an_unconfirmed_candidate():
    result = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv")],
        {"f1": csv_bytes("2026/08/10")},
    )
    item = result["files"][0]
    assert item["drive_file_id"] == "f1"
    assert item["filename"] == "Transactions_20260801-20260831.csv"
    assert item["csv_sha256"]
    assert item["parse_status"] == "success"
    assert item["row_count"] == 1
    assert item["transaction_min_date"] == "2026-08-10"
    assert item["transaction_max_date"] == "2026-08-10"
    assert item["filename_candidate_start"] == "2026-08-01"
    assert item["filename_candidate_end"] == "2026-08-31"
    assert item["operational_coverage"] == "needs_confirmation"
    assert item["reason"] == "filename_range_requires_confirmation"


@pytest.mark.parametrize("name", [
    "paypay.csv",
    "Transactions_20260230-20260831.csv",
])
def test_missing_or_invalid_filename_candidate_needs_confirmation(name):
    item = preview([drive_file("f1", name)], {"f1": csv_bytes("2026/08/10")})["files"][0]
    assert item["filename_candidate_start"] is None
    assert item["filename_candidate_end"] is None
    assert item["operational_coverage"] == "needs_confirmation"
    assert item["reason"] == "requested_range_missing"


def test_empty_csv_is_previewed_without_rejection():
    item = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv")],
        {"f1": csv_bytes()},
    )["files"][0]
    assert item["row_count"] == 0
    assert item["transaction_min_date"] is None
    assert item["transaction_max_date"] is None
    assert item["operational_coverage"] == "needs_confirmation"


def test_malformed_csv_is_rejected_but_still_previewed():
    result = preview([drive_file("bad", "bad.csv")], {"bad": b"name,amount\nfoo,1\n"})
    item = result["files"][0]
    assert item["parse_status"] == "failed"
    assert item["operational_coverage"] == "rejected"
    assert item["reason"] == "parse_error"


def test_same_csv_reshare_is_sha_duplicate():
    payload = csv_bytes("2026/08/10")
    result = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv"),
         drive_file("f2", "copy.csv")],
        {"f1": payload, "f2": payload},
    )
    assert result["duplicate_count"] == 1
    assert result["conflict_count"] == 0
    assert result["files"][1]["reason"] == "duplicate_operational_evidence"


def test_different_csvs_and_same_unconfirmed_range_are_not_conflicts():
    name = "Transactions_20260801-20260831.csv"
    result = preview(
        [drive_file("f1", name), drive_file("f2", name)],
        {"f1": csv_bytes("2026/08/10"), "f2": csv_bytes("2026/08/11")},
    )
    assert result["duplicate_count"] == 0
    assert result["conflict_count"] == 0
    assert all(item["operational_coverage"] == "needs_confirmation"
               for item in result["files"])


def test_completeness_is_never_claimed():
    result = preview([drive_file("f1", "paypay.csv")], {"f1": csv_bytes()})
    assert result["completion_status"] == "unknown"
    assert result["completeness_proven"] is False
    assert result["files"][0]["completion_status"] == "unknown"
    assert result["files"][0]["completeness_proven"] is False


def test_cli_uses_drive_inbox_preview(monkeypatch, capsys):
    expected = {"read_only": True}
    monkeypatch.setattr(sys, "argv", ["kakeibo", "paypay-drive-inbox-preview"])
    monkeypatch.setattr(cli, "Settings", lambda: type("S", (), {
        "paypay_drive_folder_id": "1234567890folder",
        "validate": lambda self, **kwargs: None,
    })())
    monkeypatch.setattr(cli, "PayPayDriveInboxPreview", lambda folder: type(
        "P", (), {"preview": lambda self: expected},
    )())
    cli.main()
    assert "'read_only': True" in capsys.readouterr().out
