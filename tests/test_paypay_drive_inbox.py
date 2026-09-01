import ast
import sys
from datetime import datetime, timezone

import pytest

from app import cli
from app.coverage_confirmation import (
    COVERAGE_REASON_OPERATIONAL_ONLY,
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    COVERAGE_STATUS_USER_CONFIRMED,
    ConfirmationIdentity,
    CoverageConfirmationIdentityResolution,
    CoverageConfirmationRecord,
    StoredCoverageConfirmation,
    content_sha256,
    coverage_confirmation_id,
    coverage_confirmation_to_sheet_row,
)
from app.paypay_drive_inbox import PayPayDriveInboxPreview


HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)
CONFIRMED_AT = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
CREATED_AT = datetime(2026, 8, 31, 12, 5, tzinfo=timezone.utc)


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


def confirmation(payload, **overrides):
    values = {
        "schema_version": "1",
        "provider": "paypay",
        "content_sha256": content_sha256(payload),
        "confirmed_start": "2026-08-01",
        "confirmed_end": "2026-08-31",
        "range_source": "user_confirmed",
        "confirmed_at": CONFIRMED_AT,
        "confirmation_version": "1",
        "source_filename": "Transactions_20260801-20260831.csv",
        "drive_file_id": None,
    }
    values.update(overrides)
    return CoverageConfirmationRecord(**values)


def exact_resolution(item):
    stored = StoredCoverageConfirmation(
        confirmation_id=coverage_confirmation_id(item.identity),
        record=item,
        coverage_status=COVERAGE_STATUS_USER_CONFIRMED,
        coverage_reason=COVERAGE_REASON_OPERATIONAL_ONLY,
        created_at=CREATED_AT,
    )
    return CoverageConfirmationIdentityResolution(
        "exact_match", "exact_identity_match", stored,
    )


class FakeResolver:
    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def resolve(self, identity):
        self.calls.append(identity)
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(identity)
        return self.response


def preview(files, payloads, *, confirmation_resolver=None, service=None):
    return PayPayDriveInboxPreview(
        "1234567890folder", service=service or FakeDrive(files),
        downloader=lambda file_id: payloads[file_id],
        confirmation_resolver=confirmation_resolver,
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


def test_exact_confirmation_makes_valid_csv_operationally_usable():
    payload = csv_bytes("2026/08/10")
    item = confirmation(payload)
    resolver = FakeResolver(exact_resolution(item))

    result = preview(
        [drive_file("f1", item.source_filename)],
        {"f1": payload},
        confirmation_resolver=resolver,
    )

    evidence = result["files"][0]
    assert evidence["requested_start"] == item.confirmed_start
    assert evidence["requested_end"] == item.confirmed_end
    assert evidence["range_source"] == "user_confirmed"
    assert evidence["range_confirmed"] is True
    assert evidence["operational_coverage"] == "usable"
    assert evidence["reason"] == "operational_checks_passed"
    assert result["usable_count"] == 1
    assert result["completeness_proven"] is False
    assert resolver.calls == [ConfirmationIdentity("paypay", content_sha256(payload))]


def test_exact_confirmation_still_rejects_transaction_outside_range():
    payload = csv_bytes("2026/09/01")
    resolver = FakeResolver(exact_resolution(confirmation(payload)))

    evidence = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv")],
        {"f1": payload},
        confirmation_resolver=resolver,
    )["files"][0]

    assert evidence["operational_coverage"] == "rejected"
    assert evidence["reason"] == "transaction_outside_requested_range"


def test_exact_confirmation_still_rejects_csv_parse_error():
    payload = b"name,amount\nfoo,1\n"
    item = confirmation(payload, source_filename="bad.csv")
    resolver = FakeResolver(exact_resolution(item))

    evidence = preview(
        [drive_file("bad", "bad.csv")],
        {"bad": payload},
        confirmation_resolver=resolver,
    )["files"][0]

    assert evidence["operational_coverage"] == "rejected"
    assert evidence["reason"] == "parse_error"
    assert evidence["parse_status"] == "failed"


