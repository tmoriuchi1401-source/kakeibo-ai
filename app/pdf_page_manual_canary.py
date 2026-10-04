"""Protected hosted owner-complete PDF manual requests, no analysis or mover.

Existing Drive authorities/source, shared captured request, existing general or
Medical manual writer, existing projection journal and exact read-back only.
Preflight uses GET-only clients. Normal scheduled PDF intake remains separate.
"""
import ast,base64,json,logging,os,re,subprocess
from contextlib import redirect_stdout,redirect_stderr
from copy import deepcopy
from hashlib import sha256
from io import StringIO
from pathlib import Path
from urllib.parse import urlsplit,unquote,parse_qs

from .drive_run_state import StateError
from .pdf_unit_readonly_analysis import REPO,BRANCH,CHECKS,SOURCE_KEY,SOURCE_HASH,github_get,open_context as authority_context
from .pdf_grouping_review import GroupingBinding,preflight_permissions
from .pdf_grouping_authority import DurablePdfGrouping
from .pdf_confirmed_source_reader import confirmed_legacy_observations
from .pdf_production_authority import DrivePdfAuthority
from .pdf_unit_processing import DriveUnitProcessingStore,SCHEMA as COMPLETION_SCHEMA,digest
from .pdf_general_manual_review import GeneralManualReview
from .pdf_page_kind import PageKindConfirmation
from .pdf_page_review import PageReviewSheet,cards,check_snapshot,process_page_request,TITLE,QUEUE,SHEET_ID
from .pdf_page_review_worker import medical_factory,medical_results
from .pdf_page_medical import PdfManualConfirmation,manual_source,manual_values,validate_manual_values
from .receipt_pdf_units import _digest
from .receipt_confirmation import ReceiptConfirmation,review_id
from .receipt_reimport_production import digest as medical_digest
from .conditional_drive_state_v2 import ConditionalDriveStateTransportV2,strong_etag

WORKFLOW='.github/workflows/pdf-page-manual-canary.yml'
SOURCE='1fcmMMGj86DLq54G0inD8LI_XSyQSPfTY'
SID='1G44cDDUryVpZazTDwuCT4eZrir5KJb2WVm9baHTRPow'
TARGETS={'medical':[1],'general_manual':[4,10,14]}


def require_context(env,head):
    if (env.get('GITHUB_ACTIONS')!='true' or env.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
            or env.get('GITHUB_REPOSITORY')!=REPO or env.get('GITHUB_REF')!='refs/heads/main'
            or env.get('GITHUB_WORKFLOW_REF')!=REPO+'/'+WORKFLOW+'@refs/heads/main'
            or env.get('RUNNER_ENVIRONMENT')!='github-hosted' or env.get('RUNNER_OS')!='Linux'
            or env.get('KAKEIBO_PRODUCTION_ENABLED')!='true'
            or not re.fullmatch('[0-9a-f]{40}',env.get('GITHUB_SHA',''))
            or env.get('GITHUB_SHA')!=env.get('KAKEIBO_VALIDATED_MAIN_SHA')
            or head!=env.get('PDF_MANUAL_APPROVED_SHA') or not re.fullmatch('[0-9a-f]{40}',head)
            or env.get('PDF_MANUAL_MODE') not in {'preflight','review'}
            or env.get('PDF_MANUAL_CONFIRM')!='MANUAL_ONLY'
            or not re.fullmatch('[1-9][0-9]*',env.get('GITHUB_RUN_ID',''))
            or not env.get('GOOGLE_SERVICE_ACCOUNT_JSON') or env.get('SPREADSHEET_ID')!=SID
            or not env.get('PDF_GROUPING_BINDING') or not env.get('RECEIPT_CONFIRMATION_BINDING')
            or not env.get('KAKEIBO_PROJECTION_FOLDER_ID') or not env.get('KAKEIBO_STATE_FOLDER_ID')
            or env.get('GEMINI_API_KEY') or env.get('PDF_ARCHIVE_ENABLED')!='false'
            or env.get('PDF_AUTOMATIC_ENABLED')!='false'
            or env.get('GOOGLE_APPLICATION_CREDENTIALS') or env.get('GOOGLE_SERVICE_ACCOUNT_FILE')
            or env.get('ACTIONS_STEP_DEBUG')=='true' or env.get('RUNNER_DEBUG')=='1'):
        raise StateError('pdf_manual_hosted_main_boundary_required')
    if env['PDF_MANUAL_MODE']=='review' and not re.fullmatch(
            '[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',env.get('PDF_MANUAL_REQUEST_ID','')):
        raise StateError('pdf_manual_captured_uuid_required')


