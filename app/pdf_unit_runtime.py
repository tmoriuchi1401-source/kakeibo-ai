"""Existing hosted receipt entry's opt-in PDF dependencies, no provisioning.

Reuse the existing encrypted grouping binding, SA/scopes, private Drive folder
and projection journal. State discovery is exact and ambiguous/missing files
stop; no fallback to local manifests, no authority mutation or source mover.
The workflow/enable Variable is not installed by this module.
"""
from dataclasses import dataclass
import json
import os
import re
import subprocess
from urllib.parse import urlsplit

from .drive_run_state import StateError
from .conditional_drive_state_v2 import ConditionalDriveStateTransportV2, strong_etag
from .pdf_grouping_authority import DriveGroupingStore
from .pdf_grouping_authority_v2 import DriveGroupingV2Store, discover
from .pdf_production_authority import DrivePdfAuthority
from .pdf_unit_processing import DriveUnitProcessingStore, SCHEMA
from .pdf_unit_intake import PdfUnitIntake
from .pdf_unit_analysis import PdfUnitAnalyzer
from .receipt_pdf_units import _digest, MAX_SOURCE_BYTES, observe_pdf, is_single_page_pdf


@dataclass(frozen=True)
class PdfStateBinding:
    folder_id:str
    file_id:str


def enabled(env=None):
    return (os.environ if env is None else env).get('PDF_UNIT_PROCESSING_ENABLED')=='true'


def require_context(env):
    from .production_flow import verify_execution_boundary, REPO
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    verify_execution_boundary(env,head)
    if (not enabled(env) or env.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or env.get('RUNNER_OS')!='Linux' or env.get('GITHUB_EVENT_NAME') not in {'schedule','workflow_dispatch'}
            or env.get('GITHUB_WORKFLOW_REF')!='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main'
            or not re.fullmatch('[1-9][0-9]*',env.get('GITHUB_RUN_ID',''))
            or env.get('ACTIONS_STEP_DEBUG')=='true' or env.get('RUNNER_DEBUG')=='1'):
        raise StateError('pdf_production_context_required')


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError()
        result[key]=value
    return result


def private_acl(service,binding,owner_digest,sa_email):
    # Same owner+SA-only contract used by the existing grouping worker.
    for target in (binding.folder_id,binding.file_id):
        m=service.files().get(fileId=target,supportsAllDrives=True,
            fields='owners(emailAddress),permissions(type,role,emailAddress,deleted)').execute(num_retries=0)
        owners=[o.get('emailAddress') for o in m.get('owners',[])]
        if len(owners)!=1 or not isinstance(owners[0],str) or _digest(owners[0])!=owner_digest:
            raise StateError('pdf_private_permissions_mismatch')
        grants={(p.get('type'),p.get('role'),p.get('emailAddress')) for p in m.get('permissions',[]) if not p.get('deleted')}
        if grants!={('user','owner',owners[0]),('user','writer',sa_email)}:
            raise StateError('pdf_private_permissions_mismatch')


def processing_name(legacy_binding):
    if not re.fullmatch('[0-9a-f]{64}',legacy_binding):raise StateError('pdf_processing_binding_invalid')
    return SCHEMA+'-'+legacy_binding+'.json'


def discover_processing(service,folder,legacy_binding):
    if not re.fullmatch('[A-Za-z0-9_-]{10,150}',folder):raise StateError('pdf_processing_binding_invalid')
    name=processing_name(legacy_binding)
    value=service.files().list(q=f"'{folder}' in parents and name = '{name}' and trashed = false",
        spaces='drive',pageSize=100,fields='nextPageToken,files(id,name,mimeType,parents)',
        supportsAllDrives=True,includeItemsFromAllDrives=True).execute(num_retries=0)
    fs=value.get('files',[])
    if (value.get('nextPageToken') or len(fs)!=1 or fs[0].get('name')!=name
            or fs[0].get('mimeType')!='application/json' or fs[0].get('parents')!=[folder]
            or not re.fullmatch('[A-Za-z0-9_-]{10,150}',fs[0].get('id',''))):
        raise StateError('pdf_processing_state_missing_or_ambiguous')
    return fs[0]['id']


