from copy import deepcopy
import re
from types import SimpleNamespace

import pytest

from app.daily_sheets import DailySheets, entered
from app.daily_view import SHEETS, cells, link, literal
from app.monthly_projection import ProjectionError
from test_daily_corrections import setup, REQUEST


class Grid:
    """Small native-range fake; stores typed values and fails after mutation."""
    sid="daily"
    def __init__(self):
        self.data={};self.writes=[];self.fail_after=False;self.on_write=None;self.charts={}
        self.svc=self;self.links={};self.gets=[];self.typed={}
    def spreadsheets(self):return self
    def values(self):return self
    def _execute_sheet_read(self,operation):return operation().execute(num_retries=0)
    def get(self,*,ranges,fields,**kwargs):
        self.gets.append((ranges,fields))
        def execute(**ignored):
            sheets={}
            for a in ranges:
                title,c1,r1,c2,r2=re.fullmatch(r"'(.+)'!([A-Z])(\d+):([A-Z])(\d+)",a).groups()
                sid=SHEETS[title][0];rows=[]
                for r in range(int(r1)-1,int(r2)):
                    values=[]
                    for c in range(ord(c1)-65,ord(c2)-64):
                        key=(sid,r,c);value=self.data.get(key,"")
                        cell=({"userEnteredValue":{"formulaValue":value}} if isinstance(value,str) and value.startswith("=") else literal(value))
                        if key in self.typed and entered(self.typed[key])==value:cell=deepcopy(self.typed[key])
                        if key in self.links:cell["userEnteredFormat"]={"textFormat":{"link":{"uri":self.links[key]}}}
                        values.append(cell)
                    rows.append({"values":values})
                sheets.setdefault(sid,{"properties":{"sheetId":sid},"data":[]})["data"].append(
                    {"startRow":int(r1)-1,"startColumn":ord(c1)-65,"rowData":rows})
            return {"sheets":list(sheets.values())}
        return SimpleNamespace(execute=execute)
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
                if "addChart" in req:
                    chart=deepcopy(req["addChart"]["chart"])
                    assert chart["chartId"] not in self.charts
                    self.charts[chart["chartId"]]=chart
                if "updateChartSpec" in req:
                    spec=req["updateChartSpec"];self.charts[spec["chartId"]]["spec"]=deepcopy(spec["spec"])
                if "updateEmbeddedObjectPosition" in req:
                    spec=req["updateEmbeddedObjectPosition"];self.charts[spec["objectId"]]["position"]=deepcopy(spec["newPosition"])
                if "updateCells" not in req:continue
                spec=req["updateCells"];rect=spec["range"]
                for r,row in enumerate(spec["rows"],rect["startRowIndex"]):
                    for c,cell in enumerate(row["values"],rect["startColumnIndex"]):
                        self.data[(rect["sheetId"],r,c)]=entered(cell)
                        self.typed[(rect["sheetId"],r,c)]={"userEnteredValue":deepcopy(cell.get("userEnteredValue",{}))}
                        if "userEnteredFormat.textFormat.link" in spec["fields"]:
                            key=(rect["sheetId"],r,c)
                            uri=cell.get("userEnteredFormat",{}).get("textFormat",{}).get("link",{}).get("uri")
                            if uri:self.links[key]=uri
                            else:self.links.pop(key,None)
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


def test_native_link_migration_url_change_removal_and_replay_preserve_inputs():
    grid=Grid();daily=DailySheets(grid,"source",None)
    url="https://docs.google.com/spreadsheets/d/source/edit#gid=123&range=A4"
    grid.put(7,3,f'=HYPERLINK("{url}","開く →")')
    grid.put(65,2,"本人の入力");grid.put(68,2,True)
    output=[cells("確認",7,[[link(url,"開く →"),"保留",1200,False,'=literal']],left=2)]
    assert daily.update_outputs(output)["daily_write_requests"]==1
    key=(SHEETS["確認"][0],6,2)
    assert grid.data[key]=="開く →" and grid.links[key]==url
    assert daily.update_outputs(output)["daily_write_requests"]==0
    new_url=url.replace("A4","A19")
    output[0]["updateCells"]["rows"][0]["values"][0]=link(new_url,"開く →")
    assert daily.update_outputs(output)["daily_changed_blocks"]==1
    assert grid.links[key]==new_url
    assert daily.update_outputs(output)["daily_write_requests"]==0
    output[0]["updateCells"]["rows"][0]["values"][0]=literal("開く →")
    daily.update_outputs(output)
    assert key not in grid.links
    assert grid.data[(SHEETS["確認"][0],64,1)]=="本人の入力"
    assert grid.data[(SHEETS["確認"][0],67,1)] is True
    assert all(req["updateCells"]["fields"]=="userEnteredValue,userEnteredFormat.textFormat.link"
               for batch in grid.writes for req in batch["requests"])


