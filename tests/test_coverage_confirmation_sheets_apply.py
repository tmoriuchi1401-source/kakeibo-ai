from copy import deepcopy
from datetime import datetime, timezone

from app.coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    ConfirmationIdentity,
    CoverageConfirmationRecord,
    coverage_confirmation_id,
    coverage_confirmation_to_sheet_row,
)
from app.coverage_confirmation_sheets_apply import (
    apply_coverage_confirmation_write,
    build_coverage_confirmation_write_plan,
)
from app.coverage_confirmation_sheets_preview import (
    preview_coverage_confirmation_sheet,
)
from app.sheets import SheetsDB


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


class FakeCoverageSheetsDB:
    def __init__(self, *, exists=True, header=None, rows=None):
        self.sid = "fake-spreadsheet-id"
        self.exists = exists
        self.header = list(
            COVERAGE_CONFIRMATION_HEADERS if header is None else header
        ) if exists else []
        self.rows = deepcopy(rows or [])
        self.reads = []
        self.writes = []
        self.data_read_count = 0
        self.inject_row_on_data_read = None
        self.silent_append_failure = False
        self.fail_create = False
        self.fail_header = False
        self.fail_append = False

    def sheet_titles(self):
        self.reads.append("sheet_titles")
        return [COVERAGE_CONFIRMATION_SHEET] if self.exists else []

    def get(self, rng):
        self.reads.append(rng)
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!1:1":
            return [deepcopy(self.header)] if self.header else []
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!A2:N":
            self.data_read_count += 1
            if (
                self.inject_row_on_data_read is not None
                and self.data_read_count == self.inject_row_on_data_read[0]
            ):
                self.rows.append(deepcopy(self.inject_row_on_data_read[1]))
            return deepcopy(self.rows)
        raise AssertionError(f"unexpected range: {rng}")

    def create_sheet(self, title):
        self.writes.append(("create_sheet", title))
        if self.fail_create:
            raise RuntimeError("create failed")
        if self.exists:
            raise RuntimeError("sheet already exists")
        self.exists = True
        self.header = []

    def write_header_raw(self, sheet, header):
        self.writes.append(("write_header", sheet, list(header)))
        if self.fail_header:
            raise RuntimeError("header failed")
        if not self.exists:
            raise RuntimeError("sheet missing")
        self.header = list(header)

    def append_raw(self, sheet, rows):
        copied = deepcopy(rows)
        self.writes.append(("append_raw", sheet, copied))
        if self.fail_append:
            raise RuntimeError("append failed")
        if not self.silent_append_failure:
            self.rows.extend(copied)


def build(db, item=None):
    return build_coverage_confirmation_write_plan(
        db, record=item or record(), created_at=CREATED_AT,
    )


def apply(db, plan, enabled=True):
    return apply_coverage_confirmation_write(db, plan, apply=enabled)


def test_apply_guard_blocks_before_any_write_or_revalidation_read():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    reads_before = list(db.reads)

    result = apply(db, plan, enabled=False)

    assert result["blocked"] is True
    assert result["reason"] == "explicit_apply_required"
    assert result["action_performed"] == ()
    assert result["external_write"] is False
    assert db.writes == []
    assert db.reads == reads_before


def test_sheet_missing_apply_creates_header_and_appends_in_fake_only():
    db = FakeCoverageSheetsDB(exists=False)
    plan = build(db)

    result = apply(db, plan)

    assert [entry[0] for entry in db.writes] == [
        "create_sheet", "write_header", "append_raw",
    ]
    assert db.header == COVERAGE_CONFIRMATION_HEADERS
    assert db.rows == [sheet_row()]
    assert result["action_performed"] == (
        "create_sheet", "write_header", "append_row",
    )
    assert result["created_sheet"] is True
    assert result["wrote_header"] is True
    assert result["appended_row"] is True
    assert result["blocked"] is False
    assert result["external_write"] is True
    assert result["postwrite_status"]["duplicate_status"] == "exact_duplicate"


def test_missing_plan_revalidation_sees_existing_sheet_and_does_not_blind_create():
    db = FakeCoverageSheetsDB(exists=False)
    plan = build(db)
    db.exists = True
    db.header = list(COVERAGE_CONFIRMATION_HEADERS)

    result = apply(db, plan)

    assert [entry[0] for entry in db.writes] == ["append_raw"]
    assert result["created_sheet"] is False
    assert result["wrote_header"] is False
    assert result["appended_row"] is True
    assert result["blocked"] is False


