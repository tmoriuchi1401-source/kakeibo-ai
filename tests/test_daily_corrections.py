from copy import deepcopy
import re

import pytest

from app.daily_corrections import DailyCorrections
from app.monthly_projection import ProjectionError
from app.projection_store import ProjectionJournal
from test_projection_refresh import initialized, row, PAIRS


REQUEST="REQ-"+"a"*32


class Ledger:
    def __init__(self,store,reader):
        self.store,self.reader=store,reader
        self.calls=[]
        self.fail_after=False
        self.mutate_before_read=None

    def get_raw(self,a1):
        if self.mutate_before_read:
            self.mutate_before_read();self.mutate_before_read=None
        position=int(re.search(r"!A(\d+):",a1)[1])
        return deepcopy(self.reader.rows[position-2:position-1])

    def update_expense_fields(self,position,cells):
        ProjectionJournal(self.store).mark([(position,position)])
        self.calls.append((position,deepcopy(cells)))
        for col,value in cells:self.reader.rows[position-2][col]=value
        if self.fail_after:
            self.fail_after=False
            raise RuntimeError("synthetic_response_lost")


def setup():
    store,reader,refresh=initialized([row("a","2025-12-31"),row("b")])
    db=Ledger(store,reader)
    return store,reader,refresh,db,DailyCorrections(store,db)


def test_fixed_id_correction_updates_only_requested_fields_and_both_months():
    store,reader,refresh,db,inbox=setup()
    before=deepcopy(reader.rows)
    inbox.prepare(REQUEST,"a",{"date":"2026-01-01","amount":120})
    assert inbox.apply(REQUEST)["state"]=="applied"
    assert db.calls==[(2,[[1,"2026-01-01"],[4,120]])]
    assert reader.rows[0][5:]==before[0][5:]
    assert reader.rows[1]==before[1]
    refresh.refresh(PAIRS)
    assert refresh.read_month("2025-12").amount==0
    assert refresh.read_month("2026-01").amount==120
    assert inbox.apply(REQUEST)["state"]=="applied" and len(db.calls)==1


def test_unknown_write_response_is_recovered_by_readback_without_second_write():
    store,reader,refresh,db,inbox=setup()
    inbox.prepare(REQUEST,"a",{"amount":80})
    db.fail_after=True
    with pytest.raises(RuntimeError):inbox.apply(REQUEST)
    assert store.data["corrections"]["requests"][REQUEST]["state"]=="pending"
    recovered=DailyCorrections(store,db).apply(REQUEST)
    assert recovered["state"]=="applied" and len(db.calls)==1
    assert len(reader.rows)==2


@pytest.mark.parametrize("position,value",[(4,101),(5,"本人分類"),(0,"different"),(12,"inactive")])
def test_latest_value_conflict_preserves_human_change(position,value):
    store,reader,refresh,db,inbox=setup()
    inbox.prepare(REQUEST,"a",{"amount":80})
    reader.rows[0][position]=value
    assert inbox.apply(REQUEST)["state"]=="failed"
    assert reader.rows[0][position]==value and db.calls==[]


def test_changed_after_pending_marker_before_mutation_is_checked_again():
    store,reader,refresh,db,inbox=setup()
    inbox.prepare(REQUEST,"a",{"amount":80})
    original=store.write
    def race(key,value):
        original(key,value)
        if key=="corrections" and value["requests"][REQUEST]["state"]=="pending":
            reader.rows[0][11]="本人の新しいメモ"
    store.write=race
    assert inbox.apply(REQUEST)["state"]=="failed"
    assert db.calls==[] and reader.rows[0][11]=="本人の新しいメモ"


def test_invalid_fields_and_reused_request_never_write():
    store,reader,refresh,db,inbox=setup()
    with pytest.raises(ProjectionError,match="fields_invalid"):
        inbox.prepare(REQUEST,"a",{"source":"synthetic"})
    inbox.prepare(REQUEST,"a",{"amount":80})
    with pytest.raises(ProjectionError,match="request_reused"):
        inbox.prepare(REQUEST,"b",{"amount":80})
    assert db.calls==[]


def test_category_rename_between_prepare_and_apply_requires_reconfirmation():
    store,reader,refresh,db,inbox=setup()
    category_id=store.data["catalog"]["categories"][0]["category_id"]
    inbox.prepare(REQUEST,"a",{"category_id":category_id})
    store.data["catalog"]["categories"][0]["minor"]="改名"
    assert inbox.apply(REQUEST)["error"]=="category_changed"
    assert db.calls==[]
