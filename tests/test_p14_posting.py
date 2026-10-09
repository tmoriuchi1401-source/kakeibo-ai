from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import json,pytest
from test_p14_plan_only import Readers,RID,OWNER
from app.receipt_plan import posting as w,posting_live as live,runner as p
from app.drive_run_state import StateError

NOW=1791530000
ATTEMPT='22222222-2222-4222-8222-222222222222'

class Store:
    def __init__(self,value):self.value=value;self.version=0;self.writes=0
    def read(self):return deepcopy(self.value),'"'+str(self.version)+'"'
    def replace(self,before,tag,after):
        assert self.read()==(before,tag)
        w.validate_state(after);self.value=deepcopy(after);self.version+=1;self.writes+=1

class Writer:
    def __init__(self,rd):self.rd=rd;self.calls=[];self.fail=None
    def append_raw(self,title,rows):
        self.calls.append((title,deepcopy(rows)))
        if self.fail=='before':raise TimeoutError('synthetic')
        saved=deepcopy(rows)
        if self.fail=='partial':saved=saved[:1]
        self.rd.tables[title].extend(saved)
        if self.fail in {'partial','after'}:raise TimeoutError('synthetic')

class Projection:
    def __init__(self):self.hidden=False
    def preflight(self,snapshot):pass
    def hide(self,snapshot):self.hidden=True

def setup():
    rd=Readers();plan=p.prepare(rd,RID)
    value=w.approval(plan,RID,OWNER,approval_reference='synthetic-owner-explicit-post-3801',approved_at=NOW-1,expires_at=NOW+600)
    store=Store(value);writer=Writer(rd);projection=Projection()
    return rd,store,writer,projection

def apply(args,operation='apply'):
    return w.execute(*args,RID,attempt_id=ATTEMPT,now=NOW,operation=operation)

def test_existing_writer_exact_and_replay_no_more_append():
    args=setup();rd,store,writer,projection=args
    first=apply(args);before=deepcopy((rd.tables,store.value,store.writes,writer.calls))
    second=apply(args,'replay')
    assert first['accounting_rows_written']==12 and second['accounting_rows_written']==0
    assert second['replay'] and before==(rd.tables,store.value,store.writes,writer.calls)
    assert len(writer.calls)==3 and [len(rows) for title,rows in writer.calls]==[1,10,1]
    assert store.value['state']=='complete' and store.value['generation']==2 and projection.hidden
    assert len(store.value['history'])==1 and first['history_events']==1
    assert sum(r[4] for r in rd.tables['支出明細'][1:])==3801

@pytest.mark.parametrize('key',['plan_digest','snapshot_digest','identity_digest','confirmation_digest','before_ledger_digest','owner_actor_id','request_id'])
def test_tampered_or_different_grant_no_ledger_calls(key):
    args=setup();store=args[1];grant=store.value['grant']
    grant[key]='3'*64 if key!='request_id' else '33333333-3333-4333-8333-333333333333'
    grant.pop('digest');store.value['grant']=w.seal(grant)
    with pytest.raises(StateError):apply(args)
    assert not args[2].calls

@pytest.mark.parametrize('field',['category','amount','item_id'])
def test_current_input_edit_rejected_before_claim(field):
    args=setup();args[0].snapshot['items'][0][field]='changed'
    with pytest.raises((StateError,ValueError)):apply(args)
    assert args[1].writes==0 and not args[2].calls

def test_expired_unused_grant_and_replay_without_posting_rejected():
    for operation in ('apply','replay'):
        args=setup();grant=args[1].value['grant'];grant.pop('digest')
        grant.update(approved_at=NOW-1000,expires_at=NOW-1);args[1].value['grant']=w.seal(grant)
        with pytest.raises(StateError):apply(args,operation)
        assert not args[2].calls

def test_existing_duplicate_keeps_unused_grant():
    args=setup();args[0].tables['レシート'].append(['other','2026-09-28','shop',3801])
    with pytest.raises(StateError):apply(args)
    assert args[1].value['state']=='unused' and not args[2].calls

def test_claimed_request_never_retries_missing_rows():
    args=setup();value=args[1].value;value.update(state='claimed',generation=1,claim={'attempt_id':ATTEMPT,'claimed_at':NOW})
    with pytest.raises(StateError,match='reconciliation'):apply(args)
    assert not args[2].calls and not args[3].hidden

