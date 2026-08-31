import json
import sys
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app import cli
from app.coverage_confirmation import (
    COVERAGE_CONFIRMATION_HEADERS,
    COVERAGE_CONFIRMATION_SHEET,
    COVERAGE_REASON_OPERATIONAL_ONLY,
    COVERAGE_STATUS_USER_CONFIRMED,
    CoverageConfirmationRecord,
    coverage_confirmation_id,
    coverage_confirmation_to_sheet_row,
)
from app.coverage_confirmation_cli import (
    CoverageConfirmationInputError,
    load_coverage_confirmation_input,
    run_coverage_confirmation_apply,
    run_coverage_confirmation_preflight,
)


HASH = "a" * 64
CREATED_AT = datetime(2026, 8, 30, 12, 5, tzinfo=timezone.utc)


def payload(**overrides):
    values = {
        "schema_version": "1",
        "provider": "paypay",
        "content_sha256": HASH,
        "confirmed_start": "2025-08-20",
        "confirmed_end": "2026-08-20",
        "range_source": "user_confirmed",
        "coverage_status": COVERAGE_STATUS_USER_CONFIRMED,
        "coverage_reason": COVERAGE_REASON_OPERATIONAL_ONLY,
        "confirmed_at": "2026-08-30T12:00:00+00:00",
        "confirmation_version": "1",
        "source_filename": "private-source-name.csv",
        "drive_file_id": "private-drive-file-id",
        "created_at": "2026-08-30T12:05:00Z",
    }
    values.update(overrides)
    return values


def write_input(tmp_path, **overrides):
    path = tmp_path / "coverage-confirmation.json"
    path.write_text(json.dumps(payload(**overrides)), encoding="utf-8")
    return path


def explicit_input(tmp_path, **overrides):
    return load_coverage_confirmation_input(write_input(tmp_path, **overrides))


def sheet_row(item, *, created_at=CREATED_AT):
    return coverage_confirmation_to_sheet_row(
        item, created_at=created_at,
    ).to_sheet_row()


class FakeDB:
    def __init__(self, *, exists=True, header=None, rows=None):
        self.sid = "fake-spreadsheet-id"
        self.exists = exists
        self.header = list(
            COVERAGE_CONFIRMATION_HEADERS if header is None else header
        ) if exists else []
        self.rows = deepcopy(rows or [])
        self.reads = []
        self.writes = []

    def sheet_titles(self):
        self.reads.append("sheet_titles")
        return [COVERAGE_CONFIRMATION_SHEET] if self.exists else []

    def get(self, rng):
        self.reads.append(rng)
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!1:1":
            return [deepcopy(self.header)] if self.header else []
        if rng == f"{COVERAGE_CONFIRMATION_SHEET}!A2:N":
            return deepcopy(self.rows)
        raise AssertionError(f"unexpected range: {rng}")

    def create_sheet(self, title):
        self.writes.append(("create_sheet", title))
        if self.exists:
            raise RuntimeError("already exists")
        self.exists = True
        self.header = []

    def write_header_raw(self, sheet, header):
        self.writes.append(("write_header", sheet, list(header)))
        self.header = list(header)

    def append_raw(self, sheet, rows):
        copied = deepcopy(rows)
        self.writes.append(("append_raw", sheet, copied))
        self.rows.extend(copied)


def test_valid_explicit_json_reconstructs_phase_one_model(tmp_path):
    loaded = explicit_input(tmp_path)

    assert isinstance(loaded.record, CoverageConfirmationRecord)
    assert loaded.record.provider == "paypay"
    assert loaded.record.content_sha256 == HASH
    assert loaded.record.confirmed_start == "2025-08-20"
    assert loaded.record.confirmed_end == "2026-08-20"
    assert loaded.record.range_source == "user_confirmed"
    assert loaded.record.source_filename == "private-source-name.csv"
    assert loaded.record.drive_file_id == "private-drive-file-id"
    assert loaded.created_at == CREATED_AT


def test_missing_required_input_is_rejected(tmp_path):
    data = payload()
    del data["confirmed_end"]
    path = tmp_path / "missing.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(
        CoverageConfirmationInputError,
        match="invalid_coverage_confirmation_input_fields",
    ):
        load_coverage_confirmation_input(path)


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    text = json.dumps(payload())
    path.write_text(
        text[:-1] + ',"provider":"paypay"}', encoding="utf-8",
    )

    with pytest.raises(
        CoverageConfirmationInputError,
        match="coverage_confirmation_input_unreadable",
    ):
        load_coverage_confirmation_input(path)


def test_invalid_sha256_is_rejected(tmp_path):
    with pytest.raises(CoverageConfirmationInputError, match="invalid_content_sha256"):
        explicit_input(tmp_path, content_sha256="not-a-sha256")


