"""Hosted p14 plan-only dispatch. Only queue metadata can be written.

No posting mode, projection refresh, image renderer, token exchange or archive.
Authenticated private evidence is reused without extending it to write authority.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json, os, re, subprocess
from urllib.parse import quote
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from ..drive_run_state import StateError
from .identity import digest
from .readers import Readers, Journal, validate_config_review, SID
from .validated import validate_plan, accounting_plan
from .planning import check_duplicates, TABLES
from .models import ReceiptResult
from .drive import strong_etag
from .completion import UUID

CONTEXT_ID='1otTGFJdRYU8YoLYC9UBt077WyibzrH3q'  # Reference only; all binding stays private Drive.
QUEUE='_PDF確認受付'
HEADER=['request_id','state','snapshot','submitted_at','result','finished_at']
BASE='https://sheets.googleapis.com/v4/spreadsheets/'+SID
SCHEMA='p14-plan-only-intake-v1'


def boundary(env,head):
    from ..production_flow import verify_execution_boundary
    verify_execution_boundary(env,head)
    if (env.get('GITHUB_EVENT_NAME')!='workflow_dispatch' or env.get('P14_PLAN_MODE')!='plan_only'
        or env.get('KAKEIBO_LEGACY_DISABLED')!='true' or env.get('RUNNER_DEBUG')=='1'
        or env.get('GITHUB_WORKFLOW_REF')!='tmoriuchi1401-source/kakeibo-ai/.github/workflows/p14-plan-only.yml@refs/heads/main'
        or env.get('SPREADSHEET_ID')!=SID or not UUID.fullmatch(env.get('P14_PLAN_REQUEST_ID',''))):
        raise StateError('p14_plan_boundary_required')


def context(info):
    http=AuthorizedSession(service_account.Credentials.from_service_account_info(info,
        scopes=['https://www.googleapis.com/auth/drive.readonly']))
    def get(media=False):
        r=http.get('https://www.googleapis.com/drive/v2/files/'+CONTEXT_ID,
            params={'alt':'media'} if media else {'fields':'id,etag,parents(id),labels(trashed),mimeType,owners(emailAddress),permissions(type,role,emailAddress,deleted)'},
            timeout=15,allow_redirects=False)
        if r.status_code!=200:raise StateError('p14_plan_context_unavailable')
        return r.content if media else r.json()
    before=get();value=json.loads(get(True));after=get()
    if before.get('id')!=CONTEXT_ID or not strong_etag(before.get('etag')) or before!=after or set(value)!={'schema','config','owner_actor_id'} or value['schema']!='receipt-item-runner-context-v1':
        raise StateError('p14_plan_context_changed')
    cfg=validate_config_review(value['config']);owners=[o.get('emailAddress') for o in before.get('owners',[])]
    grants={(x.get('type'),x.get('role'),x.get('emailAddress')) for x in before.get('permissions',[]) if not x.get('deleted')}
    if (len(owners)!=1 or digest(owners[0])!=cfg['drive']['owner_digest']
        or grants!={('user','owner',owners[0]),('user','writer',info['client_email'])}
        or before.get('parents')!=[{'id':cfg['drive']['folder']}] or before.get('mimeType')!='application/json'
        or before.get('labels',{}).get('trashed') or not re.fullmatch('[a-f0-9]{64}',value['owner_actor_id'])):
        raise StateError('p14_plan_context_acl_invalid')
    return value


def ledger(readers):
    data={};formulas={}
    for title,width in [('レシート','I'),('支出明細','M'),('取込データ','L')]:
        region="'"+title+"'!A1:"+width+'10000'
        data[title]=readers.sheet_rows(region)
        formulas[title]=readers.sheet_rows(region,render='FORMULA')
        if len(data[title])>=10000 or len(formulas[title])>=10000:raise StateError('p14_plan_ledger_truncated')
    return data, digest([data,formulas])


class LedgerView:
    def __init__(self,tables):self.tables=tables
    def get_raw(self,region):
        for title,width in [('レシート','I'),('支出明細','M'),('取込データ','L')]:
            if region=="'"+title+"'!A2:"+width:return deepcopy(self.tables[title][1:])
        raise StateError('p14_plan_ledger_range_forbidden')


def prepare(readers,rid):
    readers.drive.begin_request()
    try:
        record,snapshot,categories=readers.read();journal=Journal(readers)
        raw,etag=journal.read_versioned();value=json.loads(raw);request=value['requests'].get(rid)
        if request is None:raise StateError('p14_plan_authenticated_request_missing')
        if request['plan']['input'].get('action')!='記帳する' or request['plan']['input'].get('structure_confirmation')!='確認済み':
            raise StateError('p14_plan_explicit_intent_required')
        record,validation=validate_plan(readers,journal,rid,snapshot,readers.owner_actor_id)
        if validation['status']!='ready_to_confirm':raise StateError('p14_plan_validation_held')
        if validation['posting_authority'] is not False:raise StateError('p14_plan_authority_scope_invalid')
        unit=record['legacy']['identity']['receipt_unit_id'];tables,fingerprint=ledger(readers)
        if any(unit in str(r[0]) for rows in tables.values() for r in rows[1:] if r):
            raise StateError('p14_plan_ledger_identity_collision')
        check_duplicates(LedgerView(tables),unit,ReceiptResult.model_validate(validation['parsed']))
        stamp=datetime.fromtimestamp(request['proof']['verified_at'],timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
        rows=accounting_plan(record,validation,categories,timestamp=stamp)
        expenses=[r for t,r in rows if t=='支出明細']
        if (sum(t=='レシート' for t,r in rows)!=1 or len(expenses)!=10 or sum(t=='取込データ' for t,r in rows)!=1
            or sum(r[4] for r in expenses)!=3801 or [r[4] for r in expenses if r[4]<0]!=[-13]
            or expenses[-1][5:7]!=expenses[2][5:7] or len({r[0] for r in expenses})!=10
            or sum(i['kind']=='product' for i in record['items'])!=9):
            raise StateError('p14_plan_expected_rows_changed')
        if journal.read_versioned()!=(raw,etag):raise StateError('p14_plan_confirmation_changed')
        hga=readers.drive.read(readers.config['drive']['authority_file'])
        return dict(snapshot=snapshot,rows=rows,plan_digest=digest(rows),snapshot_digest=digest(snapshot),
            ledger_digest=fingerprint,journal_digest=digest(json.loads(raw)),journal_etag=etag,
            hga_digest=digest(json.loads(hga[0])),hga_etag=hga[1],stamp=stamp,authority_digest=request['proof']['authority_digest'])
    finally:readers.drive.end_request()


class QueuePort:
    """Separate Sheets-only credential. Exact A:F queue row PUT, no generic writes."""
    def __init__(self,info):
        self.http=AuthorizedSession(service_account.Credentials.from_service_account_info(info,
            scopes=['https://www.googleapis.com/auth/spreadsheets']))
        response=self.http.get(BASE,params={'fields':'sheets.properties'},timeout=10,allow_redirects=False)
        if response.status_code!=200:raise StateError('p14_plan_sheet_metadata_unavailable')
        tabs={x['properties']['title']:x['properties'] for x in response.json().get('sheets',[])}
        if tabs.get(QUEUE,{}).get('sheetId')!=261001092 or tabs[QUEUE].get('hidden') is not True:
            raise StateError('p14_plan_existing_queue_required')
    def rows(self):
        r=self.http.get(BASE+'/values/'+quote("'"+QUEUE+"'!A1:F1001",safe=''),
            params={'valueRenderOption':'UNFORMATTED_VALUE'},timeout=10,allow_redirects=False)
        if r.status_code!=200:raise StateError('p14_plan_queue_unavailable')
        rows=r.json().get('values',[])
        if not rows or rows[0]!=HEADER or len(rows)>=1001 or any(len(x)>6 for x in rows):
            raise StateError('p14_plan_queue_schema_changed')
        return [list(x)+['']*(6-len(x)) for x in rows]
    def replace(self,before,number,row,rid):
        # The caller holds the established kakeibo-production concurrency.
        if (self.rows()!=before or row[0]!=rid or len(row)!=6 or row[1] not in {'accepted','plan_only_complete'}
            or not 2<=number<=1001 or number>len(before)+1):raise StateError('p14_plan_queue_changed')
        envelope=json.loads(row[2])
        if (set(envelope)!={'schema','mode','request_id','context_file_id','receipt_unit_id','item_ids','identity_digest','snapshot_digest','authority_digest'}
            or envelope['schema']!=SCHEMA or envelope['mode']!='plan_only' or envelope['request_id']!=rid
            or envelope['context_file_id']!=CONTEXT_ID or not isinstance(envelope['item_ids'],list)
            or len(envelope['item_ids'])!=10 or len(set(envelope['item_ids']))!=10
            or not isinstance(envelope['receipt_unit_id'],str) or not re.fullmatch('page-receipt-v1:[a-f0-9]{64}',envelope['receipt_unit_id'])
            or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v) for v in [envelope['identity_digest'],envelope['snapshot_digest'],envelope['authority_digest'],*envelope['item_ids']])):
            raise StateError('p14_plan_queue_scope_invalid')
        if number<=len(before) and (before[number-1][0]!=rid or before[number-1][2:4]!=row[2:4]):
            raise StateError('p14_plan_queue_other_request')
        expected=deepcopy(before)
        if number==len(before)+1:expected.append(row)
        else:expected[number-1]=row
        region="'"+QUEUE+"'!A"+str(number)+':F'+str(number)
        try:
            r=self.http.put(BASE+'/values/'+quote(region,safe=''),params={'valueInputOption':'RAW'},
                json={'range':region,'majorDimension':'ROWS','values':[row]},timeout=10,allow_redirects=False)
            if r.status_code!=200:raise StateError('p14_plan_queue_delivery_unknown')
        except Exception:
            # Always reconcile even a timeout; no automatic resend.
            if self.rows()!=expected:raise StateError('p14_plan_queue_delivery_unknown') from None
        if self.rows()!=expected:raise StateError('p14_plan_queue_readback_mismatch')


def intake_payload(plan,rid):
    # Compact references only: signed full inputs remain in the private journal.
    ident=plan['snapshot']['identity']
    return {'schema':SCHEMA,'mode':'plan_only','request_id':rid,'context_file_id':CONTEXT_ID,
        'receipt_unit_id':ident['receipt_unit_id'],'item_ids':ident['item_ids'],
        'identity_digest':digest(ident),'snapshot_digest':plan['snapshot_digest'],
        'authority_digest':plan['authority_digest']}


def capture(port,rid,plan):
    previous=port.rows();matches=[(n,r) for n,r in enumerate(previous[1:],2) if r[0]==rid]
    if len(matches)>1:raise StateError('p14_plan_duplicate_queue_identity')
    envelope=intake_payload(plan,rid)
    payload=json.dumps(envelope,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    if matches:
        number,row=matches[0]
        if row[2]!=payload or row[3]!=plan['stamp'] or row[1] not in {'accepted','plan_only_complete'}:
            raise StateError('p14_plan_queue_request_conflict')
        return number,row,0
    for row in previous[1:]:
        if row[1] in {'accepted','dispatching','running','unknown'}:
            try:old=json.loads(row[2])
            except Exception:raise StateError('p14_plan_unknown_pending_queue') from None
            old_snapshot=old.get('snapshot',old) if isinstance(old,dict) else {}
            if old_snapshot.get('token')==plan['snapshot']['token'] or (isinstance(old,dict) and old.get('receipt_unit_id')==envelope['receipt_unit_id']):
                raise StateError('p14_plan_existing_pending_request')
    row=[rid,'accepted',payload,plan['stamp'],'','']
    number=len(previous)+1;port.replace(previous,number,row,rid)
    return number,row,1


def execute(readers,port,rid):
    first=prepare(readers,rid);second=prepare(readers,rid)
    if first!=second:raise StateError('p14_plan_replay_changed')
    number,row,appends=capture(port,rid,first)
    third=prepare(readers,rid)
    if first!=third:raise StateError('p14_plan_freshness_changed_after_capture')
    result={'mode':'plan_only','status':'plan_only_complete','planned_receipts':1,'planned_expense_rows':10,
        'planned_imports':1,'total':3801,'plan_digest':first['plan_digest'],'snapshot_digest':first['snapshot_digest'],
        'ledger_digest':first['ledger_digest'],'replay_exact':True,'accounting_writes':0,'medical':0,'gemini':0,'source_moves':0}
    encoded=json.dumps(result,sort_keys=True,separators=(',',':'))
    desired=[rid,'plan_only_complete',row[2],row[3],encoded,first['stamp']]
    if row!=desired:port.replace(port.rows(),number,desired,rid)
    if port.rows()[number-1]!=desired:raise StateError('p14_plan_result_readback_mismatch')
    if prepare(readers,rid)!=first:raise StateError('p14_plan_post_readback_changed')
    return {**result,'queue_appends':appends,'queue_readback_exact':True}


def main():
    try:
        env=dict(os.environ);head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        boundary(env,head)
        info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON']);cfg=context(info)
        readers=Readers(cfg['config'],info,owner_actor_id=cfg['owner_actor_id'])
        print(json.dumps(execute(readers,QueuePort(info),env['P14_PLAN_REQUEST_ID']),sort_keys=True))
    except Exception as e:
        # Exception/API payloads may contain private data: print only fixed failure.
        category=str(e) if isinstance(e,StateError) and re.fullmatch('[a-zA-Z0-9_]{1,100}',str(e)) else 'p14_plan_failed'
        print(json.dumps({'status':'p14_plan_failed','failure_category':category,'accounting_writes':0}))
        raise SystemExit(1) from None

if __name__=='__main__':main()
