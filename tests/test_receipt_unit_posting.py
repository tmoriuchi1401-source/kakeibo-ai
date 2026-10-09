"""Synthetic financial approvals only; no live credentials or receipt bytes."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import pytest
from test_receipt_unit_plan import Readers as BaseReaders,CONTEXT,Queue,RID,OWNER
from test_p14_posting import Writer,Projection,NOW,ATTEMPT,Http
from app.receipt_plan import unit_runner as u,unit_posting as w,unit_posting_live as live,posting,runner as p
from app.drive_run_state import StateError
from app.receipt_plan import items,proof

class Readers(BaseReaders):
    def sheet_rows(self,region,*,render='UNFORMATTED_VALUE'):
        if region=="'_PDF確認受付'!A1:F1001":return deepcopy(self.queue.data)
        return super().sheet_rows(region,render=render)

class Store:
    def __init__(self,value):self.value=value;self.version=0;self.writes=0
    def read(self):return deepcopy(self.value),'"'+str(self.version)+'"'
    def replace(self,before,tag,after):
        assert self.read()==(before,tag)
        w.validate_state(after);self.value=deepcopy(after);self.version+=1;self.writes+=1

def setup(page=4,index=1):
    rd=Readers(page,index);rd.queue=Queue();u.execute(rd,rd.queue,RID,CONTEXT)
    value=w.approval(u.prepare(rd,RID),RID,OWNER,CONTEXT,
        approval_reference='synthetic-explicit-unit-financial-approval',approved_at=NOW-1,expires_at=NOW+600)
    return rd,Store(value),Writer(rd),Projection()

def apply(args,operation='apply',**kw):
    return w.execute(*args,RID,kw.pop('context_id',CONTEXT),attempt_id=ATTEMPT,now=NOW,operation=operation,**kw)

def reseal_fixture(rd,*,single=False,tax=False):
    record=rd.record;legacy=record['legacy'];raw=legacy['parsed']['items']
    if single:raw[:]=raw[:1]
    if tax:raw[-1].update(name='外税',amount=81,major_category='食費',minor_category='食品')
    legacy['parsed']['total']=legacy['prefill']['amount']=sum(x['amount'] for x in raw)
    legacy.pop('candidate_digest');legacy['candidate_digest']=p.digest(legacy)
    record['items']=[dict(item_id=p.digest(['receipt-item-v1',legacy['identity']['receipt_unit_id'],legacy['candidate_digest'],n,x['name'],x['quantity'],x['amount']]),
        item_index=n,kind='tax' if tax and n==len(raw) else 'product',name=x['name'],amount=x['amount'],category='') for n,x in enumerate(raw,1)]
    detail=record['items'];record['adjustment_targets']={detail[-1]['item_id']:detail[0]['item_id']} if tax else {}
    record['review_evidence']['items']=[dict(item_id=x['item_id'],mapped=True,shape_agrees=True,category_agrees=False,category='',
        readings=[dict(name=x['name'],amount=x['amount'],category='')]*2) for x in detail]
    record.pop('digest');record['digest']=p.digest(record)
    current=items.fields(record);current.update(action='記帳する',structure_confirmation='確認済み')
    for x in current['items']:x['category']='食費｜食品' if x['item_index']==1 or x['kind']=='tax' else '日用品｜消耗品'
    view=items.card(record,categories=[('食費','食品'),('日用品','消耗品')]);by_id={x['item_id']:x for x in current['items']}
    rows=[[f,by_id[f[5:]]['name'] if f.startswith('item:') else label,
        by_id[f[5:]]['amount'] if f.startswith('item:') else current[f] if f in current else value] for f,label,value in view['rows']]
    snapshot=dict(identity=view['identity'],token=view['token'],rows=rows,items=current['items'],original_link='')
    binding=proof.binding(record,snapshot,RID)
    verified=proof.ConfirmedItems(RID,record['digest'],p.digest(snapshot),p.digest(current),p.digest(legacy['identity']),OWNER,
        NOW-100,p.digest([binding,OWNER,NOW-100]))
    rd.snapshot=snapshot;rd.journal['requests'][RID]=dict(binding=binding,proof=asdict(verified),snapshot=snapshot,
        plan=dict(input=current,validation=items.evaluate(record,current,[('食費','食品'),('日用品','消耗品')],confirmation=verified)),
        status='validated_not_written')

@pytest.mark.parametrize('single,tax,rows,total',[(True,False,1,300),(False,True,3,891)])
def test_single_item_and_printed_allocated_tax_preserved(single,tax,rows,total):
    rd=Readers();rd.queue=Queue();reseal_fixture(rd,single=single,tax=tax);u.execute(rd,rd.queue,RID,CONTEXT)
    args=(rd,Store(w.approval(u.prepare(rd,RID),RID,OWNER,CONTEXT,approval_reference='synthetic',approved_at=NOW-1,expires_at=NOW+600)),Writer(rd),Projection())
    result=apply(args);assert result['expense_rows']==rows and result['total']==total
    if tax:assert rd.tables['支出明細'][-1][3:7]==['外税',81,'食費','食品']
    assert apply(args,'replay')['accounting_rows_written']==0

@pytest.mark.parametrize('page,count,total',[(4,3,800),(10,5,1250)])
def test_existing_writer_mixed_categories_discount_exact_and_replay(page,count,total):
    args=setup(page);rd,store,writer,projection=args;old=deepcopy(rd.tables);queue=deepcopy(rd.queue.data)
    result=apply(args);before=deepcopy((rd.tables,store.value,store.version,writer.calls))
    replay=apply(args,'replay')
    assert before==(rd.tables,store.value,store.version,writer.calls)
    assert result['accounting_rows_written']==count+2 and replay['accounting_rows_written']==0 and projection.hidden
    assert [len(rows) for title,rows in writer.calls]==[1,count,1] and result['total']==total
    assert len({tuple(row[5:7]) for row in rd.tables['支出明細'][1:]})==2
    assert rd.tables['支出明細'][-1][5:7]==rd.tables['支出明細'][1][5:7]
    for title,rows in old.items():assert rd.tables[title][:len(rows)]==rows
    assert rd.queue.data==queue and store.value['state']=='complete' and store.value['generation']==2
    event=next(iter(next(iter(store.value['history'].values())).values()))
    assert event['page_number']==page and len(json.dumps(event).encode())<2048
    assert not {'items','snapshot','image','token','email','merchant'}&set(event)
    with pytest.raises(StateError):posting.validate_state(store.value) # No p14 approval reuse.

@pytest.mark.parametrize('key',['plan_digest','snapshot_digest','identity_digest','confirmation_digest',
    'before_ledger_digest','owner_actor_id','request_id','context_file_id','page_number','receipt_unit_id','budget'])
def test_resealed_different_scope_never_claims_or_writes(key):
    args=setup();grant=args[1].value['grant'];grant.pop('digest')
    if key=='page_number':grant[key]=10
    elif key=='budget':grant[key]['total']+=1
    elif key=='request_id':grant[key]='33333333-3333-4333-8333-333333333333'
    elif key=='context_file_id':grant[key]='synthetic-other-context'
    elif key=='receipt_unit_id':grant[key]='page-receipt-v1:'+'3'*64
    else:grant[key]='3'*64
    args[1].value['grant']=posting.seal(grant)
    with pytest.raises(StateError):apply(args)
    assert not args[1].writes and not args[2].calls

@pytest.mark.parametrize('change',['category','amount','item_id','structure','action','actor','segmentation','source','unit','page','review'])
def test_changed_input_identity_or_actor_rejected_before_claim(change):
    args=setup();rd=args[0]
    if change in {'category','amount','item_id'}:rd.snapshot['items'][0][change]='changed'
    elif change in {'structure','action'}:next(x for x in rd.snapshot['rows'] if x[0]==('structure_confirmation' if change=='structure' else change))[2]='未選択'
    elif change=='actor':rd.journal['requests'][RID]['proof']['actor_id']='0'*64
    elif change=='segmentation':rd.record['legacy']['hard_issues'].append('segmentation_unstable')
    else:rd.snapshot['identity'][{'source':'source_content_hash','unit':'receipt_unit_id','page':'page_number','review':'review_identity'}[change]]='changed'
    with pytest.raises((StateError,ValueError)):apply(args)
    assert not args[1].writes and not args[2].calls

@pytest.mark.parametrize('change',['missing','duplicate','wrong_request','scope','plan','not_complete'])
def test_hidden_plan_receipt_is_exact_and_readonly(change):
    args=setup();q=args[0].queue.data
    if change=='missing':q.pop()
    elif change=='duplicate':q.append(deepcopy(q[1]))
    elif change=='wrong_request':q[1][0]=ATTEMPT
    elif change=='not_complete':q[1][1]='accepted'
    else:
        column=2 if change=='scope' else 4;value=json.loads(q[1][column]);value['snapshot_digest']='0'*64;q[1][column]=json.dumps(value)
    before=deepcopy(q)
    with pytest.raises(StateError):apply(args)
    assert not args[1].writes and not args[2].calls and q==before

@pytest.mark.parametrize('stage',['source','journal','ledger'])
def test_read_failure_before_claim_no_write(stage):
    args=setup();rd=args[0]
    def fail(*a,**kw):raise TimeoutError('synthetic')
    if stage=='source':rd.read=fail
    elif stage=='journal':rd.private.read=fail
    else:rd.sheet_rows=fail
    with pytest.raises(TimeoutError):apply(args)
    assert not args[1].writes and not args[2].calls

def test_ledger_change_duplicate_and_identity_collision_hold_unused():
    for row in [['other','2026-09-28','shop',800],['R-'+setup()[0].snapshot['identity']['receipt_unit_id']],['old','2025-01-01','shop',17]]:
        args=setup();args[0].tables['レシート'].append(row)
        with pytest.raises(StateError):apply(args)
        assert args[1].value['state']=='unused' and not args[2].calls

@pytest.mark.parametrize('operation',['apply','replay'])
def test_unused_expired_approval_cannot_be_reused(operation):
    args=setup();grant=args[1].value['grant'];grant.pop('digest');grant.update(approved_at=NOW-1000,expires_at=NOW-1)
    args[1].value['grant']=posting.seal(grant)
    with pytest.raises(StateError):apply(args,operation)
    assert not args[1].writes and not args[2].calls

@pytest.mark.parametrize('failure',['before','partial','after'])
def test_timeout_readback_and_duplicate_worker_no_repeat_append(failure):
    args=setup();writer=args[2];original=writer.append_raw
    def fail(title,rows):
        if title==('取込データ' if failure=='after' else '支出明細'):writer.fail=failure
        original(title,rows)
    writer.append_raw=fail
    if failure=='after':assert apply(args)['exact_readback']
    else:
        with pytest.raises(StateError,match='reconciliation'):apply(args)
        assert args[1].value['state']=='unknown' and not args[3].hidden
    calls=deepcopy(writer.calls)
    if failure=='after':assert apply(args,'replay')['accounting_rows_written']==0
    else:
        with pytest.raises(StateError,match='reconciliation'):apply(args,'replay')
    assert writer.calls==calls

def test_claim_with_no_write_is_never_retried():
    args=setup();args[1].value.update(state='claimed',generation=1,claim={'attempt_id':ATTEMPT,'claimed_at':NOW})
    with pytest.raises(StateError,match='reconciliation'):apply(args)
    assert not args[2].calls and not args[3].hidden

def test_completion_save_failure_recovered_without_ledger_retry():
    args=setup();store=args[1];original=store.replace
    def fail(before,tag,after):
        if after['state']=='complete':raise TimeoutError('synthetic')
        original(before,tag,after)
    store.replace=fail
    with pytest.raises(TimeoutError):apply(args)
    assert store.value['state']=='claimed' and len(args[2].calls)==3
    store.replace=original
    assert apply(args,'replay')['accounting_rows_written']==0 and len(args[2].calls)==3

def test_ui_failure_after_complete_replay_only_hides():
    args=setup();projection=args[3];original=projection.hide
    projection.hide=lambda _:(_ for _ in ()).throw(TimeoutError('synthetic'))
    with pytest.raises(TimeoutError):apply(args)
    assert args[1].value['state']=='complete' and not projection.hidden
    before=deepcopy((args[1].value,args[1].version,args[2].calls))
    projection.hide=original
    assert apply(args,'replay')['accounting_rows_written']==0 and projection.hidden
    assert before==(args[1].value,args[1].version,args[2].calls)

def test_old_value_or_formula_mutation_during_write_never_terminal():
    args=setup();rd=args[0];rd.tables['レシート'].append(['old','2025-01-01','shop',100])
    args[1].value=w.approval(u.prepare(rd,RID),RID,OWNER,CONTEXT,approval_reference='synthetic',approved_at=NOW-1,expires_at=NOW+600)
    writer=args[2];original=writer.append_raw
    def mutate(title,rows):original(title,rows);rd.tables['レシート'][1][3]=101
    writer.append_raw=mutate
    with pytest.raises(StateError):apply(args)
    assert not args[3].hidden and args[1].value['state']=='unknown'

def test_scoped_existing_writer_cannot_update_delete_or_append_other_unit():
    args=setup();plan=w.fresh_plan(args[0],RID);db=posting.ScopedMaterializerDB(args[2],args[0],plan,args[1].value['grant'])
    for call in [lambda:db.append('レシート',[['other']]),lambda:db.update_row_raw('レシート',2,[]),lambda:db.clear('レシート!A2')]:
        with pytest.raises(StateError):call()
    assert not args[2].calls

def port_setup(mode='success'):
    args=setup();http=Http(args[1].value,mode)
    cfg={'candidate_file':'synthetic-candidate','journal_file':'synthetic-journal',
        'drive':{'folder':'synthetic-private-folder','authority_file':'synthetic-hga','inbox':'synthetic-inbox',
            'baseline_files':{'synthetic-baseline':'0'*64},'page':{'source':{'source_file_id':'synthetic-source'}},
            'owner_digest':p.digest('owner@synthetic.invalid')}}
    store=live.UnitPostingStore({'client_email':'sa@synthetic.invalid'},cfg,'synthetic-posting-file',CONTEXT,http=http)
    before,tag=store.read();after=deepcopy(before);after.update(state='claimed',generation=1,claim={'attempt_id':ATTEMPT,'claimed_at':NOW})
    return store,http,before,tag,after

@pytest.mark.parametrize('mode',['412','timeout_before','timeout_after','success'])
def test_conditional_store_no_unconditional_fallback_or_retry(mode):
    store,http,before,tag,after=port_setup(mode)
    if mode in {'412','timeout_before'}:
        with pytest.raises(StateError):store.replace(before,tag,after)
    else:store.replace(before,tag,after);assert store.read()[0]==after
    assert http.puts==1

def test_store_stale_etag_and_acl_rejected_before_put():
    store,http,before,tag,after=port_setup();http.meta['etag']='"changed"'
    with pytest.raises(StateError,match='HTTP_412'):store.replace(before,tag,after)
    assert http.puts==0
    http.meta['permissions'].append({'type':'anyone','role':'reader'})
    with pytest.raises(StateError):store.read()
    for fid in [CONTEXT,'synthetic-source','synthetic-inbox','synthetic-baseline']:
        with pytest.raises(StateError):live.UnitPostingStore(store.info,store.cfg,fid,CONTEXT,http=http)

def test_main_workflow_and_validated_boundary(monkeypatch):
    monkeypatch.setattr(live,'verify_execution_boundary',lambda *a:None)
    env=dict(GITHUB_EVENT_NAME='workflow_dispatch',KAKEIBO_LEGACY_DISABLED='true',
        GITHUB_WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/receipt-unit-posting-canary.yml@refs/heads/main',
        RECEIPT_UNIT_POST_CONFIRM='POST_CONFIRMED_RECEIPT_UNIT',RECEIPT_UNIT_POST_OPERATION='apply',SPREADSHEET_ID=p.SID,
        RECEIPT_UNIT_REQUEST_ID=RID,RECEIPT_UNIT_CONTEXT_ID=CONTEXT,RECEIPT_UNIT_POSTING_FILE='synthetic-posting-file')
    live.boundary(env,'head')
    for key,value in [('GITHUB_WORKFLOW_REF','other'),('RECEIPT_UNIT_POST_CONFIRM',''),('RECEIPT_UNIT_POST_OPERATION','plan_only'),
        ('RUNNER_DEBUG','1'),('RECEIPT_UNIT_POSTING_FILE',CONTEXT)]:
        with pytest.raises(StateError):live.boundary({**env,key:value},'head')
    text=(Path(__file__).parents[1]/'.github/workflows/receipt-unit-posting-canary.yml').read_text()
    assert 'kakeibo-production' in text and 'schedule:' not in text and 'inputs.approved_sha' not in text
    assert 'GEMINI' not in text and 'contents: read' in text and 'persist-credentials: false' in text
    assert set(__import__('re').findall(r'secrets\.([A-Z_]+)',text))=={'GOOGLE_SERVICE_ACCOUNT_JSON','SPREADSHEET_ID'}

def test_plan_only_never_creates_posting_permission_or_calls_real_writer(monkeypatch):
    from app import sheets
    monkeypatch.setattr(w,'approval',lambda *a,**k:pytest.fail('financial approval created'))
    monkeypatch.setattr(sheets.SheetsDB,'append_raw',lambda *a,**k:pytest.fail('real append'))
    monkeypatch.setattr(live.UnitPostingStore,'replace',lambda *a:pytest.fail('posting reached'))
    rd=Readers();rd.queue=Queue();assert u.execute(rd,rd.queue,RID,CONTEXT)['accounting_writes']==0

def test_resealed_audit_source_change_detected_on_replay():
    args=setup();apply(args)
    partition=next(iter(args[1].value['history'].values()));key=next(iter(partition));event=partition[key]
    event.pop('digest');event['source_content_hash']='0'*64;partition[key]=posting.seal(event)
    before=deepcopy(args[2].calls)
    with pytest.raises(StateError,match='history_binding'):apply(args,'replay')
    assert args[2].calls==before

def test_history_schema_rejects_raw_snapshot_or_response():
    args=setup();apply(args);event=next(iter(next(iter(args[1].value['history'].values())).values()))
    event.pop('digest');event['snapshot']={'synthetic':'no raw inputs in permanent history'}
    partition=next(iter(args[1].value['history'].values()));partition[event['event_id']]=posting.seal(event)
    with pytest.raises(StateError,match='history'):w.validate_state(args[1].value)
