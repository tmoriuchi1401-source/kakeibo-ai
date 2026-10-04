"""Owned auxiliary sheet compatibility must never become a wildcard."""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.daily_sheets import DailySheets
from app.daily_view import SHEETS, OWNED_MARKER
from app.monthly_projection import ProjectionError


def metadata(helper=True):
    sheets=[{"properties":{"title":t,"sheetId":sid,"sheetType":"GRID",
        "gridProperties":{"rowCount":rows,"columnCount":columns}}}
        for t,(sid,rows,columns) in SHEETS.items()]
    if helper:
        sheets.append({"properties":{"title":"_ホームグラフ","sheetId":261003103,
            "sheetType":"GRID","hidden":True,
            "gridProperties":{"rowCount":100,"columnCount":11}}})
    return {"sheets":sheets,"developerMetadata":[{"metadataKey":OWNED_MARKER,
        "metadataValue":sha256(b"source").hexdigest()}]}


def verify(meta):
    writes=[]
    api=SimpleNamespace(get=lambda **kw:SimpleNamespace(execute=lambda **ignored:deepcopy(meta)),
        batchUpdate=lambda **kw:writes.append(kw))
    db=SimpleNamespace(sid="daily",svc=SimpleNamespace(spreadsheets=lambda:api),
        _execute_sheet_read=lambda operation:operation().execute(num_retries=0))
    result=DailySheets(db,"source",None).verify()
    assert writes==[]
    return result


@pytest.mark.parametrize("helper",[False,True])
def test_original_and_exact_hidden_home_overlay_are_read_only_compatible(helper):
    assert verify(metadata(helper))==metadata(helper)


@pytest.mark.parametrize("corruption",["unknown","unknown_prefix","category_helper",
    "renamed","wrong_id","visible","not_grid","rows","columns","missing_base",
    "wrong_base_id","duplicate_title","duplicate_id"])
def test_unknown_or_corrupt_composition_is_rejected(corruption):
    meta=metadata();helper=meta["sheets"][-1]["properties"]
    if corruption in {"unknown","unknown_prefix","category_helper"}:
        extra=deepcopy(meta["sheets"][-1]);extra["properties"]["title"]={
            "unknown":"追加シート","unknown_prefix":"_未知の補助",
            "category_helper":"_カテゴリ確認ログ"}[corruption]
        extra["properties"]["sheetId"]=999
        meta["sheets"].append(extra)
    elif corruption=="renamed":helper["title"]="_ホームグラフ2"
    elif corruption=="wrong_id":helper["sheetId"]=999
    elif corruption=="visible":helper["hidden"]=False
    elif corruption=="not_grid":helper["sheetType"]="OBJECT"
    elif corruption=="rows":helper["gridProperties"]["rowCount"]=101
    elif corruption=="columns":helper["gridProperties"]["columnCount"]=12
    elif corruption=="missing_base":meta["sheets"].pop(0)
    elif corruption=="wrong_base_id":meta["sheets"][0]["properties"]["sheetId"]=999
    elif corruption=="duplicate_title":meta["sheets"].append(deepcopy(meta["sheets"][-1]))
    elif corruption=="duplicate_id":helper["sheetId"]=SHEETS["ホーム"][0]
    with pytest.raises(ProjectionError,match="daily_sheet_contract_changed"):verify(meta)


def test_budget_and_source_binding_still_apply_with_owned_helper():
    meta=metadata();meta["sheets"][0]["properties"]["gridProperties"]["rowCount"]=100_000
    with pytest.raises(ProjectionError,match="daily_cell_budget_exceeded"):verify(meta)
    meta=metadata();meta["developerMetadata"][0]["metadataValue"]="different-source"
    with pytest.raises(ProjectionError,match="daily_source_binding_mismatch"):verify(meta)