def check_code(env,get=github_get):
    token=env.get('GITHUB_TOKEN','');head=env['PDF_MANUAL_APPROVED_SHA']
    if not token:raise StateError('pdf_manual_github_auth_required')
    p=get('/repos/'+REPO+'/pulls/91',token)
    if (p['state']!='open' or p['draft'] is not True or p['base']['ref']!='main'
            or p['head']['repo']['full_name']!=REPO or p['head']['ref']!=BRANCH or p['head']['sha']!=head):
        raise StateError('pdf_manual_reviewed_code_changed')
    if get('/repos/'+REPO+'/branches/main',token)['commit']['sha']!=env['GITHUB_SHA']:
        raise StateError('pdf_manual_main_changed')
    runs=[r for r in get('/repos/'+REPO+'/commits/'+head+'/check-runs?per_page=100',token)['check_runs'] if r['name'] in CHECKS]
    if len(runs)!=3 or {r['name'] for r in runs}!=CHECKS or any(r['status']!='completed' or r['conclusion']!='success' for r in runs):
        raise StateError('pdf_manual_ci_required')


def require_legacy_isolation(env,get=github_get):
    """A page record must never be picked up by the old whole-file worker.

    Require the exact reviewed page isolation method/skip already used in PR91
    on current validated main before creating a live Medical page record. No
    amount, HMAC, parsing, admission or writer policy is changed by these guards.
    """
    def remote(name):
        r=get('/repos/'+REPO+'/contents/app/'+name+'?ref='+env['GITHUB_SHA'],env['GITHUB_TOKEN'])
        if r.get('encoding')!='base64':raise StateError('pdf_manual_legacy_isolation_required')
        try:return ast.parse(base64.b64decode(r['content']))
        except Exception:raise StateError('pdf_manual_legacy_isolation_required') from None
    def items(tree):
        return next(x for c in tree.body if isinstance(c,ast.ClassDef) and c.name=='ReceiptConfirmation'
            for x in c.body if isinstance(x,ast.FunctionDef) and x.name=='items')
    def skip(tree):
        function=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='archive_confirmations')
        return next(x for x in ast.walk(function) if isinstance(x,ast.If)
            and x.body and isinstance(x.body[0],ast.Continue) and "value='pdf_page'" in ast.dump(x.test))
    try:
        root=Path(__file__).parent
        if (ast.dump(items(remote('receipt_confirmation.py')))!=ast.dump(items(ast.parse((root/'receipt_confirmation.py').read_bytes())))
                or ast.dump(skip(remote('receipt_confirmation_archive.py')))!=ast.dump(skip(ast.parse((root/'receipt_confirmation_archive.py').read_bytes())))):
            raise ValueError()
    except Exception:raise StateError('pdf_manual_legacy_isolation_required') from None


def drive_fence(service,*,completion_id=None,existing_state_id=None):
    old=service._http.request
    def request(uri,method='GET',**kwargs):
        if method.upper()!='GET':
            target=urlsplit(uri)
            if (target.scheme!='https' or target.hostname!='www.googleapis.com'
                    or target.username or target.password or target.port not in (None,443)):
                raise StateError('pdf_manual_drive_write_forbidden')
            tag=next((v for k,v in kwargs.get('headers',{}).items() if k.lower()=='if-match'),None)
            conditional=(completion_id and method.upper() in {'PUT','PATCH'}
                and target.path=='/upload/drive/v2/files/'+completion_id and strong_etag(tag))
            existing=(existing_state_id and method.upper()=='PATCH'
                and target.path=='/upload/drive/v3/files/'+existing_state_id)
            if not (conditional or existing):raise StateError('pdf_manual_drive_write_forbidden')
        return old(uri,method=method,**kwargs)
    service._http.request=request