@pytest.mark.parametrize("corruption",["missing","wrong"])
def test_native_link_readback_rejects_matching_label_with_wrong_link(corruption):
    grid=Grid();daily=DailySheets(grid,"source",None)
    def corrupt():
        key=(SHEETS["確認"][0],6,2)
        if corruption=="missing":grid.links.pop(key)
        else:grid.links[key]="https://wrong.invalid/"
    grid.on_write=corrupt
    with pytest.raises(ProjectionError,match="readback_failed"):
        daily.update_outputs([cells("確認",7,[[link("https://example.test/","開く →")]],left=2)])


def test_native_link_lost_write_response_recovers_without_rewrite():
    grid=Grid();daily=DailySheets(grid,"source",None);grid.fail_after=True
    output=[cells("確認",7,[[link("#gid=123&range=A4","開く →")]],left=2)]
    with pytest.raises(RuntimeError,match="lost_response"):daily.update_outputs(output)
    assert daily.update_outputs(output)["daily_write_requests"]==0


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


@pytest.mark.parametrize("successor_status",[None,"waiting","pending","applied","closed_user","closed_machine"])
@pytest.mark.parametrize("old_error",["","原本の版が変更"])
def test_daily_reviews_match_authoritative_attention_for_obsolete_rows(successor_status,old_error):
    from app.daily_sheets import read_existing_reviews
    from app.receipt_confirmation import TITLE, review_id
    from tests.test_receipt_confirmation import medical

    review,store,source_db,verify,source=medical()
    old_key=review_id("medical",source)
    source_db.rows[TITLE][0][14]="Owner note retained"
    review.capture_inputs()
    review.items[old_key].update(status="superseded",error=old_error)
    if successor_status:
        successor=dict(source,version="2")
        review.observe_medical(successor,"synthetic-folder")
        review.items[review_id("medical",successor)]["status"]=successor_status
    review.render()
    before=deepcopy(source_db.rows)
    # Keep the successor on a different read page: exclusion must use the
    # authoritative result, not just whichever other rows fit on this page.
    displayed=source_db.rows[TITLE]
    reads=[]
    def get_raw(a1):
        reads.append(a1)
        return [r[:5] for r in (displayed[:1] if "A2:" in a1 else displayed[1:])]
    db=SimpleNamespace(sid="source",get_raw=get_raw,_sheet_metadata=lambda:{"sheets":[{
        "properties":{"title":TITLE,"sheetId":987,"gridProperties":{"rowCount":2001}}}]})
    actual=read_existing_reviews(db)
    expected={TITLE+":"+key for key in review.items if review.needs_attention(key)}
    assert {item.fixed_id for item in actual}==expected
    assert all("private" not in item.detail for item in actual)
    assert source_db.rows==before and displayed[0][14]=="Owner note retained"
    assert reads==["'領収書確認'!A2:E1001","'領収書確認'!A1002:E2001"]


def test_daily_review_does_not_hide_a_real_duplicate_error():
    from app.daily_sheets import read_existing_reviews
    row=["duplicate","一般","要再確認","source-link","同日付近・同額の既存支出あり。重複を確認してください。"]
    db=SimpleNamespace(sid="source",get_raw=lambda a1:[row],_sheet_metadata=lambda:{"sheets":[{
        "properties":{"title":"領収書確認","sheetId":987,"gridProperties":{"rowCount":2}}}]})
    reviews=read_existing_reviews(db)
    assert len(reviews)==1 and reviews[0].status=="要再確認"
    assert reviews[0].url.endswith("#gid=987&range=A2")
