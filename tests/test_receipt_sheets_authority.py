"""Requested seven scenario families; synthetic JS receiver -> real Python core.

No test asserts Sheets CAS or protection against an owner sorting mid-PUT.
Those require an excluded writer/structural-edit activation gate.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import re
from pathlib import Path
import subprocess

import pytest
import yaml

from app.drive_run_state import StateError
from app.receipt_sheets_authority import (HEADERS, UNKNOWN, SheetAuthority, canonical,
    check_row, display_record, execute, result_formula)
from app.receipt_sheets_worker import make_binding, GooglePort, run, verify_delivery_history
from test_receipt_private_adapter import fixture
from test_receipt_sheet_validation import RID, RID2

STAMP = '2026-11-16T07:00:00.000Z'


@pytest.fixture(autouse=True)
def no_legacy_or_financial_io(monkeypatch):
    from app.receipt_private_adapter import ReceiptPrivateAdapter
    from app.sheets import SheetsDB
    def deny(*a, **kw): raise AssertionError('forbidden financial/legacy IO')
    monkeypatch.setattr(ReceiptPrivateAdapter, '__init__', deny)
    monkeypatch.setattr(SheetsDB, '__init__', deny)


def registration(*, fault=None, repeat=False, category=False, setup=False):
    world, row, catalog, _ = fixture()
    # This is an entirely independent fixture, never an existing failed receipt.
    c = make_binding(catalog, spreadsheet_id='synthetic-sheet', folder_id='synthetic-folder',
        owner_email='owner@synthetic.invalid', service_email='service@synthetic.invalid',
        ref='review/receipt-sheets-authority-20261116')
    c['enabled'] = True
    row[8] = 'register:' + RID
    if category:
        row[21:23] = world.sheet['categories'][1]
    completed = subprocess.run(['node', 'apps-script/receipt-validation/Sheets.test.cjs'],
        input=json.dumps(dict(config=c, table=[HEADERS], ui=[row], request_id=RID,
                             fault=fault, repeat=repeat, setup=setup)),
        text=True, encoding='utf-8', capture_output=True, check=True)
    return world, c, row, json.loads(completed.stdout)


class Port:
    def __init__(self, rows):
        self.rows, self.writes, self.fault = deepcopy(rows), [], None
        self.after_read = None
    def read(self):
        value = deepcopy(self.rows)
        if self.after_read: self.after_read(self)
        return value
    def update(self, number, tail):
        self.writes.append((number, deepcopy(tail)))
        if self.fault == ('before', len(self.writes)): raise TimeoutError('synthetic')
        self.rows[number-1][7:] = deepcopy(tail)
        if self.fault == ('after', len(self.writes)): raise TimeoutError('synthetic')
        if self.fault == ('readback', len(self.writes)): self.rows[number-1][9] = 'damaged'


def worker(world, c, port, **kw):
    def context(snapshot):
        assert snapshot['context_json'] == c['cases'][world.case_id]['context_json']
    def source(source):
        assert source == world.source
    return execute(RID, authority=SheetAuthority(port, clock=lambda: STAMP),
                   context_check=context, source_check=source, **kw)


def display(world, port):
    return display_record(RID, port.rows, receipt_id=world.case_id, source=world.source)


def test_normal_one_checkbox_to_validated_unposted_and_existing_ui_intact():
    w, c, original, js = registration(setup=True)
    assert js['output']['state'] == '受付済み' and not js['stopped']
    assert js['writes'] == 1 and js['dispatches'] == [dict(ref=c['ref'], inputs=dict(request_id=RID))]
    assert js['ui'] == [original]
    assert js['setup']['enabled'] is False and js['setup']['token_copy'] is False
    assert js['setup']['trigger_count'] == 1 and js['setup']['trigger_mutations'] == 0
    assert js['errors'] == ['receipt_sheets_configuration_invalid']  # Stopped receiver rejected before IO.
    snapshot = check_row(js['rows'][1])
    assert '保持するメモ' not in js['rows'][1][5]
    assert snapshot['operations'][0]['changes']['amount'] == 868
    port = Port(js['rows'])
    output = worker(w, c, port)
    assert output == dict(request_id=RID, state='検証済み・未記帳', financial_write_count=0, posting_authority=False)
    assert display(w, port) == '検証済み・未記帳'
    assert len(port.writes) == 2
    assert port.rows[1][:7] == js['rows'][1][:7]
    assert check_row(port.rows[1]) == snapshot
    # Evaluate the actual formula's Sheets string-concatenation regex operand.
    # This catches Unicode escaping/quote bugs without a live Sheet mutation.
    expression=result_formula(2).split('REGEXMATCH(res,',1)[1].split(')),st&',1)[0]
    tokens=re.findall(r'"(?:[^"]|"")*"|rid|dg|st',expression)
    values={'rid':RID,'dg':port.rows[1][6],'st':port.rows[1][7]}
    pattern=''.join(token[1:-1].replace('""','"') if token.startswith('"') else values[token] for token in tokens)
    assert re.fullmatch(pattern,port.rows[1][8])
    assert not re.fullmatch(pattern,port.rows[1][8].replace(RID,RID2))
    # Lifecycle prerequisites are part of the same normal-registration family.
    for gate, reason in [('missing','receipt_sheets_existing_trigger_required'),
                         ('duplicate','receipt_sheets_existing_trigger_required'),
                         ('latch','receipt_sheets_start_not_ready'),
                         ('old_binding','receipt_sheets_existing_binding_conflict')]:
        _, _, _, rejected=registration(setup=gate)
        assert rejected['errors']==[reason] and rejected['writes']==0 and not rejected['dispatches']
        assert rejected['setup']['enabled'] is False and rejected['setup']['trigger_mutations']==0
        if gate=='old_binding':assert rejected['setup']['mode']=='old-journal'
    _, _, _, stopped=registration(fault='stop_before_lock')
    assert stopped['errors']==['receipt_sheets_configuration_invalid']
    assert stopped['writes']==0 and not stopped['calls'] and not stopped['dispatches']


def test_amount_and_category_changes_are_captured_and_later_ui_edits_are_drafts():
    w, c, original, js = registration(category=True)
    snap = check_row(js['rows'][1])
    assert snap['operations'][0]['changes']['amount'] == 868
    assert snap['operations'][0]['changes']['category'] == list(w.sheet['categories'][1])
    # Later UI changes aren't even passed to the worker; frozen contents win.
    js['ui'][0][20] = 1
    port = Port(js['rows'])
    assert worker(w, c, port)['state'] == '検証済み・未記帳'
    assert check_row(port.rows[1])['operations'] == snap['operations']


@pytest.mark.parametrize('fault', ['capture', 'insert_before', 'insert_after', 'readback', 'dispatch', 'dispatch_status'])
def test_registration_capture_or_io_failure_never_automatically_resends(fault):
    _, _, _, js = registration(fault=fault, repeat=True)
    assert len(js['dispatches']) <= 1 and js['writes'] <= 1
    assert js['output'] is None or js['output']['state'] == UNKNOWN
    if fault != 'capture':
        assert js['stopped'] and js['repeated'] is None and js['errors'][-1] == 'receipt_sheets_stopped'
    else:
        assert js['writes'] == 0  # Second invocation also rejects changed capture.


@pytest.mark.parametrize('damage', ['snapshot', 'digest', 'request', 'missing', 'duplicate', 'state'])
def test_mid_validation_table_change_prevents_result_write(damage):
    w, c, _, js = registration()
    p = Port(js['rows'])
    from app.receipt_sheets_authority import validate_frozen
    def validate(snapshot):
        if damage == 'snapshot': p.rows[1][5] = p.rows[1][5].replace('868', '869')
        if damage == 'digest': p.rows[1][6] = 'a'*64
        if damage == 'request': p.rows[1][0] = RID2
        if damage == 'missing': p.rows.pop()
        if damage == 'duplicate': p.rows.append(deepcopy(p.rows[1]))
        if damage == 'state': p.rows[1][7] = '受付済み'
        return validate_frozen(snapshot)
    assert worker(w, c, p, validate=validate)['state'] == UNKNOWN
    assert len(p.writes) == 1 and display(w, p) != '検証済み・未記帳'


def test_request_id_mismatch_stops_before_claim():
    w, c, _, js = registration()
    p = Port(js['rows']); p.rows[1][0] = RID2
    with pytest.raises(StateError): worker(w, c, p)
    assert p.writes == [] and display(w, p) == UNKNOWN


def test_worker_exception_stays_unfinished_and_rerun_is_rejected():
    w, c, _, js = registration()
    p = Port(js['rows'])
    def crash(_): raise RuntimeError('synthetic worker failure')
    assert worker(w, c, p, validate=crash)['state'] == UNKNOWN
    assert p.rows[1][7:9] == ['検証中', ''] and display(w, p) != '検証済み・未記帳'
    with pytest.raises(StateError, match='replay_rejected'): worker(w, c, p)
    assert len(p.writes) == 1


@pytest.mark.parametrize('phase', [1, 2])
@pytest.mark.parametrize('fault', ['before', 'after', 'readback'])
def test_claim_or_result_write_failure_and_unknown_response_stop_without_resend(phase, fault):
    w, c, _, js = registration()
    p = Port(js['rows']); p.fault = (fault, phase)
    assert worker(w, c, p)['state'] == UNKNOWN
    assert len(p.writes) == phase
    # A lost write response itself is never rendered as a successful reply.
    # A separate read may prove a fully committed final result (no repair).
    if phase == 2 and fault == 'after':
        assert display(w, p) == '検証済み・未記帳'
    else:
        assert display(w, p) != '検証済み・未記帳'


def test_manual_completed_request_and_duplicate_edit_are_rejected():
    w, c, _, js = registration(repeat=True)
    assert js['errors'] == ['receipt_sheets_replay_rejected']
    assert len(js['dispatches']) == js['writes'] == 1
    p = Port(js['rows']); worker(w, c, p)
    with pytest.raises(StateError, match='replay_rejected'): worker(w, c, p)
    assert len(p.writes) == 2


def test_actions_rerun_or_local_execution_rejected_before_private_connection():
    _, c, _, _ = registration()
    env = dict(RECEIPT_VALIDATION_CONFIG_JSON=canonical(c), RECEIPT_VALIDATION_ENABLED='true',
        GITHUB_REPOSITORY=c['repository'], GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_RUN_ATTEMPT='1', GITHUB_REF='refs/heads/'+c['ref'], RECEIPT_VALIDATION_REF=c['ref'],
        GITHUB_SHA='a'*40, RECEIPT_VALIDATION_SHA='a'*40, RECEIPT_REQUEST_ID=RID, GITHUB_RUN_ID='100')
    def deny(*a): raise AssertionError('must reject before connecting')
    for changes in [dict(GITHUB_RUN_ATTEMPT='2'), dict(GITHUB_EVENT_NAME='push'),
                    dict(GITHUB_REF='refs/heads/main'), dict(RECEIPT_VALIDATION_ENABLED='false'),
                    dict(GITHUB_SHA='b'*40), dict(ACTIONS_STEP_DEBUG='true')]:
        assert run(dict(env, **changes), connection=deny) == (3, UNKNOWN)


def test_new_manual_dispatch_is_rejected_even_when_first_claim_never_committed():
    env=dict(RECEIPT_REQUEST_ID=RID,GITHUB_RUN_ID='100',GITHUB_SHA='a'*40)
    first=dict(display_title='Category request '+RID,id=100,run_attempt=1,head_sha='a'*40,event='workflow_dispatch')
    verify_delivery_history(dict(total_count=1,workflow_runs=[first]),env)
    for history in [dict(total_count=2,workflow_runs=[first,dict(first,id=101)]),
                    dict(total_count=101,workflow_runs=[first]),
                    dict(total_count=0,workflow_runs=[])]:
        with pytest.raises(StateError):verify_delivery_history(history,dict(env,GITHUB_RUN_ID='101'))
    # Rerun of a job is rejected as well, irrespective of Sheet state.
    with pytest.raises(StateError):
        verify_delivery_history(dict(total_count=1,workflow_runs=[dict(first,run_attempt=2)]),env)


def test_actual_http_port_uses_only_management_tail_and_original_reads():
    w, c, _, js = registration()
    p = Port(js['rows'])
    original_metadata = deepcopy(w.metadata[w.source['source_id']])
    original_metadata['permissions'][1]['role'] = 'reader'
    class Response:
        status_code=200
        content=w.raw
        def __init__(self, data): self.data=data
        def json(self): return deepcopy(self.data)
    class Session:
        def get(self, url, **kw):
            assert kw['timeout']==20 and kw['allow_redirects'] is False
            if 'sheets.googleapis.com' in url: return Response({'values':p.read()})
            assert url.endswith('/'+w.source['source_id'])
            return Response(original_metadata)
        def put(self, url, **kw):
            assert "H2%3AJ2" in url and kw['params']=={'valueInputOption':'RAW'}
            assert kw['allow_redirects'] is False
            p.update(2, kw['json']['values'][0]); return Response({})
    g=GooglePort(Session(),c)
    assert execute(RID,authority=SheetAuthority(g,clock=lambda:STAMP),source_check=g.source_check,
                   context_check=g.context_check)['state']=='検証済み・未記帳'
    assert len(p.writes)==2


def test_one_serial_group_and_existing_result_cell_contract():
    workflow=yaml.load(Path('.github/workflows/category-sheet-request.yml').read_text(),Loader=yaml.BaseLoader)
    assert workflow['concurrency']['cancel-in-progress']=='false'
    assert 'review/receipt-sheets-' in workflow['concurrency']['group']
    assert "'receipt-synthetic-validation'" in workflow['concurrency']['group']
    job=workflow['jobs']['receipt-sheets-validation']
    assert 'github.run_attempt == 1' in job['if'] and "startsWith(vars.RECEIPT_VALIDATION_REF, 'review/receipt-sheets-')" in job['if']
    formula=result_formula(2)
    assert 'COUNTIF' in formula and 'n=1' in formula and 'UNIQUE' not in formula
    assert '_receipt_validation_results' not in formula and '_receipt_requests' in formula
    assert '登録時の内容' in formula and 'financial_write_count' in formula


def test_result_formula_closes_let_before_iferror_fallback():
    # Validate the whole generated expression, not only the result regex.
    # Appending a ')' at EOF balances text but still leaves IFERROR's fallback
    # inside LET; function arity checks catch that incorrect repair too.
    expression = result_formula(2)
    stack, functions, quoted, pos = [], [], False, 0
    while pos < len(expression):
        char = expression[pos]
        if char == '"':
            if quoted and pos+1 < len(expression) and expression[pos+1] == '"':
                pos += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == '(':
                name = re.search(r'([A-Z][A-Z0-9_]*)$', expression[:pos])
                stack.append([name.group(1) if name else None, 1])
            elif char == ',':
                assert stack, 'comma outside function'
                stack[-1][1] += 1
            elif char == ')':
                assert stack, 'unexpected closing parenthesis'
                functions.append(stack.pop())
        pos += 1
    assert not quoted and not stack, 'unterminated string or function'
    for name, argc in functions:
        if name == 'IF': assert argc == 3
        if name == 'IFERROR': assert argc == 2
        if name == 'LET': assert argc >= 3 and argc % 2 == 1