def test_exact_confirmation_still_checks_filename_candidate_conflict():
    payload = csv_bytes("2026/07/10")
    item = confirmation(
        payload,
        confirmed_start="2026-07-01",
        confirmed_end="2026-07-31",
    )
    resolver = FakeResolver(exact_resolution(item))

    evidence = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv")],
        {"f1": payload},
        confirmation_resolver=resolver,
    )["files"][0]

    assert evidence["operational_coverage"] == "needs_confirmation"
    assert evidence["reason"] == "confirmed_range_conflicts_with_filename_candidate"


def test_confirmation_not_found_preserves_existing_candidate_behavior():
    payload = csv_bytes("2026/08/10")
    resolver = FakeResolver(CoverageConfirmationIdentityResolution(
        "not_found", "confirmation_not_found",
    ))

    evidence = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv")],
        {"f1": payload},
        confirmation_resolver=resolver,
    )["files"][0]

    assert evidence["operational_coverage"] == "needs_confirmation"
    assert evidence["reason"] == "filename_range_requires_confirmation"


def test_invalid_confirmation_store_fails_closed():
    payload = csv_bytes("2026/08/10")
    resolver = FakeResolver(CoverageConfirmationIdentityResolution(
        "invalid_store", "duplicate_identity",
    ))

    evidence = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv")],
        {"f1": payload},
        confirmation_resolver=resolver,
    )["files"][0]

    assert evidence["operational_coverage"] == "rejected"
    assert evidence["reason"] == "coverage_confirmation_store_invalid"


def test_confirmation_resolver_exception_fails_closed_per_file():
    payload = csv_bytes("2026/08/10")
    resolver = FakeResolver(error=RuntimeError("read failed"))

    result = preview(
        [drive_file("f1", "Transactions_20260801-20260831.csv")],
        {"f1": payload},
        confirmation_resolver=resolver,
    )

    evidence = result["files"][0]
    assert result["files_found"] == 1
    assert evidence["operational_coverage"] == "rejected"
    assert evidence["reason"] == "coverage_confirmation_lookup_failed"


def test_null_drive_file_id_is_irrelevant_to_identity_lookup():
    payload = csv_bytes("2026/08/10")
    item = confirmation(payload, drive_file_id=None)
    resolver = FakeResolver(exact_resolution(item))

    evidence = preview(
        [drive_file("physical-file", item.source_filename)],
        {"physical-file": payload},
        confirmation_resolver=resolver,
    )["files"][0]

    assert item.drive_file_id is None
    assert evidence["operational_coverage"] == "usable"
    assert resolver.calls[0] == item.identity


def test_same_sha_physical_duplicates_reuse_one_confirmation_identity():
    payload = csv_bytes("2026/08/10")
    item = confirmation(payload)
    resolver = FakeResolver(exact_resolution(item))
    name = item.source_filename

    result = preview(
        [drive_file("f1", name), drive_file("f2", name)],
        {"f1": payload, "f2": payload},
        confirmation_resolver=resolver,
    )

    assert result["duplicate_count"] == 1
    assert result["usable_count"] == 2
    assert len(resolver.calls) == 2
    assert resolver.calls == [item.identity, item.identity]
    assert all(file["range_confirmed"] is True for file in result["files"])


def test_wrong_sha_is_not_found_and_does_not_apply_confirmation():
    confirmed_payload = csv_bytes("2026/08/10")
    other_payload = csv_bytes("2026/08/11")
    item = confirmation(confirmed_payload)

    def resolve(identity):
        if identity == item.identity:
            return exact_resolution(item)
        return CoverageConfirmationIdentityResolution(
            "not_found", "confirmation_not_found",
        )

    resolver = FakeResolver(resolve)
    evidence = preview(
        [drive_file("f1", item.source_filename)],
        {"f1": other_payload},
        confirmation_resolver=resolver,
    )["files"][0]

    assert resolver.calls == [
        ConfirmationIdentity("paypay", content_sha256(other_payload)),
    ]
    assert evidence["operational_coverage"] == "needs_confirmation"
    assert evidence["range_confirmed"] is False


