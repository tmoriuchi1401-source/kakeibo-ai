"""Separate explicit single-page AI intent; legacy kind answers never migrate.

Uses existing Drive v2 If-Match transport/ACL preflight. No accounting, Medical,
source move, runtime provisioning or local-authority fallback.
"""
from copy import deepcopy
from datetime import datetime,timezone
import json,re
from hashlib import sha256
from .drive_run_state import StateError,_aware
from .conditional_drive_state_v2 import strong_etag
from .page_receipt_model import PageUnit,digest,page_key

SCHEMA='human-general-page-authority-v1'
FLAGS={'authority_scope':['human_general_receipt','single_page_ai'],
       'accounting_allowed':False,'medical_handoff_allowed':False,'archive_allowed':False}
UNKNOWN={'unknown','sensitive_unknown'}
REASONS={'insufficient_evidence','sensitive_signal_insufficient'}
MAX_BYTES=2*1024*1024

def review_identity(page):
    return digest(['page-general-ai-review-v1',page.source.model_dump(),page.page_number,
        page.stable_page_identity,page.automatic_classification,page.automatic_reason,
        page.human_page_kind,page.authority_revision,page.observation_complete,
        page.extraction_status,page.clearly_sensitive])

def eligible(page):
    return (page.automatic_classification in UNKNOWN and page.automatic_reason in REASONS
        and not page.clearly_sensitive and page.human_page_kind not in {'medical','payroll'}
        and page.observation_complete and page.extraction_status=='extracted'
        and page.review_identity==review_identity(page))

def binding_fields(page):
    return {**page.source.model_dump(),'page_number':page.page_number,
        'stable_page_identity':page.stable_page_identity,'review_identity':page.review_identity,
        'authority_revision':page.authority_revision,
        'automatic_classification':page.automatic_classification,'automatic_reason':page.automatic_reason,
        'human_page_kind':page.human_page_kind,'observation_complete':page.observation_complete,
        'extraction_status':page.extraction_status,'clearly_sensitive':page.clearly_sensitive}

def authority(page,actor,timestamp):
    if (not eligible(page) or not isinstance(actor,dict) or set(actor)!={'provider','subject'}
            or actor['provider']!='google' or not isinstance(actor['subject'],str)
            or not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,150}',actor['subject'])):
        raise StateError('human_general_verified_actor_and_unknown_review_required')
    _aware(timestamp)
    result={**binding_fields(page),**FLAGS,'confirmed_kind':'general_receipt',
        'confirmed_at':timestamp,'confirmer':deepcopy(actor)}
    result['confirmation_digest']=digest(result)
    return result

def validate_grant(record,page):
    try:
        expected=authority(page,record['confirmer'],record['confirmed_at'])
        if record!=expected:raise ValueError()
    except Exception:raise StateError('human_general_authority_stale') from None
    return deepcopy(record)

def empty_state(binding):
    if not re.fullmatch('[0-9a-f]{64}',binding):raise StateError('human_general_binding_invalid')
    return {'schema':SCHEMA,'binding':binding,'generation':0,'grants':{},'audit':[]}

