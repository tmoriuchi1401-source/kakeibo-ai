"""Exact private posting file + existing writer, no unrelated capabilities."""
from copy import deepcopy
import json,re,os,subprocess,time,uuid
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from ..drive_run_state import StateError
from ..production_flow import verify_execution_boundary
from ..sheets import SheetsDB
from . import runner as p
from . import posting
from .items import SCHEMA as CARD_SCHEMA

def boundary(env,head):
    verify_execution_boundary(env,head)
    if (env.get('GITHUB_EVENT_NAME')!='workflow_dispatch' or env.get('KAKEIBO_LEGACY_DISABLED')!='true'
        or env.get('GITHUB_WORKFLOW_REF')!='tmoriuchi1401-source/kakeibo-ai/.github/workflows/p14-posting-canary.yml@refs/heads/main'
        or env.get('P14_POST_CONFIRM')!='POST_P14_3801' or env.get('P14_POST_OPERATION') not in {'apply','replay'}
        or env.get('RUNNER_DEBUG')=='1' or env.get('SPREADSHEET_ID')!=p.SID
        or not p.UUID.fullmatch(env.get('P14_PLAN_REQUEST_ID',''))
        or not re.fullmatch('[A-Za-z0-9_-]{10,150}',env.get('P14_POSTING_FILE',''))):
        raise StateError('posting_execution_boundary_required')

