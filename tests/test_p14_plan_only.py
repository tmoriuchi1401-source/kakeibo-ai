from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import ast,json
from pathlib import Path
import pytest
from app.receipt_plan import runner as r,items,proof
from app.receipt_plan.identity import SourceRef,PageUnit,stable_page,digest
from app.receipt_plan.authority import binding_fields,review_identity
from app.receipt_plan.models import ReceiptResult
from app.sheets import HEADERS
from app.drive_run_state import StateError

RID='11111111-1111-4111-8111-111111111111'
OWNER='a'*64
CATS=[('食費','食品'),('日用品','消耗品')]


def fixture():
    source=SourceRef(source_file_id='synthetic-plan-source',source_content_hash='b'*64,page_count=14)
    page=PageUnit(source=source,page_number=14,stable_page_identity=stable_page(source,14),automatic_classification='sensitive_unknown',automatic_reason='privacy_unresolved',observation_complete=True,extraction_status='extracted',observation_render_hash='c'*64,review_identity='d'*64,authority_revision=2)
    page=page.model_copy(update={'review_identity':review_identity(page)})
    ident={**binding_fields(page),'processing_status':'observed','receipt_unit_id':'page-receipt-v1:'+'e'*64,'segmentation_digest':'f'*64,'receipt_index':1,'bbox':dict(left=0.,top=0.,right=1.,bottom=1.)}
    amounts=[246,289,162,981,119,278,782,511,446,-13]
    raw=ReceiptResult(merchant='synthetic shop',date='2026-09-28',total=3801,payment_method='',items=[dict(name='商品'+str(n),amount=a,quantity=None,major_category=CATS[0 if n<3 else 1][0],minor_category=CATS[0 if n<3 else 1][1]) for n,a in enumerate(amounts,1)]).model_dump()
    prefill=dict(date='2026-09-28',amount=3801,category='',merchant='synthetic shop',payment='',memo='')
    legacy=dict(schema='general-receipt-completion-v1',identity=ident,prefill=prefill,blank_reasons={k:[] for k in prefill},parsed=raw,hard_issues=['reread_item_structure_changed'],provenance={k:'gemini' for k in prefill},mode='itemized',accounting_allowed=False,medical_handoff_allowed=False,archive_allowed=False)
    legacy['candidate_digest']=digest(legacy)
    detail=[]
    for n,x in enumerate(raw['items'],1):
        detail.append(dict(item_id=digest(['receipt-item-v1',ident['receipt_unit_id'],legacy['candidate_digest'],n,x['name'],x['quantity'],x['amount']]),item_index=n,kind='product' if n<10 else 'discount',name=x['name'],amount=x['amount'],category=''))
    record=dict(schema=items.SCHEMA,legacy=legacy,items=detail,adjustment_targets={detail[-1]['item_id']:detail[2]['item_id']},legacy_category='',accounting_allowed=False,medical_handoff_allowed=False,archive_allowed=False)
    record['review_evidence']=dict(schema=1,items=[dict(item_id=x['item_id'],mapped=True,shape_agrees=True,category_agrees=False,category='',readings=[dict(name=x['name'],amount=x['amount'],category=''),dict(name=x['name'],amount=x['amount'],category='')]) for x in detail],full_structure_confirmation_required=True)
    record['digest']=digest(record)
    current=items.fields(record);current.update(action='記帳する',structure_confirmation='確認済み')
    for n,x in enumerate(current['items'],1):x['category']='｜'.join(CATS[0 if n<3 else 1])
    view=items.card(record,categories=CATS)
    by_id={i['item_id']:i for i in current['items']}
    rows=[[f,by_id[f[5:]]['name'] if f.startswith('item:') else l,by_id[f[5:]]['amount'] if f.startswith('item:') else current[f] if f in items.FIELDS or f in {'action','structure_confirmation'} else v] for f,l,v in view['rows']]
    snapshot=dict(identity=view['identity'],token=view['token'],rows=rows,items=current['items'],original_link='')
    bound=proof.binding(record,snapshot,RID)
    verified=proof.ConfirmedItems(RID,record['digest'],digest(snapshot),digest(current),digest(ident),OWNER,1791526595,digest([bound,OWNER,1791526595]))
    validation=items.evaluate(record,current,CATS,confirmation=verified)
    journal=dict(schema='receipt-item-confirmations-v1',scope='f'*64,requests={RID:dict(binding=bound,proof=asdict(verified),snapshot=snapshot,plan=dict(input=current,validation=validation),status='validated_not_written')},generation=1)
    return record,snapshot,journal


