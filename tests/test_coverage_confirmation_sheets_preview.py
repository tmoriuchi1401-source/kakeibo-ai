import json
import sys
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app import cli
from app.coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    ConfirmationIdentity,
    CoverageConfirmationRecord,
    coverage_confirmation_to_sheet_row,
)
from app.coverage_confirmation_sheets_preview import (
    CoverageConfirmationReadOnlyAdapter,
    CoverageConfirmationReadOnlyResolver,
    preview_coverage_confirmation_sheet,
    verify_coverage_confirmation_schema,
)


HASH = "a" * 64
CONFIRMED_AT = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
CREATED_AT = datetime(2026, 8, 30, 12, 5, tzinfo=timezone.utc)


def record(**overrides):
    values = {
        "schema_version": "1",
        "provider": "paypay",
        "content_sha256": HASH,
        "confirmed_start": "2025-08-20",
        "confirmed_end": "2026-08-20",
        "range_source": "user_confirmed",
        "confirmed_at": CONFIRMED_AT,
        "confirmation_version": "1",
        "source_filename": "Transactions_20250820-20260820.csv",
        "drive_file_id": None,
    }
    values.update(overrides)
    return CoverageConfirmationRecord(**values)


def sheet_row(item=None, *, created_at=CREATED_AT):
    return coverage_confirmation_to_sheet_row(
        item or record(), created_at=created_at,
    ).to_sheet_row()


class ReadOnlyDB:
    def __init__(self, *, sheet_exists=True, header=None, rows=None):
        self.sheet_exists = sheet_exists
        self.header = list(COVERAGE_CONFIRMATION_HEADERS if header is None else header)
        self.rows = list(rows or [])
        self.reads = []

    def sheet_titles(self):
        self.reads.append("sheet_titles")
        return ["取込データ"] + (
            [COVERAGE_CONFIRMATION_SHEET] if self.sheet_exists else []
        )

    def get(self, rng):
        self.reads.append(rng)
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!1:1":
            return [deepcopy(self.header)] if self.header else []
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!A2:N":
            return deepcopy(self.rows)
        raise AssertionError(f"unexpected read: {rng}")

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method used: {name}")
        raise AttributeError(name)


def test_exact_header_is_exact_match():
    result = verify_coverage_confirmation_schema(
        sheet_exists=True,
        actual_headers=COVERAGE_CONFIRMATION_HEADERS,
    )

    assert result.status == "exact_match"
    assert result.actual_headers == COVERAGE_CONFIRMATION_HEADERS
    assert result.missing_headers == []
    assert result.unexpected_headers == []
    assert result.order_mismatch is False


def test_sheet_missing_is_distinct_and_does_not_read_header_or_rows():
    db = ReadOnlyDB(sheet_exists=False)

    result = preview_coverage_confirmation_sheet(db)

    assert result["sheet_exists"] is False
    assert result["schema_status"] == "sheet_missing"
    assert result["existing_row_count"] == 0
    assert db.reads == ["sheet_titles"]


def test_header_missing_is_distinct_and_does_not_read_rows():
    db = ReadOnlyDB(header=[])

    result = preview_coverage_confirmation_sheet(db)

    assert result["sheet_exists"] is True
    assert result["schema_status"] == "header_missing"
    assert result["actual_headers"] == []
    assert result["row_reads_performed"] is False
    assert db.reads == ["sheet_titles", f"{COVERAGE_CONFIRMATION_SHEET}!1:1"]


def test_missing_column_is_diagnosed_without_reading_rows():
    db = ReadOnlyDB(header=COVERAGE_CONFIRMATION_HEADERS[:-1])

    result = preview_coverage_confirmation_sheet(db)

    assert result["schema_status"] == "schema_mismatch"
    assert result["missing_headers"] == ["Created At"]
    assert result["unexpected_headers"] == []
    assert result["order_mismatch"] is False
    assert result["existing_row_count"] is None
    assert result["row_reads_performed"] is False


def test_unexpected_column_is_diagnosed_without_reading_rows():
    db = ReadOnlyDB(header=[*COVERAGE_CONFIRMATION_HEADERS, "Unexpected"])

    result = preview_coverage_confirmation_sheet(db)

    assert result["schema_status"] == "schema_mismatch"
    assert result["missing_headers"] == []
    assert result["unexpected_headers"] == ["Unexpected"]
    assert result["order_mismatch"] is False
    assert result["row_reads_performed"] is False


