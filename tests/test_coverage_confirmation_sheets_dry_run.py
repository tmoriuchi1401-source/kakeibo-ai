from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    ConfirmationIdentity,
    CoverageConfirmationRecord,
    coverage_confirmation_to_sheet_row,
)
from app.coverage_confirmation_sheets_preview import (
    HEADER_PLANNED_RANGE,
    plan_coverage_confirmation_sheet_create,
    plan_coverage_confirmation_sheets_dry_run,
    preview_coverage_confirmation_sheet,
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


def sheet_row(item=None):
    return coverage_confirmation_to_sheet_row(
        item or record(), created_at=CREATED_AT,
    ).to_sheet_row()


class WriteTrap:
    def __getattr__(self, name):
        raise AssertionError(f"external API method accessed: {name}")


class ReadOnlyDB:
    def __init__(self, *, sheet_exists=True, header=None, rows=None):
        self.sheet_exists_value = sheet_exists
        self.header_value = list(
            COVERAGE_CONFIRMATION_HEADERS if header is None else header
        )
        self.rows_value = list(rows or [])
        self.reads = []
        self.svc = WriteTrap()

    def sheet_titles(self):
        self.reads.append("sheet_titles")
        return [COVERAGE_CONFIRMATION_SHEET] if self.sheet_exists_value else []

    def get(self, rng):
        self.reads.append(rng)
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!1:1":
            return [deepcopy(self.header_value)] if self.header_value else []
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!A2:N":
            return deepcopy(self.rows_value)
        raise AssertionError(f"unexpected range: {rng}")

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"Sheets write method called: {name}")
        raise AttributeError(name)


def test_sheet_missing_plans_create_header_and_append_without_write():
    db = ReadOnlyDB(sheet_exists=False)

    result = preview_coverage_confirmation_sheet(
        db, record=record(), created_at=CREATED_AT,
    )

    assert result["sheet_status"] == "sheet_missing"
    assert result["action"] == "create_and_append"
    assert result["create_sheet_planned"] is True
    assert result["header_write_planned"] is True
    assert result["append_row_planned"] is True
    assert result["blocked"] is False
    assert result["external_write"] is False
    assert db.reads == ["sheet_titles"]


def test_sheet_missing_without_confirmation_returns_create_plan_only():
    result = preview_coverage_confirmation_sheet(ReadOnlyDB(sheet_exists=False))

    assert result["action"] == "create_sheet"
    assert result["create_sheet_planned"] is True
    assert result["header_write_planned"] is True
    assert result["header_row"] == COVERAGE_CONFIRMATION_HEADERS
    assert result["append_row_planned"] is False
    assert result["blocked"] is False
    assert result["external_write"] is False


def test_create_plan_uses_fixed_header_and_existing_schema_range_pattern():
    plan = plan_coverage_confirmation_sheet_create("sheet_missing")

    assert plan.sheet_name == COVERAGE_CONFIRMATION_SHEET
    assert plan.sheet_exists is False
    assert plan.create_sheet_required is True
    assert plan.header_write_required is True
    assert plan.expected_headers == COVERAGE_CONFIRMATION_HEADERS
    assert plan.planned_range == HEADER_PLANNED_RANGE == "Coverage確認!A1:N1"
    assert plan.safe_to_create is True
    assert all(value is not None for value in plan.expected_headers)


def test_exact_match_not_found_plans_append_only():
    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[]), record=record(), created_at=CREATED_AT,
    )

    assert result["action"] == "append"
    assert result["create_sheet_planned"] is False
    assert result["header_write_planned"] is False
    assert result["append_row_planned"] is True
    assert result["duplicate_status"] == "not_found"
    assert result["blocked"] is False


def test_exact_duplicate_plans_skip_and_preserves_phase_two_a_semantics():
    item = record()

    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[sheet_row(item)]), record=item, created_at=CREATED_AT,
    )

    assert item.identity == ConfirmationIdentity("paypay", HASH)
    assert result["action"] == "skip_duplicate"
    assert result["append_row_planned"] is False
    assert result["duplicate_status"] == "exact_duplicate"
    assert result["blocked"] is False


def test_identity_conflict_is_blocked_without_changing_duplicate_key():
    candidate = record(confirmed_start="2025-08-21")

    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[sheet_row()]), record=candidate, created_at=CREATED_AT,
    )

    assert candidate.identity == record().identity
    assert result["action"] == "blocked"
    assert result["append_row_planned"] is False
    assert result["duplicate_status"] == "identity_conflict"
    assert result["blocked"] is True


def test_schema_mismatch_is_blocked_without_migration_or_header_rewrite():
    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(header=["wrong"]), record=record(), created_at=CREATED_AT,
    )

    assert result["sheet_status"] == "schema_mismatch"
    assert result["action"] == "blocked"
    assert result["create_sheet_planned"] is False
    assert result["header_write_planned"] is False
    assert result["header_rewrite_planned"] is False
    assert result["migration_planned"] is False
    assert result["append_row_planned"] is False
    assert result["blocked"] is True


def test_existing_sheet_with_missing_header_is_blocked_not_initialized():
    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(header=[]), record=record(), created_at=CREATED_AT,
    )

    assert result["sheet_status"] == "header_missing"
    assert result["action"] == "blocked"
    assert result["create_sheet_planned"] is False
    assert result["header_write_planned"] is False
    assert result["header_row"] is None
    assert result["append_row_planned"] is False
    assert result["blocked"] is True


def test_invalid_sheet_state_is_blocked():
    result = plan_coverage_confirmation_sheets_dry_run("invalid")

    assert result.action == "blocked"
    assert result.create_sheet_planned is False
    assert result.header_write_planned is False
    assert result.append_row_planned is False
    assert result.blocked is True
    assert result.external_write is False


def test_append_row_is_the_fixed_fourteen_column_phase_two_a_row():
    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[]), record=record(), created_at=CREATED_AT,
    )

    assert result["append_row_planned"] is True
    assert result["append_row"] == sheet_row()
    assert len(result["append_row"]) == len(COVERAGE_CONFIRMATION_HEADERS) == 14


def test_create_dry_run_never_accesses_add_sheet_batch_update_or_values_api():
    db = ReadOnlyDB(sheet_exists=False)

    result = preview_coverage_confirmation_sheet(
        db, record=record(), created_at=CREATED_AT,
    )

    assert result["action"] == "create_and_append"
    assert db.svc.__class__ is WriteTrap
    assert db.reads == ["sheet_titles"]


def test_malformed_existing_rows_fail_closed_in_unified_dry_run():
    result = preview_coverage_confirmation_sheet(
        ReadOnlyDB(rows=[["malformed"]]), record=record(), created_at=CREATED_AT,
    )

    assert result["invalid_row_count"] == 1
    assert result["action"] == "blocked"
    assert result["append_row_planned"] is False
    assert result["blocked"] is True


@pytest.mark.parametrize(
    "db,item",
    [
        (ReadOnlyDB(sheet_exists=False), record()),
        (ReadOnlyDB(rows=[]), record()),
        (ReadOnlyDB(rows=[sheet_row()]), record()),
        (ReadOnlyDB(header=["wrong"]), record()),
        (ReadOnlyDB(header=[]), record()),
    ],
)
def test_external_write_is_false_for_every_dry_run_branch(db, item):
    result = preview_coverage_confirmation_sheet(
        db, record=item, created_at=CREATED_AT,
    )

    assert result["external_write"] is False
