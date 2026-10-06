"""One reviewed real page only. No Sheets, renderer, AI, writer or mover.

Dedicated private Drive HGA state; existing grouping files are GET-only and
must match the operator's cryptographically fixed, fully validated baseline.
There is no public seed/provision route or arbitrary source/page input.
"""
from hashlib import sha256
import json

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.drive_run_state import StateError
from app.conditional_drive_state_v2 import strong_etag
from app.human_general_auth_transport import ISSUER, request_binding, AI_CONSENT_ACTION
from app.human_general_authority import empty_state, validate_state, review_identity, eligible
from app.page_receipt_model import PageUnit, page_key, digest

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')

SCHEMA='human-general-verified-drive-canary-v1'
SOURCE='1fcmMMGj86DLq54G0inD8LI_XSyQSPfTY'
HASH='ca1b8cba60addc14a364438d691c40164e250708103bce76a320f671629491bf'


def validate_config(config):
    try:
        if set(config)!={'page','folder','inbox','authority_file','baseline_files','owner_digest','binding'}:raise ValueError()
        page=PageUnit.model_validate(config['page'])
        if (page.source.source_file_id!=SOURCE or page.source.source_content_hash!=HASH or page.source.page_count!=14
                or page.page_number!=14 or page.authority_revision!=2 or page.processing_status!='observed'
                or page.automatic_classification!='sensitive_unknown' or page.automatic_reason!='privacy_unresolved'
                or not eligible(page) or page.review_identity!=review_identity(page)):raise ValueError()
        fixed={'1ju2rEDWrlpALr-9d4JEN9yaPfTuKtFCq','1atHszVu7J-OXPbJkhCMhsvhiyz6QEdsR',
               '1Gss6WvRvKbSWIKVSzrkApYkxVRFdQ6g_','1J6hjORzCDEauxg39o1fZbwRS41ROe3jE'}
        if set(config['baseline_files'])!=fixed or config['authority_file'] in fixed|{SOURCE,config['folder'],config['inbox']}:raise ValueError()
        import re
        if any(not re.fullmatch('[0-9a-f]{64}',v) for v in [config['owner_digest'],*config['baseline_files'].values()]):raise ValueError()
        for v in (config['folder'],config['inbox'],config['authority_file']):
            if not re.fullmatch('[A-Za-z0-9_-]{10,150}',v):raise ValueError()
        if config['binding']!=digest([SCHEMA,config['folder'],config['authority_file'],page.source.model_dump(),14,page.review_identity]):raise ValueError()
    except Exception:raise StateError('real_page_configuration_invalid') from None
    return page


def empty_verified_state(binding):
    return {'schema':SCHEMA,'binding':binding,'authority':empty_state(binding),'actor_evidence':{}}


def validate_verified(value,config,owner_sub):
    try:
        page=validate_config(config);binding=config['binding']
        if set(value)!={'schema','binding','authority','actor_evidence'} or value['schema']!=SCHEMA or value['binding']!=binding:raise ValueError()
        state=validate_state(value['authority'],binding)
        if len(state['grants'])>1 or any(k!=page_key(page) for k in state['grants']):raise ValueError()
        if len(state['audit'])>1 or any(e['operation']!='confirm_general_receipt_ai' for e in state['audit']):raise ValueError()
        if set(value['actor_evidence'])!={e['request_id'] for e in state['audit']}:raise ValueError()
        for event in state['audit']:
            evidence=value['actor_evidence'][event['request_id']]
            actor=evidence['actor'];expected=request_binding(page,event['request_id'],AI_CONSENT_ACTION)
            if (set(evidence)!={'actor','binding','explicit_consent'} or evidence['binding']!=expected
                    or set(actor)!={'issuer','subject','email','method','verified_at','request_id','request_digest','policy_revision','verification_revision','actor_id'}
                    or type(actor['verified_at']) is not int
                    or evidence['explicit_consent']!='general_receipt_and_gemini'
                    or actor['issuer']!=ISSUER or actor['subject']!=owner_sub
                    or actor['actor_id']!=digest([ISSUER,owner_sub]) or actor['method']!='google_oidc_code_pkce_v1'
                    or actor['request_id']!=event['request_id'] or actor['request_digest']!=digest(expected)
                    or actor['verification_revision']!=1 or actor['policy_revision']!=1
                    or state['grants'][page_key(page)]['confirmer']!={'provider':'google','subject':actor['email']}):raise ValueError()
    except Exception:raise StateError('real_page_verified_state_invalid') from None
    return value