def accounting_fence(service, planned_rows, verify_owner):
    """Allow each exact durable row once; unknown delivery is never resent.

    The backend must save its intent before calling append. No receipt result,
    UI label or caller-supplied row can replace that durable plan.
    """
    old=service._http.request; sent=set(); attempts=[]
    def request(uri,method='GET',**kwargs):
        if method.upper()!='GET':
            url=urlsplit(uri);path=unquote(url.path)
            titles=('レシート','支出明細','取込データ')
            allowed={ '/v4/spreadsheets/'+SID+'/values/'+t+'!A:A:append':t for t in titles }
            if (url.scheme!='https' or url.hostname!='sheets.googleapis.com'
                    or url.username or url.password or url.port not in (None,443)
                    or method.upper()!='POST' or path not in allowed
                    or parse_qs(url.query).get('valueInputOption')!=['RAW']):
                raise StateError('pdf_manual_accounting_write_forbidden')
            verify_owner(); title=allowed[path]
            try:rows=json.loads(kwargs['body']).get('values')
            except Exception:raise StateError('pdf_manual_unplanned_accounting_append') from None
            planned=planned_rows(title)
            if (not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],list)
                    or not rows[0] or rows[0] not in planned or (title,rows[0][0]) in sent):
                raise StateError('pdf_manual_unplanned_accounting_append')
            sent.add((title,rows[0][0])); attempts.append(1)
        return old(uri,method=method,**kwargs)
    service._http.request=request
    return attempts


def projection_fence(service, allowed_ranges, verify_capture):
    """Only selected-card state/result and the same captured queue result."""
    old=service._http.request
    def request(uri,method='GET',**kwargs):
        if method.upper()!='GET':
            url=urlsplit(uri)
            if (url.scheme!='https' or url.hostname!='sheets.googleapis.com'
                    or url.username or url.password or url.port not in (None,443)
                    or method.upper()!='POST'
                    or url.path!='/v4/spreadsheets/'+SID+'/values:batchUpdate'):
                raise StateError('pdf_manual_ui_write_forbidden')
            verify_capture()
            try:data=json.loads(kwargs['body'])
            except Exception:raise StateError('pdf_manual_ui_write_forbidden') from None
            allowed=allowed_ranges()
            if (data.get('valueInputOption')!='RAW' or not isinstance(data.get('data'),list)
                    or not data['data'] or any(x.get('range') not in allowed for x in data['data'])):
                raise StateError('pdf_manual_ui_write_forbidden')
        return old(uri,method=method,**kwargs)
    service._http.request=request


def discover_completion(reader,folder,binding):
    name=COMPLETION_SCHEMA+'-'+binding+'.json'
    found=reader.files().list(q=f"'{folder}' in parents and name = '{name}' and trashed = false",
        spaces='drive',pageSize=100,fields='nextPageToken,files(id,name,mimeType,parents)',
        supportsAllDrives=True,includeItemsFromAllDrives=True).execute(num_retries=0)
    files=found.get('files',[])
    if (found.get('nextPageToken') or len(files)!=1 or files[0].get('name')!=name
            or files[0].get('mimeType')!='application/json' or files[0].get('parents')!=[folder]
            or not re.fullmatch('[A-Za-z0-9_-]{10,150}',files[0].get('id',''))):
        raise StateError('pdf_manual_completion_state_missing_or_ambiguous')
    return files[0]['id']


class TargetProjection(PageReviewSheet):
    """Update only target status/result; preserve other owner cells and cards."""
    def __init__(self,*args,target,**kwargs):
        super().__init__(*args,**kwargs);self.target=target
    def publish_cards(self,projected):
        card=next((c for c in projected if c['token']==self.target),None)
        if not card:raise StateError('pdf_manual_projection_identity_changed')
        check_snapshot(self.read_card(self.target),card)
        values={f:v for f,_,v in card['rows'] if f in {'state','result'}}
        requests=[]
        for n,row in enumerate(self._rows(),1):
            if len(row)>5 and row[3]==self.target and row[5] in values:
                requests.append({'range':f"'{TITLE}'!B{n}",'values':[[values[row[5]]]]})
        if len(requests)!=2:raise StateError('pdf_manual_projection_identity_changed')
        self._write(requests)
        fresh=self.read_card(self.target)
        if any(v!=values[f] for f,_,v in fresh['rows'] if f in values):
            raise StateError('pdf_manual_projection_readback_mismatch')


