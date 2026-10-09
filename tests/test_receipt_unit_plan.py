"""Independent Unit budgets, existing writer planning and p14 isolation."""
from copy import deepcopy
from dataclasses import asdict
import ast,json
from pathlib import Path
import pytest
from test_p14_plan_only import Readers as BaseReaders,Queue,RID,OWNER,CATS,fixture,env
from app.receipt_plan import unit_runner as u,runner as p,items,proof
from app.receipt_plan.identity import SourceRef,PageUnit,stable_page,digest
from app.receipt_plan.authority import binding_fields,review_identity
from app.receipt_plan import drive
from app.drive_run_state import StateError

CONTEXT='synthetic-private-unit-context'

def unit_fixture(page_number=4,index=1):
    old,_,_=fixture();source=SourceRef(source_file_id='synthetic-plan-source',source_content_hash='b'*64,page_count=14)
    page=PageUnit(source=source,page_number=page_number,stable_page_identity=stable_page(source,page_number),
        automatic_classification='sensitive_unknown',automatic_reason='privacy_unresolved',observation_complete=True,
        extraction_status='extracted',observation_render_hash='c'*64,review_identity='d'*64,authority_revision=2)
    page=page.model_copy(update={'review_identity':review_identity(page)})
    ident={**binding_fields(page),'processing_status':'observed','receipt_unit_id':'page-receipt-v1:'+digest([page_number,index]),
        'segmentation_digest':'f'*64,'receipt_index':index,'bbox':dict(left=0.,top=0.,right=1.,bottom=1.)}
    amounts=[300,510,-10] if page_number==4 else [120,250,500,410,-30]
    legacy=deepcopy(old['legacy']);legacy.pop('candidate_digest');legacy['identity']=ident
    legacy['parsed']['items']=legacy['parsed']['items'][:len(amounts)]
    for n,(row,amount) in enumerate(zip(legacy['parsed']['items'],amounts)):
        row.update(name='商品'+str(n+1) if amount>0 else '商品1値引き',amount=amount,
            major_category=CATS[0 if n==0 or amount<0 else 1][0],minor_category=CATS[0 if n==0 or amount<0 else 1][1])
    legacy['parsed']['total']=sum(amounts);legacy['prefill']['amount']=sum(amounts);legacy['candidate_digest']=digest(legacy)
    detail=[dict(item_id=digest(['receipt-item-v1',ident['receipt_unit_id'],legacy['candidate_digest'],n,row['name'],row['quantity'],row['amount']]),
        item_index=n,kind='discount' if row['amount']<0 else 'product',name=row['name'],amount=row['amount'],category='')
        for n,row in enumerate(legacy['parsed']['items'],1)]
    record=dict(schema=items.SCHEMA,legacy=legacy,items=detail,adjustment_targets={detail[-1]['item_id']:detail[0]['item_id']},
        legacy_category='',accounting_allowed=False,medical_handoff_allowed=False,archive_allowed=False)
    record['review_evidence']=dict(schema=1,items=[dict(item_id=x['item_id'],mapped=True,shape_agrees=True,category_agrees=False,
        category='',readings=[dict(name=x['name'],amount=x['amount'],category='')]*2) for x in detail],full_structure_confirmation_required=True)
    record['digest']=digest(record);current=items.fields(record);current.update(action='記帳する',structure_confirmation='確認済み')
    for n,row in enumerate(current['items']):row['category']='｜'.join(CATS[0 if n==0 or row['amount']<0 else 1])
    view=items.card(record,categories=CATS);by_id={i['item_id']:i for i in current['items']}
    rows=[[f,by_id[f[5:]]['name'] if f.startswith('item:') else label,
        by_id[f[5:]]['amount'] if f.startswith('item:') else current[f] if f in items.FIELDS or f in {'action','structure_confirmation'} else value]
        for f,label,value in view['rows']]
    snapshot=dict(identity=view['identity'],token=view['token'],rows=rows,items=current['items'],original_link='')
    bound=proof.binding(record,snapshot,RID);verified=proof.ConfirmedItems(RID,record['digest'],digest(snapshot),digest(current),digest(ident),OWNER,1791526595,digest([bound,OWNER,1791526595]))
    validation=items.evaluate(record,current,CATS,confirmation=verified)
    journal=dict(schema='receipt-item-confirmations-v1',scope='f'*64,requests={RID:dict(binding=bound,proof=asdict(verified),snapshot=snapshot,
        plan=dict(input=current,validation=validation),status='validated_not_written')},generation=1)
    return record,snapshot,journal

class Readers(BaseReaders):
    def __init__(self,page=4,index=1):
        super().__init__();self.record,self.snapshot,self.journal=unit_fixture(page,index)

