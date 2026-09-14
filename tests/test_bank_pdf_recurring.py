from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from app.aupay_card_recurring import SqliteRecurringRunState
from app.bank_pdf_recurring import (
    BANK_PROCESSED_PROPERTY,
    BANK_RECURRING_SOURCE,
    BankRecurringAuthority,
    BankPdfWindow,
    ProtectedBankRecurringAuthorityProvider,
    build_incremental_window,
    list_bounded_bank_pdfs,
    run_bank_pdf_recurring,
)


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Files:
    def __init__(self, response):
        self.response = response
        self.updated = []

    def list(self, **kwargs):
        self.query = kwargs
        return _Request(self.response)

    def update(self, **kwargs):
        self.updated.append(kwargs)
        return _Request({"id": kwargs["fileId"]})


class _Drive:
    def __init__(self, response):
        self._files = _Files(response)

    def files(self):
        return self._files


class _DB:
    sid = "sheet"

    def __init__(self, rows=()):
        self.rows = list(rows)

    def get(self, _range):
        return self.rows


def _authority_file(tmp_path: Path, **overrides) -> Path:
    values = {
        "policy_id": "bank-daily-v1",
        "source": BANK_RECURRING_SOURCE,
        "expected_spreadsheet_id": "sheet",
        "expected_drive_folder_id": "A" * 20,
        "max_files": 10,
        "max_rows": 100,
        "overlap_seconds": 3600,
        "max_window_seconds": 7 * 86400,
        "initial_start": "2026-09-13T00:00:00+09:00",
        "valid_from": "2026-09-13T00:00:00+09:00",
        "expires_at": "2026-09-20T00:00:00+09:00",
        "expected_branch": "main",
    }
    values.update(overrides)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_authority_and_window_are_bounded(tmp_path):
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path), repo_root=Path.cwd(),
    )
    policy = provider.load()
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    now = datetime.fromisoformat("2026-09-14T12:00:00+09:00")
    window = build_incremental_window(policy, state, now)
    assert window.end == now
    assert window.end - window.start == timedelta(hours=37)


def test_drive_listing_rejects_pagination_or_bound_overflow():
    window = BankPdfWindow(
        datetime.fromisoformat("2026-09-14T00:00:00+09:00"),
        datetime.fromisoformat("2026-09-14T12:00:00+09:00"),
    )
    drive = _Drive({"files": [{"id": "1"}], "nextPageToken": "more"})
    with pytest.raises(RuntimeError, match="file_bound_exceeded"):
        list_bounded_bank_pdfs(drive, "A" * 20, window, 10)


def test_empty_recurring_run_is_safe_noop_without_writes(tmp_path):
    authority = _authority_file(tmp_path)
    provider = ProtectedBankRecurringAuthorityProvider(authority, repo_root=Path.cwd())
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    result = run_bank_pdf_recurring(
        drive_service=_Drive({"files": []}),
        db=_DB(),
        state=state,
        authority_provider=provider,
        repo_root=Path.cwd(),
        now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"),
        dry_run=True,
    )
    assert result["status"] == "dry_run_noop"
    assert result["safe_noop"] is True
    assert result["write_attempted"] == 0
    assert state.successful_window_end() is None


def test_processed_existing_file_plans_no_import_updates(tmp_path):
    authority = _authority_file(tmp_path)
    provider = ProtectedBankRecurringAuthorityProvider(authority, repo_root=Path.cwd())
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    file = {
        "id": "pdf-old", "name": "statement.pdf", "mimeType": "application/pdf",
        "modifiedTime": "2026-09-14T02:00:00Z",
        "appProperties": {BANK_PROCESSED_PROPERTY: "2026-09-14T03:00:00+00:00"},
    }
    result = run_bank_pdf_recurring(
        drive_service=_Drive({"files": [file]}),
        db=_DB(),
        state=state,
        authority_provider=provider,
        repo_root=Path.cwd(),
        now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"),
        dry_run=True,
    )
    assert result["status"] == "dry_run_noop"
    assert result["planned_expense_writes"] == 0
    assert result["planned_import_updates"] == 0


def test_synthetic_new_eligible_run_is_preview_only(tmp_path, monkeypatch):
    authority = _authority_file(tmp_path)
    provider = ProtectedBankRecurringAuthorityProvider(authority, repo_root=Path.cwd())
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    file = {
        "id": "pdf-1", "name": "statement.pdf", "mimeType": "application/pdf",
        "modifiedTime": "2026-09-14T02:00:00Z", "appProperties": {},
    }

    class _Daily:
        candidate_identities = ("bankpdf:au-jibun:jibun-primary:new",)
        summary = {
            "parsed": 4, "existing_duplicate": 1, "true_unknown": 1,
            "withheld_by_classification": 2, "collision": 0,
        }

    monkeypatch.setattr("app.bank_pdf_recurring.build_bank_daily_preview", lambda *a, **k: _Daily())
    result = run_bank_pdf_recurring(
        drive_service=_Drive({"files": [file]}),
        db=_DB(),
        state=state,
        authority_provider=provider,
        repo_root=Path.cwd(),
        now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"),
        dry_run=True,
        download=lambda _id: b"synthetic",
    )
    assert result["status"] == "dry_run_ready"
    assert result["new_eligible"] == 1
    assert result["review"] == 1
    assert result["withheld"] == 2
    assert result["write_attempted"] == 0
