from copy import deepcopy
import re
from types import SimpleNamespace

import pytest

from app.daily_sheets import DailySheets, entered
from app.daily_view import SHEETS, cells
from app.monthly_projection import ProjectionError
from test_daily_corrections import setup, REQUEST


class Grid:
    """Small native-range fake; stores typed values and fails after mutation."""
    sid="daily"
    def __init__(self):
        self.data={};self.writes=[];self.fail_after=False;self.on_write=None
        self.svc=self
    def spreadsheets(self):return self
    def values(self):return self
    def _execute_sheet_read(self,operation):return operation().execute(num_retries=0)
    def batchGet(self,*,ranges,**kwargs):
        def execute(**ignored):
            blocks=[]
            for a1 in ranges:
                title,c1,r1,c2,r2=re.fullmatch(r"'(.+)'!([A-Z])(\d+):([A-Z])(\d+)",a1).groups()
                sid=SHEETS[title][0]
                rows=[[self.data.get((sid,r,c),"") for c in range(ord(c1)-65,ord(c2)-64)]
                      for r in range(int(r1)-1,int(r2))]
                blocks.append({"values":rows})
            return {"valueRanges":blocks}
        return SimpleNamespace(execute=execute)
    def batchUpdate(self,*,body,**kwargs):
        def execute(**ignored):
            self.writes.append(deepcopy(body))
            for req in body["requests"]:
                if "updateCells" not in req:continue
                spec=req["updateCells"];rect=spec["range"]
                for r,row in enumerate(spec["rows"],rect["startRowIndex"]):
                    for c,cell in enumerate(row["values"],rect["startColumnIndex"]):
                        self.data[(rect["sheetId"],r,c)]=entered(cell)
            if self.on_write:self.on_write()
            if self.fail_after:
                self.fail_after=False
                raise RuntimeError("lost_response")
            return {}
        return SimpleNamespace(execute=execute)
    def put(self,row,col,value,title="確認"):
        self.data[(SHEETS[title][0],row-1,col-1)]=value


def daily_setup():
    store,reader,refresh,ledger,inbox=setup()
    grid=Grid();daily=DailySheets(grid,"source",store)
    daily.verify=lambda:None
    for row,value in [(61,"a"),(63,80),(68,True)]:grid.put(row,2,value)
    grid.put(61,10,REQUEST)
    return daily,grid,ledger,reader


def test_differential_render_preserves_form_and_replay_writes_nothing():
    grid=Grid();grid.put(65,2,"本人の店舗")
    daily=DailySheets(grid,"source",None)
    output=[cells("履歴",11,[["item","",100]],width=3)]
    assert daily.update_outputs(output)["daily_changed_blocks"]==2
    assert daily.update_outputs(output)["daily_write_requests"]==0
    assert grid.data[(SHEETS["確認"][0],64,1)]=="本人の店舗"
    assert len(grid.writes)==1


def test_render_unknown_response_recovers_without_rewrite():
    grid=Grid();daily=DailySheets(grid,"source",None);grid.fail_after=True
    output=[cells("履歴",11,[["item"]],width=3)]
    with pytest.raises(RuntimeError):daily.update_outputs(output)
    assert daily.update_outputs(output)["daily_write_requests"]==0
    assert len(grid.writes)==1


def test_render_detects_bad_readback():
    grid=Grid();daily=DailySheets(grid,"source",None)
    grid.on_write=lambda:grid.put(11,1,"conflicting",title="履歴")
    with pytest.raises(ProjectionError,match="readback_failed"):
        daily.update_outputs([cells("履歴",11,[["item"]],width=3)])


def test_submission_ack_preserves_input_and_repeated_poll_does_not_write():
    daily,grid,ledger,reader=daily_setup()
    assert daily.submit(ledger)["corrections_applied"]==1
    values,token=daily.form()
    assert values[:7]==["a","",80,"","","",""] and values[7] is False
    assert token!=REQUEST and len(ledger.calls)==1
    assert daily.submit(ledger)=={"corrections_submitted":0}
    assert len(grid.writes)==2


