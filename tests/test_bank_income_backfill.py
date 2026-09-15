from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import bank_income_backfill as backfill
from app.bank_income import INCOME_HEADERS, INCOME_SHEET, classify_deposit
from app.bank_reconciliation import normalize_bank_description
from test_bank_income import DB, bank, imported


class FixedDB(DB):
    def __init__(self, imports, installed=False):
        super().__init__(imports, installed)
        self.schema_writes = []
        self.after_append = lambda: None
        self.svc = self

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def batchUpdate(self, **kwargs):
        def execute():
            assert kwargs == {"spreadsheetId": self.sid, "body": {"requests": [
                {"addSheet": {"properties": {"title": INCOME_SHEET,
                    "gridProperties": {"rowCount": 1001, "columnCount": 10}}}}]}}
            self.schema_writes.append("add")
            self.tables[INCOME_SHEET] = []
        return SimpleNamespace(execute=execute)

    def update(self, **kwargs):
        def execute():
            assert kwargs == {"spreadsheetId": self.sid, "range": f"{INCOME_SHEET}!A1:J1",
                "valueInputOption": "RAW", "body": {"values": [INCOME_HEADERS]}}
            self.schema_writes.append("header")
            self.tables[INCOME_SHEET] = [INCOME_HEADERS.copy()]
        return SimpleNamespace(execute=execute)

    def protected_values(self):
        return [{"values": deepcopy(self.tables.get(rng.split("!")[0], []))}
                for rng in backfill.PROTECTED_RANGES]

    def append_raw(self, sheet, rows):
        super().append_raw(sheet, rows)
        self.after_append()


@pytest.fixture
def fixed(monkeypatch):
    transactions = [bank(seed=str(i), amount=1000 + i) for i in range(3)]
    db = FixedDB([imported(tx) for tx in transactions])
    rows = sorted([classify_deposit(tx).row() for tx in transactions], key=lambda r: r[6])
    monkeypatch.setattr(backfill, "FIXED_COUNT", len(rows))
    monkeypatch.setattr(backfill, "CONTRACT_SHA256", backfill.digest({
        "spreadsheet_id": db.sid, "imports_through": backfill.IMPORTS_THROUGH, "rows": rows}))
    return db, rows


def test_default_preview_never_creates_schema_or_rows(fixed):
    db, _ = fixed
    before = deepcopy(db.tables)
    result = backfill.run_backfill(db, {})
    assert result["planned_new"] == 3 and result["read_only"]
    assert db.tables == before and db.writes == [] and db.schema_writes == []


def test_canary_then_rest_exact_readback_and_replay(fixed):
    db, rows = fixed
    before = backfill.protected_snapshot(db)
    result = backfill.run_backfill(db, {}, apply=True)
    assert result["created"] == 3 and result["final_count"] == 3
    assert result["canary_created"] == 1
    assert result["canary_replay_created"] == result["replay_created"] == 0
    assert result["monthly_total_verified"] and result["protected_unchanged"]
    assert db.writes == [rows[:1], rows[1:]]
    assert db.schema_writes == ["add", "header"]
    assert before == backfill.protected_snapshot(db)
    again = backfill.run_backfill(db, {}, apply=True)
    assert again["created"] == 0 and again["existing_skip"] == 3
    assert len(db.writes) == 2 and len(db.schema_writes) == 2


def test_partial_existing_and_unrelated_income_are_preserved(fixed):
    db, rows = fixed
    unrelated = classify_deposit(bank(seed="outside")).row()
    db.tables[INCOME_SHEET] = [INCOME_HEADERS.copy(), rows[0], unrelated]
    result = backfill.run_backfill(db, {}, apply=True)
    assert result["existing_skip"] == 1 and result["created"] == 2
    assert db.tables[INCOME_SHEET][2] == unrelated
    assert result["final_count"] == 3 and len(db.tables[INCOME_SHEET]) == 5


@pytest.mark.parametrize("change", ["target", "amount", "missing", "extra", "rule", "duplicate"])
def test_changed_fixed_contract_stops_before_schema(fixed, change):
    db, _ = fixed
    rules = {}
    if change == "target":
        db.sid = "other-sheet"
    elif change == "amount":
        db.tables["取込データ"][0][6] += 1
    elif change == "missing":
        db.tables["取込データ"].pop()
    elif change == "extra":
        db.tables["取込データ"].append(imported(bank(seed="extra")))
    elif change == "duplicate":
        db.tables["取込データ"].append(deepcopy(db.tables["取込データ"][0]))
    else:
        rules = {"confirmed_internal_transfers": frozenset({(normalize_bank_description("給与*勤務先"), "incoming", "test-primary")})}
    with pytest.raises(RuntimeError):
        backfill.run_backfill(db, rules, apply=True)
    assert db.schema_writes == db.writes == []