@pytest.mark.parametrize('page,count,total',[(4,3,800),(10,5,1250)])
def test_dynamic_budget_plan_replay_and_no_live_writer(monkeypatch,page,count,total):
    from app import sheets,receipt_pipeline
    for name in ('append','append_raw','update_row_raw'):
        monkeypatch.setattr(sheets.SheetsDB,name,lambda *a,**k:pytest.fail('live writer'))
    monkeypatch.setattr(receipt_pipeline.ReceiptPipeline,'process_bytes',lambda *a,**k:pytest.fail('AI'))
    readers=Readers(page);q=Queue();before=deepcopy(readers.tables)
    first=u.execute(readers,q,RID,CONTEXT);second=u.execute(readers,q,RID,CONTEXT)
    assert first['planned_expense_rows']==count and first['total']==total and first['accounting_writes']==0
    assert first['queue_appends']==1 and second['queue_appends']==0 and q.writes==2
    assert readers.tables==before and {k:v for k,v in first.items() if k!='queue_appends'}=={k:v for k,v in second.items() if k!='queue_appends'}
    with pytest.raises(StateError):p.prepare(readers,RID) # Old p14 never accepts the new budgets.

def test_two_units_same_page_independent_hold():
    held=Readers(4,1);held.record['legacy']['hard_issues'].append('segmentation_unstable');q=Queue()
    with pytest.raises(StateError):u.execute(held,q,RID,CONTEXT)
    assert not q.writes
    good=Readers(4,2);result=u.execute(good,q,RID,CONTEXT)
    assert result['status']=='plan_only_complete' and good.snapshot['identity']['receipt_unit_id']!=held.snapshot['identity']['receipt_unit_id']

@pytest.mark.parametrize('field',['source_content_hash','page_number','stable_page_identity','review_identity','receipt_unit_id','authority_revision','item_ids'])
def test_stale_identity_before_queue(field):
    rd=Readers();rd.snapshot['identity'][field]='tampered';q=Queue()
    with pytest.raises((StateError,ValueError)):u.execute(rd,q,RID,CONTEXT)
    assert not q.writes

@pytest.mark.parametrize('change',['actor','proof','structure','category','amount','duplicate'])
def test_actor_structure_categories_and_duplicate_fail_closed(change):
    rd=Readers();q=Queue()
    if change=='actor':rd.journal['requests'][RID]['proof']['actor_id']='0'*64
    elif change=='proof':rd.journal['requests'][RID]['proof']['authority_digest']='0'*64
    elif change=='duplicate':rd.tables['レシート'].append(['other','2026-09-28','shop',800])
    elif change=='structure':next(x for x in rd.snapshot['rows'] if x[0]=='structure_confirmation')[2]='未選択'
    else:rd.snapshot['items'][0][change]='changed'
    with pytest.raises((StateError,ValueError)):u.execute(rd,q,RID,CONTEXT)
    assert not q.writes

def test_context_or_duplicate_queue_conflict_never_overwrites():
    rd=Readers();q=Queue();u.execute(rd,q,RID,CONTEXT);before=deepcopy(q.data);writes=q.writes
    with pytest.raises(StateError):u.execute(rd,q,RID,CONTEXT+'-other')
    assert q.data==before and q.writes==writes
    q.data.append(q.data[-1][:])
    with pytest.raises(StateError):u.execute(rd,q,RID,CONTEXT)
    assert q.writes==writes

def test_queue_scope_fixed_to_exact_unit_envelope():
    rd=Readers();plan=u.prepare(rd,RID);port=object.__new__(u.UnitQueuePort);port.expected=u.intake_payload(plan,RID,CONTEXT)
    port.validate_envelope(deepcopy(port.expected),RID)
    for key in port.expected:
        changed=deepcopy(port.expected);changed[key]='changed'
        with pytest.raises(StateError):port.validate_envelope(changed,RID)

def test_unknown_delivery_no_retry():
    rd=Readers();q=Queue();calls=[]
    def fail(*args):calls.append(args);raise StateError('delivery_unknown')
    q.replace=fail
    with pytest.raises(StateError):u.execute(rd,q,RID,CONTEXT)
    assert len(calls)==1

@pytest.mark.parametrize('stage',['source','authority','ledger'])
def test_read_failure_no_intake(stage):
    rd=Readers();q=Queue()
    def fail(*a,**k):raise TimeoutError('synthetic')
    if stage=='source':rd.read=fail
    elif stage=='authority':rd.private.read=fail
    else:rd.sheet_rows=fail
    with pytest.raises(TimeoutError):u.execute(rd,q,RID,CONTEXT)
    assert not q.writes

