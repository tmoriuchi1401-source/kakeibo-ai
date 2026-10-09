"""Bounded p4/p10 adapter. Only private evidence GET and queue metadata PUT.

The existing p14 path remains fixed. No posting permission is created here.
Each context fixes one sealed candidate and journal; no page/item/budget input
is accepted from Actions. ReceiptPipeline runs with the memory-only PlanningDB.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json,os,re,subprocess
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from ..drive_run_state import StateError
from ..production_flow import verify_execution_boundary
from . import runner as p
from .models import ReceiptResult
from .readers import Readers,Journal,validate_config_review
from .validated import validate_plan,accounting_plan
from .planning import check_duplicates

PAGES=(4,10)
SCHEMA='receipt-unit-plan-only-intake-v1'
FILE_ID=re.compile('[A-Za-z0-9_-]{10,150}')

def boundary(env,head):
    verify_execution_boundary(env,head)
    if (env.get('GITHUB_EVENT_NAME')!='workflow_dispatch' or env.get('RECEIPT_UNIT_MODE')!='plan_only'
        or env.get('KAKEIBO_LEGACY_DISABLED')!='true' or env.get('RUNNER_DEBUG')=='1'
        or env.get('GITHUB_WORKFLOW_REF')!='tmoriuchi1401-source/kakeibo-ai/.github/workflows/receipt-unit-plan-only.yml@refs/heads/main'
        or env.get('SPREADSHEET_ID')!=p.SID or not p.UUID.fullmatch(env.get('RECEIPT_UNIT_REQUEST_ID',''))
        or not FILE_ID.fullmatch(env.get('RECEIPT_UNIT_CONTEXT_ID',''))):
        raise StateError('unit_plan_boundary_required')

def context(info,fid,*,http=None):
    if not FILE_ID.fullmatch(fid) or fid==p.CONTEXT_ID:raise StateError('unit_context_scope_invalid')
    http=http or AuthorizedSession(service_account.Credentials.from_service_account_info(info,
        scopes=['https://www.googleapis.com/auth/drive.readonly']))
    def get(media=False):
        response=http.get('https://www.googleapis.com/drive/v2/files/'+fid,
            params={'alt':'media'} if media else {'fields':'id,etag,parents(id),labels(trashed),mimeType,owners(emailAddress),permissions(type,role,emailAddress,deleted)'},
            timeout=15,allow_redirects=False)
        if response.status_code!=200 or len(response.content)>2*1024*1024:raise StateError('unit_context_unavailable')
        return response.content if media else response.json()
    before=get();value=json.loads(get(True));after=get()
    if (before!=after or before.get('id')!=fid or not p.strong_etag(before.get('etag'))
        or set(value)!={'schema','config','owner_actor_id'} or value['schema']!='receipt-item-runner-context-v1'):
        raise StateError('unit_context_changed')
    cfg=validate_config_review(value['config'],allowed_pages=PAGES)
    forbidden={cfg['candidate_file'],cfg['journal_file'],cfg['drive']['authority_file'],cfg['drive']['folder'],
        cfg['drive']['inbox'],cfg['drive']['page']['source']['source_file_id'],*cfg['drive']['baseline_files']}
    owners=[o.get('emailAddress') for o in before.get('owners',[])]
    grants={(x.get('type'),x.get('role'),x.get('emailAddress')) for x in before.get('permissions',[]) if not x.get('deleted')}
    if (fid in forbidden or len(owners)!=1 or p.digest(owners[0])!=cfg['drive']['owner_digest']
        or grants!={('user','owner',owners[0]),('user','writer',info['client_email'])}
        or before.get('parents')!=[{'id':cfg['drive']['folder']}] or before.get('mimeType')!='application/json'
        or before.get('labels',{}).get('trashed') or not re.fullmatch('[a-f0-9]{64}',value['owner_actor_id'])):
        raise StateError('unit_context_acl_invalid')
    return value

def validate_rows(record,validation,rows):
    """Validate every planned row against the authenticated final items.

    Total/tax/discount/target-category checks run in existing item evaluate and
    receipt validation first. This never substitutes an invented adjustment.
    """
    ident=record['legacy']['identity'];result=ReceiptResult.model_validate(validation['parsed'])
    receipts=[r for t,r in rows if t=='レシート'];expenses=[r for t,r in rows if t=='支出明細'];imports=[r for t,r in rows if t=='取込データ']
    if (ident['page_number'] not in PAGES or len(receipts)!=1 or len(imports)!=1
        or not 1<=len(expenses)==len(result.items)==len(record['items'])<=100
        or len(rows)!=len(expenses)+2 or result.transaction_kind!='purchase'
        or type(result.total) is not int or result.total<=0 or sum(r[4] for r in expenses)!=result.total
        or receipts[0][3]!=result.total or imports[0][6]!=result.total
        or len({r[0] for r in expenses})!=len(expenses)
        or any(r[3:7]!=[i.name,i.amount,i.major_category,i.minor_category] for r,i in zip(expenses,result.items))
        or receipts[0][0]!='R-'+ident['receipt_unit_id'] or imports[0][0]!='receipt:'+ident['receipt_unit_id']
        or any(r[0]!=receipts[0][0]+'-'+str(n).zfill(2) or r[9]!=receipts[0][0] or r[10]!=imports[0][0]
            for n,r in enumerate(expenses,1))):
        raise StateError('unit_plan_rows_changed')
    return {'receipts':1,'expenses':len(expenses),'imports':1,'total':result.total,
        'discounts':[r[4] for r in expenses if r[4]<0]}

def prepare(readers,rid):
    readers.drive.begin_request()
    try:
        record,snapshot,categories=readers.read();journal=Journal(readers)
        raw,etag=journal.read_versioned();request=json.loads(raw)['requests'].get(rid)
        if not request:raise StateError('unit_authenticated_request_missing')
        if request['plan']['input'].get('action')!='記帳する' or request['plan']['input'].get('structure_confirmation')!='確認済み':
            raise StateError('unit_explicit_intent_required')
        record,validation=validate_plan(readers,journal,rid,snapshot,readers.owner_actor_id)
        if validation['status']!='ready_to_confirm' or validation['posting_authority'] is not False:
            raise StateError('unit_plan_validation_held')
        if record['legacy']['identity']!=snapshot['identity']:
            # Snapshot also has item_ids; compare only the candidate identity fields.
            if any(snapshot['identity'].get(k)!=v for k,v in record['legacy']['identity'].items()):
                raise StateError('unit_plan_identity_changed')
        unit=record['legacy']['identity']['receipt_unit_id'];tables,fingerprint=p.ledger(readers)
        if any(unit in str(r[0]) for rows in tables.values() for r in rows[1:] if r):raise StateError('unit_ledger_identity_collision')
        check_duplicates(p.LedgerView(tables),unit,ReceiptResult.model_validate(validation['parsed']))
        stamp=datetime.fromtimestamp(request['proof']['verified_at'],timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
        rows=accounting_plan(record,validation,categories,timestamp=stamp);budget=validate_rows(record,validation,rows)
        if journal.read_versioned()!=(raw,etag):raise StateError('unit_confirmation_changed')
        hga=readers.drive.read(readers.config['drive']['authority_file'])
        return dict(snapshot=snapshot,rows=rows,budget=budget,plan_digest=p.digest(rows),snapshot_digest=p.digest(snapshot),
            ledger_digest=fingerprint,journal_digest=p.digest(json.loads(raw)),journal_etag=etag,
            hga_digest=p.digest(json.loads(hga[0])),hga_etag=hga[1],stamp=stamp,authority_digest=request['proof']['authority_digest'])
    finally:readers.drive.end_request()

def intake_payload(plan,rid,fid):
    ident=plan['snapshot']['identity']
    return dict(schema=SCHEMA,mode='plan_only',request_id=rid,context_file_id=fid,receipt_unit_id=ident['receipt_unit_id'],
        item_ids=ident['item_ids'],identity_digest=p.digest(ident),snapshot_digest=plan['snapshot_digest'],authority_digest=plan['authority_digest'])

class UnitQueuePort(p.QueuePort):
    def __init__(self,info,fid,plan,rid):
        self.expected=intake_payload(plan,rid,fid)
        super().__init__(info)
    def validate_envelope(self,envelope,rid):
        if envelope!=self.expected or envelope.get('request_id')!=rid:raise StateError('unit_queue_scope_invalid')

def execute(readers,port,rid,fid):
    first=prepare(readers,rid);second=prepare(readers,rid)
    if first!=second:raise StateError('unit_plan_replay_changed')
    envelope=intake_payload(first,rid,fid)
    number,row,appends=p.capture(port,rid,first,envelope=envelope)
    if prepare(readers,rid)!=first:raise StateError('unit_changed_after_capture')
    budget=first['budget']
    result=dict(mode='plan_only',status='plan_only_complete',page=first['snapshot']['identity']['page_number'],
        planned_receipts=budget['receipts'],planned_expense_rows=budget['expenses'],planned_imports=budget['imports'],
        total=budget['total'],plan_digest=first['plan_digest'],snapshot_digest=first['snapshot_digest'],
        ledger_digest=first['ledger_digest'],replay_exact=True,accounting_writes=0,medical=0,gemini=0,source_moves=0)
    encoded=json.dumps(result,sort_keys=True,separators=(',',':'))
    finished=row[5] if row[1]=='plan_only_complete' else datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
    if not finished:raise StateError('unit_result_timestamp_missing')
    desired=[rid,'plan_only_complete',row[2],row[3],encoded,finished]
    if row[1]=='plan_only_complete' and row!=desired:raise StateError('unit_completed_result_changed')
    if row!=desired:port.replace(port.rows(),number,desired,rid)
    if port.rows()[number-1]!=desired or prepare(readers,rid)!=first:raise StateError('unit_result_readback_changed')
    return {**result,'queue_appends':appends,'queue_readback_exact':True}

def main():
    try:
        env=dict(os.environ);head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip();boundary(env,head)
        info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON']);fid=env['RECEIPT_UNIT_CONTEXT_ID'];rid=env['RECEIPT_UNIT_REQUEST_ID']
        cfg=context(info,fid);readers=Readers(cfg['config'],info,owner_actor_id=cfg['owner_actor_id'],allowed_pages=PAGES)
        initial=prepare(readers,rid)
        print(json.dumps(execute(readers,UnitQueuePort(info,fid,initial,rid),rid,fid),sort_keys=True))
    except Exception as error:
        category=str(error) if isinstance(error,StateError) and re.fullmatch('[a-zA-Z0-9_]{1,100}',str(error)) else 'unit_plan_failed'
        print(json.dumps({'status':'unit_plan_failed','failure_category':category,'accounting_writes':0}))
        raise SystemExit(1) from None

if __name__=='__main__':main()
