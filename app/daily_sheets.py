"""Bounded daily workbook I/O; differential outputs never overwrite inputs."""
from __future__ import annotations

from hashlib import sha256
import json
from uuid import uuid4

from .daily_corrections import DailyCorrections, INPUT_ERRORS
from .daily_view import (SHEETS, OWNED_MARKER, STATE_LABELS, ReviewItem, cells, render_requests)
from .monthly_projection import ProjectionError
from .projection_refresh import load_catalog, load_month


def a1(title,first,last,left,right):
    return f"'{title}'!{chr(65+left)}{first}:{chr(64+right)}{last}"


def entered(cell):
    value=cell.get("userEnteredValue",{})
    return next(iter(value.values()),"")


class DailySheets:
    def __init__(self,db,source_id,store):
        if db.sid==source_id:raise ProjectionError("daily_copy_required")
        self.db,self.source_id,self.store=db,source_id,store

    def verify(self):
        meta=self.db._execute_sheet_read(lambda:self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid,fields="sheets(properties),developerMetadata"))
        shapes={s["properties"]["title"]:s["properties"] for s in meta.get("sheets",[])}
        if set(shapes)!=set(SHEETS):raise ProjectionError("daily_sheet_contract_changed")
        if any(shapes[t]["sheetId"]!=spec[0] for t,spec in SHEETS.items()):
            raise ProjectionError("daily_sheet_contract_changed")
        if sum(p["gridProperties"]["rowCount"]*p["gridProperties"]["columnCount"] for p in shapes.values())>100_000:
            raise ProjectionError("daily_cell_budget_exceeded")
        if not any(m.get("metadataKey")==OWNED_MARKER and
                   m.get("metadataValue")==sha256(self.source_id.encode()).hexdigest() for m in meta.get("developerMetadata",[])):
            raise ProjectionError("daily_source_binding_mismatch")
        return meta

    def read_ranges(self,ranges,*,formulas=False):
        response=self.db._execute_sheet_read(lambda:self.db.svc.spreadsheets().values().batchGet(
            spreadsheetId=self.db.sid,ranges=ranges,
            valueRenderOption="FORMULA" if formulas else "UNFORMATTED_VALUE",dateTimeRenderOption="SERIAL_NUMBER"))
        blocks=response.get("valueRanges",[])
        if len(blocks)!=len(ranges):raise ProjectionError("daily_read_incomplete")
        return [b.get("values",[]) for b in blocks]

    def controls(self,current_month):
        blocks=self.read_ranges(["'履歴'!B3:B6","'確認'!B3:B3","'ホーム'!B3:B3","'推移'!B3:B4","'設定'!B22:B23","'推移'!B145:B145","'設定'!B101:B101",
            "'履歴'!B9:B9","'推移'!B17:B17","'確認'!B73:B75","'確認'!B64:B64","'確認'!B61:B61"])
        def val(block,row,default):
            value=blocks[block][row][0] if len(blocks[block])>row and blocks[block][row] else default
            return default if value=="" else value
        return {"month":val(0,0,"直近13か月"),"category":val(0,1,"すべて"),"search":val(0,2,""),
                "page":val(0,3,1),"review_page":val(1,0,1),"home_month":val(2,0,current_month),
                "trend_year":val(3,0,current_month[:4]),"trend_category":val(3,1,"すべて"),
                "coverage_month":val(4,0,current_month),"coverage_page":val(4,1,1),"breakdown_page":val(5,0,1),"category_page":val(6,0,1),
                "history_choice_page":val(7,0,1),"trend_choice_page":val(8,0,1),"correction_category_page":val(9,0,1),
                "expense_choice_page":val(9,2,1),"correction_category":val(10,0,""),"correction_choice":val(11,0,"")}

    def update_outputs(self,requests):
        titles={spec[0]:title for title,spec in SHEETS.items()}
        content=[r["updateCells"] for r in requests if "updateCells" in r]
        ranges=[a1(titles[r["range"]["sheetId"]],r["range"]["startRowIndex"]+1,r["range"]["endRowIndex"],
                   r["range"]["startColumnIndex"],r["range"]["endColumnIndex"]) for r in content]
        current=self.read_ranges(ranges,formulas=True) if ranges else []
        changes=[]
        for spec,observed in zip(content,current):
            target=spec["range"]
            for offset,desired in enumerate(spec["rows"]):
                old=observed[offset] if len(observed)>offset else []
                changed=[]
                for column,value in enumerate(desired["values"]):
                    actual=old[column] if len(old)>column else ""
                    if actual!=entered(value):changed.append((column,value))
                # Only changed contiguous cells are written, including clearing
                # stale output. Blank padding is never sent on every refresh.
                groups=[]
                for column,value in changed:
                    if groups and column==groups[-1][0]+len(groups[-1][1]):groups[-1][1].append(value)
                    else:groups.append((column,[value]))
                for column,values in groups:
                    changes.append({"updateCells":{"range":{**target,
                        "startRowIndex":target["startRowIndex"]+offset,"endRowIndex":target["startRowIndex"]+offset+1,
                        "startColumnIndex":target["startColumnIndex"]+column,
                        "endColumnIndex":target["startColumnIndex"]+column+len(values)},
                        "rows":[{"values":values}],"fields":"userEnteredValue"}})
        formatting=[r for r in requests if "updateCells" not in r]
        combined=changes+formatting
        for first in range(0,len(combined),100):
            self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,
                body={"requests":combined[first:first+100]}).execute(num_retries=0)
        if changes:
            actual=self.read_ranges(ranges,formulas=True)
            for spec,observed in zip(content,actual):
                for i,row in enumerate(spec["rows"]):
                    old=observed[i] if i<len(observed) else []
                    if any((old[j] if j<len(old) else "")!=entered(value) for j,value in enumerate(row["values"])):
                        raise ProjectionError("daily_output_readback_failed")
        return {"daily_changed_blocks":len(changes),"daily_write_requests":len(combined)}

    def refresh(self,*,current_month,updated_at,reviews,ledger_sheet_id=None):
        self.verify()
        from .daily_coverage import CoverageForm
        from .daily_category_management import CategoryForm
        controls=self.controls(current_month)
        controls["coverage_enabled"]=CoverageForm(self).installed()
        controls["category_management_enabled"]=CategoryForm(self).installed()
        from .daily_choices import installed
        controls["choice_pages_enabled"]=installed(self)
        catalog=load_catalog(self.store.read("catalog"))
        summary=self.store.read("summary")
        if summary is None:raise ProjectionError("projection_bootstrap_required")
        requests=render_requests(read_month=lambda month:load_month(self.store.read("month-"+month)),
            summary=summary,catalog=catalog,current_month=current_month,source_id=self.source_id,
            controls=controls,reviews=reviews,updated_at=updated_at,
            ledger_sheet_id=ledger_sheet_id)
        return self.update_outputs(requests)

    def form(self):
        blocks=self.read_ranges(["'確認'!B61:B68","'確認'!J61:J61"])
        values=[rows[0] if rows else "" for rows in blocks[0]]
        values+= [""]*(8-len(values))
        token=blocks[1][0][0] if blocks[1] and blocks[1][0] else ""
        return values,token

    def submit(self,source_db):
        """Called only by apply, never by display refresh."""
        self.verify()
        values,token=self.form()
        inbox=DailyCorrections(self.store,source_db)
        old=inbox._requests()["requests"].get(token)
        if values[7] is not True and not old:return {"corrections_submitted":0}
        expense_id=str(values[0]).split("｜",1)[0].strip()
        new_submission=old is None
        form_digest=sha256(json.dumps(values[:7],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        changed_submission=bool(old and old.get("form_digest")!=form_digest)
        if not old:
            try:
                changes={name:value for name,value in zip(["date","amount","category_label","merchant","item","note"],values[1:7]) if value!=""}
                if "category_label" in changes:
                    label=changes.pop("category_label")
                    from .daily_choices import category_id
                    try:changes["category_id"]=category_id(load_catalog(self.store.read("catalog")),label,active_only=True)
                    except ProjectionError:raise ProjectionError("correction_category_invalid") from None
                old=inbox.prepare(token,expense_id,changes,form_digest=form_digest)
            except ProjectionError as error:
                if str(error) not in INPUT_ERRORS:raise
                old=inbox.reject(token,expense_id,str(error),form_digest=form_digest)
        if old["state"] in {"queued","pending"}:
            self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,body={"requests":[
                cells("確認",69,[[STATE_LABELS[old["state"]]],[old["updated_at"]]],left=1,width=1)
            ]}).execute(num_retries=0)
        result=inbox.apply(token)
        # A new edit during processing keeps its text/checkbox/token intact.
        if self.form()!=(values,token):return {"corrections_submitted":int(new_submission),"correction_input_changed":1}
        state=STATE_LABELS[result["state"]]+("・変更した入力は未送信です" if changed_submission else "")
        if result.get("error") in INPUT_ERRORS:state+="・"+INPUT_ERRORS[result["error"]]
        updates=[cells("確認",69,[[state],[result["updated_at"]]],left=1,width=1),
                 cells("確認",68,[[False]],left=1,width=1),
                 cells("確認",61,[["REQ-"+uuid4().hex]],left=9,width=1)]
        self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,body={"requests":updates}).execute(num_retries=0)
        ack=self.read_ranges(["'確認'!B69:B70","'確認'!B68:B68","'確認'!J61:J61"])
        expected=[[[state],[result["updated_at"]]],[[False]],[[entered(updates[2]["updateCells"]["rows"][0]["values"][0])]]]
        if ack!=expected:raise ProjectionError("correction_ack_readback_failed")
        return {"corrections_submitted":int(new_submission),"corrections_applied":int(result["state"]=="applied"),
                "corrections_failed":int(result["state"]=="failed")}


