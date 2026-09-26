"""Refresh migrated category choices without touching operator inputs.

Called by the existing locked daily apply stage. The migration has already
audited every consumer; only the two retained category dropdown columns are
read here, using validation-only masks. Ordinary refresh does not shrink the
grid, so a removed choice cannot invalidate an existing bounded reference.
"""
from copy import deepcopy

from .compact_categories import TITLE, _cells, category_condition, category_rows, compact_helper
from .monthly_projection import ProjectionError, month_key


def _values(db,extent):
    rows=[]
    for first in range(1,extent+1,1000):
        last=min(first+999,extent)
        block=db.get_raw(f"'{TITLE}'!A{first}:D{last}")
        rows.extend([(list(r)+[""]*4)[:4] for r in block]+[[""]*4 for _ in range(last-first+1-len(block))])
    return rows


def _validations(db,meta):
    from .sheets import CATEGORY_WORKFLOW_SHEET
    found=[]
    for sheet in meta["sheets"]:
        props=sheet["properties"];title=props["title"]
        col={CATEGORY_WORKFLOW_SHEET:2,"要確認":13}.get(title)
        if col is None:continue
        letter=chr(65+col)
        for first in range(2,props["gridProperties"]["rowCount"]+1,2000):
            last=min(first+1999,props["gridProperties"]["rowCount"])
            response=db._execute_sheet_read(lambda:db.svc.spreadsheets().get(spreadsheetId=db.sid,
                ranges=[f"'{title}'!{letter}{first}:{letter}{last}"],
                fields="sheets(data(startRow,rowData(values(dataValidation))))"))
            for returned in response.get("sheets",[]):
                for data in returned.get("data",[]):
                    for row,cells in enumerate(data.get("rowData",[]),data.get("startRow",first-1)):
                        rule=next(iter(cells.get("values",[])),{}).get("dataValidation",{})
                        condition=rule.get("condition",{})
                        values=condition.get("values",[])
                        if not any(TITLE in str(v.get("userEnteredValue","")) for v in values):continue
                        if condition.get("type")!="ONE_OF_RANGE":raise ProjectionError("compact_category_validation_changed")
                        found.append((props["sheetId"],row,col,rule))
    return found


def sync_choices(db,catalog):
    meta=db._execute_sheet_read(lambda:db.svc.spreadsheets().get(spreadsheetId=db.sid,
        fields="sheets(properties,developerMetadata)"))
    helper=compact_helper(meta)
    if helper is None:raise ProjectionError("compact_category_migration_required")
    old_extent=helper["properties"]["gridProperties"]["rowCount"]
    desired=category_rows(catalog);extent=max(old_extent,len(desired))
    desired += [[""]*4 for _ in range(extent-len(desired))]
    before=_values(db,old_extent)
    if before==desired:return {"category_choice_writes":0}
    old_rules=_validations(db,meta) if extent>old_extent else []
    grown=deepcopy(helper);grown["properties"]["gridProperties"]["rowCount"]=extent
    expected=[(sid,row,col,{**rule,"condition":category_condition(grown)}) for sid,row,col,rule in old_rules]
    sid=helper["properties"]["sheetId"];requests=[]
    if extent>old_extent:
        requests.append({"appendDimension":{"sheetId":sid,"dimension":"ROWS","length":extent-old_extent}})
    # Changed contiguous rows only; the inactive tail is cleared literally.
    before += [[""]*4 for _ in range(extent-len(before))]
    runs=[]
    for i,(old,new) in enumerate(zip(before,desired)):
        if old==new:continue
        if runs and runs[-1][1]==i:runs[-1][1]=i+1
        else:runs.append([i,i+1])
    requests += [_cells(sid,first+1,0,desired[first:last],4) for first,last in runs]
    for old,new in zip(old_rules,expected):
        if old==new:continue
        target,row,col,rule=new
        last=requests[-1].get("setDataValidation",{}) if requests else {}
        prior=last.get("range",{})
        if (last.get("rule")==rule and prior.get("sheetId")==target and
                prior.get("endRowIndex")==row and prior.get("startColumnIndex")==col):
            prior["endRowIndex"]=row+1
        else:
            requests.append({"setDataValidation":{"range":{"sheetId":target,"startRowIndex":row,
                "endRowIndex":row+1,"startColumnIndex":col,"endColumnIndex":col+1},"rule":rule}})
    db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid,body={"requests":requests}).execute(num_retries=0)
    db._invalidate_sheet_metadata()
    if _values(db,extent)!=desired or (old_rules and _validations(db,meta)!=expected):
        raise ProjectionError("compact_category_sync_readback_failed")
    return {"category_choice_writes":len(runs)}


def backfill_month_choices(requests,*,sheet_id,title,rows,extent,store):
    """Replace legacy spill formulas with bounded literal month choices.

    Read only saved month totals plus the already-read current selections.
    The complete summary, not the 13-month display window, supplies history.
    """
    from .category_backfill_ui import _month
    if store is None:raise ProjectionError("backfill_projection_binding_required")
    summary=store.read("summary")
    if summary is None:raise ProjectionError("projection_bootstrap_required")
    months={month_key(m) for m in summary["months"]}
    selected={_month(row[col]) for row in rows for col in (2,3) if len(row)>col}
    months=sorted(months | (selected-{None}))
    starts=months or [""];ends=["過去すべて",*months]
    needed=max(len(starts),len(ends))+1
    new_extent=max(extent,needed)
    # Remove only legacy Z/AA value writes; retain notes/styles and all input
    # values/checks/snapshots. Clear old formula spill tails in the same batch.
    result=[r for r in requests if not ("updateCells" in r and
        r["updateCells"]["range"].get("startColumnIndex") in (25,26))]
    if new_extent>extent:result.insert(0,{"appendDimension":{"sheetId":sheet_id,"dimension":"ROWS","length":new_extent-extent}})
    for col,label,values in [(25,"過去反映・開始月候補",starts),(26,"過去反映・終了月候補",ends)]:
        write=_cells(sheet_id,1,col,[[label],*[[v] for v in values]],1)
        write["updateCells"]["range"]["endRowIndex"]=new_extent
        result.append(write)
    for request in result:
        body=request.get("setDataValidation",{});rule=body.get("rule",{})
        if rule.get("condition",{}).get("type")!="ONE_OF_RANGE":continue
        col=body["range"]["startColumnIndex"]
        if col not in (2,3):continue
        letter,count=("Z",len(starts)) if col==2 else ("AA",len(ends))
        rule["condition"]["values"]=[{"userEnteredValue":f"='{title}'!${letter}$2:${letter}${count+1}"}]
    return result
