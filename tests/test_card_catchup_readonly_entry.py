from copy import deepcopy
from types import SimpleNamespace
import pytest

from app import aupay_card_window_preview as entry
from test_aupay_card_window_entry import env, GitHub, SHA


def catchup_env(index=0):
    (cp,end),digest=list(entry.CATCHUP_WINDOWS.items())[index]
    from datetime import datetime,timedelta
    return dict(env(),EXPECTED_CHECKPOINT=cp,PREVIEW_START=(datetime.fromisoformat(cp)-timedelta(days=2)).isoformat(),
                PREVIEW_END=end,EXPECTED_PLAN_SHA256=digest)


@pytest.mark.parametrize('index',[0,1])
def test_approved_current_checkpoint_and_cutoff_bind_readonly_contract(index):
    values=catchup_env(index);github=GitHub();github.history=8
    entry.controls(values,SHA,github)
    assert entry.preview_contract(values)==values['EXPECTED_PLAN_SHA256']


@pytest.mark.parametrize('field,value',[
    ('EXPECTED_CHECKPOINT','2026-11-05T21:00:33+09:00'),
    ('PREVIEW_START','2026-11-01T21:00:33+09:00'),
    ('PREVIEW_END','2026-11-17T21:00:33+09:00'),
    ('EXPECTED_PLAN_SHA256','f'*64),('EXPECTED_PLAN_SHA256','')])
def test_no_arbitrary_window_or_digest_is_accepted(field,value):
    values=catchup_env();values[field]=value
    with pytest.raises(entry.Stop,match='fixed_window_mismatch'):entry.controls(values,SHA,GitHub())


@pytest.mark.parametrize('fault',[None,'contract','snapshot','planner','history','no_write'])
def test_new_readonly_connection_uses_same_prepare_and_never_saves(monkeypatch,tmp_path,fault):
    from app import production_run,aupay_card_first_window_apply as apply,aupay_card_incremental_coverage as coverage
    from app import drive_run_state,production_ledger,aupay_card_incremental_guard as guard,aupay_card_incremental_recovery as recovery
    from app import amazon_money_runtime,amazon_gmail_preview
    values=catchup_env();values['GOOGLE_GMAIL_TOKEN_JSON']='synthetic'
    expected=values['EXPECTED_PLAN_SHA256'];calls=[];seen=[]
    db=SimpleNamespace(sid='sheet',svc=object(),get_raw_ranges=lambda ranges:[[],[]])
    store=SimpleNamespace(read=lambda key:None,write=lambda *a:pytest.fail('persistent write'))
    card=SimpleNamespace(binding=object(),read=lambda:b'{}')
    parent=SimpleNamespace(binding=object(),read=lambda:b'{}')
    preflight=[]
    monkeypatch.setattr(drive_run_state,'validate',lambda *a:{'native':'readonly'})
    monkeypatch.setattr(production_ledger,'validate_ledger',lambda *a:{'parent':'readonly'})
    def history(*a,**k):
        preflight.append(('history',k))
        if fault=='history':raise entry.Stop('previous_dispatch_no_retry')
    def no_write(*a,**k):
        preflight.append(('no_write',a))
        if fault=='no_write':raise entry.Stop('no_write_financial_changed')
    monkeypatch.setattr(guard,'history_controls',history)
    monkeypatch.setattr(recovery,'verify_no_write_state',no_write)
    def observe(*a,**k):
        calls.append(1)
        return {'state':'changed' if fault=='snapshot' and len(calls)>1 else 'same'}
    monkeypatch.setattr(entry,'observe',observe);monkeypatch.setattr(entry,'controls',lambda *a:None)
    monkeypatch.setattr(production_run,'run_durable_source',lambda durable,path,run,apply:run(path))
    monkeypatch.setattr(amazon_money_runtime,'_configured_writer',lambda *a,**k:store)
    monkeypatch.setattr(amazon_gmail_preview,'gmail_readonly_service',lambda *a:object())
    def prepare(**kwargs):
        seen.append(kwargs)
        if fault=='planner':raise entry.Stop('source_evidence_mismatch')
        return {'totals':{'planned':'only'}}
    monkeypatch.setattr(apply,'prepare',prepare)
    monkeypatch.setattr(coverage,'plan_contract_sha256',lambda p:'0'*64 if fault=='contract' else expected)
    key=SimpleNamespace(load=lambda:object())
    action=lambda:entry.catchup_preview(values,None,SHA,tmp_path,card,parent,object(),db,store,None,key,object(),expected)
    if fault:
        with pytest.raises(entry.Stop):action()
    else:
        result=action();assert result['plan_sha256']==expected
        assert result['persistent_state_saves']==result['review_writes']==result['financial_write_requests']==0
        assert result['apply_history_preflight'] is result['runtime_competition_recheck_required'] is True
    assert [v[0] for v in preflight]==(['history'] if fault=='history' else ['history','no_write'])
    assert preflight[0][1]['reconciliation_state']==({'native':'readonly'},{'parent':'readonly'})
    assert preflight[0][1]['checkpoint_value']==values['EXPECTED_CHECKPOINT']
    assert 'defer_reconciliation' not in preflight[0][1] and 'current' not in preflight[0][1]
    assert len(calls)==2
    if fault in {'history','no_write'}:assert not seen
    else:
        assert seen[0]['incremental'] is True
        assert seen[0]['expected_checkpoint']==values['EXPECTED_CHECKPOINT']
        assert seen[0]['cutoff'].isoformat()==values['PREVIEW_END']
