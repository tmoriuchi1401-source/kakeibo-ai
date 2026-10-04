"""Versioned, source-ordinal identity; legacy human intent remains verifiable.

No AI, writer, source mover or Medical dependency. A separate Drive file holds
v2; the original v1 file and binding are retained and read on every load.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import re

import pypdfium2 as pdfium

from .drive_run_state import StateError, _aware
from .pdf_grouping_authority import validate as validate_v1, encoded, MAX_STATE_BYTES
from .pdf_page_kind import current_answer, kind_key
from .receipt_pdf_units import _digest, MAX_SOURCE_BYTES
from .receipt_pdf_grouping import privacy_for

SCHEMA='pdf-grouping-authority-v2'
MIGRATION='source-ordinal-from-render-v1'
FLAGS={'authority_scope':['grouping_confirmed','rendered_payload_only'],
       'gemini_allowed':False,'accounting_allowed':False,'medical_handoff_allowed':False,'archive_allowed':False}


def page_identity(source_hash,number,count):
    if (not isinstance(source_hash,str) or not re.fullmatch(r'[0-9a-f]{64}',source_hash)
            or type(number) is not int or type(count) is not int or not 1<=number<=count<=50):
        raise StateError('pdf_page_identity_invalid')
    return _digest(['pdf-page-v1',source_hash,number,count])


def state_name(legacy_binding):
    if not re.fullmatch(r'[0-9a-f]{64}',legacy_binding):raise StateError('grouping_v2_binding_invalid')
    return SCHEMA+'-'+legacy_binding+'.json'


def discover(drive,folder,legacy_binding):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',folder):raise StateError('grouping_v2_binding_invalid')
    name=state_name(legacy_binding)
    result=drive.files().list(q=f"'{folder}' in parents and name = '{name}' and trashed = false",
        spaces='drive',pageSize=100,fields='files(id,name,mimeType,parents),nextPageToken',
        supportsAllDrives=True,includeItemsFromAllDrives=True).execute(num_retries=0)
    files=result.get('files',[])
    if (result.get('nextPageToken') or len(files)!=1 or files[0].get('name')!=name
            or files[0].get('mimeType')!='application/json' or files[0].get('parents')!=[folder]):
        raise StateError('grouping_v2_state_missing_or_ambiguous')
    return files[0]['id']


def legacy_scope(value,source_id):
    key=_digest(source_id);record=value['records'].get(key)
    if not record or record['status']!='grouping_confirmed' or not record['confirmation']:
        raise StateError('grouping_v2_legacy_unconfirmed')
    p=record['proposal']
    answers={kind_key(source_id,page['page_number']):current_answer(value,page) for page in p['pages']}
    if not all(answers.values()):raise StateError('grouping_v2_kind_intent_missing')
    # Keep the minimal, independently valid legacy authority evidence. No raw
    # OCR, images, merchant, amount or unrelated audit/source records are copied.
    return {'schema':'pdf-grouping-authority-v1','binding':value['binding'],'generation':0,
        'records':{key:deepcopy(record)},'page_kinds':answers,'audit':[]}


def _assemble(snapshot,*,binding,legacy_file_id,legacy_bytes_digest,migrated_at):
    key,old=next(iter(snapshot['records'].items()));p=old['proposal'];a=old['confirmation']
    sid=p['source_file_id'];count=p['page_count'];pages=[];kinds={}
    for page in p['pages']:
        answer=current_answer(snapshot,page);n=page['page_number']
        identity=page_identity(p['source_content_hash'],n,count)
        kind={**FLAGS,'authority_scope':['page_kind_only'],'source_file_id':sid,
            'source_content_hash':p['source_content_hash'],'page_number':n,'page_count':count,
            'page_identity':identity,'automatic_classification':page['classification'],
            'human_classification':answer['human_classification'],'confirmed_at':answer['confirmed_at']}
        kind['confirmation_digest']=_digest(kind)
        kinds[kind_key(sid,n)]=kind
        pages.append({'source_file_id':sid,'source_content_hash':p['source_content_hash'],
            'page_number':n,'page_count':count,'page_identity':identity,
            'automatic_classification':page['classification'],'human_classification':answer['human_classification'],
            'extraction_status':page['extraction_status'],'reason_code':page['reason_code'],
            'observation_render_hash':page['page_hash'],
            'observation_metadata':{k:page[k] for k in ('effective_render_scale','observation_complete','render_attempts') if k in page},
            'human_confirmation_digest':kind['confirmation_digest']})
    groups=[]
    for group in p['groups']:
        ns=group['page_numbers'];members=[pages[n-1] for n in ns]
        groups.append({'page_numbers':ns,'member_page_identities':[x['page_identity'] for x in members],
            'page_classifications':[privacy_for([x['automatic_classification'],x['human_classification']]) for x in members]})
    proposal={'source_file_id':sid,'source_content_hash':p['source_content_hash'],'page_count':count,
        'grouping_revision':old['revision'],'groups':groups,'pages':pages}
    # Diagnostic render bytes/scale/compression do not enter authority identity.
    identity_projection={k:proposal[k] for k in ('source_file_id','source_content_hash','page_count','grouping_revision','groups')}
    identity_projection['pages']=[{k:page[k] for k in ('page_number','page_identity','automatic_classification',
        'human_classification','human_confirmation_digest')} for page in pages]
    proposal['proposal_digest']=_digest(['pdf-grouping-proposal-v2',identity_projection])
    confirmation={**FLAGS,'source_file_id':sid,'source_content_hash':p['source_content_hash'],
        'page_count':count,'grouping_revision':old['revision'],'proposal_digest':proposal['proposal_digest'],
        'confirmed_partition':a['confirmed_partition'],'confirmed_at':a['confirmed_at']}
    confirmation['confirmation_digest']=_digest(['pdf-grouping-confirmation-v2',confirmation])
    provenance={'version':MIGRATION,'migrated_at':migrated_at,'legacy_file_id':legacy_file_id,
        'legacy_binding':snapshot['binding'],'legacy_state_bytes_digest':legacy_bytes_digest,
        'legacy_confirmation_digest':a['confirmation_digest'],'legacy_proposal_digest':p['proposal_digest'],
        'legacy_confirmed_at':a['confirmed_at'],'legacy_authority_snapshot':snapshot}
    event={'operation':'migrate_identity','timestamp':migrated_at,'source_file_id':sid,
        'source_content_hash':p['source_content_hash'],'before_revision':old['revision'],'after_revision':old['revision'],
        'proposal_digest':proposal['proposal_digest'],'confirmation_digest':confirmation['confirmation_digest'],
        'legacy_confirmation_digest':a['confirmation_digest'],'result':'migrated_existing_human_intent'}
    return {'schema':SCHEMA,'binding':binding,'migration':provenance,
        'records':{key:{'proposal':proposal,'confirmation':confirmation,'revision':old['revision'],'status':'grouping_confirmed'}},
        'page_kinds':kinds,'audit':[event]}


def migrate(legacy,legacy_bytes,source_id,content,*,binding,legacy_file_id,expected_confirmation,
            expected_partition,expected_human_kinds,migrated_at):
    validate_v1(legacy,legacy['binding']);_aware(migrated_at)
    if not isinstance(content,bytes) or len(content)>MAX_SOURCE_BYTES:raise StateError('grouping_v2_source_changed')
    snapshot=legacy_scope(legacy,source_id)
    old=snapshot['records'][_digest(source_id)];p=old['proposal'];a=old['confirmation']
    if sha256(content).hexdigest()!=p['source_content_hash']:raise StateError('grouping_v2_source_changed')
    if (a['confirmation_digest']!=expected_confirmation or a['confirmed_partition']!=expected_partition or
            [current_answer(snapshot,page)['human_classification'] for page in p['pages']]!=expected_human_kinds or
            [page['page_number'] for page in p['pages']]!=list(range(1,len(expected_human_kinds)+1))):
        raise StateError('grouping_v2_human_intent_changed')
    try:
        with closing(pdfium.PdfDocument(content)) as document:
            if len(document)!=p['page_count']:raise StateError('grouping_v2_page_structure_changed')
    except StateError:raise
    except Exception:raise StateError('grouping_v2_source_invalid') from None
    if json.loads(legacy_bytes)!=legacy:raise StateError('grouping_v2_legacy_snapshot_changed')
    return _assemble(snapshot,binding=binding,legacy_file_id=legacy_file_id,
        legacy_bytes_digest=sha256(legacy_bytes).hexdigest(),migrated_at=migrated_at)


def validate(value,binding):
    try:
        if set(value)!= {'schema','binding','migration','records','page_kinds','audit'} or value['schema']!=SCHEMA or value['binding']!=binding:
            raise ValueError()
        m=value['migration'];_aware(m['migrated_at'])
        if (m['version']!=MIGRATION or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',m['legacy_file_id'])
                or not re.fullmatch(r'[0-9a-f]{64}',m['legacy_state_bytes_digest'])):raise ValueError()
        old=validate_v1(m['legacy_authority_snapshot'],m['legacy_binding'])
        if len(old['records'])!=1 or old['audit'] or old['generation']!=0:raise ValueError()
        expected=_assemble(old,binding=binding,legacy_file_id=m['legacy_file_id'],
            legacy_bytes_digest=m['legacy_state_bytes_digest'],migrated_at=m['migrated_at'])
        if value!=expected:raise ValueError()
        return value
    except Exception:raise StateError('grouping_v2_state_invalid') from None


def validate_current(value,binding,legacy,legacy_bytes,legacy_file_id):
    validate(value,binding);validate_v1(legacy,value['migration']['legacy_binding'])
    m=value['migration'];sid=next(iter(value['records'].values()))['proposal']['source_file_id']
    # The whole-file digest records migration provenance, not freshness of
    # unrelated PDFs. The exact target source's complete legacy intent remains
    # the authority link. Adding another proposal/audit must not revoke it.
    # Still bind the parsed current state to the bytes actually read from Drive.
    try:
        def unique(pairs):
            result={}
            for key,item in pairs:
                if key in result:raise ValueError()
                result[key]=item
            return result
        current=json.loads(legacy_bytes,object_pairs_hook=unique)
    except Exception:raise StateError('grouping_v2_legacy_intent_stale') from None
    if (m['legacy_file_id']!=legacy_file_id or current!=legacy
            or legacy_scope(legacy,sid)!=m['legacy_authority_snapshot']):
        raise StateError('grouping_v2_legacy_intent_stale')
    return value


@dataclass(frozen=True)
class ConfirmedDocumentUnitV2:
    source_file_id:str
    source_content_hash:str
    page_numbers:tuple
    member_page_identities:tuple
    page_classifications:tuple
    grouping_version:int
    proposal_digest:str

    @property
    def unit_id(self):
        return 'pdf-confirmed-unit-v2:'+_digest(['pdf-unit-v2',self.source_file_id,self.source_content_hash,
            self.page_numbers,self.member_page_identities,self.grouping_version,self.proposal_digest])

    @property
    def classification(self):return privacy_for(self.page_classifications)


def units(value):
    result=[]
    for record in value['records'].values():
        p=record['proposal']
        for g in p['groups']:
            result.append(ConfirmedDocumentUnitV2(p['source_file_id'],p['source_content_hash'],tuple(g['page_numbers']),
                tuple(g['member_page_identities']),tuple(g['page_classifications']),record['revision'],p['proposal_digest']))
    return result


class DriveGroupingV2Store:
    """Load-only facade; legacy+v2 ACL, strong ETag and identity checked each load."""
    def __init__(self,transport,binding,legacy_store,legacy_file_id,*,preflight):
        self.transport,self.binding=transport,binding
        self.legacy_store,self.legacy_file_id=legacy_store,legacy_file_id
        self.preflight=preflight;self.payload=self.tag=None

    def load(self):
        return self.load_for(None)

    def load_for(self,source_id):
        """An unrelated PDF does not inherit this single-source migration.

        Always check both private ACLs, schemas and the migration binding. The
        migrated source also requires its exact current legacy human intent;
        revoking that source cannot grant or revoke a different PDF's intent.
        """
        try:
            self.preflight();legacy=self.legacy_store.load()
            payload,tag=self.transport.read_versioned()
            from .conditional_drive_state_v2 import strong_etag
            if not isinstance(payload,bytes) or len(payload)>MAX_STATE_BYTES or not strong_etag(tag):raise ValueError()
            def unique(pairs):
                result={}
                for key,item in pairs:
                    if key in result:raise ValueError()
                    result[key]=item
                return result
            value=validate(json.loads(payload,object_pairs_hook=unique),self.binding)
            migration=value['migration']
            if (migration['legacy_file_id']!=self.legacy_file_id or
                    migration['legacy_binding']!=self.legacy_store.binding):raise ValueError()
            if source_id is None or _digest(source_id) in value['records']:
                validate_current(value,self.binding,legacy,self.legacy_store.payload,self.legacy_file_id)
            self.payload,self.tag=payload,tag
            return deepcopy(value)
        except StateError:raise
        except Exception:raise StateError('grouping_v2_state_unavailable') from None