def read_existing_reviews(db):
    """All-year unresolved queues; no ledger history read and no input writes."""
    specs={"要確認":(15,14),"Amazon要確認":(12,None),"領収書確認":(5,2)}
    metadata=db._sheet_metadata()
    result=[]
    for sheet in metadata.get("sheets",[]):
        p=sheet["properties"];title=p["title"]
        if title not in specs:continue
        width,status_col=specs[title]
        for first in range(2,p["gridProperties"]["rowCount"]+1,1000):
            last=min(first+999,p["gridProperties"]["rowCount"])
            for offset,raw in enumerate(db.get_raw(a1(title,first,last,0,width))):
                row=list(raw)+[""]*max(0,width-len(raw))
                if not row[0]:continue
                if title=="Amazon要確認":
                    # Legacy schema is read until money-mode cutover removes it.
                    # No amount/status decisions are inferred from order events.
                    status="要確認"
                    if "反映済み" in row:continue
                    detail="既存のAmazon確認。切替時に金銭例外へ整理します。"
                elif title=="領収書確認":
                    status=str(row[status_col])
                    if status in {"反映済み","自動反映済み","変更不要（本人判断）","変更不要（機械判断）"}:continue
                    detail="専用画面で確認してください。" if str(row[1]) in {"医療","受付保留"} else str(row[4])
                else:
                    if str(row[status_col])=="反映済み":continue
                    status="保留" if str(row[9])=="保留" else "要確認"
                    detail=str(row[7])
                url=f"https://docs.google.com/spreadsheets/d/{db.sid}/edit#gid={p['sheetId']}&range=A{first+offset}"
                result.append(ReviewItem(title+":"+str(row[0]),title,detail,status,url))
    return result
