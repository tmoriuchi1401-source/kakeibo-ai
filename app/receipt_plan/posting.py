"""One p14 posting grant. A completed plan receipt is never posting permission.

Durable claim precedes every ledger call. Claimed/unknown invocations may only
reconcile; they never fill missing rows. Only exact read-back permits terminal.
"""
from copy import deepcopy
from datetime import datetime,timezone
import json,re
from ..drive_run_state import StateError
from ..receipt_pipeline import ReceiptPipeline
from ..sheets import HEADERS
from . import runner as p
from .models import ReceiptResult
from .planning import PlanningDB
from .validated import validate_plan,accounting_plan
from .fields import original_uri

SCHEMA='p14-explicit-posting-v1'
METHOD='explicit_owner_instruction_plus_existing_oidc_snapshot_v1'
BUDGET={'receipts':1,'expenses':10,'imports':1,'total':3801,'discount':-13}
STATES={'unused','claimed','unknown','complete'}

def seal(value):
    value=deepcopy(value);value['digest']=p.digest(value);return value

def approval(plan,request_id,owner_actor_id,*,approval_reference,approved_at,expires_at):
    """Operator-only factory, never called by Sheet/plan-only/OIDC callback."""
    if not p.UUID.fullmatch(request_id) or not re.fullmatch('[a-f0-9]{64}',owner_actor_id):
        raise StateError('posting_approval_identity_required')
    if not approval_reference or not approved_at<expires_at<=approved_at+86400:
        raise StateError('posting_approval_expiry_required')
    grant=seal(dict(schema=SCHEMA,method=METHOD,request_id=request_id,
        owner_actor_id=owner_actor_id,approval_reference=approval_reference,
        approved_at=approved_at,expires_at=expires_at,budget=BUDGET,
        identity_digest=p.digest(plan['snapshot']['identity']),
        snapshot_digest=plan['snapshot_digest'],plan_digest=plan['plan_digest'],
        confirmation_digest=plan['authority_digest'],before_ledger_digest=plan['ledger_digest']))
    return dict(schema=SCHEMA,grant=grant,state='unused',generation=0,
        claim=None,result=None,current=None,history={})

def validate_state(value):
    if set(value)!={'schema','grant','state','generation','claim','result','current','history'} or value['schema']!=SCHEMA:
        raise StateError('posting_state_invalid')
    grant=value['grant'];unsigned={k:v for k,v in grant.items() if k!='digest'}
    keys={'schema','method','request_id','owner_actor_id','approval_reference','approved_at','expires_at','budget','identity_digest','snapshot_digest','plan_digest','confirmation_digest','before_ledger_digest'}
    if (set(unsigned)!=keys or grant['digest']!=p.digest(unsigned) or grant['schema']!=SCHEMA
        or grant['method']!=METHOD or grant['budget']!=BUDGET
        or not p.UUID.fullmatch(grant['request_id']) or not isinstance(grant['approval_reference'],str)
        or not 1<=len(grant['approval_reference'])<=250
        or type(grant['approved_at']) is not int or type(grant['expires_at']) is not int
        or not grant['approved_at']<grant['expires_at']<=grant['approved_at']+86400
        or any(not isinstance(grant[k],str) or not re.fullmatch('[a-f0-9]{64}',grant[k]) for k in
               ('owner_actor_id','identity_digest','snapshot_digest','plan_digest','confirmation_digest','before_ledger_digest'))):
        raise StateError('posting_grant_invalid')
    if value['state'] not in STATES or type(value['generation']) is not int or value['generation']<0:
        raise StateError('posting_state_invalid')
    if value['state']=='unused':
        if value['generation']!=0 or any(value[k] is not None for k in ('claim','result','current')) or value['history']:
            raise StateError('posting_unused_state_invalid')
    else:
        if not isinstance(value['claim'],dict) or set(value['claim'])!={'attempt_id','claimed_at'} or not p.UUID.fullmatch(value['claim']['attempt_id']):
            raise StateError('posting_claim_invalid')
        if type(value['claim']['claimed_at']) is not int or not grant['approved_at']<=value['claim']['claimed_at']<grant['expires_at']:
            raise StateError('posting_claim_invalid')
        if value['state']=='complete':
            if value['generation']<2 or not isinstance(value['result'],dict) or not isinstance(value['current'],dict) or len(value['history'])!=1:
                raise StateError('posting_complete_state_invalid')
            if (value['current'].get('status')!='imported' or value['result'].get('plan_digest')!=grant['plan_digest']
                or value['result'].get('snapshot_digest')!=grant['snapshot_digest']):raise StateError('posting_complete_state_invalid')
            events=[e for partition in value['history'].values() for e in partition.values()]
            if len(events)!=1:raise StateError('posting_history_invalid')
            event=events[0];unsealed={k:v for k,v in event.items() if k!='digest'}
            if (event.get('digest')!=p.digest(unsealed) or event.get('event_type')!='imported'
                or event.get('authority_digest')!=grant['digest'] or event.get('actor_id')!=grant['owner_actor_id']
                or event.get('retention_class')!='permanent' or event.get('schema_version')!=1):
                raise StateError('posting_history_invalid')
        elif value['result'] is not None or value['current'] is not None or value['history']:
            raise StateError('posting_nonterminal_state_invalid')
    return value