def test_partial_write_readback_and_reinvoke_never_append():
    args=setup();writer=args[2]
    original=writer.append_raw
    def fail_second(title,rows):
        if title=='支出明細':writer.fail='partial'
        original(title,rows)
    writer.append_raw=fail_second
    with pytest.raises(StateError,match='reconciliation'):apply(args)
    assert args[1].value['state']=='unknown' and len(writer.calls)==2 and not args[3].hidden
    with pytest.raises(StateError):apply(args)
    assert len(writer.calls)==2

def test_write_before_first_response_failure_no_retry():
    args=setup();args[2].fail='before'
    with pytest.raises(StateError):apply(args)
    assert len(args[2].calls)==1 and args[1].value['state']=='unknown'
    with pytest.raises(StateError):apply(args)
    assert len(args[2].calls)==1

def test_final_write_response_lost_exact_readback_closes_without_retry():
    args=setup();writer=args[2];original=writer.append_raw
    def lose_response(title,rows):
        if title=='取込データ':writer.fail='after'
        original(title,rows)
    writer.append_raw=lose_response
    result=apply(args);assert result['exact_readback'] and len(writer.calls)==3
    apply(args,'replay');assert len(writer.calls)==3

def test_complete_write_state_save_failure_reconcile_readonly():
    args=setup();store=args[1];original=store.replace
    def lost_final(before,tag,after):
        if after['state']=='complete':raise TimeoutError('synthetic')
        original(before,tag,after)
    store.replace=lost_final
    with pytest.raises(TimeoutError):apply(args)
    assert store.value['state']=='claimed' and len(args[2].calls)==3
    store.replace=original
    result=apply(args,'replay');assert result['accounting_rows_written']==0 and len(args[2].calls)==3

def test_changed_existing_row_or_formula_never_terminal():
    args=setup();rd=args[0];rd.tables['レシート'].append(['old','2026-01-01','shop',100])
    # Re-grant this known preflight, then mutate an old row during writing.
    args[1].value=w.approval(p.prepare(rd,RID),RID,OWNER,approval_reference='synthetic-owner',approved_at=NOW-1,expires_at=NOW+600)
    writer=args[2];original=writer.append_raw
    def mutate(title,rows):
        original(title,rows);rd.tables['レシート'][1][3]=200
    writer.append_raw=mutate
    with pytest.raises(StateError):apply(args)
    assert not args[3].hidden and args[1].value['state']=='unknown'

def test_trimmed_optional_cells_are_exact_normalized():
    args=setup();writer=args[2];original=writer.append_raw
    def trim(title,rows):
        original(title,rows)
        for row in args[0].tables[title][1:]:
            while row and row[-1]=='':row.pop()
    writer.append_raw=trim
    assert apply(args)['exact_readback']

def test_scoped_adapter_rejects_unapproved_rows_update_delete():
    args=setup();plan=w.fresh_plan(args[0],RID);db=w.ScopedMaterializerDB(args[2],args[0],plan,args[1].value['grant'])
    with pytest.raises(StateError):db.append('レシート',[['rogue']])
    with pytest.raises(StateError):db.update_row_raw('レシート',2,[])
    with pytest.raises(StateError):db.clear('レシート!A2')
    assert not args[2].calls

def test_plan_only_still_has_no_posting_store_or_writer(monkeypatch):
    from test_p14_plan_only import Queue
    monkeypatch.setattr(live.PostingStore,'replace',lambda *a:pytest.fail('posting reached'))
    assert p.execute(Readers(),Queue(),RID)['accounting_writes']==0

def test_posting_workflow_no_pr_checkout_schedule_or_new_secret():
    text=(Path(__file__).parents[1]/'.github/workflows/p14-posting-canary.yml').read_text()
    assert 'schedule:' not in text and 'cron:' not in text and 'inputs.approved_sha' not in text
    assert 'kakeibo-production' in text and 'plan_only' not in text
    assert '${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}' in text and '${{ secrets.SPREADSHEET_ID }}' in text
    assert 'GEMINI' not in text and 'pull-requests: write' not in text

def test_state_event_budget_compact_and_no_raw_snapshot():
    args=setup();apply(args)
    value=args[1].value
    event=next(iter(next(iter(value['history'].values())).values()))
    assert len(json.dumps(event).encode())<2048 and event['retention_class']=='permanent'
    assert not {'items','snapshot','image','token','email','merchant'}&set(event)

