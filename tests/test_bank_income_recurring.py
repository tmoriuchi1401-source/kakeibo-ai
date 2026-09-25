from copy import deepcopy
import base64
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

import pytest

from app.aupay_card_recurring import SqliteRecurringRunState
from app.bank_income import INCOME_HEADERS, INCOME_SHEET, classify_deposit
from app.bank_income_recurring import require_income_actions
from app.bank_pdf_pipeline import BankPdfResult, SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE
from app.bank_pdf_recurring import BANK_PROCESSED_PROPERTY, run_bank_pdf_recurring
from app.bank_recurring_authority import ProtectedBankRecurringAuthorityProvider
from app.sheets import HEADERS
from test_bank_income import DB, bank, imported
from test_bank_pdf_recurring import _Drive, _authority_file


class Ledger(DB):
    def __init__(self):
        super().__init__()
        self.modes = {}

    def get(self, rng):
        if rng == "取込データ!A1:L1":
            return [HEADERS["取込データ"]]
        return super().get(rng)

    def append_raw(self, sheet, rows):
        assert sheet in {"取込データ", INCOME_SHEET}
        self.writes.append((sheet, deepcopy(rows)))
        mode = self.modes.get(sheet, "normal")
        if mode != "timeout_before_commit":
            self.tables[sheet].extend(deepcopy(rows))
        if mode == "corrupt":
            self.tables[sheet][-1][2] = "corrupt"
        if mode.startswith("timeout"):
            raise TimeoutError("synthetic response lost")


class Rig:
    def __init__(self, tmp_path, monkeypatch, **policy):
        self.db = Ledger()
        self.path = _authority_file(tmp_path, expected_spreadsheet_id=self.db.sid,
            income_enabled=True, income_worksheet=INCOME_SHEET,
            income_accounts=[[SOURCE, "test-primary"]], **policy)
        self.provider = ProtectedBankRecurringAuthorityProvider(self.path, repo_root=Path.cwd())
        self.state = SqliteRecurringRunState(tmp_path / "bank-recurring.sqlite3", repo_root=Path.cwd())
        self.state.record({"run_id": "synthetic-existing-run", "status": "dry_run_noop",
            "source_window_start": "2026-09-13T00:00:00+09:00",
            "source_window_end": "2026-09-14T00:00:00+09:00"}, advance_checkpoint=False)
        self.drive = _Drive({"files": []})
        self.documents = {}
        self.expense_calls = []
        self.audit = tmp_path / "audit.json"
        self.audit.write_text(json.dumps({"key_id": "synthetic-key", "key_b64": base64.b64encode(b"x" * 32).decode()}))
        monkeypatch.setattr("app.bank_pdf_recurring.subprocess.check_output", lambda *a, **kw: "a" * 40)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
        monkeypatch.setenv("GITHUB_SHA", "a" * 40)
        monkeypatch.setenv("GITHUB_WORKFLOW_REF", "tmoriuchi1401-source/kakeibo-ai/.github/workflows/bank-pdf-recurring.yml@refs/heads/main")
        def parse(_parser, path, **kwargs):
            transactions = self.documents[Path(path).read_text()]
            return BankPdfResult(1, len(transactions), tuple(transactions), (), 0, 0,
                                 tuple(tx.to_canonical() for tx in transactions))
        monkeypatch.setattr("app.bank_steady_state.BankPdfPipeline.parse", parse)
        def expense_writer(db, path, **kwargs):
            ids = kwargs["selected_source_identities"]
            self.expense_calls.append(ids)
            for tx in self.documents[Path(path).read_text()]:
                if tx.source_row_identity in ids:
                    assert tx.signed_amount < 0
                    db.tables["取込データ"].append(imported(tx))
                    db.tables["支出明細"].append([tx.source_row_identity, -tx.signed_amount])
            return {"confirmed_count": len(ids), "write_request_count": len(ids)}
        monkeypatch.setattr("app.bank_pdf_recurring.run_bank_recurring_production_batch", expense_writer)

    def add(self, name, *transactions, processed=False):
        self.documents[name] = transactions
        self.drive._files.response["files"].append({"id": name, "name": name + ".pdf",
            "mimeType": "application/pdf", "modifiedTime": "2026-09-14T02:00:00Z",
            "appProperties": {BANK_PROCESSED_PROPERTY: "old"} if processed else {}})

    def run(self, **kwargs):
        options = dict(drive_service=self.drive, db=self.db, state=self.state,
            authority_provider=self.provider, repo_root=Path.cwd(),
            now=datetime.fromisoformat("2026-09-14T12:00:00+09:00"), dry_run=False,
            income_write_enabled=True, audit_key_file=self.audit, download=lambda key: key.encode(),
            sleeper=lambda _: None)
        options.update(kwargs)
        return run_bank_pdf_recurring(**options)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_income_only_pdf_persists_then_posts_once_and_replays_zero(rig):
    tx = bank()
    rig.add("income", tx)
    result = rig.run()
    assert result["status"] == "complete" and result["income_created"] == 1
    assert [sheet for sheet, _ in rig.db.writes] == ["取込データ", INCOME_SHEET]
    assert rig.db.tables["取込データ"][0][9] == ""
    assert rig.db.tables[INCOME_SHEET][1:] == [classify_deposit(tx).row()]
    assert rig.expense_calls == [] and len(rig.drive._files.updated) == 1
    again = rig.run()
    assert again["income_created"] == again["deposit_imports_created"] == 0
    assert len(rig.db.writes) == 2