def bound(grant,plan,rid,owner):
    expected={'request_id':rid,'owner_actor_id':owner,'identity_digest':p.digest(plan['snapshot']['identity']),
        'snapshot_digest':plan['snapshot_digest'],'plan_digest':plan['plan_digest'],
        'confirmation_digest':plan['authority_digest']}
    if any(grant.get(k)!=v for k,v in expected.items()):raise StateError('posting_grant_binding_mismatch')

def read_ledger(readers):
    data={};formulas={}
    for title,width in [('レシート','I'),('支出明細','M'),('取込データ','L')]:
        region="'"+title+"'!A1:"+width+'10000'
        data[title]=readers.sheet_rows(region);formulas[title]=readers.sheet_rows(region,render='FORMULA')
        if not data[title] or data[title][0]!=HEADERS[title] or len(data[title])>=10000 or len(formulas[title])>=10000:
            raise StateError('posting_ledger_schema_or_extent_changed')
    return data,formulas

def fresh_plan(readers,rid):
    """The same input/HGA checks even after ledger identities become present."""
    readers.drive.begin_request()
    try:
        record,snapshot,cats=readers.read()
        record,validation=validate_plan(readers,p.Journal(readers),rid,snapshot,readers.owner_actor_id)
        if validation['status']!='ready_to_confirm' or validation['posting_authority'] is not False:
            raise StateError('posting_input_validation_held')
        raw,tag=p.Journal(readers).read_versioned();request=json.loads(raw)['requests'][rid]
        stamp=datetime.fromtimestamp(request['proof']['verified_at'],timezone.utc).astimezone(
            p.timezone(p.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
        rows=accounting_plan(record,validation,cats,timestamp=stamp)
        expenses=[r for t,r in rows if t=='支出明細']
        if (len(rows)!=12 or len(expenses)!=10 or sum(r[4] for r in expenses)!=3801
            or [r[4] for r in expenses if r[4]<0]!=[-13] or expenses[-1][5:7]!=expenses[2][5:7]):
            raise StateError('posting_budget_mismatch')
        return dict(rows=rows,snapshot=snapshot,snapshot_digest=p.digest(snapshot),plan_digest=p.digest(rows),
                    authority_digest=request['proof']['authority_digest'],stamp=stamp,
                    record=record,validation=validation,categories=cats)
    finally:readers.drive.end_request()

def outcome(readers,plan,grant):
    """Remove only exact expected new IDs and prove every old value/formula."""
    data,formulas=read_ledger(readers);clean=deepcopy(data);clean_f=deepcopy(formulas);present=0
    for title,expected in plan['rows']:
        matches=[r for r in data[title][1:] if r and r[0]==expected[0]]
        fmatches=[r for r in formulas[title][1:] if r and r[0]==expected[0]]
        if not matches and not fmatches:continue
        width=len(HEADERS[title])
        padded=lambda row:list(row)+['']*max(0,width-len(row))
        if len(matches)!=1 or len(fmatches)!=1 or padded(matches[0])!=padded(expected) or padded(fmatches[0])!=padded(expected):
            raise StateError('posting_readback_conflict')
        clean[title]=[r for r in clean[title] if not r or r[0]!=expected[0]]
        clean_f[title]=[r for r in clean_f[title] if not r or r[0]!=expected[0]]
        present+=1
    if p.digest([clean,clean_f])!=grant['before_ledger_digest']:
        raise StateError('posting_existing_ledger_changed')
    return ('written' if plan['rows'] and present==len(plan['rows']) else 'not_written' if present==0 else 'partial'),p.digest([data,formulas])

class ScopedMaterializerDB(PlanningDB):
    """Adapter around existing SheetsDB.append_raw; no update/delete interface."""
    def __init__(self,real,readers,plan,grant):
        super().__init__(plan['categories']);self.real=real;self.readers=readers;self.expected=plan['rows'];self.grant=grant;self.calls=0
    def append(self,title,rows):
        proposed=[(title,r) for r in rows]
        offset=len(self.plan)
        if p.digest(proposed)!=p.digest(self.expected[offset:offset+len(rows)]) or not rows:
            raise StateError('posting_writer_scope_violation')
        state,_=outcome(self.readers,{'rows':self.plan},self.grant)
        if state!=('written' if self.plan else 'not_written'):
            raise StateError('posting_prefix_readback_missing')
        self.real.append_raw(title,rows)
        self.calls+=1;self.plan.extend(deepcopy(proposed))
    def update_row_raw(self,*args):raise StateError('posting_update_forbidden')
    def clear(self,*args):raise StateError('posting_delete_forbidden')

def finish(store,before,tag,plan,ledger_digest,now):
    grant=before['grant'];ident=plan['snapshot']['identity'];year=str(datetime.fromtimestamp(now,timezone.utc).year)
    event=seal(dict(schema_version=1,retention_class='permanent',event_id=p.digest(['p14-posting',grant['request_id']]),
        event_type='imported',source_file_id=ident['source_file_id'],source_content_hash=ident['source_content_hash'],
        page_number=14,page_identity=ident['stable_page_identity'],receipt_unit_id=ident['receipt_unit_id'],
        ledger_id=next(row[0] for title,row in plan['rows'] if title=='レシート'),
        actor_id=grant['owner_actor_id'],confirmed_at=now,authority_digest=grant['digest'],
        input_snapshot_digest=grant['snapshot_digest'],plan_digest=grant['plan_digest'],reason_code='writer_exact_readback'))
    value=deepcopy(before);value.update(state='complete',generation=before['generation']+1,
        result={'plan_digest':grant['plan_digest'],'snapshot_digest':grant['snapshot_digest'],'ledger_digest':ledger_digest},
        current={'schema_version':1,'retention_class':'permanent','status':'imported','terminal':True,
                 'receipt_unit_id':ident['receipt_unit_id'],'ledger_id':event['ledger_id'],'last_event':event['event_id']},
        history={year:{event['event_id']:event}})
    store.replace(before,tag,value)
    return value

def execute(readers,store,writer,projection,rid,*,attempt_id,now,operation):
    if operation not in {'apply','replay'}:raise StateError('posting_operation_invalid')
    value,tag=store.read();validate_state(value);grant=value['grant']
    if value['state']=='unused':
        if operation!='apply' or not grant['approved_at']<=now<grant['expires_at']:
            raise StateError('posting_unused_or_expired_grant')
        first=p.prepare(readers,rid);second=p.prepare(readers,rid)
        if first!=second:raise StateError('posting_fresh_plan_changed')
        bound(grant,first,rid,readers.owner_actor_id)
        if first['ledger_digest']!=grant['before_ledger_digest']:raise StateError('posting_ledger_precondition_changed')
        projection.preflight(first['snapshot'])
        claimed=deepcopy(value);claimed.update(state='claimed',generation=1,claim={'attempt_id':attempt_id,'claimed_at':now})
        store.replace(value,tag,claimed)
        value,tag=store.read()
        if value!=claimed:raise StateError('posting_claim_readback_mismatch')
        third=p.prepare(readers,rid)
        if third!=first:raise StateError('posting_changed_after_claim')
        plan=fresh_plan(readers,rid);bound(grant,plan,rid,readers.owner_actor_id)
        scoped=ScopedMaterializerDB(writer,readers,plan,grant)
        try:
            ident=plan['record']['legacy']['identity']
            pipeline=ReceiptPipeline(scoped,None,clock=lambda:plan['stamp'])
            pipeline._materialize_result(ReceiptResult.model_validate(plan['validation']['parsed']),
                ident['receipt_unit_id'],original_uri(ident['source_file_id'],14),[])
            if p.digest(scoped.plan)!=grant['plan_digest']:raise StateError('posting_writer_plan_changed')
        except Exception:
            # No resend, no filling missing rows. A complete read-back may close
            # an unknown last response; partial/not-written remains held.
            try:state,ledger_digest=outcome(readers,plan,grant)
            except Exception:state='unknown'
            if state!='written':
                held=deepcopy(value);held.update(state='unknown',generation=value['generation']+1)
                store.replace(value,tag,held)
                raise StateError('posting_write_reconciliation_required') from None
        state,ledger_digest=outcome(readers,plan,grant)
        if state!='written':raise StateError('posting_exact_readback_required')
        final=fresh_plan(readers,rid);bound(grant,final,rid,readers.owner_actor_id)
        value=finish(store,value,tag,final,ledger_digest,now)
        writes=12
    else:
        plan=fresh_plan(readers,rid);bound(grant,plan,rid,readers.owner_actor_id)
        state,ledger_digest=outcome(readers,plan,grant)
        if state!='written':raise StateError('posting_claimed_reconciliation_required')
        if value['state']!='complete':value=finish(store,value,tag,plan,ledger_digest,now)
        elif value['result']['ledger_digest']!=ledger_digest:raise StateError('posting_replay_ledger_changed')
        writes=0
    projection.hide(plan['snapshot'])
    checked,_=store.read()
    if checked!=value:raise StateError('posting_completion_readback_mismatch')
    return {'status':'imported','receipts':1,'expense_rows':10,'imports':1,'total':3801,
        'accounting_rows_written':writes,'replay':writes==0,'exact_readback':True,'history_events':1,
        'terminal':True,'card_hidden':True,'medical':0,'gemini':0,'source_moves':0,
        'plan_digest':grant['plan_digest'],'snapshot_digest':grant['snapshot_digest']}
