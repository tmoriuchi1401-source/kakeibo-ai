from copy import deepcopy

import pytest

from app.category_operations import process_category_operations
from app.category_rule_ui import CategoryRuleUIPipeline
from app.daily_category_reviews import _item
from app.sheets import SheetsDB
from test_category_backfill import BackfillDB


class OperationsDB(BackfillDB):
    def __init__(self):
        super().__init__()
        self.ui=[]; self.backfill=[]; self.confirmations=[]; self.rule_rows=[]

    def category_rule_ui_rows(self): return deepcopy(self.ui)
    def category_backfill_ui_rows(self): return deepcopy(self.backfill)
    def category_backfill_confirmation_rows(self): return deepcopy(self.confirmations)
    def category_rules(self): return deepcopy(self.rule_rows)
    def ensure_category_rule_sheet(self): pass
    def replace_category_rule_ui_rows(self,rows,header): self.ui=deepcopy(rows)
    def replace_category_backfill_ui_rows(self,rows,header): self.backfill=deepcopy(rows)
    def replace_category_backfill_confirmation_rows(self,rows,header): self.confirmations=deepcopy(rows)
    def get(self,rng):
        if rng == "'ホーム'!B4": return [["2026-08"]]
        return super().get(rng)
    def append(self,sheet,rows):
        if sheet == "カテゴリ自動分類ルール": self.rule_rows.extend(deepcopy(rows))
        else: super().append(sheet,rows)
    def update_rows(self,sheet,rows):
        dest={"カテゴリ自動分類":self.ui,"カテゴリ過去反映":self.backfill,
              "カテゴリ過去反映確認":self.confirmations}.get(sheet)
        if dest is None: return super().update_rows(sheet,rows)
        for n,row in rows: dest[n-2]=deepcopy(row)
    def consume_category_rule_ui_past_choice(self,key,result):
        for row in self.ui:
            if row[6]==key: row[5]=False


def run(db,apply=True):
    return process_category_operations(db,apply=apply,rule_enabled=True,
        save_enabled=True,preview_enabled=True,backfill_enabled=True)


def proposal(db):
    CategoryRuleUIPipeline(db,ui_enabled=True,save_enabled=True).refresh()
    return next(row for row in db.ui if row[6].startswith("group:"))


def test_decline_survives_refresh_and_allows_past_only_confirmed_processing():
    db=OperationsDB(); row=proposal(db)
    row[2:6]=["食費","外食","登録しない",True]
    run(db)
    assert not db.rule_rows and not db.category_updates
    assert next(row for row in db.ui if row[6].startswith("group:"))[4] == "登録しない"
    assert len(db.backfill)==1
    db.backfill[0][2:5]=["2026-08","2026-08",True]
    result=run(db)
    assert result["category_previews_processed"]==1
    assert len(db.requests)==1 and not db.category_updates
    assert db.ui[-1][4:6]==["登録しない",False]
    # Repeating the scheduled run must not duplicate the preview or apply it.
    run(db)
    assert len(db.requests)==1 and not db.category_updates and not db.rule_rows
    db.confirmations[0][2]=True
    run(db)
    assert len(db.category_updates)==1 and not db.rule_rows
    assert db.expenses["M-one"][1][5:7]==["食費","外食"]
    run(db)
    assert next(row for row in db.ui if row[6]=="M-one")[4]=="登録しない"


def test_checked_and_dropdown_registration_is_consumed_and_not_repeated():
    db=OperationsDB(); row=proposal(db)
    row[2:6]=["食費","外食",True,False]
    before=deepcopy(db.__dict__)
    assert run(db,False)["category_registration_pending"]==1
    assert db.__dict__==before  # Includes UI: preview cannot migrate/refresh it.
    assert run(db)["category_registration_processed"]==1
    assert len(db.rule_rows)==1 and not db.category_updates
    assert db.ui[-1][1].endswith("登録済み") and db.ui[-1][4] is False
    db.ui[-1][4]="登録する"  # Already saved: refreshing consumes stale re-checks.
    assert run(db)["category_registration_processed"]==0
    assert len(db.rule_rows)==1 and not db.category_updates


