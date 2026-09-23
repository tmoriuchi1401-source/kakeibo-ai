from copy import deepcopy
from types import SimpleNamespace
import pytest

from app.category_sheet_requests import (CapturedCategoryDB, execute_request,
    merge_results, parse_snapshot, snapshot_digest)
from app.category_rule_ui import CategoryRuleUIPipeline
from app.drive_run_state import StateError
from app.sheets import SheetsDB, CATEGORY_REQUEST_SHEET, CATEGORY_WORKFLOW_MARKERS
from test_category_operations import OperationsDB

REQUEST = '12345678-1234-1234-1234-123456789abc'
ENV = {name: 'true' for name in ('CATEGORY_RULE_UI_ENABLED', 'CATEGORY_RULE_SAVE_ENABLED',
    'CATEGORY_BACKFILL_PREVIEW_ENABLED', 'CATEGORY_BACKFILL_APPLY_ENABLED')}


def blocks(db):
    defaults = SheetsDB._category_workflow_defaults(None)
    return {k: (defaults[k], deepcopy(rows)) for k, rows in
        [('rule',db.ui), ('backfill',db.backfill), ('confirm',db.confirmations)]}


def physical(value):
    output = []
    for section,(header,rows) in value.items():
        output += [[CATEGORY_WORKFLOW_MARKERS[section]] + ['']*11,
                   SheetsDB._workflow_physical_row(section,header,compact=True,header=True)]
        output += [SheetsDB._workflow_physical_row(section,r,compact=True) for r in rows]
    return [['TRUE' if v is True else 'FALSE' if v is False else str(v) for v in r] for r in output]


class RequestDB(OperationsDB):
    def _category_workflow_blocks(self):
        self._category_workflow_last_read = repr(blocks(self))
        return blocks(self)
    def _check_category_workflow_input(self, prior):
        assert prior == self._category_workflow_last_read
    def _write_category_workflow_blocks(self, value):
        self.ui,self.backfill,self.confirmations = [deepcopy(value[k][1]) for k in ('rule','backfill','confirm')]


class Store:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.meta = [REQUEST, 'accepted'] + ['']*8
        self.states = []
    def read(self): return self.meta
    def snapshot(self, metadata): return deepcopy(self.value)
    def update(self, metadata, state, message, **kwargs):
        self.meta[1] = state
        self.states.append(state)


def prepared():
    db = RequestDB()
    CategoryRuleUIPipeline(db,ui_enabled=True,save_enabled=True).refresh()
    row = next(r for r in db.ui if r[6].startswith('group:'))
    row[2:6] = ['食費','外食',True,False]
    return db, row


def test_snapshot_roundtrip_and_tamper_shape():
    db,_ = prepared()
    source = physical(blocks(db))
    parsed = parse_snapshot(source)
    assert parsed['rule'][1][-1][2:6] == ['食費','外食',True,'FALSE']
    assert snapshot_digest(source) == snapshot_digest(deepcopy(source))
    source[0][0] = 'wrong'
    with pytest.raises(StateError): parse_snapshot(source)


def test_capture_never_reads_or_writes_live_ui():
    db,row = prepared()
    saved = blocks(db)
    adapter = CapturedCategoryDB(db,saved)
    row[2:6] = ['住まい','家賃',False,True]
    adapter.replace_category_rule_ui_rows([], saved['rule'][0])
    assert db.ui and row[2:6] == ['住まい','家賃',False,True]
    assert adapter.category_rule_ui_rows() == []


def test_only_submitted_category_is_registered_later_edits_are_retained():
    db,row = prepared()
    store = Store(parse_snapshot(physical(blocks(db))))
    original_key = row[6]
    row[2:6] = ['日用品','消耗品',True,True]
    result = execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=lambda: {})
    assert result['category_registration_processed'] == 1
    assert len(db.rule_rows) == 1
    assert db.rule_rows[0][9:11] == ['食費','外食']
    current = next(r for r in db.ui if r[6] == original_key)
    assert current[2:6] == ['日用品','消耗品',True,True]
    assert store.states == ['running','complete']
    assert result['category_later_edits_retained'] == 1
    assert not db.category_updates


@pytest.mark.parametrize('state',['running','complete','error','dispatch_failed'])
def test_duplicate_or_uncertain_request_never_replays(state):
    db,_ = prepared()
    store = Store(blocks(db)); store.meta[1] = state
    result = execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=lambda: pytest.fail('refresh'))
    assert result == {'category_request_ignored':1}
    assert not db.rule_rows and not store.states


def test_new_confirmation_after_submit_cannot_apply_past_records():
    db,row = prepared(); row[4:6] = ['登録しない',True]
    from test_category_operations import run
    run(db)
    db.backfill[0][2:5] = ['2026-08','2026-08',True]
    run(db)
    store = Store(parse_snapshot(physical(blocks(db))))
    db.confirmations[0][2] = True
    result = execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=lambda: {})
    assert result['category_expenses_applied'] == 0 and not db.category_updates
    assert db.confirmations[0][2] is True
    assert not db.rule_rows


def test_failure_is_terminal_and_no_automatic_retry():
    db,_ = prepared(); store = Store(blocks(db))
    def fail(): raise RuntimeError('private data must not escape')
    with pytest.raises(StateError) as exc:
        execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=fail)
    assert 'private data' not in str(exc.value)
    assert store.states == ['running','error']
    assert execute_request(db,REQUEST,env=ENV,store=store,refresh_projection=fail) == {'category_request_ignored':1}


def test_request_header_is_reserved_across_refresh():
    db = object.__new__(SheetsDB); db.sheet_titles=lambda:[CATEGORY_REQUEST_SHEET]
    assert db._workflow_positions({'rule':([],[]),'backfill':([],[]),'confirm':([],[])})['rule']['start'] == 8


def test_regular_runtime_does_not_consume_unsubmitted_edits(monkeypatch):
    from app import category_operations as operations
    monkeypatch.setattr('app.production_flow.verify_execution_boundary',lambda *a:None)
    monkeypatch.setattr('app.google_clients.sheets_service',lambda:object())
    monkeypatch.setattr('app.sheets.SheetsDB',lambda *a,**kw:SimpleNamespace(sheet_titles=lambda:[CATEGORY_REQUEST_SHEET]))
    monkeypatch.setattr(operations,'process_category_operations',lambda *a,**kw:pytest.fail('unsubmitted'))
    assert operations.run_category_operations(ENV,apply=True) == {'category_awaiting_sheet_submission':1}
