from copy import deepcopy
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.bank_meaning_rules import RULE_SHEET
from app.bank_pdf_pipeline import NormalizedBankTransaction, SOURCE
from app.bank_review_decisions import DECISION_SHEET, evaluate, parse_rows
from app.bank_review_groups import make_groups, encoded
from app.bank_review_requests import BankReviewRequestProcessor
from app.bank_review_store import BankReviewStore
from app.bank_review_ui import build_rows, submitted_answers
from test_bank_review_ui import transaction

REQUEST = "12345678-1234-1234-1234-123456789abc"
NOW = datetime.fromisoformat("2026-10-01T01:00:00+00:00")


class MemoryBankDB:
    sid = "synthetic_sheet"
    def __init__(self):
        self.sheets, self.tables, self.calls = {}, {}, []
        self.svc = self
        self.error = None
        self.after_write = None
    def _invalidate_sheet_metadata(self): pass
    def _sheet_metadata(self):
        return {"sheets": [{"properties": deepcopy(properties)} for properties in self.sheets.values()]}
    def get_raw(self, rng):
        title = rng.split("!")[0].strip("'")
        return deepcopy(self.tables[title])
    def spreadsheets(self): return self
    def batchUpdate(self, **kwargs):
        self.pending = kwargs
        return self
    def execute(self, *, num_retries):
        assert num_retries == 0
        self.calls.append(deepcopy(self.pending))
        if self.error == "before":
            raise TimeoutError("private failure must not be logged")
        for request in self.pending["body"]["requests"]:
            if "addSheet" in request:
                properties = request["addSheet"]["properties"]
                title = properties["title"]
                assert title in {RULE_SHEET, DECISION_SHEET}
                self.sheets[title] = deepcopy(properties)
                self.tables[title] = []
            elif "appendDimension" in request:
                value = request["appendDimension"]
                title = next(title for title, props in self.sheets.items() if props["sheetId"] == value["sheetId"])
                self.sheets[title]["gridProperties"]["rowCount"] += value["length"]
            elif "updateCells" in request:
                value = request["updateCells"]
                title = next(title for title, props in self.sheets.items() if props["sheetId"] == value["range"]["sheetId"])
                assert title in {RULE_SHEET, DECISION_SHEET}
                for offset, data in enumerate(value["rows"], value["range"]["startRowIndex"]):
                    while len(self.tables[title]) <= offset:
                        self.tables[title].append([])
                    self.tables[title][offset] = [next(iter(cell.get("userEnteredValue", {"stringValue": ""}).values()))
                                                  for cell in data["values"]]
            else:
                pytest.fail("unbounded request")
        if self.after_write:
            self.after_write(self)
        if self.error == "after":
            raise TimeoutError("uncertain metadata write")
        return {}


def prepared(*, future=False, other=False):
    row = transaction(identity="bankpdf:au-jibun:jibun-primary:" + "a" * 24, source=SOURCE)
    data = [row]
    if other:
        data.append(transaction(identity="bankpdf:au-jibun:jibun-primary:" + "b" * 24,
            source_hash="b" * 64, source=SOURCE, description="振込 別の特典"))
    groups = make_groups(data)
    rows = build_rows(groups)
    for index in range(0, len(rows), 2):
        rows[index][2:6] = ["収入", "その他確認済収入", "登録する" if future else "今回のみ", True]
    txs = {row["identity"]: NormalizedBankTransaction(row["source"], row["account_alias"], row["date"],
        row["description"], row["signed_amount"], row["page"], row["row"], row["identity"],
        row["source_hash"], row["transaction_kind"]) for row in data}
    source = SimpleNamespace(groups=groups, transactions=txs,
        accounts={("au-jibun", "jibun-primary"), ("chiba", "chiba-primary")},
        legacy_rules={}, legacy_rules_serialized={})
    return MemoryBankDB(), rows, source


def processor(db, source):
    return BankReviewRequestProcessor(db, lambda: deepcopy(source), clock=lambda: NOW)


def test_once_only_answer_persists_exact_members_without_future_rule_or_ledger_write():
    db, rows, source = prepared()
    counts, output = processor(db, source).process(rows, REQUEST)
    assert counts["bank_groups_confirmed"] == 1 and counts["bank_metadata_writes"] == 1
    assert counts["bank_rules_registered"] == counts["bank_ledger_writes"] == 0
    assert not output[0][5] and "用途確認済み" in output[0][1]
    master = BankReviewStore(db).read()
    decisions = master.records(DECISION_SHEET)
    assert not decisions[0].future and not master.records(RULE_SHEET)
    tx = next(iter(source.transactions.values()))
    assert evaluate(tx, decisions, [])["classification"] == "income"
    future_tx = replace(tx, source_row_identity="bankpdf:au-jibun:jibun-primary:" + "b" * 24, source_row_hash="b" * 64)
    assert evaluate(future_tx, decisions, [])["state"] == "no_sheet_rule"