def test_mixed_pdf_expense_selection_and_income_are_separate(rig):
    rig.add("mixed", bank(), bank("口座振替 公共", seed="expense", amount=-500,
                                 transaction_kind="withdrawal"))
    result = rig.run()
    assert result["status"] == "complete", result
    assert result["income_created"] == 1
    assert len(rig.expense_calls) == 1
    assert len(rig.db.tables["支出明細"]) == 2
    assert rig.run()["income_created"] == 0
    assert len(rig.expense_calls) == 1


def test_unknown_rewards_are_saved_for_review_without_income(rig):
    rig.add("reward", bank("給与振込特典", seed="review"))
    result = rig.run()
    assert result["status"] == "complete" and result["income_created"] == 0
    row = rig.db.tables["取込データ"][0]
    assert row[8] == "needs_review" and row[9] == ""
    assert "deposit_purpose_unconfirmed" in row[11]
    assert len(rig.db.tables[INCOME_SHEET]) == 1


def test_file_review_guard_still_withholds_expenses_and_confirmed_deposits(rig):
    rig.add("review", bank(), bank("振込 不明", seed="unknown"),
            bank("口座振替 公共", seed="expense", amount=-500, transaction_kind="withdrawal"))
    result = rig.run()
    assert result["income_created"] == 0 and rig.expense_calls == []
    assert all(row[8] == "needs_review" for row in rig.db.tables["取込データ"])
    assert rig.drive._files.updated == []


@pytest.mark.parametrize("sheet", ["取込データ", INCOME_SHEET])
def test_committed_append_response_loss_resumes_from_exact_readback_with_no_new_pdf(rig, sheet):
    rig.add("income", bank())
    rig.db.modes[sheet] = "timeout_after_commit"
    failed = rig.run()
    assert failed["status"] == "failed" and rig.state.successful_window_end() is None
    assert rig.drive._files.updated == []
    rig.db.modes.clear()
    rig.drive._files.response["files"] = []
    result = rig.run()
    assert result["status"] in {"complete", "noop"}
    assert len(rig.db.tables[INCOME_SHEET]) == 2
    assert len(rig.db.tables["取込データ"]) == 1
    assert [name for name, _ in rig.db.writes].count(INCOME_SHEET) == 1


