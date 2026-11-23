"""One separately approved finish, using only fabricated storage/services."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace
import pytest

from test_receipt_pr_b import registered, deny_finance_and_network
from app.page_receipt_model import digest
from app.receipt_review_worker import AttemptFence
from app.receipt_review_finish import (FinishAttemptFence, RestReviewReader, prepare_finish_plan,
                                       execute_finish_only, VALIDATOR_REVISION)
from app.receipt_sheets_authority import UNKNOWN

BOOK, DEPLOYMENT = 'synthetic-finish-book', 'synthetic-finish-deployment'


class ReaderSession:
    def __init__(self, port): self.port, self.calls = port, []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert url == 'https://sheets.googleapis.com/v4/spreadsheets/' + BOOK
        assert kwargs['allow_redirects'] is False
        sheets = []
        for title in ('_receipt_cases', '_receipt_requests'):
            table = self.port.state['cases' if title == '_receipt_cases' else 'requests']
            sheets.append(dict(properties=dict(title=title, gridProperties=dict(rowCount=1000, columnCount=26)),
                data=[dict(rowData=[dict(values=[dict(effectiveValue={'stringValue':v}) if v else {} for v in row]) for row in table])]))
        return SimpleNamespace(status_code=200, json=lambda:dict(spreadsheetId=BOOK, sheets=deepcopy(sheets)))


def setup(tmp_path):
    entry, port = registered(); port.http_ports = True; rid = entry['request_id']
    port.claim(rid, port.read(rid)['row'])
    port.script_id = DEPLOYMENT
    path = tmp_path / 'attempts.sqlite'
    original = AttemptFence(path); original.consume(rid)
    plan = prepare_finish_plan(port.read(rid), rid, spreadsheet_id=BOOK, deployment_id=DEPLOYMENT)
    reader = RestReviewReader(ReaderSession(port), BOOK)
    port.calls.clear(); port.checks = 0
    return entry, port, reader, plan, FinishAttemptFence(path), original


def run(port, reader, plan, fence, approved=None):
    return execute_finish_only(plan, approved_plan_digest=approved or plan['plan_digest'],
                               port=port, reader=reader, fence=fence)


def test_offline_plan_and_finish_have_no_live_validation_claim_or_financial_write(tmp_path, monkeypatch):
    entry, port, reader, plan, fence, original = setup(tmp_path)
    assert plan['validator_revision'] == VALIDATOR_REVISION
    assert (plan['result']['receipt_total'], plan['result']['item_total'], plan['result']['difference']) == (1000,1000,0)
    monkeypatch.setattr('app.receipt_review_finish.validate_snapshot', lambda *_:pytest.fail('live revalidate forbidden'))
    monkeypatch.setattr(port, 'claim', lambda *_:pytest.fail('reclaim forbidden'))
    monkeypatch.setattr(port, 'intake', lambda *_:pytest.fail('intake forbidden'))
    outcome = run(port, reader, plan, fence)
    assert outcome == plan['result'] and outcome['financial_write_count'] == 0 and outcome['posting_authority'] is False
    assert port.calls == ['read', 'finish'] and port.checks == 2
    assert len(reader.session.calls) == 3
    assert port.state['requests'][1][:7] == plan['before'][:7]
    assert original.attempted(entry['request_id'])


@pytest.mark.parametrize('column', range(10))
def test_all_ten_before_cells_must_match_before_finish(tmp_path, column):
    _, port, reader, plan, fence, _ = setup(tmp_path)
    port.state['requests'][1][column] = '2026-01-01T00:00:00.000Z' if column == 9 else 'changed-private-canary'
    assert run(port, reader, plan, fence)['state'] == UNKNOWN
    assert 'finish' not in port.calls and port.checks == 0


def test_finish_fence_survives_restart_and_original_claim_fence_is_never_reset(tmp_path):
    _, port, reader, plan, fence, original = setup(tmp_path)
    assert run(port, reader, plan, fence)['state'] == '検証済み・未記帳'
    # Even restoring the old row cannot re-use the same UUID with another plan.
    port.state['requests'][1] = deepcopy(plan['before']); port.calls.clear()
    assert run(port, reader, plan, FinishAttemptFence(fence.path))['state'] == UNKNOWN
    assert 'finish' not in port.calls and original.attempted(plan['request_id'])
    with sqlite3.connect(fence.path) as db:
        assert db.execute('select count(*) from attempts').fetchone() == (1,)
        assert db.execute('select request_id from finish_attempts').fetchall() == [(plan['request_id'],)]


@pytest.mark.parametrize('fault', ['before', 'after', 'readback'])
def test_unknown_finish_still_checks_source_and_independent_get_and_never_resends(tmp_path, fault):
    _, port, reader, plan, fence, _ = setup(tmp_path)
    original_finish = port.finish
    def broken(*args):
        port.fault = fault
        return original_finish(*args)
    port.finish = broken
    assert run(port, reader, plan, fence)['state'] == UNKNOWN
    assert port.calls.count('finish') == 1 and port.checks == 2 and len(reader.session.calls) == 3
    port.fault = None
    assert run(port, reader, plan, FinishAttemptFence(fence.path))['state'] == UNKNOWN
    assert port.calls.count('finish') == 1


@pytest.mark.parametrize('fault', ['id', 'hash', 'version', 'owner', 'parent', 'after', 'network'])
def test_source_checks_before_and_after_finish_are_mandatory(tmp_path, fault):
    _, port, reader, plan, fence, _ = setup(tmp_path); port.source_fault = fault
    assert run(port, reader, plan, fence)['state'] == UNKNOWN
    assert port.calls.count('finish') == (1 if fault == 'after' else 0)
    assert port.checks == (2 if fault == 'after' else 1)


@pytest.mark.parametrize('field,value', [('financial_write_count',1), ('financial_write_count',0.0), ('posting_authority',True)])
def test_even_newly_pinned_plan_cannot_grant_financial_authority(tmp_path, field, value):
    _, port, reader, plan, fence, _ = setup(tmp_path)
    plan['result'][field] = value; plan['result_digest'] = digest(plan['result'])
    plan['plan_digest'] = digest({k:v for k,v in plan.items() if k != 'plan_digest'})
    assert run(port, reader, plan, fence)['state'] == UNKNOWN
    assert port.calls == [] and reader.session.calls == []


def test_changed_plan_requires_new_explicit_digest_approval(tmp_path):
    _, port, reader, plan, fence, _ = setup(tmp_path); approved = plan['plan_digest']
    plan['result']['state'] = '保留'; plan['result_digest'] = digest(plan['result'])
    plan['plan_digest'] = digest({k:v for k,v in plan.items() if k != 'plan_digest'})
    assert run(port, reader, plan, fence, approved)['state'] == UNKNOWN
    assert not port.calls


def test_finish_requires_existing_consumed_claim_fence(tmp_path):
    _, port, reader, plan, _, _ = setup(tmp_path)
    blank = tmp_path / 'unclaimed.sqlite'; AttemptFence(blank)
    assert run(port, reader, plan, FinishAttemptFence(blank))['state'] == UNKNOWN
    assert 'finish' not in port.calls


@pytest.mark.parametrize('target', ['spreadsheet_id','script_id'])
def test_execution_target_cannot_be_swapped(tmp_path, target):
    _, port, reader, plan, fence, _ = setup(tmp_path)
    setattr(reader if target == 'spreadsheet_id' else port, target, 'different-target')
    assert run(port, reader, plan, fence)['state'] == UNKNOWN
    assert not port.calls


def test_stale_independent_final_readback_is_unknown_even_when_finish_was_saved(tmp_path):
    _, port, reader, plan, fence, _ = setup(tmp_path)
    get = reader.session.get
    def stale_final(*args, **kwargs):
        response = get(*args, **kwargs)
        if len(reader.session.calls) == 3:
            body = response.json()
            sheet = next(s for s in body['sheets'] if s['properties']['title'] == '_receipt_requests')
            sheet['data'][0]['rowData'][1]['values'] = [dict(effectiveValue={'stringValue':v}) if v else {} for v in plan['before']]
            return SimpleNamespace(status_code=200, json=lambda:body)
        return response
    reader.session.get = stale_final
    assert run(port, reader, plan, fence)['state'] == UNKNOWN
    assert port.state['requests'][1][7] == '検証済み・未記帳'
    assert port.calls.count('finish') == 1
    assert run(port, reader, plan, FinishAttemptFence(fence.path))['state'] == UNKNOWN
    assert port.calls.count('finish') == 1


def test_finish_response_alone_cannot_override_independent_final_state(tmp_path):
    _, port, reader, plan, fence, _ = setup(tmp_path)
    finish = port.finish
    def wrong_response(*args):
        row = finish(*args)
        row[9] = '2026-01-01T00:00:00.000Z'
        return row
    port.finish = wrong_response
    assert run(port, reader, plan, fence)['state'] == UNKNOWN
    assert port.calls.count('finish') == 1 and len(reader.session.calls) == 3
