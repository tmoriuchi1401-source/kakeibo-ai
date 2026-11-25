"""Real receiver kernel in Node, fake clocks/gate/storage, no live credentials."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.receipt_review_daily_permit import PermitClient, canonical, validate_permit, require_daily_transport
from test_receipt_pr_b import case, registered, js, deny_finance_and_network

NOW = 1800000000000
BINDING = dict(worker_sha='a'*40, script_id='synthetic-script-b', review_spreadsheet_id='synthetic-sheet-b')
PID = '11111111-1111-4111-8111-111111111111'
SECRET = 'SYNTHETIC_CREDENTIAL_DO_NOT_LOG'


def permit(**changes):
    return dict(schema='receipt-daily-permit-v1', permit_id=PID, issued_at_ms=NOW,
                expires_at_ms=NOW+300000, **BINDING) | changes


def daily(command=None, *, sequence=1, value=None, now=NOW, **extra):
    return dict(action='daily', now=now, envelope=dict(permit=value or permit(), sequence=sequence,
                command=command or {'action':'status'}), **extra)


def invoke(commands, *, value=None, properties=None, **kwargs):
    return js(config={'enabled':False}, properties={'RECEIPT_GITHUB_TOKEN':None, **(properties or {})},
              gate=canonical(value or permit()), commands=commands, **kwargs)


@pytest.mark.parametrize('change', [
    {'expires_at_ms':NOW+300001}, {'expires_at_ms':NOW}, {'issued_at_ms':NOW+1},
    {'worker_sha':'b'*40}, {'script_id':'synthetic-other-script'},
    {'review_spreadsheet_id':'synthetic-other-book'}, {'permit_id':'bad'},
    {'expires_at_ms':True}, {'token':SECRET},
])
def test_invalid_permit_rejected_by_both_sides_before_any_effect(change):
    value = permit(**change)
    with pytest.raises(ValueError): validate_permit(value, BINDING, NOW)
    result = invoke([daily(value=value)], value=value)
    assert result['errors'] and result['writes'] == 0
    assert not any(k.startswith('RECEIPT_REVIEW_DAILY') for k in result['properties'])
    assert SECRET not in canonical(result)


@pytest.mark.parametrize('time', [NOW+300000, NOW+300001, NOW-1])
def test_receiver_refuses_expired_or_future_even_when_pc_clock_not_available(time):
    result = invoke([daily(now=time)])
    assert result['errors'] and result['writes'] == 0


def test_receiver_reads_are_leased_without_long_on_or_registration_changes():
    result = invoke([daily(), daily({'action':'close'}, sequence=2)])
    assert result['errors'] == [] and result['outputs'][0]['result']['enabled'] is True
    assert json.loads(result['properties']['RECEIPT_REVIEW_CONFIG_JSON'])['enabled'] is False
    assert result['properties']['RECEIPT_REVIEW_REGISTER_ENABLED'] == 'true'
    assert result['properties']['RECEIPT_REVIEW_DAILY_USED_'+PID]
    assert 'RECEIPT_REVIEW_DAILY_ACTIVE' not in result['properties']
    assert result['writes'] == 0 and not result['diagnostics']


def test_receiver_rejects_envelope_replay_closed_id_and_clock_rollback():
    for commands in ([daily(),daily()],
                     [daily(),daily({'action':'close'},sequence=2),daily(sequence=3)],
                     [daily(now=NOW+1000),daily(sequence=2,now=NOW+999)]):
        result = invoke(commands)
        assert len(result['errors']) == 1 and result['writes'] == 0


@pytest.mark.parametrize('change,sequence',[({'sequence':None},1),({'sequence':True},2),({'inflight':'false'},2)])
def test_corrupt_saved_fence_is_not_coerced_or_replaced(change,sequence):
    first=invoke([daily()])
    properties=deepcopy(first['properties'])
    key='RECEIPT_REVIEW_DAILY_USED_'+PID
    record=json.loads(properties[key]);record.update(change)
    properties[key]=canonical(record)
    result=invoke([daily(sequence=sequence)],properties=properties)
    assert result['errors']==['daily_fence_invalid'] and result['writes']==0
    assert result['properties']==properties


@pytest.mark.parametrize('gate', ['false', 'true', '{}', 'not-json', canonical(permit(worker_sha='b'*40))])
def test_new_receiver_cannot_reinterpret_old_or_mismatched_gate(gate):
    result = invoke([daily(gate=gate)])
    assert result['errors'] == ['daily_gate_mismatch'] and result['writes'] == 0


def test_gate_network_unknown_is_sanitized_without_consuming_permit():
    result = invoke([daily()], daily_gate_fault=SECRET)
    assert result['errors'] == ['daily_gate_unknown']
    assert SECRET not in canonical(result) and result['writes'] == 0


def test_expiry_before_intake_side_effect_keeps_history_and_stop():
    entry, row = case('daily-expiry')
    result = invoke([daily({'action':'intake','entry':entry,'row':row})],
                    daily_before_write=NOW+300000)
    assert result['errors'] == ['daily_outcome_unconfirmed']
    assert result['writes'] == 0 and result['cases'] == [['case_id','source_id','source_version','source_sha256','case_json','case_digest','request_id']]
    assert result['ui'] == [] and result['properties']['RECEIPT_REVIEW_STOPPED']


@pytest.mark.parametrize('action,left', [('intake',119999), ('claim',59999), ('finish',59999), ('status',14999)])
def test_receiver_insufficient_reserve_never_begins_rpc(action,left):
    entry, row = case('reserve')
    command = {'action':action}
    if action == 'intake': command.update(entry=entry,row=row)
    elif action in {'claim','finish'}:
        command.update(request_id=entry['request_id'],before=[])
        if action == 'finish':command['result']={}
    value = permit(expires_at_ms=NOW+left)
    result = invoke([daily(command,value=value)], value=value)
    assert result['errors'] == ['daily_insufficient_time'] and result['writes'] == 0


def test_real_intake_and_backend_claim_finish_work_under_same_permit():
    entry, row = case('daily-success')
    result = invoke([daily({'action':'intake','entry':entry,'row':row})], http_ports=True)
    assert result['errors'] == [] and result['writes'] == 1
    assert result['cases'][1][6] == entry['request_id']
    assert result['properties']['RECEIPT_REVIEW_REGISTER_ENABLED'] == 'true'
    # Existing immutable validator still owns claim/finish, not the permit.
    entry, port = registered()
    before = port.state['requests'][1]
    result = invoke([daily({'action':'claim','request_id':entry['request_id'],'before':before})],
                    cases=port.state['cases'], requests=port.state['requests'], ui=port.state['ui'])
    assert result['errors'] == [] and result['requests'][1][7] == '検証中'


def test_actual_validator_completes_frozen_request_with_leased_receiver(tmp_path):
    from app.receipt_review_worker import AttemptFence, execute_review
    entry, port = registered()
    original = deepcopy(port.state['requests'][1][:7])
    original_ui = deepcopy(port.state['ui'])
    port.state['properties']['RECEIPT_GITHUB_TOKEN']=None
    sequence=0
    def receive(command):
        nonlocal sequence
        sequence+=1
        result=invoke([daily(command,sequence=sequence)],properties=port.state['properties'],
                      cases=port.state['cases'],requests=port.state['requests'],ui=port.state['ui'])
        port.state={k:result[k] for k in ('properties','cases','requests','ui')}
        if result['errors']:raise RuntimeError(result['errors'][0])
        return result['outputs'][0]['result']
    port.invoke=receive
    outcome=execute_review(entry['request_id'],port=port,fence=AttemptFence(tmp_path/'attempts.sqlite'))
    assert outcome['state']=='検証済み・未記帳'
    assert outcome['financial_write_count']==0 and outcome['posting_authority'] is False
    assert port.state['requests'][1][:7]==original and port.state['ui']==original_ui
    assert receive({'action':'close'})=={'closed':True,'financial_write_count':0}


@pytest.mark.parametrize('action', ['claim','finish'])
def test_expiry_between_read_and_transition_prevents_side_effect(action):
    entry,port=registered()
    before=deepcopy(port.state['requests'][1])
    if action=='finish':before[7]='検証中'
    rows=deepcopy(port.state['requests']);rows[1]=before
    command=dict(action=action,request_id=entry['request_id'],before=before)
    if action=='finish':command['result']=dict(request_id=entry['request_id'],snapshot_digest=before[6],
        state='保留',issues=[],posting_authority=False,financial_write_count=0)
    result=invoke([daily(command)],cases=port.state['cases'],requests=rows,ui=port.state['ui'],
                  daily_expire_during='readRequests',daily_expire_at=NOW+300000)
    assert result['errors']==['daily_outcome_unconfirmed'] and result['writes']==0
    assert result['requests']==rows


@pytest.mark.parametrize('fault', ['before','after','readback'])
def test_uncertain_receiver_write_latches_stop_and_used_id(fault):
    entry,row=case('daily-unknown')
    first=invoke([daily({'action':'intake','entry':entry,'row':row})], fault=fault,http_ports=True)
    assert first['errors'] == ['daily_outcome_unconfirmed']
    second=invoke([daily(sequence=2)], properties=first['properties'], cases=first['cases'],
                  requests=first['requests'],ui=first['ui'])
    assert second['errors'] == ['daily_existing_stop'] and second['writes'] == 0
    assert second['properties'] == first['properties']


def test_live_entry_cannot_receive_or_echo_secret_or_mutate():
    result=invoke([{'action':'daily_public','token':SECRET}])
    assert result['errors'] == ['daily_pat_free_gate_reader_required']
    assert SECRET not in canonical(result) and not result['diagnostics'] and result['writes']==0
    with pytest.raises(RuntimeError,match='daily_pat_free_gate_reader_required'):require_daily_transport()


def client(tmp_path, *, transport=None):
    db=tmp_path/'attempts.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS attempts(request_id TEXT PRIMARY KEY)')
        conn.execute("INSERT OR IGNORE INTO attempts VALUES ('original-fence')")
    clock=SimpleNamespace(wall=NOW,elapsed=0)
    calls=[]
    def send(envelope):
        calls.append(envelope)
        return dict(permit_id=PID,sequence=envelope['sequence'],result={'closed':True,'financial_write_count':0} if envelope['command']['action']=='close' else {})
    obj=PermitClient(permit(), binding=BINDING, state_dir=tmp_path/'state',attempt_db=db,
                     transport=transport or send,wall_ms=lambda:clock.wall,monotonic=lambda:clock.elapsed)
    return obj,clock,calls,db


@pytest.mark.parametrize('failure', ['power_cut','network','lost_reply','expiry','clock_back','clock_forward'])
def test_pc_interruption_persists_fences_and_never_retries(tmp_path,failure):
    def unknown(_):raise TimeoutError(SECRET)
    obj,clock,calls,db=client(tmp_path,transport=unknown if failure in {'network','lost_reply'} else None)
    if failure=='power_cut':
        with pytest.raises(RuntimeError,match='daily_stop_or_running_preserved'):
            PermitClient(permit(),binding=BINDING,state_dir=obj.state,attempt_db=db,transport=lambda _:pytest.fail('no send'))
    else:
        if failure=='expiry':clock.wall+=300000;clock.elapsed=300
        elif failure=='clock_back':clock.wall-=1
        elif failure=='clock_forward':clock.wall+=10000
        with pytest.raises(RuntimeError,match='daily_rpc_unknown_no_retry'):obj.rpc('status')
        with pytest.raises(RuntimeError):obj.rpc('status')
        assert obj.stop.exists() and SECRET not in obj.stop.read_text()
    assert obj.running.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT request_id FROM attempts').fetchall()==[('original-fence',)]
        assert conn.execute('SELECT * FROM daily_permits').fetchall()==[(PID,)]
        assert conn.execute('SELECT COUNT(*) FROM daily_rpc_attempts').fetchone()[0] <= 1


def test_client_close_keeps_permanent_used_id_and_refuses_reopen(tmp_path):
    obj,clock,calls,db=client(tmp_path)
    obj.rpc('status');obj.rpc('close')
    assert not obj.running.exists()
    with pytest.raises(RuntimeError):
        PermitClient(permit(),binding=BINDING,state_dir=obj.state,attempt_db=db,transport=lambda _:pytest.fail('no resend'),wall_ms=lambda:NOW)
    assert len(calls)==2


def test_client_secret_rejected_before_injected_transport(tmp_path):
    obj,clock,calls,db=client(tmp_path)
    with pytest.raises(RuntimeError):obj.rpc('status',token=SECRET)
    assert calls==[] and SECRET not in obj.stop.read_text()


def test_client_will_not_start_new_ai_with_insufficient_time(tmp_path):
    obj,clock,calls,db=client(tmp_path)
    clock.wall+=120001;clock.elapsed=120.001
    with pytest.raises(RuntimeError,match='daily_no_new_ai'):obj.before_ai()
    assert calls==[] and obj.stop.exists()


def test_client_ai_permission_is_consumed_once_before_send(tmp_path):
    obj,clock,calls,db=client(tmp_path)
    assert obj.before_ai()==300000
    with pytest.raises(RuntimeError,match='daily_no_new_ai'):obj.before_ai()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT * FROM daily_ai_attempts').fetchall()==[(PID,)]
    assert calls==[] and obj.stop.exists()


def test_receiver_expiry_after_pc_disappears_needs_no_cleanup_or_trigger():
    first=invoke([daily()])
    expired=invoke([daily(sequence=2,now=NOW+300000)],properties=first['properties'])
    assert expired['errors']==['daily_expired'] and expired['writes']==0
    assert expired['properties']==first['properties']
    # A new nonce cannot silently replace the interrupted cycle either.
    newer=permit(permit_id='22222222-2222-4222-8222-222222222222',
                 issued_at_ms=NOW+300000,expires_at_ms=NOW+600000)
    replacement=invoke([daily(value=newer,now=NOW+300000)],value=newer,properties=first['properties'])
    assert replacement['errors']==['daily_start_not_ready'] and replacement['writes']==0


def test_receiver_keeps_preexisting_stop_and_permanent_fences():
    properties={'RECEIPT_REVIEW_STOPPED':'original-stop','original-fence':'untouched'}
    result=invoke([daily()],properties=properties)
    assert result['errors']==['daily_existing_stop'] and result['writes']==0
    assert all(result['properties'][k]==v for k,v in properties.items())


def test_old_start_and_workflow_contract_refuse_new_json_gate():
    from app.receipt_sheets_worker import validate_execution
    from app.drive_run_state import StateError
    value=canonical(permit())
    result=js(config={'enabled':False},gate=value,commands=[{'action':'start'}])
    assert result['errors']==['receipt_sheets_acceptance_not_confirmed']
    workflow=Path('.github/workflows/category-sheet-request.yml').read_text(encoding='utf-8')
    assert "vars.RECEIPT_VALIDATION_ENABLED == 'true'" in workflow
    with pytest.raises(StateError,match='execution_not_approved'):
        validate_execution({'RECEIPT_VALIDATION_ENABLED':value},{'enabled':True})


def test_process_killed_without_finally_retains_running_and_rpc_fence(tmp_path):
    class PowerLoss(BaseException):pass
    def killed(envelope):raise PowerLoss()
    obj,clock,calls,db=client(tmp_path,transport=killed)
    with pytest.raises(PowerLoss):obj.rpc('status')
    assert obj.running.is_file() and not obj.stop.exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT * FROM daily_rpc_attempts').fetchall()==[(PID,1)]
        assert conn.execute('SELECT request_id FROM attempts').fetchall()==[('original-fence',)]
    with pytest.raises(RuntimeError,match='daily_stop_or_running_preserved'):
        PermitClient(permit(),binding=BINDING,state_dir=obj.state,attempt_db=db,transport=lambda _:pytest.fail('no restart send'))


def test_daily_entry_refuses_activation_before_auth_or_remote_io(tmp_path,monkeypatch):
    import sys
    from scripts import run_receipt_review_daily as runner
    from test_receipt_review_daily_safety import arguments
    monkeypatch.setattr(sys,'argv',arguments(tmp_path))
    (tmp_path/'config').write_text('{"enabled":true,"financial_write_enabled":false}')
    monkeypatch.setattr(runner,'access_token',lambda *_:pytest.fail('must not load owner auth'))
    monkeypatch.setattr(runner.subprocess,'check_output',lambda *_a,**_k:pytest.fail('must stop before revision check'))
    assert runner.main()==3
    assert json.loads((tmp_path/'STOP.json').read_text())['exception_type']=='RuntimeError'
    assert (tmp_path/'RUNNING.json').exists()


def test_receiver_kernel_has_no_credential_sinks_or_live_fetch():
    source=Path('apps-script/receipt-validation/DailyPermit.gs').read_text(encoding='utf-8')
    for sink in ('Logger.', 'console.', 'CacheService.', 'UrlFetchApp.', 'getOAuthToken('):
        assert sink not in source
    # The public function has no argument-to-log/property/HTTP path at all.
    public=source.split('function receiptReviewDailyBackend()',1)[1].split('function receiptReviewDailyPermit_',1)[0]
    assert 'throw Error(\'daily_pat_free_gate_reader_required\')' in public
    assert 'setProperty(' not in public


def test_registration_entrypoints_and_manifest_are_unchanged_from_approved_base():
    import re
    import subprocess
    path='apps-script/receipt-validation/Review.gs'
    before=subprocess.check_output(['git','show','952d64663756bdac46e0894d108ab00c3f64220a:'+path],text=True,encoding='utf-8')
    after=Path(path).read_text(encoding='utf-8')
    for name in ('receiptReviewRegistrationEnable','receiptReviewRegistrationDisable','receiptReviewRegistrationStatus',
                 'receiptReviewRegister_','receiptReviewEdited','receiptReviewSnapshot_'):
        pattern=r'function '+name+r'\([\s\S]*?(?=\nfunction |\n/\*\*|\Z)'
        assert re.search(pattern,before).group(0)==re.search(pattern,after).group(0)
    manifest='apps-script/receipt-validation/appsscript.json'
    assert Path(manifest).read_bytes().replace(b'\r\n',b'\n')==subprocess.check_output(['git','show','952d64663756bdac46e0894d108ab00c3f64220a:'+manifest]).replace(b'\r\n',b'\n')


def test_nested_credential_canary_never_reaches_receiver_storage_or_client_transport(tmp_path):
    marker='github_pat_'+SECRET
    entry,row=case('credential-negative')
    row[7]=marker
    command=dict(action='intake',entry=entry,row=row)
    result=invoke([daily(command)])
    assert result['errors']==['daily_secret_forbidden'] and marker not in canonical(result)
    obj,clock,calls,db=client(tmp_path)
    with pytest.raises(RuntimeError):obj.rpc('intake',entry=entry,row=row)
    assert calls==[] and marker not in obj.stop.read_text()
