from copy import deepcopy
import pytest

from app.bank_review_decisions import DECISION_SHEET
from app.bank_review_store import BankReviewStore
from app.bank_review_ui import BANK_UI_HEADERS
from app.category_sheet_requests import execute_request, parse_snapshot
from app.sheets import WORKFLOW_MARKERS, SheetsDB
from test_category_sheet_requests import RequestDB, blocks, Store, ENV, REQUEST
from test_bank_review_requests import prepared, processor


class CombinedUI(RequestDB):
    def __init__(self, bank_rows):
        super().__init__()
        self.bank_rows = deepcopy(bank_rows)
    def _category_workflow_blocks(self):
        result = blocks(self)
        result["bank"] = (BANK_UI_HEADERS.copy(), deepcopy(self.bank_rows))
        self._category_workflow_last_read = repr(result)
        return result
    def _write_category_workflow_blocks(self, value):
        super()._write_category_workflow_blocks(value)
        self.bank_rows = deepcopy(value["bank"][1])


def snapshot(db):
    physical = []
    for section, (header, rows) in db._category_workflow_blocks().items():
        physical += [[WORKFLOW_MARKERS[section]] + [""] * 11,
            SheetsDB._workflow_physical_row(section, header, compact=True, header=True)]
        physical += [SheetsDB._workflow_physical_row(section, row, compact=True) for row in rows]
    return parse_snapshot([["TRUE" if value is True else "FALSE" if value is False else str(value)
                            for value in row] for row in physical])


def test_shared_request_saves_captured_bank_answer_and_keeps_later_user_edit_for_next_submission():
    bank_db, rows, source = prepared()
    db = CombinedUI(rows)
    store = Store(snapshot(db))
    db.bank_rows[0][2:4] = ["対象外", ""]
    result = execute_request(db, REQUEST, env={**ENV, "BANK_REVIEW_ENABLED": "true"},
        store=store, bank_processor=processor(bank_db, source), refresh_projection=lambda: {})
    decision = BankReviewStore(bank_db).read().records(DECISION_SHEET)[0]
    assert decision.classification == "income" and decision.income_category == "その他確認済収入"
    assert db.bank_rows[0][2:4] == ["対象外", ""] and db.bank_rows[0][5] is True
    assert result["bank_groups_confirmed"] == result["category_later_edits_retained"] == 1
    assert result["bank_ledger_writes"] == 0 and not db.rule_rows and not db.category_updates
    assert store.states == ["running", "complete"]
    before = len(bank_db.calls)
    assert execute_request(db, REQUEST, env=ENV, store=store,
        bank_processor=processor(bank_db, source), refresh_projection=lambda: {}) == {"category_request_ignored": 1}
    assert len(bank_db.calls) == before


def test_shared_request_reports_held_bank_answer_without_saving_or_claiming_it_resolved():
    bank_db, rows, source = prepared()
    rows[0][2:4] = ["個別確認", ""]
    db = CombinedUI(rows)
    store = Store(snapshot(db))
    result = execute_request(db, REQUEST, env={**ENV, "BANK_REVIEW_ENABLED": "true"},
        store=store, bank_processor=processor(bank_db, source), refresh_projection=lambda: {})
    assert result["bank_held"] == 1 and not bank_db.calls
    assert store.states == ["running", "review"] and "個別確認" in db.bank_rows[0][1]


def test_bank_refresh_follows_saved_answers_and_final_merge_without_consuming_later_edits():
    bank_db, rows, source = prepared()
    db = CombinedUI(rows)
    store = Store(snapshot(db))
    db.bank_rows[0][2:4] = ["対象外", ""]
    refreshed = []
    def refresh():
        assert BankReviewStore(bank_db).read().records(DECISION_SHEET)
        assert db.bank_rows[0][2:4] == ["対象外", ""]
        assert store.states == ["running"]
        refreshed.append(True)
        return {"bank_confirmation_groups": 1, "bank_ui_updates": 1}
    result = execute_request(db, REQUEST, env={**ENV, "BANK_REVIEW_ENABLED": "true"},
        store=store, bank_processor=processor(bank_db, source), bank_refresh=refresh, refresh_projection=lambda: {})
    assert result["bank_ui_updates"] == 1 and refreshed == [True]
    assert store.states == ["running", "complete"]


def test_bank_settlement_runs_between_answer_readback_and_refresh_and_reports_remaining_pdfs():
    bank_db, rows, source = prepared()
    db = CombinedUI(rows)
    store = Store(snapshot(db))
    order = []
    def replay(confirmed):
        assert confirmed == 1 and BankReviewStore(bank_db).read().records(DECISION_SHEET)
        assert not db.bank_rows[0][5] and store.states == ["running"]
        order.append("replay")
        return {"bank_replay_completed": 1, "bank_ledger_writes": 0,
                "bank_replay_files_processed": 0, "bank_replay_files_withheld": 4}
    def refresh():
        order.append("refresh")
        return {"bank_income_missing": 6}
    result = execute_request(db, REQUEST, env={**ENV, "BANK_REVIEW_ENABLED": "true"}, store=store,
        bank_processor=processor(bank_db, source), bank_replay=replay, bank_refresh=refresh,
        refresh_projection=lambda: order.append("projection") or {})
    assert order == ["replay", "refresh", "projection"]
    assert result["bank_replay_files_withheld"] == 4 and store.states == ["running", "review"]


def test_failed_settlement_refreshes_display_keeps_saved_meaning_and_does_not_retry():
    bank_db, rows, source = prepared()
    db = CombinedUI(rows)
    store = Store(snapshot(db))
    order = []
    def replay(confirmed):
        order.append("replay")
        raise ValueError("synthetic settlement stopped")
    def refresh():
        assert BankReviewStore(bank_db).read().records(DECISION_SHEET)
        order.append("refresh")
        return {}
    from app.category_operations import CategoryOperationFailure
    with pytest.raises(CategoryOperationFailure):
        execute_request(db, REQUEST, env={**ENV, "BANK_REVIEW_ENABLED": "true"}, store=store,
            bank_processor=processor(bank_db, source), bank_replay=replay, bank_refresh=refresh,
            refresh_projection=lambda: pytest.fail("projection after failed settlement"))
    assert order == ["replay", "refresh"] and store.states == ["running", "error"]


def test_all_held_bank_answers_do_not_start_financial_replay():
    bank_db, rows, source = prepared()
    rows[0][2:4] = ["個別確認", ""]
    db = CombinedUI(rows)
    store = Store(snapshot(db))
    execute_request(db, REQUEST, env={**ENV, "BANK_REVIEW_ENABLED": "true"}, store=store,
        bank_processor=processor(bank_db, source), bank_replay=lambda confirmed: pytest.fail("no approved meaning"),
        refresh_projection=lambda: {})
    assert store.states == ["running", "review"] and not bank_db.calls
