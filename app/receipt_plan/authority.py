"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from copy import deepcopy
import re
from ..drive_run_state import StateError,_aware
from .identity import digest,page_key,PageUnit
SCHEMA='human-general-page-authority-v1'
FLAGS={'authority_scope':['human_general_receipt','single_page_ai'],'accounting_allowed':False,'medical_handoff_allowed':False,'archive_allowed':False}
UNKNOWN={'unknown','sensitive_unknown'}
REASONS={'insufficient_evidence','sensitive_signal_insufficient','privacy_unresolved'}
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
