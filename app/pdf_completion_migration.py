"""Adopt posted canary intents from Drive, never local diagnostics/artifacts.

Read-only legacy state + fresh grouping/source + exact accounting read-back.
Only the new completion journal may change; no accounting/legacy-state writer,
AI, Medical or source mover is available. Existing Unit IDs and rows survive.
"""
from copy import deepcopy
import json
import re
from datetime import datetime

from .conditional_drive_state_v2 import ConditionalDriveStateTransportV2,strong_etag
from .drive_run_state import StateError
from .pdf_production_authority import DrivePdfAuthority
from .pdf_receipt_materialization import readback
from .pdf_unit_processing import DriveUnitProcessingStore,digest,_spec,_route
from .receipt_validation import POLICY_VERSION
from .receipt_reimport_production import digest as legacy_digest


def _json(payload):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError()
            result[key]=value
        return result
    return json.loads(payload,object_pairs_hook=unique)


def adopt_canary_intents(transport,preflight,authority,completion,db,source_id):
    if (not isinstance(transport,ConditionalDriveStateTransportV2) or not callable(preflight)
            or not isinstance(authority,DrivePdfAuthority) or authority.migrated is None
            or not isinstance(completion,DriveUnitProcessingStore)):
        raise StateError('pdf_migration_drive_proof_required')
    preflight();payload,tag=transport.read_versioned()
    try:
        if not strong_etag(tag):raise ValueError()
        value=_json(payload);m=value['manifest']
        current=authority.current(source_id)
        if (set(value)!= {'schema','manifest','records'} or value['schema']!='pdf-receipt-write-canary-v1'
                or not isinstance(value['records'],dict) or m['source_file_id']!=source_id
                or set(m)!={'source_file_id','source_content_hash','confirmation_digest','grouping_revision',
                    'authority_binding','policy_version','scope','archive_allowed','medical_handoff_allowed',
                    'unit_ids','page_identities'}
                or m['source_content_hash']!=current.source_content_hash
                or m['authority_binding']!=authority.migrated.binding
                or m['scope']!='operator_receipt_write_canary' or m['archive_allowed'] is not False
                or m['medical_handoff_allowed'] is not False or m['policy_version']!=POLICY_VERSION):
            raise ValueError()
        if type(m['grouping_revision']) is not int:raise ValueError()
        units={s['unit_id']:s for s in current.units};records=[]
        for key,old in value['records'].items():
            s=units[key];_spec(s);number=old['page_number']
            fields={'unit_id','page_number','phase','policy_version','timestamp','parsed_digest',
                    'proof','payload_sha256','plan','plan_digest'}
            if not fields<=set(old) or set(old)-fields-{'duplicate_comparison'}:raise ValueError()
            datetime.strptime(old['timestamp'],'%Y-%m-%d %H:%M:%S')
            proof={'unit_id':key,'page_identity':s['member_page_identities'][0],
                'page_number':number,'source_content_hash':s['source_content_hash'],
                'confirmation_digest':s['confirmation_digest'],'grouping_revision':s['grouping_revision'],
                'human_classification':'normal','effective_classification':'normal'}
            if (s['page_numbers']!=[number] or old['unit_id']!=key or old['phase']!='applied'
                    or old['policy_version']!=POLICY_VERSION or old['proof']!=proof
                    or old['plan_digest']!=legacy_digest(old['plan'])
                    or m['grouping_revision']!=s['grouping_revision']
                    or m['confirmation_digest']!=s['confirmation_digest']
                    or m['unit_ids'].get(str(number))!=key
                    or m['page_identities'].get(str(number))!=s['member_page_identities'][0]
                    or not re.fullmatch('[0-9a-f]{64}',old['payload_sha256'])
                    or not re.fullmatch('[0-9a-f]{64}',old['parsed_digest'])):raise ValueError()
            plan={}
            for title,row in old['plan']:plan.setdefault(title,[]).append(deepcopy(row))
            _route(s,'receipt',plan,'f'*64)
            if (plan['レシート'][0][7]!=old['timestamp'] or
                    plan['取込データ'][0][1]!=old['timestamp']):raise ValueError()
            # parsed_digest uses compact UTF-8 JSON in the old canary, whereas
            # import[10] uses canonical_hash's spaced JSON. Neither is a source
            # PDF hash and the two must not be compared as equal fingerprints.
            # Duplicate comparison/proof remains in its original durable state;
            # the migration linkage includes the complete immutable old record.
            records.append((s,old,plan))
    except Exception:raise StateError('pdf_migration_canary_proof_invalid') from None
    # Validate every old record/row before making even the first journal claim.
    for s,old,plan in records:
        if authority.verify(s) is not True:raise StateError('pdf_migration_source_changed')
        readback(db,plan)
    def verify(spec):
        preflight()
        if transport.read_versioned()!=(payload,tag):raise StateError('pdf_migration_legacy_state_changed')
        return authority.verify(spec)
    adopted=[]
    for s,old,plan in records:
        reference=digest(['pdf-canary-completion-migration-v1',transport.binding.file_id,digest(old)])
        record,created=completion.reserve(s,'receipt',old['parsed_digest'],plan,reference,verify_current=verify)
        done=completion.complete(s['unit_id'],record['intent_digest'],verify_current=verify,
            verify_readback=lambda r:readback(db,r['planned_rows']))
        adopted.append({'page_number':old['page_number'],'unit_id':s['unit_id'],
            'status':done['phase'],'replayed':not created})
    return {'adopted':adopted,'accounting_writes':0,'legacy_state_writes':0,
            'gemini_calls':0,'medical_calls':0,'source_moves':0}
