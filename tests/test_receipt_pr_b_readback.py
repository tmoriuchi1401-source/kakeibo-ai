"""Real Script ports: external REST writes versus execution-local cached reads."""
from copy import deepcopy
import json
import pytest
from test_receipt_pr_b import case, js, ready, registered, deny_finance_and_network
from app.receipt_review_cases import validate_snapshot


def claimed_state():
    entry, port = registered(); rid = entry['request_id']
    port.state['requests'][1][9] = '2026-01-01T00:00:00.000Z'
    return entry, port, rid


@pytest.mark.parametrize('legacy', [True, False])
def test_claim_cached_spreadsheet_reproduces_pr205_unknown_but_rest_readback_passes(legacy):
    entry, port, rid = claimed_state(); before = deepcopy(port.state['requests'][1])
    reply = js(**port.state, http_ports=True, stale_spreadsheet=True, legacy_spreadsheet_reader=legacy,
               commands=[dict(action='claim', request_id=rid, before=before)])
    assert reply['writes'] == 1 and reply['requests'][1][7] == '検証中'
    if legacy:
        assert reply['errors'] == ['review_transition_unknown']
        assert reply['properties']['RECEIPT_REVIEW_STOPPED'] == 'outcome_unconfirmed'
        assert reply['diagnostics'] == [dict(event='review_readback_mismatch', code='review_transition_unknown',
                                             table='_receipt_requests', columns=[8, 10])]
        assert reply['spreadsheetReads'] > 0
    else:
        assert reply['errors'] == [] and reply['diagnostics'] == []
        assert 'RECEIPT_REVIEW_STOPPED' not in reply['properties']
        assert reply['restGets'] >= 3 and reply['spreadsheetReads'] == 0


@pytest.mark.parametrize('legacy', [True, False])
def test_intake_cache_model_reproduces_saved_case_without_decoration(legacy):
    entry, row = case()
    reply = js(http_ports=True, stale_spreadsheet=True, legacy_spreadsheet_reader=legacy,
               commands=[dict(action='intake', entry=entry, row=row)])
    assert reply['writes'] == 1 and len(reply['cases']) == 2 and reply['ui'] == [row]
    if legacy:
        assert reply['errors'] == ['review_intake_readback_unknown']
        assert reply['validationCount'] == 0 and reply['properties']['RECEIPT_REVIEW_STOPPED']
    else:
        assert reply['errors'] == [] and reply['validationCount'] == 42


def test_stale_rest_response_keeps_stop_and_never_retries_or_logs_values():
    entry, port, rid = claimed_state(); before = deepcopy(port.state['requests'][1])
    reply = js(**port.state, http_ports=True, stale_rest={'_receipt_requests': port.state['requests']},
               commands=[dict(action='claim', request_id=rid, before=before)] * 2)
    assert reply['errors'] == ['review_transition_unknown', 'review_disabled_or_unknown']
    assert reply['writes'] == 1 and reply['properties']['RECEIPT_REVIEW_STOPPED']
    logs = json.dumps(reply['diagnostics'])
    for private in [rid, entry['case_id'], before[5], before[6], '合成商店', 'github_pat_']:
        assert private not in logs
    assert set(reply['diagnostics'][0]) == {'event', 'code', 'table', 'columns'}


@pytest.mark.parametrize('options,error', [({'rest_status':503}, 'review_read_unknown'),
                                          ({'grid_rows':1001}, 'review_table_invalid')])
def test_bad_rest_read_fails_before_any_write(options, error):
    entry, port, rid = claimed_state()
    reply = js(**port.state, http_ports=True, **options,
               commands=[dict(action='claim', request_id=rid, before=port.state['requests'][1])])
    assert reply['errors'] == [error] and reply['writes'] == 0


@pytest.mark.parametrize('legacy', [True, False])
def test_finish_uses_fresh_rest_get_and_rejects_double_finish(legacy):
    entry, port, rid = claimed_state()
    before = port.claim(rid, port.read(rid)['row']); before[9] = '2026-01-01T00:00:00.000Z'
    port.state['requests'][1] = before
    plan = validate_snapshot(json.loads(before[5]), entry)
    result = dict(request_id=rid, snapshot_digest=before[6], **plan)
    reply = js(**port.state, http_ports=True, stale_spreadsheet=True, legacy_spreadsheet_reader=legacy,
               commands=[dict(action='finish', request_id=rid, before=before, result=result)] * 2)
    assert reply['writes'] == 1 and reply['requests'][1][7] == '検証済み・未記帳'
    assert reply['requests'][1][:7] == before[:7]
    assert reply['errors'] == (['review_transition_unknown', 'review_disabled_or_unknown'] if legacy else ['review_stale_request'])
