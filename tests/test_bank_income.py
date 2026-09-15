from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib

import pytest

from app.bank_income import (
    BankIncomePipeline, INCOME_HEADERS, INCOME_SHEET, classify_deposit,
    deposit_decisions, deposits_from_imports, income_id, monthly_income,
)
from app.bank_pdf_pipeline import SOURCE, DOCOMO_SMTB_SOURCE, BankPdfResult, NormalizedBankTransaction
from app.bank_reconciliation import normalize_bank_description
from app.bank_steady_state import build_bank_daily_preview
from app.canonical_import import materialize_import_row
from app.sheets import HEADERS, SheetsDB


def bank(description="給与*勤務先", amount=1000, seed="first", **changes):
    digest = hashlib.sha256(seed.encode()).hexdigest()
    tx = NormalizedBankTransaction(SOURCE, "test-primary", "2026-08-25", description,
        amount, 1, 1, f"bankpdf:au-jibun:test-primary:{digest[:24]}", digest, "deposit")
    return replace(tx, **changes)


def imported(tx, **changes):
    row = materialize_import_row(tx.to_canonical(), imported_at=datetime(2026, 9, 1, tzinfo=timezone.utc), status="bank_income")
    for index, value in changes.items():
        row[int(index)] = value
    return row


class DB:
    sid = "test-sheet"

    def __init__(self, imports=(), installed=True):
        self.tables = {"取込データ": deepcopy(list(imports)), "支出明細": [["expense", 200]],
                       "給与明細ヘッダ": [["salary-id", 1500, 500, 1000]],
                       "給与明細項目": [["deductions", 500]]}
        if installed:
            self.tables[INCOME_SHEET] = [INCOME_HEADERS.copy()]
        self.writes = []
        self.reads = []
        self.append_mode = "normal"

    def sheet_titles(self):
        return list(self.tables)

    def get(self, rng):
        self.reads.append(rng)
        if rng == "取込データ!A2:L":
            return deepcopy(self.tables["取込データ"])
        assert rng in {f"{INCOME_SHEET}!A1:J1", f"{INCOME_SHEET}!A2:J"}
        return deepcopy(self.tables[INCOME_SHEET][:1] if rng.endswith("J1") else self.tables[INCOME_SHEET][1:])

    def append_raw(self, sheet, rows):
        assert sheet == INCOME_SHEET
        self.writes.append(deepcopy(rows))
        if self.append_mode != "missing":
            self.tables[sheet].extend(deepcopy(rows))
        if self.append_mode == "timeout_after_commit":
            raise TimeoutError("response lost")
        if self.append_mode == "corrupt":
            self.tables[sheet][-1][2] += 1


def apply(db, ids):
    return BankIncomePipeline(db).apply(tuple(ids), income_write_enabled=True, approved_spreadsheet_id=db.sid)


@pytest.mark.parametrize(("description", "outcome", "category"), [
    ("給与*勤務先", "confirmed_income", "給与"),
    ("賞与 勤務先", "confirmed_income", "賞与"),
    ("普通預金利息", "confirmed_income", "利息"),
    ("利息", "confirmed_income", "利息"),
    ("リソク", "confirmed_income", "利息"),
    ("振込 勤務先", "needs_review", ""),
    ("振込 給与プログラム特典", "needs_review", ""),
    ("給与振込特典", "needs_review", ""),
    ("振込 銀行名", "needs_review", ""),
    ("振込 預金特典", "needs_review", ""),
    ("給与 返金", "reimbursement", ""),
    ("立替精算 勤務先", "reimbursement", ""),
    ("借入 融資実行", "other_non_income", ""),
    ("定額自動入金", "needs_review", ""),
])
def test_bank_evidence_not_income_label_or_counterparty(description, outcome, category):
    result = classify_deposit(bank(description))
    assert (result.outcome, result.category) == (outcome, category)


@pytest.mark.parametrize(("kind", "outcome"), [("transfer", "transfer"), ("income", "confirmed_income"),
    ("reimbursement", "reimbursement"), ("other_nonwrite", "other_non_income"), ("needs_review", "needs_review")])
def test_existing_exact_rules_are_reused_and_account_scoped(kind, outcome):
    tx = bank("振込 確認済み")
    key = (normalize_bank_description(tx.description), "incoming", tx.account_alias)
    rules = {"confirmed_internal_transfers": frozenset({key})} if kind == "transfer" else {
        "confirmed_non_own_classifications": frozenset({(*key, kind)})}
    assert classify_deposit(tx, **rules).outcome == outcome
    other = replace(tx, account_alias="other-account", source_row_identity=tx.source_row_identity.replace("test-primary", "other-account"))
    assert classify_deposit(other, **rules).outcome == "needs_review"