class RealPageDrive:
    """Existing SA, existing Drive scope. Exact IDs; one conditional PUT only."""
    def __init__(self,config,info,*,session=None):
        self.page=validate_config(config);self.config=config;self.sa=info['client_email']
        auth=service_account.Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/drive'])
        self.http=session or AuthorizedSession(auth)
        self.allowed={SOURCE,config['folder'],config['authority_file'],*config['baseline_files']}

    def request(self,fid,*,media=False,fields=None,put=None,tag=None):
        if fid not in self.allowed:raise StateError('real_page_drive_target_forbidden')
        if put is None:
            url='https://www.googleapis.com/drive/v2/files/'+fid
            params={'alt':'media'} if media else {'fields':fields}
            method='GET';headers={};body=None
        else:
            if fid!=self.config['authority_file'] or not strong_etag(tag) or type(put) is not bytes or len(put)>2*1024*1024:
                raise StateError('real_page_conditional_write_forbidden')
            url='https://www.googleapis.com/upload/drive/v2/files/'+fid
            params={'uploadType':'media','fields':'id,etag'};method='PUT'
            headers={'If-Match':tag,'Content-Type':'application/json'};body=put
        try:
            response=self.http.request(method,url,params=params,data=body,headers=headers,timeout=25,allow_redirects=False)
            if response.status_code==412:raise StateError('HTTP_412')
            if response.status_code!=200:raise StateError('real_page_drive_unavailable')
            if len(response.content)>100*1024*1024:raise StateError('real_page_drive_size_limit')
            return response.content if media else response.json()
        except StateError:raise
        except Exception:raise StateError('real_page_drive_unavailable') from None

    def metadata(self,fid):
        value=self.request(fid,fields='id,etag,parents(id),labels(trashed),mimeType')
        if value.get('id')!=fid or value.get('labels',{}).get('trashed') or not strong_etag(value.get('etag')):
            raise StateError('real_page_drive_metadata_invalid')
        folder=self.config['inbox'] if fid==SOURCE else self.config['folder']
        mime='application/pdf' if fid==SOURCE else 'application/json'
        if value.get('parents')!=[{'id':folder}] or value.get('mimeType')!=mime:
            raise StateError('real_page_source_or_state_moved')
        return value

    def read(self,fid):
        before=self.metadata(fid);payload=self.request(fid,media=True);after=self.metadata(fid)
        if before!=after:raise StateError('real_page_changed_during_read')
        return payload,before['etag']

    def acl(self):
        for fid in (self.config['folder'],self.config['authority_file']):
            value=self.request(fid,fields='id,owners(emailAddress),permissions(type,role,emailAddress,deleted)')
            owners=[o.get('emailAddress') for o in value.get('owners',[])]
            if len(owners)!=1 or digest(owners[0])!=self.config['owner_digest']:raise StateError('real_page_acl_mismatch')
            grants={(p.get('type'),p.get('role'),p.get('emailAddress')) for p in value.get('permissions',[]) if not p.get('deleted')}
            if grants!={('user','owner',owners[0]),('user','writer',self.sa)}:raise StateError('real_page_acl_mismatch')

    def fresh(self):
        for fid,expected in self.config['baseline_files'].items():
            if sha256(self.read(fid)[0]).hexdigest()!=expected:raise StateError('real_page_baseline_stale')
        # Whole source equality fixes page count and ordinal structure as well.
        self.source(SOURCE)
        return self.page

    def source(self,sid):
        if sid!=SOURCE:raise StateError('real_page_source_forbidden')
        raw=self.read(sid)[0]
        if sha256(raw).hexdigest()!=HASH:raise StateError('real_page_source_changed')
        return raw


class VerifiedDriveTransport:
    """Core-compatible inner state, durable signed-actor provenance envelope."""
    def __init__(self,drive,owner_sub,*,actor=None):
        self.drive,self.owner_sub,self.actor=drive,owner_sub,actor

    def read_versioned(self):
        self.drive.acl()
        raw,tag=self.drive.read(self.drive.config['authority_file'])
        try:value=validate_verified(json.loads(raw),self.drive.config,self.owner_sub)
        except (ValueError,TypeError):raise StateError('real_page_verified_state_invalid') from None
        return canonical(value['authority']),tag

    def replace_versioned(self,before,tag,after):
        current,current_tag=self.read_versioned()
        if before!=current or tag!=current_tag:raise StateError('HTTP_412')
        if self.actor is None:raise StateError('real_page_verified_actor_required')
        raw,again=self.drive.read(self.drive.config['authority_file'])
        if again!=tag:raise StateError('HTTP_412')
        value=json.loads(raw);new=json.loads(after);actor=self.actor.record();rid=actor['request_id']
        if value['authority']['generation']!=0 or new['generation']!=1:raise StateError('real_page_one_grant_only')
        expected=request_binding(self.drive.page,rid,AI_CONSENT_ACTION)
        value.update(authority=new,actor_evidence={rid:{'actor':actor,'binding':expected,'explicit_consent':'general_receipt_and_gemini'}})
        validate_verified(value,self.drive.config,self.owner_sub)
        proposed=canonical(value)
        self.drive.fresh()
        self.drive.request(self.drive.config['authority_file'],put=proposed,tag=tag)
        check,_=self.drive.read(self.drive.config['authority_file'])
        if check!=proposed:raise StateError('real_page_authority_readback_mismatch')