@pytest.mark.parametrize("sheet", ["取込データ", INCOME_SHEET])
def test_missing_uncertain_append_is_never_automatically_retried(rig, sheet):
    rig.add("income", bank())
    rig.db.modes[sheet] = "timeout_before_commit"
    assert rig.run()["status"] == "failed"
    writes = deepcopy(rig.db.writes)
    rig.db.modes.clear()
    result = rig.run()
    assert result["status"] == "failed"
    assert "reconciliation_required" in result["failure_reason"]
    assert rig.db.writes == writes and rig.drive._files.updated == []


def test_existing_saved_income_resumes_with_empty_drive(rig):
    rig.db.tables["取込データ"] = [imported(bank())]
    result = rig.run()
    assert result["files_new"] == 0 and result["income_created"] == 1
    assert rig.db.writes[0][0] == INCOME_SHEET


def test_legacy_processed_pdf_can_supply_missing_deposits_without_expense_replay(rig):
    rig.add("old", bank(), processed=True)
    assert rig.run()["income_created"] == 1
    assert rig.drive._files.updated == [] and rig.expense_calls == []


def test_overlap_dedupes_identity_but_same_day_amount_separate_transactions_survive(rig):
    one, two = bank(seed="one"), bank(seed="two")
    rig.add("one", one)
    rig.add("overlap", one, two)
    result = rig.run()
    assert result["income_created"] == 2 and result["deposit_imports_created"] == 2
    assert len(rig.db.tables[INCOME_SHEET]) == 3


def test_income_off_withholds_unposted_deposit_without_reading_income_sheet(rig):
    rig.add("one", bank())
    del rig.db.tables[INCOME_SHEET]
    result = rig.run(income_write_enabled=False)
    assert result["status"] == "noop" and rig.db.writes == []
    assert not any(INCOME_SHEET in rng for rng in rig.db.reads)
    assert result["files_withheld"] == 1
    assert rig.drive._files.updated == []


def test_dry_run_does_not_save_deposits_income_or_processed(rig):
    rig.add("one", bank())
    result = rig.run(dry_run=True)
    assert result["planned_income_writes"] == result["planned_deposit_imports"] == 1
    assert rig.db.writes == rig.drive._files.updated == []
    assert rig.state.successful_window_end() is None


@pytest.mark.parametrize("bad", ["target", "account", "disabled", "limit", "header", "local"])
def test_authority_and_limit_fail_before_any_google_write(rig, monkeypatch, bad):
    rig.add("one", bank(), bank(seed="two"))
    policy = json.loads(rig.path.read_text())
    if bad == "target":
        policy["expected_spreadsheet_id"] = "other"
    elif bad == "account":
        policy["income_accounts"] = [[SOURCE, "other-primary"]]
    elif bad == "disabled":
        for key in ("income_enabled", "income_accounts", "income_worksheet"):
            policy.pop(key)
    elif bad == "limit":
        policy["max_rows"] = 1
    elif bad == "header":
        rig.db.tables[INCOME_SHEET][0] = ["wrong"]
    else:
        monkeypatch.delenv("GITHUB_ACTIONS")
    rig.path.write_text(json.dumps(policy))
    try:
        result = rig.run()
        assert result["status"] == "failed"
    except RuntimeError:
        pass
    assert rig.db.writes == rig.drive._files.updated == []


def test_combined_expense_income_limit_is_not_doubled(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, max_rows=1)
    rig.add("income", bank())
    rig.add("expense", bank("口座振替 公共", seed="expense", amount=-500, transaction_kind="withdrawal"))
    result = rig.run()
    assert result["status"] == "failed" and "shared_row_bound" in result["failure_reason"]
    assert rig.db.writes == rig.drive._files.updated == rig.expense_calls == []


