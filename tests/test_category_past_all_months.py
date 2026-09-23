from copy import deepcopy
import json

from app.category_operations import process_category_operations
from app.category_rule_ui import CategoryRuleUIPipeline
from app.category_backfill import CategoryBackfillPipeline
from app.category_past_all_months import LEGACY_PAST_HEADER, PAST_HEADER
from app.category_sheet_requests import execute_request, parse_snapshot
from test_category_backfill import import_row, service_rule
from test_category_sheet_requests import RequestDB, Store, REQUEST, ENV, blocks, physical


def add(db, key, month, category=("その他", "未分類"), merchant="同じ店", source="PayPay"):
    imported="p-"+key
    db.expenses[key]=(len(db.expenses)+2,[key,month+"-10",merchant,"自動計上",100,*category,"",source,"",imported,"","active"])
    tx=import_row(imported,merchant,100,key); tx[2]=source; tx[4]=month+"-10"
    db.imports.append(tx)


def fixture():
    db=RequestDB(); db.expenses={}; db.imports=[]
    add(db,"M-old","2025-01")
    add(db,"M-new","2026-09")
    add(db,"M-classified","2026-08",("住まい","家賃"))
    add(db,"M-other","2026-07",merchant="別の店")
    add(db,"M-source","2026-06",source="OtherSource")
    db.category_pairs.append(("住まい","家賃"))
    refresh(db)
    row=next(r for r in db.ui if r[6].startswith("group:") and "同じ店" in r[0] and r[9]=="PayPay")
    row[2:6]=["食費","外食","登録しない",True]
    return db,row


def refresh(db):
    return CategoryRuleUIPipeline(db,ui_enabled=True,save_enabled=True).refresh()


def run(db):
    return process_category_operations(db,apply=True,rule_enabled=True,save_enabled=True,
                                       preview_enabled=True,backfill_enabled=True,all_months=True)


def test_all_months_changes_only_unclassified_and_removes_related_month_rows_durably():
    db,row=fixture(); before=deepcopy(db.expenses)
    result=run(db)
    assert result["category_expenses_applied"]==2 and result["category_held"]==0
    for key in ("M-old","M-new"):
        assert db.expenses[key][1][5:7]==["食費","外食"]
    for key in ("M-classified","M-other","M-source"):
        assert db.expenses[key]==before[key]
    assert not db.rule_rows
    assert not any(r[9]=="PayPay" and "同じ店" in r[0] for r in db.ui)
    assert not db.backfill and not db.confirmations
    assert db.requests[0][2]=="complete"
    assert json.loads(db.requests[0][3])["ui_all_months"] is True
    run(db); refresh(db)
    assert len(db.requests)==1 and len(db.category_updates)==2
    assert not any(r[9]=="PayPay" and "同じ店" in r[0] for r in db.ui)


def test_later_unclassified_import_reopens_condition_and_restore_reopens_it():
    db,_=fixture(); run(db)
    add(db,"M-later","2024-02")
    refresh(db)
    assert any(r[6].startswith("group:") and "同じ店" in r[0] for r in db.ui)
    db.expenses.pop("M-later")
    CategoryBackfillPipeline(db,preview_enabled=True,apply_enabled=True).restore(db.requests[0][0])
    refresh(db)
    assert any(r[6].startswith("group:") and "同じ店" in r[0] for r in db.ui)


def test_invalid_or_conflicting_category_and_unsafe_history_stay_visible():
    db,row=fixture(); row[2:4]=["存在しない","カテゴリ"]
    assert run(db)["category_held"]==1 and not db.category_updates
    assert any("held: all_month:" in r[1] for r in db.ui)
    db,row=fixture()
    db.rule_rows=[service_rule("saved", "同じ店", ("住まい","家賃")).to_row()]
    assert run(db)["category_held"]==1 and not db.category_updates
    assert any("held: all_month:" in r[1] for r in db.ui)
    db,row=fixture(); db.imports[0][11]="返金"
    assert run(db)["category_held"]==1
    assert db.expenses["M-old"][1][5:7]==["その他","未分類"]
    assert any("held: all_month:" in r[1] for r in db.ui)


def test_two_categories_for_same_checked_condition_do_not_apply_either():
    db,row=fixture()
    other=next(r for r in db.ui if r[6]=="M-classified")
    other[5]=True
    assert run(db)["category_held"]==1
    assert not db.category_updates


def test_submitted_header_keeps_old_preview_approval_from_becoming_all_month_apply():
    db,row=fixture()
    captured=physical(blocks(db))
    captured[1][5]=LEGACY_PAST_HEADER
    store=Store(parse_snapshot(captured))
    result=execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=lambda:{})
    assert result["category_expenses_applied"]==0 and not db.category_updates
    assert len(db.backfill)==1


def test_new_header_executes_captured_choice_and_preserves_later_edit():
    db,row=fixture(); captured=physical(blocks(db))
    assert captured[1][5]==PAST_HEADER
    store=Store(parse_snapshot(captured))
    row[2:6]=["住まい","家賃",False,True]
    result=execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=lambda:{})
    assert result["category_expenses_applied"]==2
    assert db.expenses["M-old"][1][5:7]==["食費","外食"]
    assert any(r[2:6]==["住まい","家賃",False,True] for r in db.ui)


def test_current_source_change_requires_reapproval_and_no_future_rule_is_implicit():
    db,row=fixture(); refresh(db)
    db.imports[0][5]="変更後の店"
    run(db)
    assert not db.category_updates and not db.rule_rows


def test_zero_targets_record_resolution_without_modifying_classified_expense():
    db,row=fixture()
    db.expenses.pop("M-old"); db.expenses.pop("M-new")
    refresh(db)
    row=next(r for r in db.ui if r[6]=="M-classified")
    row[5]=True
    before=deepcopy(db.expenses)
    assert run(db)["category_expenses_applied"]==0
    assert db.expenses==before and not db.category_updates
    assert len(db.requests)==1 and db.requests[0][2]=="complete"
    assert not any(r[6]=="M-classified" for r in db.ui)