class Readers:
    def __init__(self):
        self.record,self.snapshot,self.journal=fixture();self.owner_actor_id=OWNER;self.config={'scope':'f'*64,'journal_file':'journal','drive':{'authority_file':'hga'}}
        self.private=SimpleNamespace(read=lambda _: (json.dumps(self.journal).encode(),'"journal1"'))
        self.drive=SimpleNamespace(begin_request=lambda:None,end_request=lambda:None,read=lambda _:(b'{"authority":1}','"hga1"'))
        self.tables={t:[HEADERS[t]] for t in r.TABLES}
    def read(self):return deepcopy((self.record,self.snapshot,CATS))
    def sheet_rows(self,region,*,render='UNFORMATTED_VALUE'):
        for title in self.tables:
            if region.startswith("'"+title+"'!"):return deepcopy(self.tables[title])
        pytest.fail('unexpected range')


class Queue:
    def __init__(self):self.data=[r.HEADER];self.writes=0
    def rows(self):return deepcopy(self.data)
    def replace(self,before,n,row,rid):
        assert before==self.data and row[0]==rid
        assert json.loads(row[2])['mode']=='plan_only'
        self.writes+=1
        if n==len(self.data)+1:self.data.append(deepcopy(row))
        else:self.data[n-1]=deepcopy(row)


def test_success_replay_same_writer_no_external_writer(monkeypatch):
    from app import sheets,receipt_pipeline
    monkeypatch.setattr(sheets.SheetsDB,'append',lambda *a,**k:pytest.fail('live append'))
    monkeypatch.setattr(receipt_pipeline.ReceiptPipeline,'process_bytes',lambda *a,**k:pytest.fail('AI/intake'))
    readers=Readers();queue=Queue();before=deepcopy(readers.tables)
    first=r.execute(readers,queue,RID);second=r.execute(readers,queue,RID)
    assert first['queue_appends']==1 and second['queue_appends']==0 and queue.writes==2
    assert {k:v for k,v in first.items() if k!='queue_appends'}=={k:v for k,v in second.items() if k!='queue_appends'}
    assert first['accounting_writes']==0 and readers.tables==before and len(queue.data)==2
    assert len(r.prepare(readers,RID)['rows'])==12


@pytest.mark.parametrize('field',['source_content_hash','stable_page_identity','review_identity','receipt_unit_id','authority_revision'])
def test_identity_tamper_fails_before_queue(field):
    readers=Readers();readers.snapshot['identity'][field]='changed';q=Queue()
    with pytest.raises(StateError):r.execute(readers,q,RID)
    assert q.writes==0


@pytest.mark.parametrize('change',['category','amount','item','actor','uuid','structure','action','proof','segmentation'])
def test_snapshot_actor_and_gate_fail_closed(change):
    rd=Readers();q=Queue()
    if change=='actor':rd.journal['requests'][RID]['proof']['actor_id']='0'*64
    elif change=='uuid':rd.journal['requests']['22222222-2222-4222-8222-222222222222']=rd.journal['requests'].pop(RID)
    elif change=='proof':rd.journal['requests'][RID]['proof']['authority_digest']='0'*64
    elif change=='segmentation':rd.record['legacy']['hard_issues'].append('segmentation_unstable')
    elif change in {'category','amount','item'}:rd.snapshot['items'][0]['item_id' if change=='item' else change]='changed'
    else:next(x for x in rd.snapshot['rows'] if x[0]==('action' if change=='action' else 'structure_confirmation'))[2]='未選択'
    with pytest.raises((StateError,ValueError)):r.execute(rd,q,RID)
    assert q.writes==0


def test_duplicate_and_collision_never_capture():
    for row in [['other','2026-09-28','synthetic',3801],['R-page-receipt-v1:'+'e'*64]]:
        rd=Readers();rd.tables['レシート'].append(row);q=Queue()
        with pytest.raises(StateError):r.execute(rd,q,RID)
        assert not q.writes


@pytest.mark.parametrize('stage',['source','authority','ledger'])
def test_read_timeout_cannot_post_or_capture(stage):
    rd=Readers();q=Queue()
    def fail(*a,**k):raise TimeoutError('synthetic')
    if stage=='source':rd.read=fail
    elif stage=='authority':rd.private.read=fail
    else:rd.sheet_rows=fail
    with pytest.raises(TimeoutError):r.execute(rd,q,RID)
    assert not q.writes


def test_unknown_queue_delivery_no_automatic_retry():
    rd=Readers();q=Queue();calls=[]
    def fail(*args):calls.append(args);raise StateError('p14_plan_queue_delivery_unknown')
    q.replace=fail
    with pytest.raises(StateError):r.execute(rd,q,RID)
    assert len(calls)==1


def test_duplicate_queue_uuid_rejected():
    rd=Readers();q=Queue();r.execute(rd,q,RID);q.data.append(q.data[-1][:]);before=q.writes
    with pytest.raises(StateError):r.execute(rd,q,RID)
    assert q.writes==before


