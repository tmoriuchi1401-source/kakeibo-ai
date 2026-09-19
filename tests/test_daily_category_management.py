from copy import deepcopy
from hashlib import sha256

import pytest

from app.daily_category_management import CategoryForm, MARKER, run_categories, upgrade_requests, render_requests
from app.category_management import CategoryManagement
from app.monthly_projection import Category, CategoryCatalog, ProjectionError
from app.daily_sheets import DailySheets
from app.projection_refresh import load_catalog
from test_category_management import setup, Master, TOKEN
from test_daily_sheets import Grid


def fill(daily,grid,identity):
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"source").hexdigest()}]}
    for r,v in enumerate(["改名（IDを保持）",identity,"食費｜食品",True],90):grid.put(r,2,v,title="設定")
    grid.put(90,6,TOKEN,title="設定")


def configured(monkeypatch):
    store,reader,refresh,master,sync,inbox,identity=setup()
    grid=Grid();daily=DailySheets(grid,"source",store);fill(daily,grid,identity)
    monkeypatch.setattr("app.daily_category_management.CategoryMaster",lambda source:master)
    monkeypatch.setattr("app.compact_category_sync.sync_choices",lambda source,catalog:None)
    return store,reader,master,grid,daily,CategoryForm(daily,object())


def test_preview_then_submit_and_replay_preserve_inputs_and_never_write_expenses(monkeypatch):
    store,reader,master,grid,daily,form=configured(monkeypatch)
    before=deepcopy(store.data);values=form.form()[0][:3]
    assert run_categories(daily,object(),apply=False)=={"categories_pending":0,"category_form_ready":1}
    assert store.data==before and master.writes==grid.writes==[]
    assert run_categories(daily,object(),apply=True)["categories_applied"]==1
    assert form.form()[0][:3]==values and form.form()[0][3] is False
    writes=list(store.writes);native=len(grid.writes)
    assert run_categories(daily,object(),apply=True)["categories_submitted"]==0
    assert store.writes==writes and len(grid.writes)==native and len(master.writes)==1 and reader.reads==[]


def test_user_edit_during_submission_survives_ack_unknown_without_resubmission(monkeypatch):
    store,reader,master,grid,daily,form=configured(monkeypatch)
    apply=form.inbox.apply
    def edit(token):
        result=apply(token);grid.put(92,2,"食費｜変更中",title="設定");return result
    form.inbox.apply=edit
    assert form.submit()["category_input_changed"]==1
    form.inbox.apply=apply;grid.fail_after=True
    with pytest.raises(RuntimeError):form.submit()
    assert form.submit()=={"categories_submitted":0}
    assert form.form()[0][2]=="食費｜変更中" and form.form()[0][3] is False
    assert len(master.writes)==1
    assert "変更した入力は未送信です" in daily.read_ranges(["'設定'!B94:B94"])[0][0][0]


def test_runtime_resumes_unknown_native_write_before_projection_refresh(monkeypatch):
    from test_daily_apply_runtime import configured as runtime_setup
    from app.daily_runtime import run_daily_requests
    env,daily,grid,ledger,reader,refresh=runtime_setup(monkeypatch)
    identity=load_catalog(daily.store.read("catalog")).resolve("食費","食料品")
    fill(daily,grid,identity);grid.put(68,2,False)
    master=Master();master.fail=True
    ledger.categories=lambda:[tuple(r) for r in master.rows[1:]]
    monkeypatch.setattr("app.daily_category_management.CategoryMaster",lambda source:master)
    with pytest.raises(RuntimeError):run_daily_requests(env,apply=True)
    assert daily.store.data["category-requests"]["requests"][TOKEN]["state"]=="pending"
    assert run_daily_requests(env,apply=True)["categories_applied"]==1
    assert load_catalog(daily.store.read("catalog")).resolve("食費","食品")==identity
    assert run_daily_requests(env,apply=True)["categories_applied"]==0
    assert ledger.calls==[] and len(master.writes)==1


def test_paginated_catalog_outputs_all_ids_without_overwriting_form():
    catalog=CategoryCatalog([Category(f"CAT-{i:032x}","分類",str(i)) for i in range(123)])
    requests=render_requests(catalog,{"category_page":3})
    authored=[r["updateCells"] for r in requests if "updateCells" in r]
    assert all(r["range"]["startRowIndex"]>=101 for r in authored)
    body=next(r for r in authored if r["range"]["startRowIndex"]==103)
    assert sum(bool(r["values"][0]) for r in body["rows"])==23


def test_upgrade_checks_owned_binding_and_entire_output_area(monkeypatch):
    store,reader,master,grid,daily,form=configured(monkeypatch)
    assert upgrade_requests(daily)==[]
    grid.data.clear();daily.verify=lambda:None
    assert upgrade_requests(daily)
    grid.put(153,2,"本人メモ",title="設定")
    with pytest.raises(ProjectionError,match="occupied"):upgrade_requests(daily)
    daily.verify=lambda:{"developerMetadata":[{"metadataKey":MARKER,"metadataValue":"wrong"}]}
    with pytest.raises(ProjectionError,match="binding_invalid"):upgrade_requests(daily)