def test_unknown_ack_response_cannot_resubmit_accounting():
    daily,grid,ledger,reader=daily_setup()
    grid.on_write=lambda:setattr(grid,"fail_after",len(grid.writes)==2)
    with pytest.raises(RuntimeError):daily.submit(ledger)
    assert daily.submit(ledger)=={"corrections_submitted":0}
    assert len(ledger.calls)==1


def test_input_edited_during_submission_is_retained_without_silent_resubmit():
    daily,grid,ledger,reader=daily_setup()
    original=ledger.update_expense_fields
    def edit(position,patch):
        original(position,patch);grid.put(63,2,90)
    ledger.update_expense_fields=edit
    assert daily.submit(ledger)["correction_input_changed"]==1
    assert daily.form()[0][2]==90 and daily.form()[0][7] is True
    # The old request's result is acknowledged; the changed text needs a new send.
    daily.submit(ledger)
    assert daily.form()[0][2]==90 and daily.form()[0][7] is False
    assert "未送信" in grid.data[(SHEETS["確認"][0],68,1)]
    assert len(ledger.calls)==1 and reader.rows[0][4]==80


def test_daily_never_targets_the_canonical_workbook():
    grid=Grid()
    with pytest.raises(ProjectionError,match="daily_copy_required"):
        DailySheets(grid,grid.sid,None)


@pytest.mark.parametrize("row,value,message", [(62,"wrong","日付"),(63,"wrong","整数"),(64,"不存在","カテゴリ")])
def test_invalid_input_is_preserved_and_reported_without_stopping_later_runs(row,value,message):
    daily,grid,ledger,reader=daily_setup()
    grid.put(row,2,value)
    result=daily.submit(ledger)
    assert result["corrections_failed"]==1 and ledger.calls==[]
    assert daily.form()[0][row-61]==value
    assert message in grid.data[(SHEETS["確認"][0],68,1)]
    assert daily.form()[0][7] is False
    assert daily.submit(ledger)=={"corrections_submitted":0}


def test_form_changes_after_catalog_rename_do_not_reinterpret_old_approval():
    daily,grid,ledger,reader=daily_setup()
    label=daily.store.data["catalog"]["categories"][0]
    grid.put(64,2,label["major"]+"｜"+label["minor"])
    original=ledger.update_expense_fields
    def changed(position,patch):
        original(position,patch)
        daily.store.data["catalog"]["categories"][0]["minor"]="新しい名前"
        grid.put(63,2,90)
    ledger.update_expense_fields=changed
    assert daily.submit(ledger)["correction_input_changed"]==1
    # Old form label is no longer an active choice. The old request must still
    # be acknowledged; no attempt to reinterpret its category is allowed.
    daily.submit(ledger)
    assert daily.form()[0][2]==90 and daily.form()[0][7] is False
    assert len(ledger.calls)==1


def test_all_period_reviews_are_bounded_and_medical_inputs_are_not_read():
    from app.daily_sheets import read_existing_reviews
    reads=[]
    def get_raw(a1):
        reads.append(a1)
        return [["old-year-medical","医療","未確認","private-link","private-reason"],
                ["closed","一般","反映済み","private-link","closed"]] if "A2:" in a1 else []
    db=SimpleNamespace(sid="source",get_raw=get_raw,_sheet_metadata=lambda:{"sheets":[{
        "properties":{"title":"領収書確認","sheetId":987,"gridProperties":{"rowCount":2001}}}]})
    reviews=read_existing_reviews(db)
    assert len(reviews)==1 and reviews[0].fixed_id=="領収書確認:old-year-medical"
    assert "private" not in reviews[0].detail
    assert reads==["'領収書確認'!A2:E1001","'領収書確認'!A1002:E2001"]
