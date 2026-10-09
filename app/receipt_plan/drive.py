"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from hashlib import sha256
import json,time,re
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from ..drive_run_state import StateError
from .identity import PageUnit,page_key,digest
from .authority import validate_state,review_identity,eligible,binding_fields
SCHEMA='human-general-verified-drive-canary-v1'
SOURCE='1fcmMMGj86DLq54G0inD8LI_XSyQSPfTY'
HASH='ca1b8cba60addc14a364438d691c40164e250708103bce76a320f671629491bf'
ISSUER='https://accounts.google.com'
AI_CONSENT_ACTION='general_receipt_and_gemini_permission'
def request_binding(page,rid,action):
    if action!=AI_CONSENT_ACTION:raise StateError('plan_request_invalid')
    return {**binding_fields(page),'request_id':rid,'requested_action':action,'page_processing_status':page.processing_status}
def strong_etag(value):
    return isinstance(value,str) and bool(re.fullmatch(r'"[^"\r\n]+"',value))
def validate_config(config,*,allowed_pages=(14,)):
    try:
        if not allowed_pages or set(allowed_pages)-{4,10,14}:raise ValueError()
        if set(config)!={'page','folder','inbox','authority_file','baseline_files','owner_digest','binding'}:raise ValueError()
        page=PageUnit.model_validate(config['page'])
        if (page.source.source_file_id!=SOURCE or page.source.source_content_hash!=HASH or page.source.page_count!=14
                or page.page_number not in allowed_pages or page.authority_revision!=2 or page.processing_status!='observed'
                or page.automatic_classification!='sensitive_unknown' or page.automatic_reason!='privacy_unresolved'
                or not eligible(page) or page.review_identity!=review_identity(page)):raise ValueError()
        fixed={'1ju2rEDWrlpALr-9d4JEN9yaPfTuKtFCq','1atHszVu7J-OXPbJkhCMhsvhiyz6QEdsR',
               '1Gss6WvRvKbSWIKVSzrkApYkxVRFdQ6g_','1J6hjORzCDEauxg39o1fZbwRS41ROe3jE'}
        if set(config['baseline_files'])!=fixed or config['authority_file'] in fixed|{SOURCE,config['folder'],config['inbox']}:raise ValueError()
        import re
        if any(not re.fullmatch('[0-9a-f]{64}',v) for v in [config['owner_digest'],*config['baseline_files'].values()]):raise ValueError()
        for v in (config['folder'],config['inbox'],config['authority_file']):
            if not re.fullmatch('[A-Za-z0-9_-]{10,150}',v):raise ValueError()
        if config['binding']!=digest([SCHEMA,config['folder'],config['authority_file'],page.source.model_dump(),page.page_number,page.review_identity]):raise ValueError()
    except Exception:raise StateError('real_page_configuration_invalid') from None
    return page

def validate_verified(value,config,owner_sub,*,allowed_pages=(14,)):
    try:
        page=validate_config(config,allowed_pages=allowed_pages);binding=config['binding']
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
                    or evidence['explicit_consent']!=AI_CONSENT_ACTION
                    or actor['issuer']!=ISSUER or actor['subject']!=owner_sub
                    or actor['actor_id']!=digest([ISSUER,owner_sub]) or actor['method']!='google_oidc_code_pkce_v1'
                    or actor['request_id']!=event['request_id'] or actor['request_digest']!=digest(expected)
                    or actor['verification_revision']!=1 or actor['policy_revision']!=1
                    or state['grants'][page_key(page)]['confirmer']!={'provider':'google','subject':actor['email']}):raise ValueError()
    except Exception:raise StateError('real_page_verified_state_invalid') from None
    return value

class RealPageDrive:
    """Existing SA, read-only Drive scope. Exact IDs; no write API."""
    def __init__(self,config,info,*,session=None,allowed_pages=(14,)):
        self.page=validate_config(config,allowed_pages=allowed_pages);self.config=config;self.sa=info['client_email']
        auth=service_account.Credentials.from_service_account_info(info,scopes=['https://www.googleapis.com/auth/drive.readonly'])
        self.http=session or AuthorizedSession(auth)
        self.allowed={SOURCE,config['folder'],config['authority_file'],*config['baseline_files']}
        self._request_cache=None
        self._acl_checked=False
        self._deadline=None

    def begin_request(self):
        # HTTP-local only. Never share source bytes, ACL or authority across requests.
        self._request_cache={};self._acl_checked=False
        self._deadline=time.monotonic()+45

    def end_request(self):
        self._request_cache=None;self._acl_checked=False;self._deadline=None


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
        # Only immutable evidence within this request, never the authority store.
        reusable=fid==SOURCE or fid in self.config['baseline_files']
        if reusable and self._request_cache is not None and fid in self._request_cache:
            return self._request_cache[fid]
        before=self.metadata(fid);payload=self.request(fid,media=True);after=self.metadata(fid)
        if before!=after:raise StateError('real_page_changed_during_read')
        result=(payload,before['etag'])
        if reusable and self._request_cache is not None:self._request_cache[fid]=result
        return result

    def acl(self):
        if self._request_cache is not None and self._acl_checked:return
        for fid in (self.config['folder'],self.config['authority_file']):
            value=self.request(fid,fields='id,owners(emailAddress),permissions(type,role,emailAddress,deleted)')
            owners=[o.get('emailAddress') for o in value.get('owners',[])]
            if len(owners)!=1 or digest(owners[0])!=self.config['owner_digest']:raise StateError('real_page_acl_mismatch')
            grants={(p.get('type'),p.get('role'),p.get('emailAddress')) for p in value.get('permissions',[]) if not p.get('deleted')}
            if grants!={('user','owner',owners[0]),('user','writer',self.sa)}:raise StateError('real_page_acl_mismatch')
        if self._request_cache is not None:self._acl_checked=True


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
    def request(self,fid,*,media=False,fields=None):
        if fid not in self.allowed:raise StateError('real_page_drive_target_forbidden')
        remaining=25 if self._deadline is None else min(25,self._deadline-time.monotonic())
        if remaining<=0:raise StateError('real_page_request_budget_exceeded')
        response=self.http.get('https://www.googleapis.com/drive/v2/files/'+fid,
            params={'alt':'media'} if media else {'fields':fields},timeout=remaining,allow_redirects=False)
        if response.status_code!=200 or len(response.content)>100*1024*1024:raise StateError('real_page_drive_unavailable')
        return response.content if media else response.json()