def test_exact_match_not_found_apply_appends_only():
    db = FakeCoverageSheetsDB(rows=[])
    result = apply(db, build(db))

    assert [entry[0] for entry in db.writes] == ["append_raw"]
    assert result["action_requested"] == "append"
    assert result["action_performed"] == ("append_row",)
    assert result["created_sheet"] is False
    assert result["wrote_header"] is False


def test_revalidation_exact_duplicate_is_idempotent_skip_without_write():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.rows.append(sheet_row())

    result = apply(db, plan)

    assert db.writes == []
    assert result["skipped_duplicate"] is True
    assert result["action_performed"] == ("skip_duplicate",)
    assert result["blocked"] is False
    assert result["external_write"] is False


def test_revalidation_identity_conflict_is_blocked():
    candidate = record(confirmed_start="2025-08-21")
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db, candidate)
    db.rows.append(sheet_row())

    result = apply(db, plan)

    assert db.writes == []
    assert result["blocked"] is True
    assert result["reason"] == "identity_range_conflict"
    assert result["prewrite_status"]["duplicate_status"] == "identity_conflict"
    assert result["external_write"] is False


def test_revalidation_schema_mismatch_is_blocked():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.header = ["wrong"]

    result = apply(db, plan)

    assert db.writes == []
    assert result["blocked"] is True
    assert result["prewrite_status"]["schema_status"] == "schema_mismatch"


def test_revalidation_header_missing_is_blocked_and_not_repaired():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.header = []

    result = apply(db, plan)

    assert db.writes == []
    assert result["blocked"] is True
    assert result["prewrite_status"]["schema_status"] == "header_missing"
    assert result["wrote_header"] is False


def test_revalidation_invalid_row_is_blocked():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.rows.append(["malformed"])

    result = apply(db, plan)

    assert db.writes == []
    assert result["blocked"] is True
    assert result["prewrite_status"]["duplicate_status"] == "invalid"
    assert result["external_write"] is False


def test_plan_built_from_existing_header_missing_is_never_auto_repaired():
    db = FakeCoverageSheetsDB(header=[])
    plan = build(db)

    result = apply(db, plan)

    assert plan.blocked is True
    assert db.writes == []
    assert result["blocked"] is True
    assert result["reason"] == "planned_action_was_blocked"


def test_header_write_occurs_only_after_this_apply_created_the_sheet():
    created = FakeCoverageSheetsDB(exists=False)
    existing = FakeCoverageSheetsDB(header=[])

    created_result = apply(created, build(created))
    existing_result = apply(existing, build(existing))

    assert any(entry[0] == "write_header" for entry in created.writes)
    assert not any(entry[0] == "write_header" for entry in existing.writes)
    assert created_result["wrote_header"] is True
    assert existing_result["blocked"] is True


def test_append_immediately_rechecks_duplicate_and_skips_racing_row():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.inject_row_on_data_read = (3, sheet_row())

    result = apply(db, plan)

    assert db.data_read_count >= 3
    assert db.writes == []
    assert result["skipped_duplicate"] is True
    assert result["appended_row"] is False
    assert result["external_write"] is False


def test_postwrite_verification_success_requires_exact_duplicate():
    db = FakeCoverageSheetsDB(rows=[])

    result = apply(db, build(db))

    assert result["blocked"] is False
    assert result["reason"] == "postwrite_verification_passed"
    assert result["postwrite_status"]["schema_status"] == "exact_match"
    assert result["postwrite_status"]["duplicate_status"] == "exact_duplicate"
    assert result["postwrite_status"]["identity_conflict_count"] == 0


def test_postwrite_verification_failure_is_not_reported_as_success():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.silent_append_failure = True

    result = apply(db, plan)

    assert [entry[0] for entry in db.writes] == ["append_raw"]
    assert result["appended_row"] is True
    assert result["external_write"] is True
    assert result["blocked"] is True
    assert result["reason"] == "postwrite_verification_failed"


def test_create_failure_stops_before_header_and_append():
    db = FakeCoverageSheetsDB(exists=False)
    plan = build(db)
    db.fail_create = True

    result = apply(db, plan)

    assert [entry[0] for entry in db.writes] == ["create_sheet"]
    assert result["blocked"] is True
    assert result["reason"] == "sheet_create_failed"
    assert result["external_write"] is True


def test_header_failure_stops_before_append():
    db = FakeCoverageSheetsDB(exists=False)
    plan = build(db)
    db.fail_header = True

    result = apply(db, plan)

    assert [entry[0] for entry in db.writes] == [
        "create_sheet", "write_header",
    ]
    assert result["created_sheet"] is True
    assert result["wrote_header"] is False
    assert result["appended_row"] is False
    assert result["blocked"] is True
    assert result["reason"] == "header_write_failed"
    assert result["external_write"] is True