def validate_state(value,binding):
    try:
        if (set(value)!={'schema','binding','generation','grants','audit'} or value['schema']!=SCHEMA
                or value['binding']!=binding or type(value['generation']) is not int or value['generation']<0
                or not isinstance(value['grants'],dict) or not isinstance(value['audit'],list)):
            raise ValueError()
        for key,record in value['grants'].items():
            source={k:record[k] for k in ('source_file_id','source_content_hash','page_count','source_kind')}
            page=PageUnit(source=source,page_number=record['page_number'],stable_page_identity=record['stable_page_identity'],
                automatic_classification=record['automatic_classification'],automatic_reason=record['automatic_reason'],
                observation_complete=record['observation_complete'],extraction_status=record['extraction_status'],
                clearly_sensitive=record['clearly_sensitive'],observation_render_hash='0'*64,
                review_identity=record['review_identity'],authority_revision=record['authority_revision'])
            page=PageUnit.model_validate({**page.model_dump(),'human_page_kind':record['human_page_kind']})
            if page_key(page)!=key:raise ValueError()
            validate_grant(record,page)
        for event in value['audit']:
            if set(event)!={'operation','timestamp','source_identity','before_revision','after_revision',
                            'review_identity','confirmation_digest','request_id','result'}:raise ValueError()
            _aware(event['timestamp'])
            if (event['operation'],event['result']) not in {('confirm_general_receipt_ai','confirmed'),('hold_general_receipt_ai','held')}:raise ValueError()
            for name in ('source_identity','review_identity','confirmation_digest'):
                if not re.fullmatch('[0-9a-f]{64}',event[name]):raise ValueError()
            if (type(event['before_revision']) is not int or type(event['after_revision']) is not int
                    or not 0<=event['before_revision']<=event['after_revision']
                    or (event['result']=='confirmed' and event['after_revision']<1)
                    or (event['result']=='held' and event['after_revision']!=event['before_revision'])
                    or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',event['request_id'])):
                raise ValueError()
        if value['generation']!=len(value['audit']):raise ValueError()
        latest={};requests=set()
        for event in value['audit']:
            key=event['source_identity']
            if event['before_revision']!=(latest[key]['after_revision'] if key in latest else 0):raise ValueError()
            if event['request_id'] in requests:raise ValueError()
            requests.add(event['request_id']);latest[key]=event
        if {key for key,event in latest.items() if event['result']=='confirmed'}!=set(value['grants']):raise ValueError()
        for key,grant in value['grants'].items():
            event=latest[key]
            if (event['after_revision']!=grant['authority_revision'] or event['timestamp']!=grant['confirmed_at']
                    or event['confirmation_digest']!=grant['confirmation_digest']
                    or event['review_identity']!=grant['review_identity']):raise ValueError()
    except Exception:raise StateError('human_general_state_invalid') from None
    return value

class HumanGeneralAuthorityStore:
    def __init__(self,transport,binding,*,preflight,validator=None):
        self.transport,self.binding,self.preflight=transport,binding,preflight
        self.validate=validator or validate_state
        self.payload=self.tag=None
    def load(self):
        try:
            self.preflight();payload,tag=self.transport.read_versioned()
            if type(payload) is not bytes or len(payload)>MAX_BYTES or not strong_etag(tag):raise ValueError()
            def unique(pairs):
                out={}
                for k,v in pairs:
                    if k in out:raise ValueError()
                    out[k]=v
                return out
            value=self.validate(json.loads(payload,object_pairs_hook=unique),self.binding)
            self.payload,self.tag=payload,tag
            return deepcopy(value)
        except Exception:raise StateError('human_general_state_unavailable') from None
    def save(self,value):
        self.validate(value,self.binding)
        payload=json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode()
        if self.payload is None or len(payload)>MAX_BYTES:raise StateError('human_general_state_unavailable')
        self.transport.replace_versioned(self.payload,self.tag,payload) # never retry 412
        check,tag=self.transport.read_versioned()
        if check!=payload or not strong_etag(tag):raise StateError('human_general_readback_mismatch')
        self.payload,self.tag=check,tag

class HumanGeneralConfirmation:
    def __init__(self,store,current_page,verified_actor,*,load_source,clock=None):
        self.store,self.current_page,self.verified_actor=store,current_page,verified_actor
        self.load_source=load_source # trusted current Drive bytes, never cache/Sheet
        self.clock=clock or (lambda:datetime.now(timezone.utc).isoformat(timespec='seconds'))
    def fresh_source(self,page):
        raw=self.load_source(page.source.source_file_id)
        if type(raw) is not bytes or sha256(raw).hexdigest()!=page.source.source_content_hash:
            raise StateError('human_general_source_changed')
    def confirm(self,expected,*,operation,request_id):
        if operation!='confirm_general_receipt_ai' or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',request_id):
            raise StateError('human_general_explicit_operation_required')
        current=self.current_page(expected.source.source_file_id,expected.page_number)
        if binding_fields(current)!=binding_fields(expected):raise StateError('stale_proposal')
        self.fresh_source(current)
        if not eligible(current):raise StateError('human_general_kind_forbidden')
        # verified_actor is the trusted auth/event adapter, never a Sheet cell.
        actor=self.verified_actor(request_id)
        timestamp=self.clock();grant=authority(current,actor,timestamp)
        state=self.store.load();old=state['grants'].get(page_key(current))
        if old:
            try:return validate_grant(old,current) # replay: unchanged time/revision/digest
            except StateError:
                if old['authority_revision']>=current.authority_revision:
                    raise StateError('human_general_revision_conflict') from None
        if any(e['request_id']==request_id for e in state['audit']):
            raise StateError('human_general_request_replaced')
        events=[e for e in state['audit'] if e['source_identity']==page_key(current)]
        before=events[-1]['after_revision'] if events else 0
        if current.authority_revision<before:raise StateError('human_general_revision_conflict')
        state['grants'][page_key(current)]=grant;state['generation']+=1
        state['audit'].append({'operation':operation,'timestamp':timestamp,'source_identity':page_key(current),
            'before_revision':before,'after_revision':current.authority_revision,'review_identity':current.review_identity,
            'confirmation_digest':grant['confirmation_digest'],'request_id':request_id,'result':'confirmed'})
        again=self.current_page(expected.source.source_file_id,expected.page_number)
        if binding_fields(again)!=binding_fields(current):raise StateError('stale_proposal')
        self.fresh_source(again)
        self.store.save(state)
        return validate_grant(self.store.load()['grants'][page_key(current)],current)
    def current(self,page):
        now=self.current_page(page.source.source_file_id,page.page_number)
        if binding_fields(now)!=binding_fields(page):raise StateError('stale_proposal')
        self.fresh_source(now)
        grant=self.store.load()['grants'].get(page_key(page))
        if not grant:raise StateError('human_general_authority_missing')
        return validate_grant(grant,page)
    def hold(self,expected,*,request_id):
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',request_id):
            raise StateError('human_general_explicit_operation_required')
        current=self.current_page(expected.source.source_file_id,expected.page_number)
        if binding_fields(current)!=binding_fields(expected):raise StateError('stale_proposal')
        self.fresh_source(current)
        state=self.store.load();key=page_key(current)
        prior=next((e for e in state['audit'] if e['request_id']==request_id),None)
        if prior:
            if prior['operation']!='hold_general_receipt_ai' or prior['source_identity']!=key:
                raise StateError('human_general_request_replaced')
            if key in state['grants']:raise StateError('human_general_hold_replaced')
            return None
        timestamp=self.clock();authority(current,self.verified_actor(request_id),timestamp)
        old=state['grants'].pop(key,None)
        events=[e for e in state['audit'] if e['source_identity']==key]
        before=events[-1]['after_revision'] if events else 0
        state['audit'].append({'operation':'hold_general_receipt_ai','timestamp':timestamp,'source_identity':key,
            'before_revision':before,'after_revision':before,'review_identity':current.review_identity,
            'confirmation_digest':old['confirmation_digest'] if old else '0'*64,'request_id':request_id,'result':'held'})
        state['generation']+=1
        if binding_fields(self.current_page(expected.source.source_file_id,expected.page_number))!=binding_fields(current):
            raise StateError('stale_proposal')
        self.fresh_source(current)
        self.store.save(state)
        if key in self.store.load()['grants']:raise StateError('human_general_hold_readback_mismatch')
        return None

def legacy_migration_allowed(_legacy_answer):
    # page_kind_only/gemini_allowed=false is never external-AI consent.
    return False
