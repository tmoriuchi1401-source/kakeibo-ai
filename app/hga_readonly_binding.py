"""Private Drive metadata is authoritative; Actions receives only its file ID.

GET-only, existing service account, exact private ACL and strong read-back.
No token, image, Medical field or binding payload is persisted in GitHub.
"""
from hashlib import sha256
import json
import re
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from .drive_run_state import StateError
from .conditional_drive_state_v2 import strong_etag
from .page_receipt_model import digest
from .private_state_bindings import unwrap

SCHEMA='human-general-readonly-binding-v1'
MAX_BYTES=32768
FIELDS={'drive','actor_id','authority_sha256'}


def document(config):
    from services.human_general.real_page import validate_config
    if not isinstance(config,dict) or set(config)!=FIELDS:raise StateError('readonly_hga_binding_invalid')
    validate_config(config['drive'])
    if any(not re.fullmatch('[0-9a-f]{64}',config[k]) for k in ('actor_id','authority_sha256')):
        raise StateError('readonly_hga_binding_invalid')
    return {'schema':SCHEMA,'config':config,'digest':digest([SCHEMA,config])}


class PrivateBindingReader:
    def __init__(self,env,pem,info,*,session=None):
        fid=env.get('PDF_HGA_READONLY_BINDING_ID','')
        if not isinstance(fid,str) or not re.fullmatch('[A-Za-z0-9_-]{10,150}',fid):
            raise StateError('readonly_hga_reference_required')
        try:
            grouping=json.loads(env['PDF_GROUPING_BINDING'])
            self.folder=unwrap('KAKEIBO_STATE_FOLDER_ID',grouping['folder'],pem)
            self.owner=grouping['owner_digest']
            assert re.fullmatch('[0-9a-f]{64}',self.owner)
            assert re.fullmatch('[A-Za-z0-9_-]{10,150}',self.folder)
        except Exception:raise StateError('readonly_hga_reference_invalid') from None
        self.fid,self.sa=fid,info['client_email']
        self.http=session or AuthorizedSession(service_account.Credentials.from_service_account_info(
            info,scopes=['https://www.googleapis.com/auth/drive.readonly']))

    def get(self,fid,*,media=False):
        if fid not in {self.fid,self.folder}:raise StateError('readonly_hga_reference_invalid')
        params={'alt':'media'} if media else {'fields':'id,etag,parents(id),labels(trashed),mimeType,owners(emailAddress),permissions(type,role,emailAddress,deleted)'}
        try:
            response=self.http.request('GET','https://www.googleapis.com/drive/v2/files/'+fid,
                params=params,timeout=25,allow_redirects=False)
            if response.status_code!=200 or len(response.content)>MAX_BYTES:raise ValueError()
            return response.content if media else response.json()
        except Exception:raise StateError('readonly_hga_binding_unavailable') from None

    def metadata(self,fid):
        meta=self.get(fid)
        if meta.get('id')!=fid or meta.get('labels',{}).get('trashed') or not strong_etag(meta.get('etag')):
            raise StateError('readonly_hga_binding_metadata_invalid')
        owners=[o.get('emailAddress') for o in meta.get('owners',[])]
        grants={(p.get('type'),p.get('role'),p.get('emailAddress')) for p in meta.get('permissions',[]) if not p.get('deleted')}
        if len(owners)!=1 or digest(owners[0])!=self.owner or grants!={('user','owner',owners[0]),('user','writer',self.sa)}:
            raise StateError('readonly_hga_binding_acl_mismatch')
        if fid==self.folder:
            if meta.get('mimeType')!='application/vnd.google-apps.folder':raise StateError('readonly_hga_binding_metadata_invalid')
        elif meta.get('parents')!=[{'id':self.folder}] or meta.get('mimeType')!='application/json':
            raise StateError('readonly_hga_binding_metadata_invalid')
        return meta

    def read(self):
        self.metadata(self.folder)
        before=self.metadata(self.fid);raw=self.get(self.fid,media=True)
        if self.metadata(self.fid)!=before:raise StateError('readonly_hga_binding_changed')
        try:
            value=json.loads(raw)
            if set(value)!={'schema','config','digest'} or value!=document(value['config']):raise ValueError()
            config=value['config']
            if config['drive']['folder']!=self.folder or config['drive']['owner_digest']!=self.owner:
                raise ValueError()
        except Exception:raise StateError('readonly_hga_binding_invalid') from None
        return config,sha256(raw).hexdigest(),before['etag']