def target_card(kinds,sheet,snapshot):
    identity=snapshot.get('identity',{});kind=identity.get('kind');numbers=identity.get('page_numbers')
    if (identity.get('source_file_id')!=SOURCE or identity.get('source_content_hash')!=SOURCE_HASH
            or not isinstance(numbers,list) or len(numbers)!=1 or type(numbers[0]) is not int
            or numbers[0] not in TARGETS.get(kind,[])):
        raise StateError('pdf_manual_target_forbidden')
    value,p,answers=kinds.current(SOURCE)
    card=next((x for x in cards(kinds.grouping.view(value['records'][SOURCE_KEY]),answers,sheet.categories)
        if x['token']==snapshot.get('token')),None)
    if not card:raise StateError('stale_proposal')
    fields=check_snapshot(snapshot,card)
    if fields!=(check_snapshot(sheet.read_card(card['token']),card)):
        # State/result are projection, not input; capture labels can differ.
        live=check_snapshot(sheet.read_card(card['token']),card)
        from .pdf_page_review import EDITABLE
        if any(fields.get(k)!=live.get(k) for k in EDITABLE):raise StateError('pdf_manual_owner_snapshot_changed')
    if kind=='general_manual':
        from .pdf_page_general import manual_values as general_values, CONFIRM_ACTIONS
        if fields.get('manual_action') not in CONFIRM_ACTIONS:raise StateError('pdf_manual_explicit_action_required')
        general_values(fields,sheet.categories)
    else:
        if fields.get('medical_action') not in {'医療費を確定','既存支出と重複（紐付け）','重複候補と別の支出として確定'}:
            raise StateError('pdf_manual_explicit_action_required')
        validate_manual_values(manual_values(fields,sheet.categories),sheet.categories)
    return card


def medical_completion(review,provider,completion):
    """Reference the existing durable manual backend, never its Medical inputs."""
    if not isinstance(review,PdfManualConfirmation):raise StateError('pdf_manual_existing_medical_backend_required')
    key=review.key;original=review.source
    current=provider.current(original['pdf_page']['original_file_id'])
    number=original['pdf_page']['page_number']
    spec=next((s for s in current.medical_pages if s['page_numbers']==[number]),None)
    if spec is None:raise StateError('pdf_manual_medical_kind_changed')
    def verified(item):
        review.verify_source(original,review.folder)
        if (item.get('source')!=original or item.get('kind')!='medical' or item.get('status')!='applied'
                or item.get('decision_origin')!='human' or not item.get('plan')
                or item.get('confirmation_hash')!=medical_digest(item.get('inputs',[]))
                or item.get('inputs')!=review.read_owner_inputs()
                or not review._complete(item['plan'])
                or review.store.transport.read()!=review.store.payload):
            raise StateError('pdf_manual_medical_readback_required')
        return True
    item=review.items[key];verified(item)
    record,_=completion.reserve(spec,'medical_manual',item['confirmation_hash'],{},
        digest(['existing-medical-manual-v1',key,item['confirmation_hash']]),verify_current=provider.verify)
    completion.complete(spec['unit_id'],record['intent_digest'],verify_current=provider.verify,
        verify_readback=lambda _:verified(review.items[key]))
    return '医療確定済み'