@pytest.mark.parametrize("description", ["給与 返金", "借入 融資実行", "定額自動入金", "振込 不明"])
def test_refund_loan_transfer_unknown_never_post_as_income(rig, description):
    rig.add("excluded", bank(description))
    result = rig.run()
    assert result["income_created"] == 0
    assert len(rig.db.tables[INCOME_SHEET]) == 1
    assert all(row[9] == "" and row[8] != "bank_income" for row in rig.db.tables["取込データ"])


def test_existing_review_and_link_guards_are_not_overwritten(rig):
    tx = bank()
    rig.db.tables["取込データ"] = [imported(tx)]
    rig.db.tables["取込データ"][0][9] = "existing-expense-link"
    before = deepcopy(rig.db.tables["取込データ"])
    rig.add("linked", tx)
    assert rig.run()["income_created"] == 0
    assert rig.db.tables["取込データ"] == before
    assert rig.db.writes == []


def test_income_intents_preserve_existing_native_state_schema(rig):
    from app.drive_run_state import _validate_file
    rig.add("one", bank())
    assert rig.run()["status"] == "complete"
    _validate_file("bank_pdf_drive", "bank-recurring.sqlite3", rig.state.path.read_bytes())


def test_missing_audit_key_stops_before_import_and_processed_write(rig):
    rig.audit.unlink()
    rig.add("one", bank())
    assert rig.run()["status"] == "failed"
    assert rig.db.writes == rig.drive._files.updated == []


def test_three_banks_use_existing_classifier_with_exact_account_scope(rig):
    policy = json.loads(rig.path.read_text())
    policy["income_accounts"] = [[source, "test-primary"] for source in (SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE)]
    rig.path.write_text(json.dumps(policy))
    for namespace, source in [("au-jibun", SOURCE), ("docomo-smtb", DOCOMO_SMTB_SOURCE), ("chiba", CHIBA_BANK_SOURCE)]:
        tx = bank(seed=namespace)
        tx = replace(tx, source=source, source_row_identity=tx.source_row_identity.replace("au-jibun", namespace))
        rig.add(namespace, tx)
    assert rig.run()["income_created"] == 3


@pytest.mark.parametrize("field,value", [("income_enabled", "true"), ("income_worksheet", "支出明細"),
    ("income_accounts", [[SOURCE, "*"]]), ("income_accounts", [["other-bank", "test-primary"]])])
def test_malformed_income_authority_is_rejected(rig, field, value):
    policy = json.loads(rig.path.read_text())
    policy[field] = value
    rig.path.write_text(json.dumps(policy))
    with pytest.raises(RuntimeError, match="protected_bank_recurring_authority_invalid"):
        rig.provider.load()


def test_corrupt_income_after_append_never_retries_or_marks_pdf_processed(rig):
    rig.add("one", bank())
    rig.db.modes[INCOME_SHEET] = "corrupt"
    assert rig.run()["status"] == "failed"
    before = deepcopy(rig.db.writes)
    rig.db.modes.clear()
    assert rig.run()["status"] == "failed"
    assert rig.db.writes == before and rig.drive._files.updated == []