def test_manual_validated_main_boundary():
    e=env();e.update(RECEIPT_UNIT_MODE='plan_only',RECEIPT_UNIT_REQUEST_ID=RID,RECEIPT_UNIT_CONTEXT_ID=CONTEXT,
        GITHUB_WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/receipt-unit-plan-only.yml@refs/heads/main')
    u.boundary(e,'a'*40)
    for key,value in [('RECEIPT_UNIT_MODE','write'),('GITHUB_REF','feature'),('GITHUB_EVENT_NAME','schedule'),('RUNNER_DEBUG','1'),
        ('KAKEIBO_VALIDATED_MAIN_SHA','b'*40),('RECEIPT_UNIT_REQUEST_ID','bad'),('RECEIPT_UNIT_CONTEXT_ID','bad'),('SPREADSHEET_ID','other')]:
        changed={**e,key:value}
        with pytest.raises(StateError):u.boundary(changed,'a'*40)

def test_adapter_has_no_posting_or_source_mutation_capability():
    source=Path(u.__file__).read_text(encoding='utf-8');tree=ast.parse(source)
    assert all(not isinstance(node,ast.Attribute) or node.attr not in {'append_raw','process_bytes','delete','move','put'} for node in ast.walk(tree))
    assert 'posting' not in [node.module for node in ast.walk(tree) if isinstance(node,ast.ImportFrom)]
    workflow=(Path(__file__).parents[1]/'.github/workflows/receipt-unit-plan-only.yml').read_text(encoding='utf-8')
    assert 'schedule:' not in workflow and 'options: [plan_only]' in workflow and 'KAKEIBO_VALIDATED_MAIN_SHA' in workflow

def private_context(number):
    source=SourceRef(source_file_id=drive.SOURCE,source_content_hash=drive.HASH,page_count=14)
    page=PageUnit(source=source,page_number=number,stable_page_identity=stable_page(source,number),
        automatic_classification='sensitive_unknown',automatic_reason='privacy_unresolved',observation_complete=True,
        extraction_status='extracted',observation_render_hash='c'*64,review_identity='d'*64,authority_revision=2)
    page=page.model_copy(update={'review_identity':review_identity(page)})
    ids=['1ju2rEDWrlpALr-9d4JEN9yaPfTuKtFCq','1atHszVu7J-OXPbJkhCMhsvhiyz6QEdsR','1Gss6WvRvKbSWIKVSzrkApYkxVRFdQ6g_','1J6hjORzCDEauxg39o1fZbwRS41ROe3jE']
    cfg=dict(page=page.model_dump(),folder='synthetic-private-folder',inbox='synthetic-inbox-folder',
        authority_file='synthetic-hga-private',owner_digest=digest('owner@example.test'),baseline_files={i:'a'*64 for i in ids})
    cfg['binding']=digest([drive.SCHEMA,cfg['folder'],cfg['authority_file'],source.model_dump(),number,page.review_identity])
    review=dict(drive=cfg,candidate_file='synthetic-candidate-private',candidate_digest='b'*64,journal_file='synthetic-journal-private')
    review['scope']=digest(['receipt-item-confirmation-live-v1',cfg['binding'],review['candidate_file'],review['journal_file'],review['candidate_digest']])
    return dict(schema='receipt-item-runner-context-v1',config=review,owner_actor_id=OWNER)

class ContextHTTP:
    def __init__(self,value):
        from types import SimpleNamespace
        self.value=value;self.calls=0;self.meta=dict(id=CONTEXT,etag='"v1"',parents=[{'id':'synthetic-private-folder'}],
            labels={'trashed':False},mimeType='application/json',owners=[{'emailAddress':'owner@example.test'}],
            permissions=[dict(type='user',role='owner',emailAddress='owner@example.test'),dict(type='user',role='writer',emailAddress='sa@example.test')])
    def get(self,url,**kw):
        from types import SimpleNamespace
        assert url.endswith('/'+CONTEXT) and kw['allow_redirects'] is False
        self.calls+=1;value=self.value if kw['params'].get('alt')=='media' else deepcopy(self.meta)
        return SimpleNamespace(status_code=200,content=json.dumps(value).encode(),json=lambda:value)

@pytest.mark.parametrize('page',[4,10])
def test_private_context_exact_acl_and_p14_default_stays_fixed(page):
    value=private_context(page);http=ContextHTTP(value)
    assert u.context({'client_email':'sa@example.test'},CONTEXT,http=http)==value and http.calls==3
    with pytest.raises(StateError):drive.validate_config(value['config']['drive'])
    assert drive.validate_config(value['config']['drive'],allowed_pages=(4,10)).page_number==page

@pytest.mark.parametrize('change',['page14','medical','owner','public_acl','parent','weak_etag','candidate','journal','digest'])
def test_private_context_permission_identity_and_scope_reject(change):
    value=private_context(14 if change=='page14' else 4);http=ContextHTTP(value)
    if change=='medical':value['config']['drive']['page']['automatic_classification']='medical'
    elif change=='owner':value['owner_actor_id']='self-reported-email'
    elif change=='public_acl':http.meta['permissions'].append(dict(type='anyone',role='reader'))
    elif change=='parent':http.meta['parents']=[{'id':'shared-folder'}]
    elif change=='weak_etag':http.meta['etag']='W/"v1"'
    elif change in {'candidate','journal'}:value['config'][change+'_file']=value['config']['drive']['authority_file']
    elif change=='digest':value['config']['candidate_digest']='0'*64
    with pytest.raises(StateError):u.context({'client_email':'sa@example.test'},CONTEXT,http=http)