def test_column_order_mismatch_is_diagnosed_without_reading_rows():
    header = list(COVERAGE_CONFIRMATION_HEADERS)
    header[0], header[1] = header[1], header[0]
    db = ReadOnlyDB(header=header)

    result = preview_coverage_confirmation_sheet(db)

    assert result["schema_status"] == "schema_mismatch"
    assert result["missing_headers"] == []
    assert result["unexpected_headers"] == []
    assert result["order_mismatch"] is True
    assert result["row_reads_performed"] is False


def test_empty_data_rows_have_zero_health_counts():
    db = ReadOnlyDB(rows=[])

    result = preview_coverage_confirmation_sheet(db)

    assert result["schema_status"] == "exact_match"
    assert result["existing_row_count"] == 0
    assert result["valid_row_count"] == 0
    assert result["invalid_row_count"] == 0
    assert result["duplicate_identity_count"] == 0
    assert result["row_reads_performed"] is True


def test_valid_existing_row_is_counted_without_exposing_it():
    private_filename = "private-file-name.csv"
    db = ReadOnlyDB(rows=[sheet_row(record(source_filename=private_filename))])

    result = preview_coverage_confirmation_sheet(db)

    assert result["existing_row_count"] == 1
    assert result["valid_row_count"] == 1
    assert result["invalid_row_count"] == 0
    assert private_filename not in json.dumps(result, ensure_ascii=False)


def test_malformed_existing_row_is_counted_and_marked_unsafe():
    db = ReadOnlyDB(rows=[["malformed"]])

    result = preview_coverage_confirmation_sheet(db)

    assert result["schema_status"] == "exact_match"
    assert result["existing_row_count"] == 1
    assert result["valid_row_count"] == 0
    assert result["invalid_row_count"] == 1
    assert result["diagnostic"] == "unsafe_existing_rows"


def test_duplicate_identity_health_count_reuses_phase_two_a_row_validation():
    first = sheet_row()
    second = sheet_row(
        record(confirmed_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)),
        created_at=datetime(2026, 8, 31, 9, 5, tzinfo=timezone.utc),
    )

    result = preview_coverage_confirmation_sheet(ReadOnlyDB(rows=[first, second]))

    assert result["valid_row_count"] == 2
    assert result["duplicate_identity_count"] == 1
    assert result["identity_conflict_count"] == 0


def test_read_only_adapter_calls_only_title_and_get_methods():
    db = ReadOnlyDB(rows=[sheet_row()])
    adapter = CoverageConfirmationReadOnlyAdapter(db)

    assert adapter.sheet_exists() is True
    assert adapter.header() == COVERAGE_CONFIRMATION_HEADERS
    assert adapter.data_rows() == [sheet_row()]
    assert db.reads == [
        "sheet_titles",
        f"{COVERAGE_CONFIRMATION_SHEET}!1:1",
        f"{COVERAGE_CONFIRMATION_SHEET}!A2:N",
    ]


def test_read_only_resolver_returns_full_exact_record():
    item = record()
    db = ReadOnlyDB(rows=[sheet_row(item)])
    resolver = CoverageConfirmationReadOnlyResolver(
        CoverageConfirmationReadOnlyAdapter(db),
    )

    result = resolver.resolve(item.identity)

    assert result.status == "exact_match"
    assert result.record == item
    assert result.stored_confirmation is not None
    assert result.stored_confirmation.created_at == CREATED_AT
    assert db.reads == [
        "sheet_titles",
        f"{COVERAGE_CONFIRMATION_SHEET}!1:1",
        f"{COVERAGE_CONFIRMATION_SHEET}!A2:N",
    ]


def test_read_only_resolver_sheet_missing_is_safe_not_found():
    db = ReadOnlyDB(sheet_exists=False)
    resolver = CoverageConfirmationReadOnlyResolver(
        CoverageConfirmationReadOnlyAdapter(db),
    )

    result = resolver.resolve(ConfirmationIdentity("paypay", HASH))

    assert result.status == "not_found"
    assert result.diagnostic == "sheet_missing"
    assert result.record is None
    assert db.reads == ["sheet_titles"]


@pytest.mark.parametrize("header", [
    [],
    ["wrong"],
    COVERAGE_CONFIRMATION_HEADERS[:-1],
])
def test_read_only_resolver_invalid_schema_fails_closed_without_row_read(header):
    db = ReadOnlyDB(header=header, rows=[sheet_row()])
    resolver = CoverageConfirmationReadOnlyResolver(
        CoverageConfirmationReadOnlyAdapter(db),
    )

    result = resolver.resolve(ConfirmationIdentity("paypay", HASH))

    assert result.status == "invalid_store"
    assert result.record is None
    assert f"{COVERAGE_CONFIRMATION_SHEET}!A2:N" not in db.reads