def test_expense_failure_after_income_does_not_repost_income_on_resume(rig, monkeypatch):
    from app import bank_pdf_recurring as recurring
    original = recurring.run_bank_recurring_production_batch
    calls = []
    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic_expense_failure_before_write")
        return original(*args, **kwargs)
    monkeypatch.setattr(recurring, "run_bank_recurring_production_batch", fail_once)
    rig.add("mixed", bank(), bank("口座振替 公共", seed="expense", amount=-500, transaction_kind="withdrawal"))
    assert rig.run()["status"] == "failed"
    assert len(rig.db.tables[INCOME_SHEET]) == 2 and rig.drive._files.updated == []
    result = rig.run()
    assert result["status"] == "complete" and result["income_created"] == 0
    assert len(rig.expense_calls) == 1
    assert [sheet for sheet, _ in rig.db.writes].count(INCOME_SHEET) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_passes_explicit_flag_and_readonly_mode_to_preview_runner(rig, monkeypatch, enabled):
    import sys
    from types import SimpleNamespace
    from app import cli
    observed = {}
    monkeypatch.setenv("BANK_INCOME_WRITE_ENABLED", "true" if enabled else "false")
    monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace(
        spreadsheet_id=rig.db.sid, bank_pdf_drive_folder_id="", bank_pdf_processed_drive_folder_id="",
        validate=lambda **kw: None, bank_confirmed_internal_transfers=lambda: frozenset(),
        bank_confirmed_non_own_classifications=lambda: frozenset()))
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: object())
    monkeypatch.setattr(cli, "read_only_drive_service", lambda: rig.drive)
    monkeypatch.setattr(cli, "SheetsDB", lambda *a, **kw: rig.db)
    monkeypatch.setattr(cli, "run_bank_pdf_catch_up_preview", lambda **kw: observed.update(kw) or {"status": "dry_run_noop"})
    monkeypatch.setattr(sys, "argv", ["app.cli", "bank-pdf-recurring", "--dry-run",
        "--state-dir", str(rig.state.path.parent), "--authority-file", str(rig.path)])
    cli.main()
    assert observed["income_write_enabled"] is enabled
    assert observed["dry_run"] is True


def test_parent_passes_income_flag_without_promoting_bank_preview(tmp_path):
    from app.production_flow import command, source_environment
    env = {"BANK_INCOME_WRITE_ENABLED": "true", "BANK_PDF_RECURRING_AUTHORITY_JSON": '{"synthetic":true}'}
    prepared = source_environment("bank", tmp_path, env)
    assert prepared["BANK_INCOME_WRITE_ENABLED"] == "true"
    assert command("bank", apply=False)[-1] == "--dry-run"
    assert command("bank", apply=True)[-1] == "--apply"


def test_workflow_income_flag_defaults_off_and_schedule_commands_unchanged():
    from test_production_workflows import workflows
    parsed = workflows()
    for name in ("bank-pdf-recurring.yml", "kakeibo-production.yml"):
        job = next(iter(parsed[name]["jobs"].values()))
        assert job["env"]["BANK_INCOME_WRITE_ENABLED"] == "${{ vars.BANK_INCOME_WRITE_ENABLED || 'false' }}"
    assert parsed["bank-pdf-recurring.yml"]["on"]["workflow_dispatch"]["inputs"]["apply"]["default"] == "false"
    assert parsed["kakeibo-production.yml"]["on"]["workflow_dispatch"]["inputs"]["bank_apply"]["default"] == "false"


def test_lost_state_is_not_silently_initialized_for_income_write(rig):
    rig.state = SqliteRecurringRunState(rig.state.path.parent / "empty.sqlite3", repo_root=Path.cwd())
    rig.add("one", bank())
    result = rig.run()
    assert result["status"] == "failed" and "existing_state_required" in result["failure_reason"]
    assert "existing_state_required" in rig.run()["failure_reason"]
    assert rig.db.writes == rig.drive._files.updated == []


def test_provider_failure_payload_is_not_exposed_in_run_summary(rig, monkeypatch):
    rig.add("one", bank())
    def fail(*args):
        raise RuntimeError("provider error contains synthetic account and amount")
    monkeypatch.setattr(rig.db, "append_raw", fail)
    result = rig.run()
    assert result["failure_reason"] == "RuntimeError"
    assert "synthetic account" not in json.dumps(result)


def test_saved_income_legacy_marker_can_archive_without_new_writes(rig):
    tx = bank(amount=5000)
    rig.add("income", tx)
    assert rig.run()["status"] == "complete"
    file = rig.drive._files.response["files"][0]
    file["appProperties"] = {BANK_PROCESSED_PROPERTY: "old"}
    file["parents"] = ["A" * 20]
    before = len(rig.db.writes)
    assert rig.run(processed_folder_id="B" * 20)["status"] == "noop"
    assert len(rig.db.writes) == before
    assert file["parents"] == ["B" * 20]