def http_fence(service,*,completion_id=None):
    original=service._http.request
    def request(uri,method='GET',**kwargs):
        if method.upper()!='GET':
            target=urlsplit(uri);headers=kwargs.get('headers',{})
            tag=next((v for k,v in headers.items() if k.lower()=='if-match'),None)
            if (completion_id is None or method.upper() not in {'PUT','PATCH'}
                    or target.scheme!='https' or target.hostname!='www.googleapis.com'
                    or target.username or target.password or target.port not in (None,443)
                    or target.path!='/upload/drive/v2/files/'+completion_id or not strong_etag(tag)):
                raise StateError('pdf_runtime_cloud_write_forbidden')
        return original(uri,method=method,**kwargs)
    service._http.request=request


def open_intake(settings,db,ai=None,*,apply=False,env=None):
    env=os.environ if env is None else env
    if not enabled(env):return None
    require_context(env)  # Before credential decoding or any Google/API call.
    from googleapiclient.discovery import build
    from .google_clients import credentials,READ_ONLY_SCOPES
    from .private_state_bindings import unwrap
    from .drive_receipts import normalize_folder_id
    from .gemini_ai import GeminiAI
    try:
        info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'],object_pairs_hook=_unique)
        config=json.loads(env['PDF_GROUPING_BINDING'],object_pairs_hook=_unique)
        folder=unwrap('KAKEIBO_STATE_FOLDER_ID',config['folder'],info['private_key'])
        legacy_id=unwrap('PDF_GROUPING_STATE_FILE_ID',config['file'],info['private_key'])
        owner=config['owner_digest'];sa=info['client_email']
        if (not re.fullmatch('[0-9a-f]{64}',owner) or not sa
                or settings.spreadsheet_id!=db.sid
                or env.get('KAKEIBO_STATE_FOLDER_ID') not in (None,'',folder)):
            raise ValueError()
    except Exception:raise StateError('pdf_runtime_binding_invalid') from None
    reader=build('drive','v3',credentials=credentials(READ_ONLY_SCOPES),cache_discovery=False)
    authority_v2=build('drive','v2',credentials=credentials(READ_ONLY_SCOPES),cache_discovery=False)
    for client in (reader,authority_v2):http_fence(client)
    old=PdfStateBinding(folder,legacy_id);legacy_binding=_digest([folder,legacy_id,settings.spreadsheet_id])
    acl=lambda binding:private_acl(reader,binding,owner,sa)
    legacy=DriveGroupingStore(ConditionalDriveStateTransportV2(authority_v2,old),legacy_binding,
        preflight=lambda:acl(old))
    legacy.load()
    migrated_id=discover(reader,folder,legacy_binding)
    migrated_binding=PdfStateBinding(folder,migrated_id)
    migrated=DriveGroupingV2Store(ConditionalDriveStateTransportV2(authority_v2,migrated_binding),
        _digest([folder,migrated_id,settings.spreadsheet_id]),legacy,legacy_id,
        preflight=lambda:acl(migrated_binding))
    # Validate the physical v2 schema/binding without making unrelated sources
    # inherit the single canary migration's human confirmation.
    migrated.load_for('pdf-runtime-binding-preflight')
    completion_id=discover_processing(reader,folder,legacy_binding)
    forbidden={legacy_id,migrated_id}|{env.get(k) for k in (
        'AMAZON_STATE_FILE_ID','BANK_STATE_FILE_ID','AUPAY_CARD_STATE_FILE_ID','KAKEIBO_RUN_LEDGER_FILE_ID')}
    if env.get('RECEIPT_CONFIRMATION_BINDING'):
        try:
            confirmation=json.loads(env['RECEIPT_CONFIRMATION_BINDING'],object_pairs_hook=_unique)
            forbidden.add(unwrap('RECEIPT_REIMPORT_FILE_ID',confirmation['file'],info['private_key']))
        except Exception:raise StateError('pdf_runtime_binding_invalid') from None
    if completion_id in forbidden:raise StateError('pdf_processing_store_must_be_separate')
    cb=PdfStateBinding(folder,completion_id)
    writer_v2=build('drive','v2',credentials=credentials() if apply else credentials(READ_ONLY_SCOPES),cache_discovery=False)
    http_fence(writer_v2,completion_id=completion_id if apply else None)
    completion=DriveUnitProcessingStore(ConditionalDriveStateTransportV2(writer_v2,cb),
        _digest([folder,completion_id,settings.spreadsheet_id]),preflight=lambda:acl(cb))
    completion.load()
    if apply:
        from .sheets import SheetsDB
        from .projection_store import ProjectionJournal,DriveProjectionStore
        from .projection_cache import RollingProjectionStore
        projection=db.projection_store() if isinstance(db,SheetsDB) else None
        if (not env.get('KAKEIBO_PROJECTION_FOLDER_ID') or not isinstance(projection,RollingProjectionStore)
                or not isinstance(projection.backing,DriveProjectionStore)):
            raise StateError('pdf_production_projection_required')
        ProjectionJournal(projection).read()  # Before AI or the first receipt append.
    inbox=normalize_folder_id(settings.receipt_drive_folder_id)
    def source(source_id):
        if not re.fullmatch('[A-Za-z0-9_-]{10,150}',source_id):raise StateError('pdf_production_source_invalid')
        def metadata():
            m=reader.files().get(fileId=source_id,fields='id,parents,mimeType,version,trashed',
                supportsAllDrives=True).execute(num_retries=0)
            if (m.get('id')!=source_id or m.get('parents')!=[inbox] or m.get('trashed')
                    or m.get('mimeType')!='application/pdf' or not m.get('version')):
                raise StateError('pdf_production_source_changed')
            return m
        before=metadata()
        content=reader.files().get_media(fileId=source_id,supportsAllDrives=True).execute(num_retries=0)
        if not isinstance(content,bytes) or not 0<len(content)<=MAX_SOURCE_BYTES or metadata()!=before:
            raise StateError('pdf_production_source_changed')
        return content
    def gemini_factory():
        if not apply:raise StateError('pdf_preview_ai_forbidden')
        settings.validate(need_gemini=True)
        return GeminiAI(settings.gemini_api_key,settings.normal_receipt_gemini_model or settings.gemini_model)
    analyzer=PdfUnitAnalyzer(ai if apply else None,gemini_factory=gemini_factory)
    return PdfUnitIntake(DrivePdfAuthority(legacy,source,migrated=migrated),completion,db,analyzer)


def preview_pdf(content,source_id,intake=None):
    """PDF dispatch before whole-file privacy/Medical intake, even when off."""
    if intake is not None and not is_single_page_pdf(content):return intake.preview(content,source_id)
    observed=observe_pdf(content,source_id)
    return {'document_type':'pdf_page_units','status':'grouping_required','archive_allowed':False,
        'units':[{'page_numbers':[p.page_number],'status':'new_eligible' if len(observed.pages)==1
            and p.classification=='normal' and p.observation_complete and p.extraction_status=='extracted'
            else 'grouping_required'} for p in observed.pages]}


def counts(result):
    out={'written':0,'unchanged':0,'needs_review':0,'failure':0,'new_eligible':0}
    units=result.get('units',[])
    if not isinstance(units,list) or not units:out['needs_review']=1;return out
    for unit in units:
        status=unit.get('status')
        if status=='imported':out['unchanged' if unit.get('replayed') else 'written']+=1
        elif status=='unchanged':out['unchanged']+=1
        elif status=='new_eligible':out['new_eligible']+=1
        elif status in {'authority_held','analysis_or_write_held'}:out['failure']+=1
        else:out['needs_review']+=1
    return out