def test_resolver_is_called_once_per_csv_with_paypay_identity():
    payload = csv_bytes("2026/08/10")
    item = confirmation(payload)
    resolver = FakeResolver(exact_resolution(item))

    preview(
        [drive_file("f1", item.source_filename)],
        {"f1": payload},
        confirmation_resolver=resolver,
    )

    assert resolver.calls == [ConfirmationIdentity("paypay", item.content_sha256)]


def test_preview_uses_no_drive_or_confirmation_write_api():
    class WriteGuardFiles(FakeFiles):
        def update(self, **kwargs):
            raise AssertionError("Drive write attempted")

        def create(self, **kwargs):
            raise AssertionError("Drive write attempted")

        def delete(self, **kwargs):
            raise AssertionError("Drive write attempted")

    class WriteGuardDrive(FakeDrive):
        def __init__(self, files):
            self.resource = WriteGuardFiles(files)

    class WriteGuardResolver(FakeResolver):
        def append(self, *args, **kwargs):
            raise AssertionError("Sheets write attempted")

        def update(self, *args, **kwargs):
            raise AssertionError("Sheets write attempted")

    payload = csv_bytes("2026/08/10")
    item = confirmation(payload)
    resolver = WriteGuardResolver(exact_resolution(item))

    result = preview(
        [drive_file("f1", item.source_filename)],
        {"f1": payload},
        confirmation_resolver=resolver,
        service=WriteGuardDrive([drive_file("f1", item.source_filename)]),
    )

    assert result["read_only"] is True
    assert result["usable_count"] == 1


def test_cli_without_sheet_setting_preserves_drive_inbox_preview(monkeypatch, capsys):
    expected = {"read_only": True}
    captured = []
    monkeypatch.setattr(sys, "argv", ["kakeibo", "paypay-drive-inbox-preview"])
    monkeypatch.setattr(cli, "Settings", lambda: type("S", (), {
        "paypay_drive_folder_id": "1234567890folder",
        "spreadsheet_id": "",
        "validate": lambda self, **kwargs: None,
    })())
    monkeypatch.setattr(
        cli, "read_only_sheets_service",
        lambda: pytest.fail("Sheets service must not be created"),
    )

    def inbox(folder, *, confirmation_resolver=None):
        captured.append((folder, confirmation_resolver))
        return type("P", (), {"preview": lambda self: expected})()

    monkeypatch.setattr(cli, "PayPayDriveInboxPreview", inbox)
    cli.main()
    assert "'read_only': True" in capsys.readouterr().out
    assert captured == [("1234567890folder", None)]


def test_cli_assembles_read_only_resolver_and_runs_confirmed_preview(
    monkeypatch, capsys,
):
    payload = csv_bytes("2026/08/10")
    item = confirmation(payload)
    stored_row = coverage_confirmation_to_sheet_row(
        item, created_at=CREATED_AT,
    ).to_sheet_row()
    reads = []

    class ReadOnlyCoverageDB:
        def sheet_titles(self):
            reads.append("sheet_titles")
            return [COVERAGE_CONFIRMATION_SHEET]

        def get(self, rng):
            reads.append(rng)
            if rng == f"{COVERAGE_CONFIRMATION_SHEET}!1:1":
                return [list(COVERAGE_CONFIRMATION_HEADERS)]
            if rng == f"{COVERAGE_CONFIRMATION_SHEET}!A2:N":
                return [list(stored_row)]
            raise AssertionError(f"unexpected read: {rng}")

        def append_raw(self, *args, **kwargs):
            raise AssertionError("Sheets write attempted")

        def create_sheet(self, *args, **kwargs):
            raise AssertionError("Sheets write attempted")

    class Settings:
        paypay_drive_folder_id = "1234567890folder"
        spreadsheet_id = "fake-spreadsheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_paypay_drive": True}

    sheets_service = object()
    db = ReadOnlyCoverageDB()
    files = [drive_file("f1", item.source_filename)]
    drive = FakeDrive(files)
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: sheets_service)
    monkeypatch.setattr(
        cli,
        "SheetsDB",
        lambda spreadsheet_id, service=None: db
        if spreadsheet_id == "fake-spreadsheet-id" and service is sheets_service
        else pytest.fail("unexpected SheetsDB construction"),
    )

    def inbox(folder, *, confirmation_resolver=None):
        assert folder == "1234567890folder"
        assert confirmation_resolver is not None
        return PayPayDriveInboxPreview(
            folder,
            service=drive,
            downloader=lambda file_id: {"f1": payload}[file_id],
            confirmation_resolver=confirmation_resolver,
        )

    monkeypatch.setattr(cli, "PayPayDriveInboxPreview", inbox)
    monkeypatch.setattr(sys, "argv", ["kakeibo", "paypay-drive-inbox-preview"])

    cli.main()

    result = ast.literal_eval(capsys.readouterr().out)
    assert result["read_only"] is True
    assert result["usable_count"] == 1
    assert result["files"][0]["range_confirmed"] is True
    assert result["files"][0]["requested_start"] == "2026-08-01"
    assert result["files"][0]["requested_end"] == "2026-08-31"
    assert reads == [
        "sheet_titles",
        f"{COVERAGE_CONFIRMATION_SHEET}!1:1",
        f"{COVERAGE_CONFIRMATION_SHEET}!A2:N",
    ]