@pytest.mark.parametrize("failing_read,diagnostic", [
    ("sheet_titles", "sheet_titles_read_failed"),
    (f"{COVERAGE_CONFIRMATION_SHEET}!1:1", "header_read_failed"),
    (f"{COVERAGE_CONFIRMATION_SHEET}!A2:N", "data_rows_read_failed"),
])
def test_read_only_resolver_read_error_fails_closed(failing_read, diagnostic):
    class ReadErrorDB(ReadOnlyDB):
        def sheet_titles(self):
            if failing_read == "sheet_titles":
                raise RuntimeError("read failed")
            return super().sheet_titles()

        def get(self, rng):
            if failing_read == rng:
                raise RuntimeError("read failed")
            return super().get(rng)

    resolver = CoverageConfirmationReadOnlyResolver(
        CoverageConfirmationReadOnlyAdapter(ReadErrorDB(rows=[sheet_row()])),
    )

    result = resolver.resolve(ConfirmationIdentity("paypay", HASH))

    assert result.status == "invalid_store"
    assert result.diagnostic == diagnostic
    assert result.record is None


def test_read_only_resolver_never_uses_write_api():
    class WriteGuardDB(ReadOnlyDB):
        def append(self, *args, **kwargs):
            raise AssertionError("write attempted")

        def update(self, *args, **kwargs):
            raise AssertionError("write attempted")

        def clear(self, *args, **kwargs):
            raise AssertionError("write attempted")

        def ensure_sheet(self, *args, **kwargs):
            raise AssertionError("write attempted")

    item = record()
    db = WriteGuardDB(rows=[sheet_row(item)])
    resolver = CoverageConfirmationReadOnlyResolver(
        CoverageConfirmationReadOnlyAdapter(db),
    )

    result = resolver.resolve(item.identity)

    assert result.status == "exact_match"
    assert result.record == item
    assert db.reads == [
        "sheet_titles",
        f"{COVERAGE_CONFIRMATION_SHEET}!1:1",
        f"{COVERAGE_CONFIRMATION_SHEET}!A2:N",
    ]


def test_lookup_integration_preserves_phase_two_a_exact_duplicate():
    item = record()

    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[sheet_row(item)]), record=item, created_at=CREATED_AT,
    )

    assert result["lookup_integration"] == {
        "action": "skip",
        "append_planned": False,
        "duplicate_skip_planned": True,
        "diagnostic": "exact_identity_and_range_duplicate",
        "lookup_status": "exact_duplicate",
    }


def test_lookup_integration_preserves_phase_two_a_identity_conflict():
    candidate = record(confirmed_start="2025-08-21")

    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[sheet_row()]), record=candidate, created_at=CREATED_AT,
    )

    assert result["lookup_integration"]["lookup_status"] == "identity_conflict"
    assert result["lookup_integration"]["action"] == "skip"
    assert result["lookup_integration"]["append_planned"] is False


def test_schema_mismatch_never_advances_to_append_preview():
    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(header=["wrong"], rows=[sheet_row()]),
        record=record(),
        created_at=CREATED_AT,
    )

    assert result["schema_status"] == "schema_mismatch"
    assert result["lookup_integration"] is None
    assert result["row_reads_performed"] is False


def test_malformed_row_fails_closed_in_lookup_integration():
    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[["malformed"]]),
        record=record(),
        created_at=CREATED_AT,
    )

    assert result["invalid_row_count"] == 1
    assert result["lookup_integration"]["lookup_status"] == "invalid"
    assert result["lookup_integration"]["action"] == "skip"
    assert result["lookup_integration"]["append_planned"] is False


def test_cli_uses_existing_read_only_sheets_path_and_prints_safe_json(
    monkeypatch, capsys,
):
    service = object()
    db = object()

    class Settings:
        spreadsheet_id = "private-sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: service)
    monkeypatch.setattr(
        cli,
        "SheetsDB",
        lambda spreadsheet_id, service=None: db
        if spreadsheet_id == "private-sheet-id" and service is not None else None,
    )
    monkeypatch.setattr(
        cli,
        "preview_coverage_confirmation_sheet",
        lambda value: {
            "sheet_name": COVERAGE_CONFIRMATION_SHEET,
            "schema_status": "sheet_missing",
            "external_write": False,
        } if value is db else {},
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "coverage-confirmation-sheet-preview"],
    )

    cli.main()

    assert json.loads(capsys.readouterr().out) == {
        "sheet_name": COVERAGE_CONFIRMATION_SHEET,
        "schema_status": "sheet_missing",
        "external_write": False,
    }