def test_append_exception_is_blocked_and_external_write_is_true():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.fail_append = True

    result = apply(db, plan)

    assert [entry[0] for entry in db.writes] == ["append_raw"]
    assert result["appended_row"] is False
    assert result["blocked"] is True
    assert result["reason"] == "append_write_failed"
    assert result["external_write"] is True


def test_phase_two_a_identity_id_and_fixed_row_are_reused():
    item = record()
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db, item)

    result = apply(db, plan)

    appended = db.writes[0][2][0]
    assert item.identity == ConfirmationIdentity("paypay", HASH)
    assert appended == sheet_row(item)
    assert appended[0] == coverage_confirmation_id(item.identity)
    assert len(appended) == len(COVERAGE_CONFIRMATION_HEADERS) == 14
    assert result["postwrite_status"]["duplicate_status"] == "exact_duplicate"


def test_external_write_tracks_actual_writer_invocation():
    duplicate_db = FakeCoverageSheetsDB(rows=[sheet_row()])
    blocked_db = FakeCoverageSheetsDB(header=["wrong"])
    append_db = FakeCoverageSheetsDB(rows=[])

    duplicate = apply(duplicate_db, build(duplicate_db))
    blocked = apply(blocked_db, build(blocked_db))
    appended = apply(append_db, build(append_db))

    assert duplicate["external_write"] is False
    assert blocked["external_write"] is False
    assert appended["external_write"] is True
    assert duplicate_db.writes == blocked_db.writes == []
    assert len(append_db.writes) == 1


def test_preview_route_never_accesses_write_methods():
    class PreviewOnlyDB(FakeCoverageSheetsDB):
        def create_sheet(self, title):
            raise AssertionError("preview called create")

        def write_header_raw(self, sheet, header):
            raise AssertionError("preview called header write")

        def append_raw(self, sheet, rows):
            raise AssertionError("preview called append")

    result = preview_coverage_confirmation_sheet(
        PreviewOnlyDB(exists=False), record=record(), created_at=CREATED_AT,
    )

    assert result["action"] == "create_and_append"
    assert result["external_write"] is False


def test_target_spreadsheet_change_is_blocked_before_write():
    db = FakeCoverageSheetsDB(rows=[])
    plan = build(db)
    db.sid = "different-fake-spreadsheet-id"

    result = apply(db, plan)

    assert db.writes == []
    assert result["blocked"] is True
    assert result["reason"] == "target_spreadsheet_changed"
    assert result["prewrite_status"]["spreadsheet_matches"] is False


def test_sheetsdb_safe_primitives_use_fixed_raw_api_shapes():
    calls = []

    class Request:
        def execute(self):
            return {}

    class Values:
        def update(self, **kwargs):
            calls.append(("update", kwargs))
            return Request()

        def append(self, **kwargs):
            calls.append(("append", kwargs))
            return Request()

    class Spreadsheets:
        def batchUpdate(self, **kwargs):
            calls.append(("batchUpdate", kwargs))
            return Request()

        def values(self):
            return Values()

    class Service:
        def spreadsheets(self):
            return Spreadsheets()

    db = SheetsDB("fake-sheet-id", service=Service())
    db.create_sheet(COVERAGE_CONFIRMATION_SHEET)
    db.write_header_raw(COVERAGE_CONFIRMATION_SHEET, COVERAGE_CONFIRMATION_HEADERS)
    db.append_raw(COVERAGE_CONFIRMATION_SHEET, [sheet_row()])

    assert calls[0] == ("batchUpdate", {
        "spreadsheetId": "fake-sheet-id",
        "body": {"requests": [{"addSheet": {"properties": {
            "title": COVERAGE_CONFIRMATION_SHEET,
            "gridProperties": {"frozenRowCount": 1},
        }}}]},
    })
    assert calls[1][0] == "update"
    assert calls[1][1]["range"] == f"{COVERAGE_CONFIRMATION_SHEET}!A1"
    assert calls[1][1]["valueInputOption"] == "RAW"
    assert calls[1][1]["body"] == {"values": [COVERAGE_CONFIRMATION_HEADERS]}
    assert calls[2][0] == "append"
    assert calls[2][1]["range"] == f"{COVERAGE_CONFIRMATION_SHEET}!A:A"
    assert calls[2][1]["valueInputOption"] == "RAW"
    assert calls[2][1]["insertDataOption"] == "INSERT_ROWS"
