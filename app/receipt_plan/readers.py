"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from copy import deepcopy
import json,re
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from ..drive_run_state import StateError
from .identity import digest,page_key
from .completion import UUID,unit_identity
from .authority import validate_grant
from .items import validate,card,check_snapshot,evaluate
from .wire import read_snapshot
from .proof import binding,ConfirmedItems,ACTION
from .fields import original_uri
from .drive import RealPageDrive,validate_config,validate_verified,SCHEMA,ISSUER
SID='1G44cDDUryVpZazTDwuCT4eZrir5KJb2WVm9baHTRPow'
def validate_config_review(config):
    if set(config)!={'drive','candidate_file','candidate_digest','journal_file','scope'}:
        raise StateError('item_confirmation_config_invalid')
    page=validate_config(config['drive'])
    ids=[config['candidate_file'],config['journal_file']]
    if (page.page_number!=14 or len(set(ids))!=2 or any(not re.fullmatch('[A-Za-z0-9_-]{10,150}',x) for x in ids)
        or set(ids)&{page.source.source_file_id,config['drive']['authority_file'],config['drive']['folder']}
        or not re.fullmatch('[a-f0-9]{64}',config['candidate_digest'])
        or config['scope']!=digest(['receipt-item-confirmation-live-v1',config['drive']['binding'],*ids,config['candidate_digest']])):
        raise StateError('item_confirmation_config_invalid')
    return config

def validate_journal(value,scope):
    if set(value)!={'schema','scope','requests','generation'} or value['schema']!='receipt-item-confirmations-v1' or value['scope']!=scope:
        raise StateError('item_confirmation_journal_invalid')
    if (not isinstance(value['requests'],dict) or len(value['requests'])>100 or type(value['generation']) is not int
            or value['generation']!=len(value['requests'])):
        raise StateError('item_confirmation_journal_invalid')
    for rid,r in value['requests'].items():
        if not isinstance(rid,str) or not UUID.fullmatch(rid):raise StateError('item_confirmation_journal_invalid')
        if set(r)!={'binding','proof','snapshot','plan','status'} or r['status']!='validated_not_written':
            raise StateError('item_confirmation_journal_invalid')
        if (r['proof']['request_id']!=rid or r['proof']['snapshot_digest']!=digest(r['snapshot'])
                or r['proof']['input_digest']!=digest(r['plan']['input'])
                or r['binding']['request_id']!=rid or r['binding']['snapshot_digest']!=digest(r['snapshot'])):
            raise StateError('item_confirmation_journal_invalid')
        proof=ConfirmedItems(**r['proof'])
        if (r['binding']['requested_action']!=ACTION or proof.identity_digest!=digest(r['binding']['identity'])
                or proof.candidate_digest!=r['binding']['candidate_digest']
                or type(proof.verified_at) is not int or proof.verified_at<=0
                or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v) for v in
                    (proof.candidate_digest,proof.snapshot_digest,proof.input_digest,proof.identity_digest,proof.actor_id,proof.authority_digest))
                or proof.authority_digest!=digest([r['binding'],proof.actor_id,proof.verified_at])):
            raise StateError('item_confirmation_journal_invalid')
    return value

class Readers:
    def __init__(self,config,info,owner_sub=None,*,owner_actor_id=None):
        self.config=validate_config_review(config);self.owner_sub=owner_sub
        self.owner_actor_id=owner_actor_id or digest([ISSUER,owner_sub])
        self.drive=RealPageDrive(config['drive'],info)
        original=self.drive.request
        def get(fid,**kw):
            if kw.get('put') is not None:raise StateError('item_confirmation_hga_write_forbidden')
            return original(fid,**kw)
        self.drive.request=get
        own=deepcopy(config['drive']);own['authority_file']=config['journal_file']
        own['binding']=digest([SCHEMA,own['folder'],own['authority_file'],own['page']['source'],14,own['page']['review_identity']])
        self.private=RealPageDrive(own,info);self.private.allowed.add(config['candidate_file'])
        self.sheets=AuthorizedSession(service_account.Credentials.from_service_account_info(info,
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']))
        self.cache=None
        begin,end=self.drive.begin_request,self.drive.end_request
        def begin_request():self.cache=None;begin()
        def end_request():self.cache=None;end()
        self.drive.begin_request=begin_request;self.drive.end_request=end_request

    def sheet_rows(self,region,*,render='UNFORMATTED_VALUE'):
        response=self.sheets.get('https://sheets.googleapis.com/v4/spreadsheets/'+SID+'/values/'+region,
            params={'valueRenderOption':render,'dateTimeRenderOption':'SERIAL_NUMBER'},timeout=10,allow_redirects=False)
        if response.status_code!=200:raise StateError('item_confirmation_sheet_unavailable')
        return response.json().get('values',[])

    def read(self):
        # HTTP-local observation only; discarded on teardown and explicitly
        # refreshed immediately before the conditional private-state write.
        if self.cache is not None:return deepcopy(self.cache)
        self.drive.acl();page=self.drive.fresh();self.private.acl()
        raw,_=self.private.read(self.config['candidate_file']);payload=json.loads(raw)
        acl=self.private.request(self.config['candidate_file'],fields='owners(emailAddress),permissions(type,role,emailAddress,deleted)')
        owners=[o.get('emailAddress') for o in acl.get('owners',[])]
        allowed={(p.get('type'),p.get('role'),p.get('emailAddress')) for p in acl.get('permissions',[]) if not p.get('deleted')}
        if len(owners)!=1 or digest(owners[0])!=self.config['drive']['owner_digest'] or allowed!={('user','owner',owners[0]),('user','writer',self.private.sa)}:
            raise StateError('item_confirmation_candidate_acl_mismatch')
        if set(payload)!={'record','view','manifest','retention'}:raise StateError('item_confirmation_candidate_invalid')
        record=validate(payload['record'])
        if record['digest']!=self.config['candidate_digest'] or unit_identity(page,payload['manifest'],record['legacy']['identity']['receipt_unit_id'])!=record['legacy']['identity']:
            raise StateError('item_confirmation_unit_stale')
        raw_grants=json.loads(self.drive.read(self.config['drive']['authority_file'])[0])
        actors=[e['actor'] for e in raw_grants.get('actor_evidence',{}).values()]
        if len(actors)!=1 or actors[0]['actor_id']!=self.owner_actor_id:raise StateError('item_confirmation_hga_actor_stale')
        grants=validate_verified(raw_grants,self.config['drive'],self.owner_sub or actors[0]['subject'])
        grant=grants['authority']['grants'].get(page_key(page));validate_grant(grant,page)
        # Existing category master, never private embedded category values.
        categories=self.sheet_rows("'カテゴリ'!A1:B1000")
        categories=[tuple(r[:2]) for r in categories[1:] if len(r)>=2 and all(isinstance(v,str) and v for v in r[:2])]
        if not categories:raise StateError('item_confirmation_category_master_unavailable')
        rows=self.sheet_rows("'PDFページ確認'!A1:W1000")
        if len(rows)>=1000:raise StateError('item_confirmation_sheet_truncated')
        snap=read_snapshot(rows,payload['view'],original_uri(page.source.source_file_id,14))
        self.cache=(record,snap,categories);return deepcopy(self.cache)

class Journal:
    def __init__(self,readers):self.readers=readers
    def read_versioned(self):
        raw,tag=self.readers.private.read(self.readers.config['journal_file'])
        validate_journal(json.loads(raw),self.readers.config['scope'])
        return raw,tag