def test_future_rule_and_group_answer_save_in_one_atomic_batch_and_replay_writes_zero():
    db, rows, source = prepared(future=True)
    first, _ = processor(db, source).process(rows, REQUEST)
    assert first["bank_rules_registered"] == first["bank_groups_confirmed"] == 1
    assert first["bank_metadata_writes"] == 2 and len(db.calls) == 1
    second, _ = processor(db, source).process(rows, REQUEST)
    assert second["bank_metadata_writes"] == second["bank_ledger_writes"] == 0
    assert second["bank_groups_already_confirmed"] == second["bank_rules_already_registered"] == 1
    assert len(db.calls) == 1


def test_only_checked_group_saved_other_group_does_not_gain_approval():
    db, rows, source = prepared(other=True)
    rows[2][5] = False
    counts, _ = processor(db, source).process(rows, REQUEST)
    assert counts["bank_groups_checked"] == counts["bank_groups_confirmed"] == 1
    assert len(BankReviewStore(db).read().records(DECISION_SHEET)) == 1


def test_live_membership_change_holds_without_any_metadata_write():
    db, rows, source = prepared()
    source.groups[0]["membership_digest"] = "c" * 64
    counts, output = processor(db, source).process(rows, REQUEST)
    assert counts["bank_held"] == 1 and not db.calls
    assert "再表示" in output[0][1]


def test_source_change_immediately_before_apply_prevents_all_metadata_writes():
    db, rows, source = prepared(future=True)
    calls = []
    def reread():
        result = deepcopy(source)
        calls.append(1)
        if len(calls) == 2:
            result.groups[0]["membership_digest"] = "d" * 64
        return result
    counts, _ = BankReviewRequestProcessor(db, reread, clock=lambda: NOW).process(rows, REQUEST)
    assert counts["bank_held"] == 1 and counts["bank_metadata_writes"] == 0
    assert not db.calls and len(calls) == 2


def test_legacy_conflict_and_explicit_review_cannot_be_overwritten():
    for meaning in ("needs_review", "reimbursement", "transfer"):
        db, rows, source = prepared(future=True)
        group = source.groups[0]
        source.legacy_rules = {"confirmed_non_own_classifications": {
            (group["normalized_description"], group["direction"], group["account_alias"], meaning)}}
        counts, _ = processor(db, source).process(rows, REQUEST)
        assert counts["bank_held"] == 1 and not db.calls


def test_conflicting_prior_answer_does_not_append_or_rewrite():
    db, rows, source = prepared()
    processor(db, source).process(rows, REQUEST)
    rows[0][2:5] = ["返金", "", "今回のみ"]
    counts, _ = processor(db, source).process(rows, REQUEST)
    assert counts["bank_held"] == 1 and counts["bank_metadata_writes"] == 0
    assert len(db.calls) == 1


def test_missing_answer_and_individual_review_are_held_not_auto_approved():
    for meaning, category in (("未選択", ""), ("個別確認", ""), ("収入", "")):
        db, rows, source = prepared()
        rows[0][2:4] = [meaning, category]
        counts, _ = processor(db, source).process(rows, REQUEST)
        assert counts["bank_held"] == 1 and not db.calls


def test_transfer_uses_bank_label_and_alias_but_never_creates_income():
    db, rows, source = prepared(future=True)
    rows[0][2:4] = ["自己口座間振替", ""]
    rows[1][2:4] = ["千葉銀行", "chiba-primary"]
    counts, _ = processor(db, source).process(rows, REQUEST)
    assert counts["bank_groups_confirmed"] == 1 and counts["bank_ledger_writes"] == 0
    master = BankReviewStore(db).read()
    tx = next(iter(source.transactions.values()))
    resolution = evaluate(tx, master.records(DECISION_SHEET), master.records(RULE_SHEET))
    assert resolution["classification"] == "transfer" and resolution["source_bank"] == "chiba"


@pytest.mark.parametrize("failure", ["before", "after"])
def test_uncertain_write_is_not_retried(failure):
    db, rows, source = prepared(future=True)
    db.error = failure
    with pytest.raises(TimeoutError):
        processor(db, source).process(rows, REQUEST)
    assert len(db.calls) == 1
    if failure == "after":
        db.error = None
        second, _ = processor(db, source).process(rows, REQUEST)
        assert second["bank_metadata_writes"] == 0 and len(db.calls) == 1


def test_readback_mismatch_does_not_claim_success_or_retry():
    db, rows, source = prepared()
    db.after_write = lambda database: database.tables[DECISION_SHEET][1].__setitem__(12, False)
    with pytest.raises(ValueError, match="readback_failed"):
        processor(db, source).process(rows, REQUEST)
    assert len(db.calls) == 1


def test_master_header_or_active_tampering_is_fail_closed():
    db, rows, source = prepared()
    processor(db, source).process(rows, REQUEST)
    db.tables[DECISION_SHEET][0][1] = "変更された見出し"
    with pytest.raises(ValueError, match="header_invalid"):
        BankReviewStore(db).read()


def test_master_change_just_before_mutation_is_rejected():
    db, rows, source = prepared()
    store = BankReviewStore(db)
    before = store.read()
    processor(db, source).process(rows, REQUEST)
    with pytest.raises(ValueError, match="master_changed"):
        store.commit(before, {DECISION_SHEET: [(2, db.tables[DECISION_SHEET][1])]})
    assert len(db.calls) == 1