def test_cli_missing_coverage_credentials_falls_back_without_resolver(
    monkeypatch, capsys,
):
    captured = []

    class Settings:
        paypay_drive_folder_id = "1234567890folder"
        spreadsheet_id = "fake-spreadsheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_paypay_drive": True}

    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(
        cli,
        "read_only_sheets_service",
        lambda: (_ for _ in ()).throw(RuntimeError("credential missing")),
    )

    def inbox(folder, *, confirmation_resolver=None):
        captured.append(confirmation_resolver)
        return type("P", (), {"preview": lambda self: {"read_only": True}})()

    monkeypatch.setattr(cli, "PayPayDriveInboxPreview", inbox)
    monkeypatch.setattr(sys, "argv", ["kakeibo", "paypay-drive-inbox-preview"])

    cli.main()

    assert ast.literal_eval(capsys.readouterr().out) == {"read_only": True}
    assert captured == [None]


def test_manifest_cli_without_paypay_csv_stays_offline(
    monkeypatch, capsys,
):
    expected = {"manifest": "unchanged"}
    monkeypatch.setattr(
        cli,
        "build_paypay_coverage_confirmation_resolver",
        lambda *_args: pytest.fail("PayPay resolver assembly reached manifest"),
    )
    monkeypatch.setattr(
        cli,
        "preview_payment_coverage_manifests",
        lambda **kwargs: expected,
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "payment-coverage-manifest-preview"],
    )

    cli.main()

    assert ast.literal_eval(capsys.readouterr().out) == expected


def test_manifest_cli_reuses_optional_read_only_resolver_factory(
    monkeypatch, capsys,
):
    expected = {"manifest": "confirmed"}
    resolver = object()
    settings = object()
    captured = []
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli, "build_paypay_coverage_confirmation_resolver",
        lambda value: resolver if value is settings else pytest.fail("wrong settings"),
    )

    def preview_manifest(**kwargs):
        captured.append(kwargs)
        return expected

    monkeypatch.setattr(cli, "preview_payment_coverage_manifests", preview_manifest)
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "payment-coverage-manifest-preview",
        "--paypay-csv", "Transactions_20260801-20260831.csv",
    ])

    cli.main()

    assert ast.literal_eval(capsys.readouterr().out) == expected
    assert captured[0]["confirmation_resolver"] is resolver


def test_manifest_cli_missing_sheet_configuration_passes_no_resolver(
    monkeypatch, capsys,
):
    expected = {"manifest": "offline"}
    captured = []
    monkeypatch.setattr(cli, "Settings", lambda: object())
    monkeypatch.setattr(
        cli, "build_paypay_coverage_confirmation_resolver", lambda _settings: None,
    )
    monkeypatch.setattr(
        cli, "preview_payment_coverage_manifests",
        lambda **kwargs: captured.append(kwargs) or expected,
    )
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "payment-coverage-manifest-preview",
        "--paypay-csv", "local.csv",
    ])

    cli.main()

    assert ast.literal_eval(capsys.readouterr().out) == expected
    assert captured[0]["confirmation_resolver"] is None
