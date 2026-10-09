"""One explicitly approved p4/p10 Unit; plan-only never grants posting.

The existing writer and exact ledger/formula reconciliation are reused. An
unknown/partial claim can only be read back, never retried or filled in.
"""
from copy import deepcopy
from datetime import datetime,timezone
import json,re
from ..drive_run_state import StateError
from ..receipt_pipeline import ReceiptPipeline
from . import runner as p,unit_runner as u,posting as w
from .readers import Journal
from .validated import validate_plan,accounting_plan
from .models import ReceiptResult
from .fields import original_uri

SCHEMA='receipt-unit-explicit-posting-v1'
METHOD=w.METHOD

def valid_budget(b):
    return (isinstance(b,dict) and set(b)=={'receipts','expenses','imports','total','discounts'}
        and all(type(b[k]) is int for k in ('receipts','expenses','imports','total'))
        and b['receipts']==b['imports']==1 and 1<=b['expenses']<=100 and b['total']>0
        and isinstance(b['discounts'],list) and len(b['discounts'])<=b['expenses']
        and all(type(x) is int and x<0 for x in b['discounts']))

def approval(plan,rid,owner,context_id,*,approval_reference,approved_at,expires_at):
    """Operator-only, after explicit financial approval of this exact plan.

    Never called from a Sheet, OAuth callback or plan-only workflow. Each grant
    is prepared immediately before its own post, including the current ledger.
    """
    ident=plan['snapshot']['identity']
    grant=w.seal(dict(schema=SCHEMA,method=METHOD,request_id=rid,owner_actor_id=owner,
        context_file_id=context_id,page_number=ident['page_number'],receipt_unit_id=ident['receipt_unit_id'],
        approval_reference=approval_reference,approved_at=approved_at,expires_at=expires_at,
        budget=deepcopy(plan['budget']),identity_digest=p.digest(ident),snapshot_digest=plan['snapshot_digest'],
        plan_digest=plan['plan_digest'],confirmation_digest=plan['authority_digest'],before_ledger_digest=plan['ledger_digest']))
    return validate_state(dict(schema=SCHEMA,grant=grant,state='unused',generation=0,
        claim=None,result=None,current=None,history={}))