def execute(env,head):
    require_context(env,head);check_code(env)
    from googleapiclient.discovery import build
    from .google_clients import credentials,READ_ONLY_SCOPES
    from .private_state_bindings import unwrap
    from .receipt_confirmation_production import open_context as confirmation_context
    from .projection_store import ProjectionJournal,store_from_environment
    from .sheets import SheetsDB,SheetsReadPacer
    authority,source,master,expected,pem=authority_context(env)
    value=authority.load();authority_proof=(authority.payload,authority.tag,authority.legacy_store.payload,authority.legacy_store.tag)
    config=json.loads(env['PDF_GROUPING_BINDING']);info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    folder=unwrap('KAKEIBO_STATE_FOLDER_ID',config['folder'],pem)
    legacy_id=unwrap('PDF_GROUPING_STATE_FILE_ID',config['file'],pem)
    if unwrap('KAKEIBO_STATE_FOLDER_ID',env['KAKEIBO_STATE_FOLDER_ID'],pem)!=folder:
        raise StateError('pdf_manual_private_binding_changed')
    local={**env,'KAKEIBO_STATE_FOLDER_ID':folder}
    reader=build('drive','v3',credentials=credentials(READ_ONLY_SCOPES),cache_discovery=False);drive_fence(reader)
    completion_id=discover_completion(reader,folder,_digest([folder,legacy_id,SID]))
    cb=GroupingBinding(folder,completion_id)
    acl=lambda:preflight_permissions(reader,cb,config['owner_digest'],info['client_email'])
    acl()
    provider=DrivePdfAuthority(authority.legacy_store,source,migrated=authority)
    grouping=DurablePdfGrouping(authority.legacy_store,
        lambda sid,previous:confirmed_legacy_observations(authority,source,sid,previous))
    kinds=PageKindConfirmation(grouping)
    # Resolve read-only backend before creating any mutation capability.
    _,medical_store,readonly_db,_=confirmation_context(local,False)
    ui=PageReviewSheet(readonly_db.svc,SID,master)
    fresh,_,answers=kinds.current(SOURCE)
    projected=cards(kinds.grouping.view(fresh['records'][SOURCE_KEY]),answers,master)
    if env['PDF_MANUAL_MODE']=='preflight':
        check=build('drive','v2',credentials=credentials(READ_ONLY_SCOPES),cache_discovery=False);drive_fence(check)
        completion=DriveUnitProcessingStore(ConditionalDriveStateTransportV2(check,cb),_digest([folder,completion_id,SID]),preflight=acl)
        completion.load();provider.current(SOURCE)
        ready=0
        for card in projected:
            if card['identity']['kind'] not in TARGETS:continue
            try:target_card(kinds,ui,ui.read_card(card['token']));ready+=1
            except (StateError,ValueError):pass
        return {'status':'preflight_ok','owner_requests_ready':ready,'accounting_appends':0,
            'medical_manual_completed':0,'gemini_calls':0,'source_moves':0,'archive':0}
    request_id=env['PDF_MANUAL_REQUEST_ID'];_,snapshot=ui.request(request_id)
    card=target_card(kinds,ui,snapshot);kind=card['identity']['kind']
    if kind=='medical':require_legacy_isolation(env)
    # Existing journal must be healthy before a single accounting append.
    projection=store_from_environment(SID,local)
    if projection is None:raise StateError('pdf_manual_projection_binding_required')
    journal=ProjectionJournal(projection);journal.read();journal_id=projection.backing._file('journal')
    if not journal_id:raise StateError('pdf_manual_projection_binding_required')
    drive_fence(projection.backing.service,existing_state_id=journal_id)
    v2=build('drive','v2',credentials=credentials(),cache_discovery=False);drive_fence(v2,completion_id=completion_id)
    completion=DriveUnitProcessingStore(ConditionalDriveStateTransportV2(v2,cb),_digest([folder,completion_id,SID]),preflight=acl)
    before_completion=completion.load()
    # Existing general writer; no create/update/delete or extra target methods.
    svc=build('sheets','v4',credentials=credentials(),cache_discovery=False)
    def planned_rows(title):
        if kind=='general_manual':
            spec=provider.current(SOURCE).units
            spec=next(s for s in spec if s['page_numbers']==card['identity']['page_numbers'])
            record=completion.load()['records'].get(spec['unit_id'])
            return record['planned_rows'].get(title,[]) if record and record['phase']=='pending' else []
        legacy=authority.legacy_store.load();p=legacy['records'][SOURCE_KEY]['proposal']
        a=kinds.current(SOURCE)[2][1];key=review_id('medical',manual_source(p['pages'][0],a))
        if medical_store.transport.read()!=medical_store.payload:
            raise StateError('pdf_manual_medical_state_changed')
        item=medical_store.value.get('confirmation_items',{}).get(key)
        return [row for table,row in item.get('plan',[]) if table==title] if item and item['status']=='pending' else []
    append_requests=accounting_fence(svc,planned_rows,lambda:target_card(kinds,ui,snapshot))
    db=SheetsDB(SID,service=svc,read_pacer=SheetsReadPacer(),projection_journal=journal)
    db._restore_expense_category_validation_for_append=lambda _:None
    general=GeneralManualReview(provider,completion,db)
    if kind=='medical':
        # Reuse exactly the existing Medical state/parser/confirmation/writer.
        _,medical_store,_,_=confirmation_context(local,True)
        state_id=medical_store.transport.binding.file_id
        if state_id in {legacy_id,completion_id}:raise StateError('pdf_manual_medical_store_must_be_separate')
        drive_fence(medical_store.transport.service,existing_state_id=state_id)
    create_medical=medical_factory(kinds,medical_store,db,env['RECEIPT_DRIVE_FOLDER_ID'])
    def create(source_proof,owner):
        real=create_medical(source_proof,owner)
        class Complete:
            def confirm(self,*,submitted_inputs):
                result=real.confirm(submitted_inputs=submitted_inputs)
                if result=='医療確定済み':return medical_completion(real,provider,completion)
                return result
        return Complete()
    # UI projection has a different client and an exact owned-range fence.
    ui_service=build('sheets','v4',credentials=credentials(),cache_discovery=False)
    def verify_capture():
        if ui.request(request_id)[1]!=snapshot:raise StateError('pdf_manual_owner_snapshot_changed')
    def allowed_ranges():
        position,_=ui.request(request_id)
        allowed={f"'{QUEUE}'!B{position}",f"'{QUEUE}'!E{position}:F{position}"}
        allowed.update(f"'{TITLE}'!B{n}" for n,row in enumerate(ui._rows(),1)
            if len(row)>5 and row[3]==card['token'] and row[5] in {'state','result'})
        return allowed
    projection_fence(ui_service,allowed_ranges,verify_capture)
    page=TargetProjection(ui_service,SID,master,target=card['token'],
        load_medical_results=lambda:medical_results(kinds,medical_store,db,env['RECEIPT_DRIVE_FOLDER_ID']))
    result=process_page_request(kinds,page,request_id,medical_factory=create,general_factory=general)
    completed=result in {'一般手入力済み','医療確定済み'}
    # Replay through the same captured UUID: no second append or state update.
    first=completion.load();first_bytes=(completion.payload,completion.tag)
    medical_bytes=medical_store.payload;count=len(append_requests)
    if completed:
        replay=process_page_request(kinds,page,request_id,medical_factory=create,general_factory=general)
        if (replay!=result or len(append_requests)!=count or completion.load()!=first
                or (completion.payload,completion.tag)!=first_bytes or medical_store.payload!=medical_bytes):
            raise StateError('pdf_manual_replay_mutation')
    authority.load()
    if authority_proof!=(authority.payload,authority.tag,authority.legacy_store.payload,authority.legacy_store.tag):
        raise StateError('pdf_manual_grouping_authority_changed')
    return {'status':'manual_complete' if completed else 'manual_held','accounting_appends':count,
        'replay_additional_appends':len(append_requests)-count,
        'medical_manual_completed':int(result=='医療確定済み'),
        'general_manual_completed':int(result=='一般手入力済み'),
        'gemini_calls':0,'source_moves':0,'archive':0,'authority_writes':0,
        'completed_units':len(first['records'])-len(before_completion['records'])}


def main():
    env=dict(os.environ);logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(StringIO()),redirect_stderr(StringIO()):
            head=subprocess.check_output(['git','rev-parse','HEAD'],text=True,stderr=subprocess.DEVNULL).strip()
            result=execute(env,head)
        print(json.dumps(result,sort_keys=True));return 0 if result['status']!='manual_held' else 1
    except Exception:
        # Medical duplicate/input errors can contain owner fields and IDs.
        # Never put raw error text, request snapshots or amounts in logs/artifacts.
        print(json.dumps({'status':'manual_held','reason':'manual_request_not_completed'}));return 1


if __name__=='__main__':raise SystemExit(main())