@pytest.mark.parametrize("field", ["confirmed_at", "created_at"])
def test_invalid_timestamp_is_rejected(tmp_path, field):
    with pytest.raises(CoverageConfirmationInputError, match=f"invalid_{field}"):
        explicit_input(tmp_path, **{field: "not-a-timestamp"})


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"provider": "unknown"}, "unsupported_provider"),
        ({"range_source": "guessed"}, "invalid_range_source"),
        ({"coverage_status": "complete"}, "invalid_coverage_status"),
        ({"coverage_reason": "guessed"}, "invalid_coverage_reason"),
    ],
)
def test_unknown_provider_and_invalid_enums_are_rejected(
    tmp_path, overrides, reason,
):
    with pytest.raises(CoverageConfirmationInputError, match=reason):
        explicit_input(tmp_path, **overrides)


def test_confirmation_id_is_generated_and_cannot_be_supplied_in_json(tmp_path):
    loaded = explicit_input(tmp_path)
    assert loaded.confirmation_id == coverage_confirmation_id(loaded.record.identity)

    data = payload(confirmation_id=loaded.confirmation_id)
    path = tmp_path / "id-not-accepted.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CoverageConfirmationInputError):
        load_coverage_confirmation_input(path)


def test_apply_without_apply_flag_returns_preflight_and_never_calls_writer(
    tmp_path, monkeypatch,
):
    import app.coverage_confirmation_cli as module

    db = FakeDB(rows=[])
    loaded = explicit_input(tmp_path)
    monkeypatch.setattr(
        module,
        "apply_coverage_confirmation_write",
        lambda *_args, **_kwargs: pytest.fail("write service called"),
    )

    result = run_coverage_confirmation_apply(db, loaded)

    assert result["expected_action"] == "append"
    assert result["external_write"] is False
    assert db.writes == []


def test_confirmation_id_mismatch_blocks_before_phase_two_d_apply(
    tmp_path, monkeypatch,
):
    import app.coverage_confirmation_cli as module

    db = FakeDB(rows=[])
    loaded = explicit_input(tmp_path)
    monkeypatch.setattr(
        module,
        "apply_coverage_confirmation_write",
        lambda *_args, **_kwargs: pytest.fail("write service called"),
    )

    result = run_coverage_confirmation_apply(
        db, loaded, apply=True, confirm_id="CC-wrong",
    )

    assert result["apply_result"]["blocked"] is True
    assert result["apply_result"]["reason"] == "confirmation_id_mismatch"
    assert result["external_write"] is False
    assert db.writes == []


def test_sheet_missing_preflight_plans_create_and_header(tmp_path):
    result = run_coverage_confirmation_preflight(
        FakeDB(exists=False), explicit_input(tmp_path),
    )

    assert result["spreadsheet_identified"] is True
    assert result["sheet_status"] == "sheet_missing"
    assert result["expected_action"] == "create_and_append"
    assert result["create_sheet_planned"] is True
    assert result["header_write_planned"] is True
    assert result["append_row_planned"] is True
    assert result["blocked"] is False
    assert result["external_write"] is False


def test_schema_mismatch_preflight_is_blocked(tmp_path):
    result = run_coverage_confirmation_preflight(
        FakeDB(header=["wrong"]), explicit_input(tmp_path),
    )

    assert result["schema_status"] == "schema_mismatch"
    assert result["expected_action"] == "blocked"
    assert result["blocked"] is True
    assert result["external_write"] is False


def test_exact_duplicate_preflight_plans_idempotent_skip(tmp_path):
    loaded = explicit_input(tmp_path)
    db = FakeDB(rows=[sheet_row(loaded.record)])

    result = run_coverage_confirmation_preflight(db, loaded)

    assert result["duplicate_status"] == "exact_duplicate"
    assert result["expected_action"] == "skip_duplicate"
    assert result["append_row_planned"] is False
    assert result["blocked"] is False


def test_identity_conflict_preflight_is_blocked(tmp_path):
    loaded = explicit_input(tmp_path)
    existing = CoverageConfirmationRecord(
        **{
            **loaded.record.__dict__,
            "confirmed_start": "2025-08-21",
        }
    )

    result = run_coverage_confirmation_preflight(
        FakeDB(rows=[sheet_row(existing)]), loaded,
    )

    assert result["duplicate_status"] == "identity_conflict"
    assert result["expected_action"] == "blocked"
    assert result["blocked"] is True