def validate_state(value):
    if (not isinstance(value,dict) or set(value)!={'schema','grant','state','generation','claim','result','current','history'}
        or value['schema']!=SCHEMA):raise StateError('unit_posting_state_invalid')
    grant=value['grant']
    keys={'schema','method','request_id','owner_actor_id','context_file_id','page_number','receipt_unit_id',
        'approval_reference','approved_at','expires_at','budget','identity_digest','snapshot_digest',
        'plan_digest','confirmation_digest','before_ledger_digest'}
    if not isinstance(grant,dict) or set(grant)!=keys|{'digest'}:raise StateError('unit_posting_grant_invalid')
    unsigned={k:v for k,v in grant.items() if k!='digest'}
    if (grant['digest']!=p.digest(unsigned) or grant['schema']!=SCHEMA or grant['method']!=METHOD
        or not valid_budget(grant['budget']) or not isinstance(grant['request_id'],str) or not p.UUID.fullmatch(grant['request_id'])
        or not isinstance(grant['context_file_id'],str) or not u.FILE_ID.fullmatch(grant['context_file_id'])
        or grant['context_file_id']==p.CONTEXT_ID or type(grant['page_number']) is not int or grant['page_number'] not in u.PAGES
        or not isinstance(grant['receipt_unit_id'],str) or not re.fullmatch('page-receipt-v1:[a-f0-9]{64}',grant['receipt_unit_id'])
        or not isinstance(grant['approval_reference'],str) or not 1<=len(grant['approval_reference'])<=250
        or type(grant['approved_at']) is not int or type(grant['expires_at']) is not int
        or not grant['approved_at']<grant['expires_at']<=grant['approved_at']+86400
        or any(not isinstance(grant[k],str) or not re.fullmatch('[a-f0-9]{64}',grant[k]) for k in
            ('owner_actor_id','identity_digest','snapshot_digest','plan_digest','confirmation_digest','before_ledger_digest'))):
        raise StateError('unit_posting_grant_invalid')
    if (value['state'] not in w.STATES or type(value['generation']) is not int or value['generation']<0
        or not isinstance(value['history'],dict)):raise StateError('unit_posting_state_invalid')
    if value['state']=='unused':
        if value['generation']!=0 or any(value[k] is not None for k in ('claim','result','current')) or value['history']:
            raise StateError('unit_posting_unused_state_invalid')
    else:
        claim=value['claim']
        if (not isinstance(claim,dict) or set(claim)!={'attempt_id','claimed_at'}
            or not isinstance(claim['attempt_id'],str) or not p.UUID.fullmatch(claim['attempt_id'])
            or type(claim['claimed_at']) is not int or not grant['approved_at']<=claim['claimed_at']<grant['expires_at']
            or value['generation']<1):raise StateError('unit_posting_claim_invalid')
        if value['state']=='complete':
            current=value['current'];result=value['result']
            if (value['generation']<2 or not isinstance(result,dict) or set(result)!={'plan_digest','snapshot_digest','ledger_digest'}
                or result['plan_digest']!=grant['plan_digest'] or result['snapshot_digest']!=grant['snapshot_digest']
                or not isinstance(result['ledger_digest'],str) or not re.fullmatch('[a-f0-9]{64}',result['ledger_digest'])
                or not isinstance(current,dict) or set(current)!={'schema_version','retention_class','status','terminal','receipt_unit_id','ledger_id','last_event'}
                or current.get('schema_version')!=1 or current.get('retention_class')!='permanent'
                or current.get('status')!='imported' or current.get('terminal') is not True
                or current.get('receipt_unit_id')!=grant['receipt_unit_id'] or len(value['history'])!=1):
                raise StateError('unit_posting_complete_invalid')
            if any(not re.fullmatch('[0-9]{4}',year) or not isinstance(partition,dict) for year,partition in value['history'].items()):
                raise StateError('unit_posting_history_invalid')
            events=[(year,key,event) for year,partition in value['history'].items() for key,event in partition.items()]
            if len(events)!=1:raise StateError('unit_posting_history_invalid')
            year,key,event=events[0]
            if (not isinstance(event,dict) or set(event)!={'schema_version','retention_class','event_id','event_type',
                    'source_file_id','source_content_hash','page_number','page_identity','receipt_unit_id','ledger_id',
                    'actor_id','confirmed_at','authority_digest','input_snapshot_digest','plan_digest','reason_code','digest'}
                or event.get('digest')!=p.digest({k:v for k,v in event.items() if k!='digest'})
                or event.get('event_id')!=key or key!=p.digest(['receipt-unit-posting',grant['context_file_id'],grant['request_id']])
                or event.get('event_type')!='imported' or event.get('page_number')!=grant['page_number']
                or event.get('receipt_unit_id')!=grant['receipt_unit_id'] or event.get('actor_id')!=grant['owner_actor_id']
                or event.get('authority_digest')!=grant['digest'] or event.get('input_snapshot_digest')!=grant['snapshot_digest']
                or event.get('plan_digest')!=grant['plan_digest'] or event.get('retention_class')!='permanent'
                or event.get('schema_version')!=1 or event.get('ledger_id')!='R-'+grant['receipt_unit_id']
                or current.get('ledger_id')!=event['ledger_id'] or current.get('last_event')!=key
                or type(event.get('confirmed_at')) is not int
                or str(datetime.fromtimestamp(event['confirmed_at'],timezone.utc).year)!=year):
                raise StateError('unit_posting_history_invalid')
        elif value['result'] is not None or value['current'] is not None or value['history']:
            raise StateError('unit_posting_nonterminal_invalid')
    return value

def bound(grant,plan,rid,owner,context_id):
    w.bound(grant,plan,rid,owner)
    ident=plan['snapshot']['identity']
    if (grant['context_file_id']!=context_id or grant['page_number']!=ident['page_number']
        or grant['receipt_unit_id']!=ident['receipt_unit_id'] or grant['budget']!=plan['budget']):
        raise StateError('unit_posting_scope_mismatch')