def env():
    return dict(GITHUB_ACTIONS='true',GITHUB_REF='refs/heads/main',GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',KAKEIBO_PRODUCTION_ENABLED='true',KAKEIBO_LEGACY_DISABLED='true',KAKEIBO_VALIDATED_MAIN_SHA='a'*40,GITHUB_SHA='a'*40,GITHUB_EVENT_NAME='workflow_dispatch',P14_PLAN_MODE='plan_only',P14_PLAN_REQUEST_ID=RID,SPREADSHEET_ID=r.SID,GITHUB_WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/p14-plan-only.yml@refs/heads/main')


@pytest.mark.parametrize('key,value',[('P14_PLAN_MODE','write'),('GITHUB_REF','refs/heads/feature'),('GITHUB_EVENT_NAME','schedule'),('KAKEIBO_VALIDATED_MAIN_SHA','b'*40),('RUNNER_DEBUG','1'),('P14_PLAN_REQUEST_ID','bad'),('GITHUB_WORKFLOW_REF','PR91'),('SPREADSHEET_ID','other')])
def test_execution_boundary(key,value):
    e=env();r.boundary(e,'a'*40);e[key]=value
    with pytest.raises(StateError):r.boundary(e,'a'*40)


def test_read_only_dependency_api_has_no_authority_write():
    from app.receipt_plan.drive import RealPageDrive
    from app.receipt_plan.readers import Journal
    assert not hasattr(Journal,'replace') and not hasattr(RealPageDrive,'refresh_before_write')
    assert 'put' not in __import__('inspect').signature(RealPageDrive.request).parameters
    source=Path(r.__file__).read_text(encoding='utf8')
    assert 'process_bytes(' not in source and 'append_raw(' not in source and 'move(' not in source
    workflow=Path('.github/workflows/p14-plan-only.yml').read_text(encoding='utf8')
    assert 'schedule:' not in workflow and 'GEMINI_API_KEY' not in workflow and 'approved_sha' not in workflow


@pytest.mark.parametrize('outcome',['written_then_timeout','not_written_timeout','HTTP_412','readback_changed'])
def test_queue_transport_ambiguous_delivery_and_conditional_failure(outcome):
    port=object.__new__(r.QueuePort);before=[r.HEADER];rd=Readers();plan=r.prepare(rd,RID)
    envelope=r.intake_payload(plan,RID)
    row=[RID,'accepted',json.dumps(envelope),plan['stamp'],'',''];state=deepcopy(before);calls=[]
    class Http:
        def put(self,url,**kw):
            calls.append((url,kw))
            assert 'values/' in url  # URL-encoded exact queue only.
            assert kw['params']=={'valueInputOption':'RAW'} and kw['json']['values']==[row]
            if outcome in {'written_then_timeout','readback_changed'}:state.append(row[:])
            if outcome=='readback_changed':state[-1][2]='changed'
            if outcome=='HTTP_412':return SimpleNamespace(status_code=412)
            raise TimeoutError('synthetic')
    port.http=Http();port.rows=lambda:deepcopy(state)
    if outcome=='written_then_timeout':port.replace(before,2,row,RID)
    else:
        with pytest.raises(StateError):port.replace(before,2,row,RID)
    assert len(calls)==1


def test_queue_cannot_overwrite_another_uuid_or_intake_mode():
    port=object.__new__(r.QueuePort);port.rows=lambda:[r.HEADER]
    port.http=SimpleNamespace(put=lambda *a,**kw:pytest.fail('unauthorized write'))
    for row in [[RID,'complete','{}','','',''],[RID,'accepted',json.dumps(dict(schema=r.SCHEMA,mode='write',request_id=RID,snapshot={},snapshot_digest=digest({}))),'','','']]:
        with pytest.raises(StateError):port.replace([r.HEADER],2,row,RID)


def test_evidence_etag_change_fail_closed():
    rd=Readers();q=Queue();calls=[]
    def changing(_):
        calls.append(1)
        return json.dumps(rd.journal).encode(),'"v'+str(len(calls))+'"'
    rd.private.read=changing
    with pytest.raises(StateError):r.execute(rd,q,RID)
    assert q.writes==0


def test_cached_plan_not_reused_after_source_change(monkeypatch):
    rd=Readers();q=Queue();original=r.prepare;calls=[]
    def prepare(*a):
        result=original(*a);calls.append(1)
        if len(calls)==2:result['hga_etag']='"changed"'
        return result
    monkeypatch.setattr(r,'prepare',prepare)
    with pytest.raises(StateError):r.execute(rd,q,RID)
    assert q.writes==0



def test_intake_is_compact_metadata_no_input_clone():
    plan=r.prepare(Readers(),RID);value=r.intake_payload(plan,RID)
    assert set(value)=={'schema','mode','request_id','context_file_id','receipt_unit_id','item_ids','identity_digest','snapshot_digest','authority_digest'}
    assert 'snapshot' not in value and 'rows' not in value and len(json.dumps(value).encode())<2000