def test_registration_failure_survives_next_refresh_without_retrying():
    db=OperationsDB(); row=proposal(db)
    row[2:6]=["存在しない","カテゴリ",True,False]
    run(db)
    assert "held: invalid_category_pair" in db.ui[-1][1]
    run(db)
    assert "held: invalid_category_pair" in db.ui[-1][1]
    assert not db.rule_rows and db.ui[-1][4] is False


def test_empty_proposal_is_not_a_conflict_with_an_existing_rule():
    from test_category_backfill import condition
    db=OperationsDB()
    db.rule_rows=[condition().to_row()]
    row=proposal(db)
    assert row[2:4]==["", ""] and row[1].endswith("カテゴリを選択")
    assert "競合" not in row[1]


def test_saved_rule_preview_does_not_scan_unrelated_source_checkboxes(monkeypatch):
    from test_category_backfill import condition
    db=OperationsDB(); db.rule_rows=[condition().to_row()]
    run(db)
    db.backfill[0][2:5]=["2026-08","2026-08",True]
    monkeypatch.setattr(db,"consume_category_rule_ui_past_choice",lambda *args:pytest.fail("saved rules have no source checkbox"))
    assert run(db)["category_previews_processed"]==1
    assert len(db.requests)==1 and not db.category_updates


def test_category_diagnostics_never_include_private_error_body():
    from app.category_operations import CategoryOperationFailure
    from types import SimpleNamespace
    cause=RuntimeError("PRIVATE ACCOUNT DETAILS")
    cause.resp=SimpleNamespace(status=429)
    failure=CategoryOperationFailure(cause)
    assert failure.details=={"category_exception":"RuntimeError","category_http_status":429}
    assert "PRIVATE" not in str(failure)


def test_many_preview_choices_read_default_month_only_once(monkeypatch):
    from app.category_backfill_ui import CategoryBackfillUIPipeline
    from test_category_backfill import condition
    db=OperationsDB(); db.rule_rows=[condition().to_row() for _ in range(20)]
    reads=[]; original=db.get
    def get(rng):
        reads.append(rng)
        return original(rng)
    monkeypatch.setattr(db,"get",get)
    CategoryBackfillUIPipeline(db,ui_enabled=True,apply_enabled=False).refresh()
    assert len(db.backfill)==20
    assert reads.count("'ホーム'!B4")==1


def test_category_runtime_shares_read_budget_with_projection(monkeypatch):
    from app import category_operations as operations
    from app.sheets import SheetsReadPacer
    seen={}
    def db(sid,**kwargs):
        seen.update(kwargs)
        return object()
    def projection(env,**kwargs):
        assert kwargs["read_pacer"] is seen["read_pacer"]
        return {"refreshed":1}
    monkeypatch.setattr("app.production_flow.verify_execution_boundary",lambda *args:None)
    monkeypatch.setattr("app.google_clients.sheets_service",lambda:object())
    monkeypatch.setattr("app.sheets.SheetsDB",db)
    monkeypatch.setattr("app.projection_runtime.run_projection",projection)
    monkeypatch.setattr(operations,"process_category_operations",lambda *args,**kwargs:{})
    assert operations.run_category_operations({"CATEGORY_RULE_UI_ENABLED":"true"},apply=True)=={"refreshed":1}
    assert isinstance(seen["read_pacer"],SheetsReadPacer) and seen["read_retry_base"]==20


def test_decline_removes_unclassified_proposal_from_daily_queue_but_keeps_past_request():
    row=["条件","カテゴリを選択","","","登録しない",False,"group:key","id"]
    assert _item("rule",row,"url",{},combined=True) is None
    row[5]=True
    assert _item("rule",row,"url",{},combined=True).status == "処理待ち"
    row[4]="登録する"; row[5]=False
    assert _item("rule",row,"url",{},combined=True).status == "処理待ち"


@pytest.mark.parametrize("choice,physical",[(True,"登録する"),(False,"未選択"),("登録しない","登録しない")])
def test_dropdown_roundtrip_preserves_all_three_decisions_and_past_checkbox(choice,physical):
    logical=["条件","状態","食費","外食",choice,True,"key","id"]+[""]*4
    row=SheetsDB._workflow_physical_row("rule",logical,compact=True)
    assert row[4:6]==[physical,True]
    assert SheetsDB._workflow_logical_row("rule",row,compact=True)==logical