def plan_receipt(readers,plan,rid,context_id):
    """An exact plan-only intake receipt is required, not treated as permission.

    Its old ledger fingerprint may differ after another independently approved
    Unit posts. The posting grant separately fixes the fresh full fingerprint.
    """
    rows=readers.sheet_rows("'_PDF確認受付'!A1:F1001")
    if not rows or rows[0]!=p.HEADER or len(rows)>=1001:raise StateError('unit_posting_intake_schema_invalid')
    matches=[r for r in rows[1:] if r and r[0]==rid]
    if len(matches)!=1 or len(matches[0])!=6:raise StateError('unit_posting_intake_missing_or_duplicate')
    row=matches[0]
    if row[1]!='plan_only_complete' or not row[3] or not row[5]:raise StateError('unit_posting_plan_incomplete')
    if json.loads(row[2])!=u.intake_payload(plan,rid,context_id):raise StateError('unit_posting_intake_binding_changed')
    result=json.loads(row[4]);budget=plan['budget']
    expected=dict(mode='plan_only',status='plan_only_complete',page=plan['snapshot']['identity']['page_number'],
        planned_receipts=1,planned_expense_rows=budget['expenses'],planned_imports=1,total=budget['total'],
        plan_digest=plan['plan_digest'],snapshot_digest=plan['snapshot_digest'],replay_exact=True,
        accounting_writes=0,medical=0,gemini=0,source_moves=0)
    if (set(result)!=set(expected)|{'ledger_digest'} or any(result.get(k)!=v for k,v in expected.items())
        or not isinstance(result['ledger_digest'],str) or not re.fullmatch('[a-f0-9]{64}',result['ledger_digest'])):
        raise StateError('unit_posting_plan_receipt_changed')

