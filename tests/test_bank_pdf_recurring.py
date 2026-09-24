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
from app.bank_pdf_pipeline import CHIBA_BANK_SOURCE, DOCOMO_SMTB_SOURCE, SOURCE


@pytest.fixture(autouse=True)
def _fixed_git_head(monkeypatch):
    monkeypatch.setattr(
        "app.bank_pdf_recurring.subprocess.check_output",
        lambda *args, **kwargs: "a" * 40,
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

    def get(self, fileId, **kwargs):
        if fileId == "B" * 20:
            return _Request({"id": fileId, "mimeType": "application/vnd.google-apps.folder",
                             "capabilities": {"canAddChildren": True}})
        return _Request(next(f for f in self.response["files"] if f["id"] == fileId))

    def update(self, **kwargs):
        self.updated.append(kwargs)
        if kwargs.get("addParents"):
            file = next(f for f in self.response["files"] if f["id"] == kwargs["fileId"])
            file["parents"] = sorted((set(file["parents"]) - {kwargs["removeParents"]}) | {kwargs["addParents"]})
            file["appProperties"] = kwargs["body"]["appProperties"]
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
        "schema_version": 1,
        "policy_id": "bank-daily-v1",
        "source": BANK_RECURRING_SOURCE,
        "expected_spreadsheet_id": "sheet",
        "expected_drive_folder_id": "A" * 20,
        "expected_worksheet": "取込データ",
        "target_binding_version": 1,
        "canonical_schema_version": 1,
        "supported_bank_sources": [SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE],
        "allowed_classifications": ["expense"],
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


def test_stale_bank_checkpoint_uses_first_authorized_catch_up_window(tmp_path):
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path, expires_at="2026-10-01T00:00:00+09:00"), repo_root=Path.cwd(),
    )
    policy = provider.load()
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    now = datetime.fromisoformat("2026-09-24T12:00:00+09:00")
    first = build_incremental_window(policy, state, now)
    assert first.start == datetime.fromisoformat("2026-09-12T23:00:00+09:00")
    assert first.end - first.start == timedelta(days=7)
    assert first.end < now
    state.record({"run_id": "first-window", "status": "noop",
                  "source_window_start": first.start.isoformat(),
                  "source_window_end": first.end.isoformat()}, advance_checkpoint=True)
    second = build_incremental_window(policy, state, now)
    assert second.start == first.end - timedelta(hours=1)
    assert second.end == now


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
        expense_candidate_identities = candidate_identities
        summary = {
            "parsed": 4, "existing_duplicate": 1, "true_unknown": 0,
            "withheld_by_classification": 2, "collision": 0,
            "new_income": 0,
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
    assert result["review"] == 0
    assert result["withheld"] == 2
    assert result["write_attempted"] == 0


def test_review_file_is_withheld_from_recurring_write(tmp_path, monkeypatch):
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path), repo_root=Path.cwd(),
    )
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    file = {
        "id": "pdf-review", "name": "review.pdf", "mimeType": "application/pdf",
        "modifiedTime": "2026-09-14T02:00:00Z", "appProperties": {},
    }

    class _Daily:
        candidate_identities = ("bankpdf:au-jibun:jibun-primary:expense",)
        expense_candidate_identities = candidate_identities
        summary = {
            "parsed": 2, "existing_duplicate": 0, "true_unknown": 1,
            "withheld_by_classification": 1, "collision": 0, "new_income": 0,
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

    assert result["status"] == "dry_run_noop"
    assert result["new_eligible"] == 0
    assert result["review"] == 1
    assert result["write_attempted"] == 0


def test_income_candidate_is_not_an_expense_write(tmp_path, monkeypatch):
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path), repo_root=Path.cwd(),
    )
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    file = {
        "id": "pdf-income", "name": "income.pdf", "mimeType": "application/pdf",
        "modifiedTime": "2026-09-14T02:00:00Z", "appProperties": {},
    }

    class _Daily:
        candidate_identities = ("bankpdf:au-jibun:jibun-primary:income",)
        expense_candidate_identities = ()
        summary = {
            "parsed": 1, "existing_duplicate": 0, "true_unknown": 0,
            "withheld_by_classification": 0, "collision": 0, "new_income": 1,
            "household_income": {"classification": {
                "confirmed_income": {"count": 1}, "needs_review": {"count": 0},
            }},
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

    assert result["status"] == "dry_run_noop"
    assert result["income"] == 1
    assert result["household_income_confirmed"] == 1
    assert result["household_income_review"] == 0
    assert result["income_write_enabled"] is False
    assert result["planned_expense_writes"] == 0


def test_synthetic_recurring_apply_uses_standing_authority_without_manual_approval(
    tmp_path, monkeypatch,
):
    provider = ProtectedBankRecurringAuthorityProvider(
        _authority_file(tmp_path), repo_root=Path.cwd(),
    )
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    drive = _Drive({"files": [{
        "id": "pdf-expense", "name": "expense.pdf", "mimeType": "application/pdf",
        "modifiedTime": "2026-09-14T02:00:00Z", "appProperties": {},
    }]})
    identity = "bankpdf:au-jibun:jibun-primary:expense"

    class _Daily:
        candidate_identities = (identity,)
        expense_candidate_identities = candidate_identities
        summary = {
            "parsed": 1, "existing_duplicate": 0, "true_unknown": 0,
            "withheld_by_classification": 0, "collision": 0, "new_income": 0,
        }

    observed = {}

    def _apply(*args, **kwargs):
        observed.update(kwargs)
        return {
            "confirmed_count": 1,
            "write_request_count": 1,
            "recurring_authority_ref": kwargs["recurring_context"].authority_ref,
        }

    monkeypatch.setattr("app.bank_pdf_recurring.build_bank_daily_preview", lambda *a, **k: _Daily())
    monkeypatch.setattr("app.bank_pdf_recurring.run_bank_recurring_production_batch", _apply)
    result = run_bank_pdf_recurring(
        drive_service=drive,
        db=_DB(),
        state=state,
        authority_provider=provider,
        repo_root=Path.cwd(),
        now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"),
        dry_run=False,
        audit_key_file=tmp_path / "audit.json",
        download=lambda _id: b"synthetic",
        sleeper=lambda _delay: None,
    )

    assert result["status"] == "complete"
    assert result["written"] == 1
    assert result["write_requests"] == 1
    assert observed["selected_source_identities"] == (identity,)
    assert "approval_file" not in observed
    assert len(drive._files.updated) == 1


@pytest.mark.parametrize("mode", ["duplicate", "write", "review", "income_disabled", "empty", "partial_write", "preview"])
def test_bank_archive_only_after_complete_processing(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    provider = ProtectedBankRecurringAuthorityProvider(_authority_file(tmp_path), repo_root=Path.cwd())
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    file = {"id": "pdf", "mimeType": "application/pdf", "parents": ["A" * 20, "unrelated"], "appProperties": {}}
    drive = _Drive({"files": [file]})
    ids = ("bankpdf:expense",) if mode in {"write", "partial_write", "preview"} else ()
    daily = SimpleNamespace(expense_candidate_identities=ids, summary={
        "parsed": 0 if mode == "empty" else 1, "true_unknown": int(mode == "review"),
        "collision": 0, "new_income": int(mode == "income_disabled"),
    })
    monkeypatch.setattr("app.bank_pdf_recurring.build_bank_daily_preview", lambda *a, **k: daily)
    monkeypatch.setattr("app.bank_pdf_recurring.run_bank_recurring_production_batch",
                        lambda *a, **k: {"confirmed_count": 0 if mode == "partial_write" else 1})
    result = run_bank_pdf_recurring(drive_service=drive, db=_DB(), state=state,
        authority_provider=provider, repo_root=Path.cwd(),
        now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"), dry_run=mode == "preview",
        processed_folder_id="B" * 20, download=lambda _: b"synthetic",
        audit_key_file=tmp_path / "audit.json")
    if mode in {"duplicate", "write"}:
        assert set(file["parents"]) == {"B" * 20, "unrelated"}
        assert file["appProperties"][BANK_PROCESSED_PROPERTY]
    else:
        assert drive._files.updated == []
        assert "A" * 20 in file["parents"]
    if mode == "partial_write":
        assert result["status"] == "failed"
        assert state.successful_window_end() is None


def test_bank_archive_failure_replay_has_no_duplicate_append(tmp_path, monkeypatch):
    from types import SimpleNamespace
    provider = ProtectedBankRecurringAuthorityProvider(_authority_file(tmp_path), repo_root=Path.cwd())
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    file = {"id": "pdf", "mimeType": "application/pdf", "parents": ["A" * 20], "appProperties": {}}
    drive = _Drive({"files": [file]})
    db = _DB()
    daily = SimpleNamespace(expense_candidate_identities=("bankpdf:expense",), summary={"parsed": 1})
    monkeypatch.setattr("app.bank_pdf_recurring.build_bank_daily_preview", lambda *a, **k: daily)
    writes = []
    def append(*a, **k):
        writes.append(1)
        db.rows.append(["bankpdf:expense"])
        return {"confirmed_count": 1}
    monkeypatch.setattr("app.bank_pdf_recurring.run_bank_recurring_production_batch", append)
    update = drive._files.update
    monkeypatch.setattr(drive._files, "update", lambda **kw: (_ for _ in ()).throw(TimeoutError()))
    options = dict(drive_service=drive, db=db, state=state, authority_provider=provider,
        repo_root=Path.cwd(), now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"),
        dry_run=False, processed_folder_id="B" * 20, download=lambda _: b"synthetic",
        audit_key_file=tmp_path / "audit.json")
    assert run_bank_pdf_recurring(**options)["status"] == "failed"
    assert state.successful_window_end() is None
    monkeypatch.setattr(drive._files, "update", update)
    assert run_bank_pdf_recurring(**options)["status"] == "noop"
    assert writes == [1]
    assert file["parents"] == ["B" * 20]


def test_overlapping_pdf_is_not_archived_before_first_write_succeeds(tmp_path, monkeypatch):
    from types import SimpleNamespace
    provider = ProtectedBankRecurringAuthorityProvider(_authority_file(tmp_path), repo_root=Path.cwd())
    state = SqliteRecurringRunState(tmp_path / "state.sqlite3", repo_root=Path.cwd())
    drive = _Drive({"files": [{"id": str(i), "mimeType": "application/pdf",
                              "parents": ["A" * 20], "appProperties": {}} for i in range(2)]})
    daily = SimpleNamespace(expense_candidate_identities=("bankpdf:overlap",), summary={"parsed": 1})
    monkeypatch.setattr("app.bank_pdf_recurring.build_bank_daily_preview", lambda *a, **k: daily)
    def fail(*a, **k):
        raise TimeoutError("write failed")
    monkeypatch.setattr("app.bank_pdf_recurring.run_bank_recurring_production_batch", fail)
    result = run_bank_pdf_recurring(drive_service=drive, db=_DB(), state=state,
        authority_provider=provider, repo_root=Path.cwd(),
        now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"), dry_run=False,
        processed_folder_id="B" * 20, download=lambda _: b"synthetic",
        audit_key_file=tmp_path / "audit.json")
    assert result["status"] == "failed"
    assert drive._files.updated == []
    assert state.successful_window_end() is None