def test_boundary_rejects_wrong_workflow_write_mode_and_debug(monkeypatch):
    monkeypatch.setattr(live,'verify_execution_boundary',lambda *a:None)
    env=dict(GITHUB_EVENT_NAME='workflow_dispatch',KAKEIBO_LEGACY_DISABLED='true',GITHUB_WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/p14-posting-canary.yml@refs/heads/main',P14_POST_CONFIRM='POST_P14_3801',P14_POST_OPERATION='apply',SPREADSHEET_ID=p.SID,P14_PLAN_REQUEST_ID=RID,P14_POSTING_FILE='synthetic-posting-file')
    live.boundary(env,'head')
    for key,value in [('GITHUB_WORKFLOW_REF','other'),('P14_POST_CONFIRM',''),('P14_POST_OPERATION','write_all'),('RUNNER_DEBUG','1')]:
        with pytest.raises(StateError):live.boundary({**env,key:value},'head')

class Http:
    def __init__(self,value,mode='success'):
        self.value=value;self.mode=mode;self.version=0;self.puts=0
        self.meta={'id':'synthetic-posting-file','etag':'"0"','parents':[{'id':'synthetic-private-folder'}],
            'mimeType':'application/json','owners':[{'emailAddress':'owner@synthetic.invalid'}],
            'permissions':[{'type':'user','role':'owner','emailAddress':'owner@synthetic.invalid'},
                           {'type':'user','role':'writer','emailAddress':'sa@synthetic.invalid'}]}
    def get(self,url,**kwargs):
        if kwargs['params'].get('alt')=='media':return SimpleNamespace(status_code=200,content=json.dumps(self.value).encode())
        return SimpleNamespace(status_code=200,json=lambda:deepcopy(self.meta))
    def put(self,url,**kwargs):
        self.puts+=1
        assert url=='https://www.googleapis.com/upload/drive/v2/files/synthetic-posting-file'
        assert kwargs['headers']['If-Match']==self.meta['etag']
        if self.mode=='412':return SimpleNamespace(status_code=412)
        if self.mode=='timeout_before':raise TimeoutError('synthetic')
        self.value=json.loads(kwargs['data']);self.version+=1;self.meta['etag']='"'+str(self.version)+'"'
        if self.mode=='timeout_after':raise TimeoutError('synthetic')
        return SimpleNamespace(status_code=200)

def port_setup(mode='success'):
    args=setup();http=Http(args[1].value,mode)
    cfg={'candidate_file':'synthetic-candidate','journal_file':'synthetic-journal',
         'drive':{'folder':'synthetic-private-folder','authority_file':'synthetic-hga',
                  'page':{'source':{'source_file_id':'synthetic-source'}},'owner_digest':p.digest('owner@synthetic.invalid')}}
    store=live.PostingStore({'client_email':'sa@synthetic.invalid'},cfg,'synthetic-posting-file',http=http)
    before,tag=store.read();after=deepcopy(before);after.update(state='claimed',generation=1,claim={'attempt_id':ATTEMPT,'claimed_at':NOW})
    return store,http,before,tag,after

def test_conditional_412_does_not_fallback_or_retry():
    store,http,before,tag,after=port_setup('412')
    with pytest.raises(StateError,match='HTTP_412'):store.replace(before,tag,after)
    assert http.puts==1 and store.read()[0]==before

def test_stale_etag_rejected_before_put():
    store,http,before,tag,after=port_setup();http.meta['etag']='"changed"'
    with pytest.raises(StateError,match='HTTP_412'):store.replace(before,tag,after)
    assert http.puts==0

@pytest.mark.parametrize('mode',['success','timeout_after'])
def test_conditional_exact_readback_and_known_lost_response(mode):
    store,http,before,tag,after=port_setup(mode);store.replace(before,tag,after)
    assert http.puts==1 and store.read()[0]==after

def test_conditional_unknown_no_repeat_put():
    store,http,before,tag,after=port_setup('timeout_before')
    with pytest.raises(StateError,match='unknown'):store.replace(before,tag,after)
    assert http.puts==1

def test_authority_file_acl_and_source_target_rejected():
    store,http,before,tag,after=port_setup();http.meta['permissions'].append({'type':'anyone','role':'reader'})
    with pytest.raises(StateError):store.read()
    with pytest.raises(StateError):live.PostingStore(store.info,store.cfg,'synthetic-source',http=http)

def test_ui_failure_before_claim_no_write():
    args=setup()
    def fail(_):raise StateError('posting_projection_identity_changed')
    args[3].preflight=fail
    with pytest.raises(StateError):apply(args)
    assert args[1].value['state']=='unused' and not args[2].calls
