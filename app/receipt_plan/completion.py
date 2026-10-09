"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from copy import deepcopy
import re
from ..drive_run_state import StateError
from ..receipt_reimport import _date
from .identity import LocatedReceipt,digest,page_key
from .authority import binding_fields
from .manifest import validate as validate_manifest,SCHEMA as MANIFEST_SCHEMA
SCHEMA='general-receipt-completion-v1'
FIELDS=('date','amount','category','merchant','payment','memo')
FLAGS={'accounting_allowed':False,'medical_handoff_allowed':False,'archive_allowed':False}
UUID=re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
def unit_identity(page, manifest, unit_id):
    # Check the established frozen separation, not the model's array index.
    validate_manifest({'schema':MANIFEST_SCHEMA,'binding':'0'*64,'generation':1,
        'pages':{page_key(page):manifest},'accounting_allowed':False},'0'*64)
    units=[u for u in manifest['units'] if u['receipt_unit_id']==unit_id]
    if len(units)!=1:raise StateError('completion_receipt_identity_stale')
    return {**binding_fields(page),'processing_status':page.processing_status,
        'receipt_unit_id':unit_id,'segmentation_digest':manifest['segmentation_digest'],
        'receipt_index':units[0]['receipt_index'],'bbox':units[0]['bbox']}

def validate_draft(record):
    copied=deepcopy(record)
    signature=copied.pop('candidate_digest',None)
    if (signature!=digest(copied) or record.get('schema')!=SCHEMA
            or set(record)-{'item_category_evidence'}!={'schema','identity','prefill','blank_reasons','parsed','hard_issues',
                              'provenance','mode','candidate_digest',*FLAGS}
            or any(record.get(k) is not False for k in FLAGS)
            or set(record.get('prefill',{}))!=set(FIELDS)
            or set(record.get('blank_reasons',{}))!=set(FIELDS)
            or set(record.get('provenance',{}))!=set(FIELDS)
            or record.get('mode')!=('manual' if record.get('parsed') is None else 'itemized')):
        raise StateError('completion_candidate_changed')
    if record['parsed'] is not None:
        LocatedReceipt(bbox=record['identity']['bbox'],receipt=record['parsed'])
    if 'item_category_evidence' in record:
        evidence=record['item_category_evidence']
        items=(record.get('parsed') or {}).get('items',[])
        if (not isinstance(evidence,list) or len(evidence)!=len(items) or
                any(set(e)!={'item_index','category','corroborated'} or e['item_index']!=i or
                    type(e['corroborated']) is not bool or not isinstance(e['category'],str) or
                    bool(e['category'])!=e['corroborated'] for i,e in enumerate(evidence,1))):
            raise StateError('completion_item_evidence_invalid')
    return record

def _normalized(field,value):
    if field=='date':return _date(value) or value
    if field=='amount':
        from ..receipt_reimport import _money
        number=_money(str(value).replace(',',''))
        return int(number) if number is not None else value
    return value