def fresh_plan(readers,rid):
    """All authenticated input/HGA checks also run after our ledger IDs exist."""
    readers.drive.begin_request()
    try:
        record,snapshot,cats=readers.read();journal=Journal(readers)
        raw,tag=journal.read_versioned();request=json.loads(raw)['requests'].get(rid)
        if (not request or request['plan']['input'].get('action')!='記帳する'
            or request['plan']['input'].get('structure_confirmation')!='確認済み'):
            raise StateError('unit_explicit_intent_required')
        record,validation=validate_plan(readers,journal,rid,snapshot,readers.owner_actor_id)
        if validation['status']!='ready_to_confirm' or validation['posting_authority'] is not False:
            raise StateError('unit_posting_validation_held')
        if any(snapshot['identity'].get(k)!=v for k,v in record['legacy']['identity'].items()):
            raise StateError('unit_plan_identity_changed')
        stamp=datetime.fromtimestamp(request['proof']['verified_at'],timezone(p.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
        rows=accounting_plan(record,validation,cats,timestamp=stamp);budget=u.validate_rows(record,validation,rows)
        if journal.read_versioned()!=(raw,tag):raise StateError('unit_confirmation_changed')
        return dict(rows=rows,budget=budget,snapshot=snapshot,snapshot_digest=p.digest(snapshot),plan_digest=p.digest(rows),
            authority_digest=request['proof']['authority_digest'],stamp=stamp,record=record,validation=validation,categories=cats)
    finally:readers.drive.end_request()

def finish(store,before,tag,plan,ledger_digest,now):
    grant=before['grant'];ident=plan['snapshot']['identity'];year=str(datetime.fromtimestamp(now,timezone.utc).year)
    event=w.seal(dict(schema_version=1,retention_class='permanent',
        event_id=p.digest(['receipt-unit-posting',grant['context_file_id'],grant['request_id']]),event_type='imported',
        source_file_id=ident['source_file_id'],source_content_hash=ident['source_content_hash'],page_number=ident['page_number'],
        page_identity=ident['stable_page_identity'],receipt_unit_id=ident['receipt_unit_id'],ledger_id='R-'+ident['receipt_unit_id'],
        actor_id=grant['owner_actor_id'],confirmed_at=now,authority_digest=grant['digest'],input_snapshot_digest=grant['snapshot_digest'],
        plan_digest=grant['plan_digest'],reason_code='writer_exact_readback'))
    value=deepcopy(before);value.update(state='complete',generation=before['generation']+1,
        result={'plan_digest':grant['plan_digest'],'snapshot_digest':grant['snapshot_digest'],'ledger_digest':ledger_digest},
        current={'schema_version':1,'retention_class':'permanent','status':'imported','terminal':True,
            'receipt_unit_id':ident['receipt_unit_id'],'ledger_id':event['ledger_id'],'last_event':event['event_id']},
        history={year:{event['event_id']:event}})
    validate_state(value);store.replace(before,tag,value)
    return value

def audit_binding(value,plan):
    ident=plan['snapshot']['identity']
    event=next(iter(next(iter(value['history'].values())).values()))
    expected=dict(source_file_id=ident['source_file_id'],source_content_hash=ident['source_content_hash'],
        page_number=ident['page_number'],page_identity=ident['stable_page_identity'],receipt_unit_id=ident['receipt_unit_id'])
    if any(event.get(k)!=v for k,v in expected.items()):raise StateError('unit_posting_history_binding_changed')

def execute(readers,store,writer,projection,rid,context_id,*,attempt_id,now,operation):
    if operation not in {'apply','replay'}:raise StateError('unit_posting_operation_invalid')
    value,tag=store.read();validate_state(value);grant=value['grant']
    if value['state']=='unused':
        if operation!='apply' or not grant['approved_at']<=now<grant['expires_at']:
            raise StateError('unit_posting_unused_or_expired_grant')
        first=u.prepare(readers,rid);second=u.prepare(readers,rid)
        if first!=second:raise StateError('unit_posting_fresh_plan_changed')
        bound(grant,first,rid,readers.owner_actor_id,context_id)
        if first['ledger_digest']!=grant['before_ledger_digest']:raise StateError('unit_posting_ledger_precondition_changed')
        plan_receipt(readers,first,rid,context_id);projection.preflight(first['snapshot'])
        claimed=deepcopy(value);claimed.update(state='claimed',generation=1,claim={'attempt_id':attempt_id,'claimed_at':now})
        validate_state(claimed);store.replace(value,tag,claimed);value,tag=store.read()
        if value!=claimed:raise StateError('unit_posting_claim_readback_mismatch')
        if u.prepare(readers,rid)!=first:raise StateError('unit_posting_changed_after_claim')
        plan=fresh_plan(readers,rid);bound(grant,plan,rid,readers.owner_actor_id,context_id)
        plan_receipt(readers,plan,rid,context_id)
        scoped=w.ScopedMaterializerDB(writer,readers,plan,grant)
        try:
            ident=plan['record']['legacy']['identity']
            ReceiptPipeline(scoped,None,clock=lambda:plan['stamp'])._materialize_result(
                ReceiptResult.model_validate(plan['validation']['parsed']),ident['receipt_unit_id'],
                original_uri(ident['source_file_id'],ident['page_number']),[])
            if p.digest(scoped.plan)!=grant['plan_digest']:raise StateError('unit_posting_writer_plan_changed')
        except Exception:
            try:state,ledger_digest=w.outcome(readers,plan,grant)
            except Exception:state='unknown'
            if state!='written':
                held=deepcopy(value);held.update(state='unknown',generation=value['generation']+1)
                store.replace(value,tag,held)
                raise StateError('unit_posting_write_reconciliation_required') from None
        state,ledger_digest=w.outcome(readers,plan,grant)
        if state!='written':raise StateError('unit_posting_exact_readback_required')
        final=fresh_plan(readers,rid);bound(grant,final,rid,readers.owner_actor_id,context_id)
        value=finish(store,value,tag,final,ledger_digest,now);writes=len(plan['rows'])
    else:
        plan=fresh_plan(readers,rid);bound(grant,plan,rid,readers.owner_actor_id,context_id)
        plan_receipt(readers,plan,rid,context_id)
        state,ledger_digest=w.outcome(readers,plan,grant)
        if state!='written':raise StateError('unit_posting_claimed_reconciliation_required')
        if value['state']!='complete':value=finish(store,value,tag,plan,ledger_digest,now)
        else:
            audit_binding(value,plan)
            if value['result']['ledger_digest']!=ledger_digest:raise StateError('unit_posting_replay_ledger_changed')
        writes=0
    projection.hide(plan['snapshot']);checked,_=store.read()
    if checked!=value:raise StateError('unit_posting_completion_readback_mismatch')
    budget=grant['budget']
    return dict(status='imported',page=grant['page_number'],receipts=1,expense_rows=budget['expenses'],imports=1,total=budget['total'],
        accounting_rows_written=writes,replay=writes==0,exact_readback=True,history_events=1,terminal=True,card_hidden=True,
        medical=0,gemini=0,source_moves=0,plan_digest=grant['plan_digest'],snapshot_digest=grant['snapshot_digest'])