def test_new_arrival_is_excluded_by_commitment(fixed):
    db, rows = fixed
    later = imported(bank(seed="later"))
    later[1] = "2026-09-15 09:00:00"
    db.tables["取込データ"].append(later)
    assert backfill.current_fixed_rows(db, {}) == rows
    assert backfill.run_backfill(db, {}, apply=True)["created"] == 3
    assert len(db.tables[INCOME_SHEET]) == 4


def test_native_serial_import_timestamps_have_same_fixed_membership(fixed):
    db, rows = fixed
    for row in db.tables["取込データ"]:
        date = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
        row[1] = (date - datetime(1899, 12, 30)).total_seconds() / 86400
    assert backfill.current_fixed_rows(db, {}) == rows


@pytest.mark.parametrize("value", ["bad", None, True, float("inf")])
def test_invalid_timestamp_stops_without_write(fixed, value):
    db, _ = fixed
    db.tables["取込データ"][0][1] = value
    with pytest.raises(RuntimeError, match="timestamp_invalid"):
        backfill.run_backfill(db, {}, apply=True)
    assert db.writes == db.schema_writes == []


@pytest.mark.parametrize("mode", ["missing", "corrupt", "timeout_after_commit"])
def test_unknown_or_bad_canary_never_attempts_rest_or_retries(fixed, mode):
    db, _ = fixed
    db.append_mode = mode
    with pytest.raises((RuntimeError, TimeoutError)):
        backfill.run_backfill(db, {}, apply=True)
    assert len(db.writes) == 1 and len(db.writes[0]) == 1


def test_protected_change_stops_after_canary_without_rollback(fixed):
    db, _ = fixed
    db.after_append = lambda: db.tables["給与明細項目"].append(["changed"])
    with pytest.raises(RuntimeError, match="protected_data_changed"):
        backfill.run_backfill(db, {}, apply=True)
    assert len(db.writes) == 1 and len(db.tables[INCOME_SHEET]) == 2


@pytest.mark.parametrize("bad", ["header", "content", "duplicate"])
def test_existing_conflict_never_repaired(fixed, bad):
    db, rows = fixed
    db.tables[INCOME_SHEET] = [INCOME_HEADERS.copy(), deepcopy(rows[0])]
    if bad == "header":
        db.tables[INCOME_SHEET][0][0] = "wrong"
    elif bad == "content":
        db.tables[INCOME_SHEET][1][2] += 1
    else:
        db.tables[INCOME_SHEET].append(deepcopy(rows[0]))
    before = deepcopy(db.tables)
    with pytest.raises(RuntimeError):
        backfill.run_backfill(db, {}, apply=True)
    assert db.tables == before and db.writes == db.schema_writes == []


def test_guard_blocks_payload_change_immediately_before_append(fixed):
    db, rows = fixed
    guarded = backfill.IncomeOnlyDB(db, rows)
    changed = deepcopy(rows)
    changed[0][4] = "different"
    for sheet, payload in [("支出明細", rows), (INCOME_SHEET, changed),
                           (INCOME_SHEET, [rows[0], rows[0]])]:
        with pytest.raises(RuntimeError, match="payload_changed"):
            guarded.append_raw(sheet, payload)
    assert db.writes == []


@pytest.mark.parametrize("bad", [None, "local", "branch", "workflow", "event", "sha", "head", "remote"])
def test_only_exact_manual_main_can_run(monkeypatch, bad):
    values = {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": "tmoriuchi1401-source/kakeibo-ai/.github/workflows/bank-income-backfill.yml@refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": "tested"}
    key = {"local": "GITHUB_ACTIONS", "branch": "GITHUB_REF", "workflow": "GITHUB_WORKFLOW_REF",
           "event": "GITHUB_EVENT_NAME", "sha": "GITHUB_SHA"}.get(bad)
    if key:
        values[key] = "wrong"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(backfill.subprocess, "check_output", lambda args, **kw:
        "wrong" if (bad == "head" and args[-1] == "HEAD") or (bad == "remote" and args[-1] == "origin/main") else "tested")
    if bad:
        with pytest.raises(RuntimeError, match="requires_dispatched_validated_main"):
            backfill.require_actions_main("tested")
    else:
        backfill.require_actions_main("tested")