def test_preflight_json_omits_spreadsheet_credentials_and_private_source_fields(
    tmp_path, monkeypatch, capsys,
):
    path = write_input(tmp_path)
    db = FakeDB(exists=False)
    private_spreadsheet_id = "private-real-shaped-spreadsheet-id"

    class Settings:
        spreadsheet_id = private_spreadsheet_id

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    service = object()
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: service)
    monkeypatch.setattr(
        cli,
        "SheetsDB",
        lambda spreadsheet_id, service=None: db
        if spreadsheet_id == private_spreadsheet_id and service is not None else None,
    )
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "coverage-confirmation-preflight",
        "--input-json", str(path),
    ])

    cli.main()

    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["external_write"] is False
    assert result["confirmation_id"]
    assert result["content_sha256"] == HASH
    assert private_spreadsheet_id not in output
    assert "private-source-name.csv" not in output
    assert "private-drive-file-id" not in output


def test_apply_command_without_apply_uses_read_only_preflight_only(
    tmp_path, monkeypatch, capsys,
):
    path = write_input(tmp_path)
    db = object()
    service = object()

    class Settings:
        spreadsheet_id = "fake-sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: service)
    monkeypatch.setattr(
        cli, "SheetsDB",
        lambda spreadsheet_id, service=None: db
        if spreadsheet_id == "fake-sheet-id" and service is not None else None,
    )
    monkeypatch.setattr(
        cli,
        "run_coverage_confirmation_preflight",
        lambda value, explicit: {
            "external_write": False,
            "confirmation_id": explicit.confirmation_id,
        } if value is db else {},
    )
    monkeypatch.setattr(
        cli,
        "run_coverage_confirmation_apply",
        lambda *_args, **_kwargs: pytest.fail("apply route called"),
    )
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "coverage-confirmation-apply", "--input-json", str(path),
    ])

    cli.main()

    assert json.loads(capsys.readouterr().out)["external_write"] is False


def test_cli_calls_phase_two_d_route_only_with_apply_and_matching_id(
    tmp_path, monkeypatch, capsys,
):
    path = write_input(tmp_path)
    loaded = load_coverage_confirmation_input(path)
    db = object()

    class Settings:
        spreadsheet_id = "fake-sheet-id"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True}

    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(
        cli, "read_only_sheets_service",
        lambda: pytest.fail("read-only route used for apply"),
    )
    monkeypatch.setattr(
        cli, "SheetsDB",
        lambda spreadsheet_id, service=None: db
        if spreadsheet_id == "fake-sheet-id" and service is None else None,
    )

    calls = []

    def fake_apply(value, explicit, *, apply, confirm_id):
        calls.append((value, explicit.confirmation_id, apply, confirm_id))
        return {"external_write": True, "apply_result": {"blocked": False}}

    monkeypatch.setattr(cli, "run_coverage_confirmation_apply", fake_apply)
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "coverage-confirmation-apply", "--input-json", str(path),
        "--apply", "--confirm-id", loaded.confirmation_id,
    ])

    cli.main()

    assert calls == [(db, loaded.confirmation_id, True, loaded.confirmation_id)]
    assert json.loads(capsys.readouterr().out)["external_write"] is True


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--apply"],
        ["--apply", "--confirm-id", "CC-wrong"],
        ["--confirm-id", "CC-wrong"],
    ],
)
def test_unsafe_apply_intent_exits_before_settings_or_write_service(
    tmp_path, monkeypatch, extra_args,
):
    path = write_input(tmp_path)
    monkeypatch.setattr(
        cli, "Settings",
        lambda: pytest.fail("Settings initialized before guard"),
    )
    monkeypatch.setattr(
        cli,
        "run_coverage_confirmation_apply",
        lambda *_args, **_kwargs: pytest.fail("apply service called"),
    )
    monkeypatch.setattr(sys, "argv", [
        "kakeibo", "coverage-confirmation-apply", "--input-json", str(path),
        *extra_args,
    ])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_missing_cli_input_exits_before_credentials(monkeypatch):
    monkeypatch.setattr(
        cli, "Settings",
        lambda: pytest.fail("credentials must not be loaded"),
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "coverage-confirmation-preflight"],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2


def test_fake_apply_path_preserves_phase_two_d_create_header_append_semantics(
    tmp_path,
):
    loaded = explicit_input(tmp_path)
    db = FakeDB(exists=False)

    result = run_coverage_confirmation_apply(
        db,
        loaded,
        apply=True,
        confirm_id=loaded.confirmation_id,
    )

    assert [write[0] for write in db.writes] == [
        "create_sheet", "write_header", "append_raw",
    ]
    assert result["apply_result"]["blocked"] is False
    assert result["apply_result"]["created_sheet"] is True
    assert result["apply_result"]["wrote_header"] is True
    assert result["apply_result"]["appended_row"] is True
    assert result["apply_result"]["external_write"] is True