def test_conflicting_rules_withhold_income():
    tx = bank()
    key = (normalize_bank_description(tx.description), "incoming", tx.account_alias)
    assert classify_deposit(tx, confirmed_internal_transfers=frozenset({key}),
        confirmed_non_own_classifications=frozenset({(*key, "income")})).reason == "conflicting_confirmed_rules"


def test_payroll_and_bank_count_bank_once_with_date_and_actual_amount():
    tx = bank()
    db = DB([imported(tx)])
    before = deepcopy(db.tables)
    result = apply(db, [tx.source_row_identity])
    assert result == {"read_only": False, "incomes_created": 1, "read_back_verified": True}
    row = db.tables[INCOME_SHEET][1]
    assert row[:3] == [income_id(tx.source_row_identity), "2026-08-25", 1000]
    assert monthly_income([row]) == {"2026-08": 1000}
    assert apply(db, [tx.source_row_identity])["incomes_created"] == 0
    assert len(db.writes) == 1
    for sheet in before:
        if sheet != INCOME_SHEET:
            assert db.tables[sheet] == before[sheet]
    assert not any("給与" in rng or "支出" in rng for rng in db.reads)
    del db.tables["給与明細ヘッダ"]
    del db.tables["給与明細項目"]
    assert BankIncomePipeline(db).preview()["existing_income"] == 1


def test_confirmed_bank_without_payroll_and_missing_income_sheet_previews():
    db = DB([imported(bank())], installed=False)
    del db.tables["給与明細ヘッダ"]
    assert BankIncomePipeline(db).preview()["planned_income_writes"] == 1
    assert INCOME_SHEET not in HEADERS
    assert INCOME_SHEET not in db.sheet_titles()
    assert db.writes == []


def test_reimport_overlap_dedupes_identity_but_preserves_distinct_same_day_amount():
    first, second = bank(), bank(seed="second")
    repeated = replace(first, source_page=3, source_row=12)
    db = DB([imported(first), imported(repeated), imported(second)])
    plan = BankIncomePipeline(db).preview()
    assert plan["duplicate_import_rows"] == 1
    assert plan["planned_income_writes"] == 2
    assert apply(db, [first.source_row_identity, second.source_row_identity])["incomes_created"] == 2
    assert monthly_income(db.tables[INCOME_SHEET][1:]) == {"2026-08": 2000}


def test_same_identity_changed_amount_is_review_not_first_wins():
    tx = bank()
    db = DB([imported(tx), imported(replace(tx, signed_amount=1500))])
    plan = BankIncomePipeline(db).preview()
    assert plan["planned_income_writes"] == 0
    assert plan["decisions"][0]["reason"] == "bank_identity_collision"


def test_parser_occurrence_suffixes_remain_distinct():
    tx = bank()
    first = replace(tx, source_row_identity=tx.source_row_identity + ":001")
    second = replace(tx, source_row_identity=tx.source_row_identity + ":002")
    db = DB([imported(first), imported(second)])
    assert BankIncomePipeline(db).preview()["planned_income_writes"] == 2