class PostingStore:
    validate_state=staticmethod(posting.validate_state)
    def __init__(self,info,cfg,fid,*,http=None):
        self.info,self.cfg,self.fid=info,cfg,fid
        forbidden={p.CONTEXT_ID,cfg['candidate_file'],cfg['journal_file'],cfg['drive']['folder'],cfg['drive']['authority_file'],cfg['drive']['page']['source']['source_file_id']}
        if fid in forbidden or not re.fullmatch('[A-Za-z0-9_-]{10,150}',fid):raise StateError('posting_file_scope_invalid')
        self.http=http or AuthorizedSession(service_account.Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/drive']))
    def metadata(self):
        r=self.http.get('https://www.googleapis.com/drive/v2/files/'+self.fid,
            params={'fields':'id,etag,parents(id),labels(trashed),mimeType,owners(emailAddress),permissions(type,role,emailAddress,deleted)'},timeout=15,allow_redirects=False)
        if r.status_code!=200:raise StateError('posting_file_metadata_unavailable')
        meta=r.json();owners=[o.get('emailAddress') for o in meta.get('owners',[])]
        grants={(x.get('type'),x.get('role'),x.get('emailAddress')) for x in meta.get('permissions',[]) if not x.get('deleted')}
        if (meta.get('id')!=self.fid or not p.strong_etag(meta.get('etag')) or len(owners)!=1
            or p.digest(owners[0])!=self.cfg['drive']['owner_digest']
            or grants!={('user','owner',owners[0]),('user','writer',self.info['client_email'])}
            or meta.get('parents')!=[{'id':self.cfg['drive']['folder']}]
            or meta.get('mimeType')!='application/json' or meta.get('labels',{}).get('trashed')):
            raise StateError('posting_file_acl_or_identity_invalid')
        return meta
    def read(self):
        before=self.metadata()
        r=self.http.get('https://www.googleapis.com/drive/v2/files/'+self.fid,params={'alt':'media'},timeout=15,allow_redirects=False)
        if r.status_code!=200:raise StateError('posting_file_read_unavailable')
        after=self.metadata()
        if before!=after:raise StateError('posting_file_changed_during_read')
        value=self.validate_state(json.loads(r.content))
        return value,before['etag']
    def replace(self,before,tag,after):
        self.validate_state(after)
        if (after['grant']!=before['grant'] or after['generation']!=before['generation']+1
            or (before['state'],after['state']) not in {('unused','claimed'),('claimed','unknown'),('claimed','complete'),('unknown','complete')}
            or before['state']!='unused' and before['claim']!=after['claim']):
            raise StateError('posting_state_transition_invalid')
        if self.read()!=(before,tag):raise StateError('HTTP_412')
        encoded=json.dumps(after,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
        try:
            r=self.http.put('https://www.googleapis.com/upload/drive/v2/files/'+self.fid,
                params={'uploadType':'media','fields':'id'},headers={'If-Match':tag,'Content-Type':'application/json'},
                data=encoded,timeout=15,allow_redirects=False)
        except Exception:
            current,_=self.read()
            if current!=after:raise StateError('posting_state_write_unknown') from None
        else:
            if r.status_code==412:raise StateError('HTTP_412')
            if r.status_code!=200:
                current,_=self.read()
                if current!=after:raise StateError('posting_state_write_unknown')
        if self.read()[0]!=after:raise StateError('posting_state_exact_readback_failed')

class CardProjection:
    def __init__(self,info,readers):
        self.readers=readers
        self.http=AuthorizedSession(service_account.Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/spreadsheets']))
    def preflight(self,snapshot):
        rows=self.readers.sheet_rows("'PDFページ確認'!A1:W1000")
        ids=[n for n,r in enumerate(rows) if len(r)>20 and r[17]==CARD_SCHEMA and r[18]==snapshot['token']]
        if (len(ids)!=len(snapshot['rows']) or ids!=list(range(ids[0],ids[-1]+1))
            or any(json.loads(rows[n][19])!=snapshot['identity'] for n in ids)):
            raise StateError('posting_projection_identity_changed')
        return ids
    def hide(self,snapshot):
        ids=self.preflight(snapshot)
        def hidden():
            response=self.http.get(p.BASE,params={'ranges':"'PDFページ確認'!A"+str(ids[0]+1)+':B'+str(ids[-1]+1),
                'includeGridData':'true','fields':'sheets(properties(sheetId),data(startRow,rowMetadata(hiddenByUser)))'},timeout=15,allow_redirects=False)
            if response.status_code!=200:raise StateError('posting_projection_readback_unavailable')
            flags={}
            for sheet in response.json()['sheets']:
                if sheet['properties']['sheetId']!=261001091:raise StateError('posting_projection_sheet_changed')
                for data in sheet.get('data',[]):
                    flags.update({data.get('startRow',0)+i:row.get('hiddenByUser',False) for i,row in enumerate(data.get('rowMetadata',[]))})
            return all(flags.get(i) is True for i in ids)
        if hidden():return
        # Visibility only: values/identities/validation/formats are preserved.
        body={'requests':[{'updateDimensionProperties':{'range':{'sheetId':261001091,'dimension':'ROWS',
            'startIndex':ids[0],'endIndex':ids[-1]+1},'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}}]}
        try:
            response=self.http.post(p.BASE+':batchUpdate',json=body,timeout=15,allow_redirects=False)
            if response.status_code!=200:raise StateError('posting_projection_delivery_unknown')
        except Exception:
            if not hidden():raise StateError('posting_projection_delivery_unknown') from None
        if not hidden():raise StateError('posting_projection_exact_readback_failed')

def main():
    try:
        env=dict(os.environ);head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip();boundary(env,head)
        info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON']);cfg=p.context(info)
        readers=p.Readers(cfg['config'],info,owner_actor_id=cfg['owner_actor_id'])
        store=PostingStore(info,cfg['config'],env['P14_POSTING_FILE'])
        result=posting.execute(readers,store,SheetsDB(p.SID),CardProjection(info,readers),env['P14_PLAN_REQUEST_ID'],
            attempt_id=str(uuid.uuid4()),now=int(time.time()),operation=env['P14_POST_OPERATION'])
        print(json.dumps(result,sort_keys=True))
    except Exception as error:
        category=str(error) if isinstance(error,StateError) and re.fullmatch('[A-Za-z0-9_]{1,100}',str(error)) else 'posting_failed_readback_required'
        print(json.dumps({'status':'posting_stopped','failure_category':category,'automatic_retry':False}))
        raise SystemExit(1) from None

if __name__=='__main__':main()