def test_income_preview_cli_uses_read_only_service_and_has_no_apply(monkeypatch, capsys):
    import app.cli as cli
    db = DB([imported(bank())], installed=False)
    service = object()
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: service)
    def make_db(sid, **kwargs):
        assert kwargs == {"service": service}
        return db
    monkeypatch.setattr(cli, "SheetsDB", make_db)
    monkeypatch.setattr(cli.Settings, "validate", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["cli", "bank-income-preview"])
    cli.main()
    import json
    assert json.loads(capsys.readouterr().out)["planned_income_writes"] == 1
    assert not db.writes
    monkeypatch.setattr("sys.argv", ["cli", "bank-income-preview", "--apply"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("mode", ["missing", "corrupt"])
def test_readback_checks_actual_content(mode):
    tx = bank()
    db = DB([imported(tx)])
    db.append_mode = mode
    with pytest.raises(RuntimeError, match="readback_mismatch"):
        apply(db, [tx.source_row_identity])
    assert len(db.writes) == 1


def test_unknown_append_result_no_retry_then_verified_replay():
    tx = bank()
    db = DB([imported(tx)])
    db.append_mode = "timeout_after_commit"
    with pytest.raises(TimeoutError):
        apply(db, [tx.source_row_identity])
    assert len(db.writes) == 1
    assert apply(db, [tx.source_row_identity])["incomes_created"] == 0
    assert len(db.writes) == 1


def test_existing_content_conflict_never_overwritten():
    tx = bank()
    db = DB([imported(tx)])
    apply(db, [tx.source_row_identity])
    db.tables[INCOME_SHEET][1][2] += 10
    with pytest.raises(RuntimeError, match="existing_content_conflict"):
        apply(db, [tx.source_row_identity])
    assert len(db.writes) == 1


def test_writer_disabled_target_bound_and_schema_required():
    tx = bank()
    db = DB([imported(tx)], installed=False)
    p = BankIncomePipeline(db)
    with pytest.raises(RuntimeError, match="write_disabled"):
        p.apply((tx.source_row_identity,))
    with pytest.raises(RuntimeError, match="target_not_approved"):
        p.apply((tx.source_row_identity,), income_write_enabled=True)
    with pytest.raises(RuntimeError, match="schema_not_installed"):
        apply(db, [tx.source_row_identity])
    assert db.writes == []


@pytest.mark.parametrize(("column", "value"), [(6, "1000.5"), (4, "2026-02-30"),
    (8, "needs_review_bank_finalization"), (9, "existing-expense"), (10, ""), (0, "payroll:one")])
def test_malformed_or_previously_linked_rows_withheld(column, value):
    row = imported(bank())
    row[column] = value
    assert BankIncomePipeline(DB([row])).preview()["planned_income_writes"] == 0


def test_connector_null_blank_cells_are_not_expense_links():
    row = imported(bank())
    row[9] = None
    assert BankIncomePipeline(DB([row])).preview()["planned_income_writes"] == 1


def test_non_income_groups_do_not_write_even_in_enabled_batch():
    transactions = [bank("振込 不明", seed="unknown"), bank("返金", seed="refund"), bank("借入", seed="loan")]
    db = DB([imported(tx) for tx in transactions])
    assert apply(db, [tx.source_row_identity for tx in transactions])["incomes_created"] == 0
    assert not db.writes


def test_new_pdf_uses_same_income_assessment_without_changing_expense_plan(monkeypatch):
    income, unknown = bank(), bank("振込 特典", seed="unknown")
    expense = bank("口座振替 公共", amount=-500, seed="expense", transaction_kind="withdrawal")
    parsed = BankPdfResult(1, 3, (income, unknown, expense), (), 0, 0,
        tuple(tx.to_canonical() for tx in (income, unknown, expense)))
    monkeypatch.setattr("app.bank_steady_state.BankPdfPipeline.parse", lambda *a, **k: parsed)
    monkeypatch.setattr("app.bank_steady_state.pdf_digest", lambda path: "d" * 64)
    db = DB()
    result = build_bank_daily_preview(db, "fake.pdf", target_spreadsheet_id=db.sid, expected_git_head="a" * 40)
    summary = result.summary["household_income"]
    assert summary["classification"]["confirmed_income"]["count"] == 1
    assert summary["classification"]["needs_review"]["count"] == 1
    assert summary["income_write_enabled"] is False
    assert result.expense_candidate_identities == (expense.source_row_identity,)
    assert db.writes == []
    saved = BankIncomePipeline(DB([imported(income), imported(unknown)])).preview()
    assert saved["classification"] == summary["classification"]


def test_raw_transport_keeps_bank_text_literal():
    calls = []
    class Service:
        def spreadsheets(self):return self
        def values(self):return self
        def append(self, **kwargs):
            calls.append(kwargs)
            return self
        def execute(self):return {}
    db = SheetsDB("test-sheet", service=Service())
    db.append_raw(INCOME_SHEET, [["=not_a_formula", "2026-08-25", 1000]])
    assert calls[0]["valueInputOption"] == "RAW"
    assert calls[0]["body"]["values"][0][0] == "=not_a_formula"


def test_monthly_aggregation_rejects_payroll_or_duplicate_provenance():
    row = classify_deposit(bank()).row()
    with pytest.raises(RuntimeError, match="duplicate_existing_id"):
        monthly_income([row, row])
    row[7] = "Payroll"
    with pytest.raises(RuntimeError, match="provenance_invalid"):
        monthly_income([row])
