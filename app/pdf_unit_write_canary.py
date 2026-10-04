"""Manual hosted canary only; cached validated analysis, existing normal writer.

The registered main workflow owns the production mutex and validated-main
boundary. Reviewed PR code is checked separately. Gemini/Medical/move are absent.
"""
from contextlib import redirect_stdout,redirect_stderr
from copy import deepcopy
from hashlib import sha256
from io import BytesIO,StringIO
from pathlib import Path
from urllib.parse import urlsplit,unquote,parse_qs
import json,logging,os,re,subprocess,zipfile

from .drive_run_state import StateError
from .pdf_unit_readonly_analysis import (REPO,BRANCH,CHECKS,SOURCE_KEY,SOURCE_HASH,
    github_get,open_context,readonly_http,encrypted,decrypted,ARTIFACT,OUTPUT,WORKFLOW)
from .pdf_receipt_readonly import ReadonlyPdfReceipts,selected_unit
from .pdf_receipt_write_canary import CanaryStore,run_canary,ALLOWED_PAGES
from .receipt_validation import POLICY_VERSION
from .models import ReceiptResult

WRITE_WORKFLOW='.github/workflows/pdf-unit-write-canary.yml'
INTENT_ID='1J6hjORzCDEauxg39o1fZbwRS41ROe3jE'
FOLDER='1WNrdIkbV2dzTHZ44hSF3DXadZOMD8_PO'
SID='1G44cDDUryVpZazTDwuCT4eZrir5KJb2WVm9baHTRPow'
SAFE_DIFF={'app/pdf_unit_write_canary.py','app/pdf_receipt_write_canary.py',
    'tests/test_pdf_unit_write_canary.py','tests/test_pdf_receipt_write_canary.py',
    WRITE_WORKFLOW,'docs/pdf-unit-write-canary-actions.md'}


def require_context(env,head):
    if (env.get('GITHUB_ACTIONS')!='true' or env.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
            or env.get('GITHUB_REPOSITORY')!=REPO or env.get('GITHUB_REF')!='refs/heads/main'
            or env.get('GITHUB_WORKFLOW_REF')!=REPO+'/'+WRITE_WORKFLOW+'@refs/heads/main'
            or env.get('RUNNER_ENVIRONMENT')!='github-hosted' or env.get('RUNNER_OS')!='Linux'
            or env.get('KAKEIBO_PRODUCTION_ENABLED')!='true'
            or env.get('GITHUB_SHA')!=env.get('KAKEIBO_VALIDATED_MAIN_SHA')
            or not re.fullmatch('[0-9a-f]{40}',env.get('GITHUB_SHA',''))
            or head!=env.get('PDF_CANARY_APPROVED_SHA')
            or not re.fullmatch('[0-9a-f]{40}',head)
            or not re.fullmatch('[0-9a-f]{40}',env.get('PDF_CANARY_ANALYSIS_SHA',''))
            or env.get('PDF_CANARY_CONFIRM')!='WRITE_CANARY'
            or env.get('PDF_CANARY_STAGE') not in {'preflight','p11','small','remaining'}
            or not env.get('KAKEIBO_PROJECTION_FOLDER_ID') or not env.get('GOOGLE_SERVICE_ACCOUNT_JSON')
            or env.get('SPREADSHEET_ID')!=SID or not env.get('PDF_GROUPING_BINDING')
            or env.get('GEMINI_API_KEY') or env.get('PDF_MEDICAL_ENABLED')!='false'
            or env.get('PDF_ARCHIVE_ENABLED')!='false' or env.get('ACTIONS_STEP_DEBUG')=='true'
            or env.get('RUNNER_DEBUG')=='1'):
        raise StateError('canary_hosted_main_boundary_required')
    for key in ('PDF_CANARY_P2_RUN_ID','PDF_CANARY_REMAINING_RUN_ID'):
        if not re.fullmatch('[1-9][0-9]*',env.get(key,'')):raise StateError('canary_actions_proof_required')


def check_code(env,get=github_get):
    token=env['GITHUB_TOKEN'];head=env['PDF_CANARY_APPROVED_SHA'];old=env['PDF_CANARY_ANALYSIS_SHA']
    pr=get('/repos/'+REPO+'/pulls/91',token)
    if (pr['state']!='open' or pr['draft'] is not True or pr['head']['ref']!=BRANCH
            or pr['head']['repo']['full_name']!=REPO or pr['head']['sha']!=head or pr['base']['ref']!='main'):
        raise StateError('canary_pr_changed')
    if get('/repos/'+REPO+'/branches/main',token)['commit']['sha']!=env['GITHUB_SHA']:
        raise StateError('canary_main_changed')
    for commit in {head,old}:
        runs=[r for r in get('/repos/'+REPO+'/commits/'+commit+'/check-runs?per_page=100',token)['check_runs'] if r['name'] in CHECKS]
        if len(runs)!=3 or {r['name'] for r in runs}!=CHECKS or any(r['conclusion']!='success' or r['status']!='completed' for r in runs):
            raise StateError('canary_ci_required')
    if head!=old:
        diff=get('/repos/'+REPO+'/compare/'+old+'...'+head,token)
        if (diff['status']!='ahead' or diff['merge_base_commit']['sha']!=old
                or not diff.get('files') or len(diff['files'])>=300
                or any(f['filename'] not in SAFE_DIFF or f.get('previous_filename',f['filename']) not in SAFE_DIFF for f in diff['files'])):
            raise StateError('canary_analysis_policy_changed')


def load_proofs(env,pem,expected,get=github_get):
    rows={}
    for key,mode,pages in (('PDF_CANARY_P2_RUN_ID','p2',[2]),('PDF_CANARY_REMAINING_RUN_ID','remaining',list(range(3,15)))):
        run_id=env[key];token=env['GITHUB_TOKEN']
        run=get('/repos/'+REPO+'/actions/runs/'+run_id,token)
        if (run['event']!='workflow_dispatch' or run['status']!='completed' or run['conclusion']!='success'
                or run['path'].split('@')[0]!=WORKFLOW or run['repository']['full_name']!=REPO):
            raise StateError('canary_actions_proof_invalid')
        artifacts=[a for a in get('/repos/'+REPO+'/actions/runs/'+run_id+'/artifacts?per_page=100',token)['artifacts']
                   if a['name']==ARTIFACT and not a['expired']]
        if len(artifacts)!=1:raise StateError('canary_actions_proof_missing')
        data=get('/repos/'+REPO+'/actions/artifacts/'+str(artifacts[0]['id'])+'/zip',token,binary=True)
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if archive.namelist()!=[OUTPUT] or archive.getinfo(OUTPUT).file_size>8*1024*1024:
                raise StateError('canary_actions_proof_invalid')
            value=decrypted(json.loads(archive.read(OUTPUT)),pem)
        if (value['schema']!='pdf-unit-readonly-diagnostic-v2' or value['run_id']!=run_id or value['mode']!=mode
                or value['code_sha']!=env['PDF_CANARY_ANALYSIS_SHA'] or not value['authority_unchanged']
                or any(value[k]!=0 for k in ('cloud_writes','medical_calls','source_moves','p1_rendered','p1_submitted'))
                or any(value[k]!=expected[k] for k in ('source_content_hash','confirmation_digest','proposal_digest','grouping_revision'))
                or [r['page_number'] for r in value['results']]!=pages):
            raise StateError('canary_actions_proof_stale')
        for row in value['results']:rows[row['page_number']]=row
    return rows


def execute(env,head):
    require_context(env,head);check_code(env)
    from googleapiclient.discovery import build
    from .google_clients import credentials,sheets_service
    from .pdf_grouping_review import GroupingBinding,preflight_permissions
    from .conditional_drive_state_v2 import ConditionalDriveStateTransportV2
    from .projection_store import ProjectionJournal,store_from_environment
    from .sheets import SheetsDB,SheetsReadPacer
    authority,source,_,expected,pem=open_context(env)
    info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON']);config=json.loads(env['PDF_GROUPING_BINDING'])
    value=authority.load();p=value['records'][SOURCE_KEY]['proposal']
    original_bytes,original_tag=authority.payload,authority.tag
    rows=load_proofs(env,pem,expected)
    manifest={'source_file_id':expected['source_file_id'],'source_content_hash':SOURCE_HASH,
        'confirmation_digest':expected['confirmation_digest'],'grouping_revision':expected['grouping_revision'],
        'authority_binding':expected['binding'],'policy_version':POLICY_VERSION,
        'scope':'operator_receipt_write_canary','archive_allowed':False,'medical_handoff_allowed':False,
        'unit_ids':{str(n):expected['unit_ids'][n] for n in ALLOWED_PAGES},
        'page_identities':{str(n):p['pages'][n-1]['page_identity'] for n in ALLOWED_PAGES}}
    drive=build('drive','v3',credentials=credentials(),cache_discovery=False);readonly_http(drive)
    binding=GroupingBinding(FOLDER,INTENT_ID)
    preflight_permissions(drive,binding,config['owner_digest'],info['client_email'])
    v2=build('drive','v2',credentials=credentials(),cache_discovery=False);http=v2._http.request
    def intent_http(uri,method='GET',**kwargs):
        if method.upper()!='GET' and (method.upper()!='PUT' or urlsplit(uri).hostname!='www.googleapis.com'
                or urlsplit(uri).path!='/upload/drive/v2/files/'+INTENT_ID
                or not kwargs.get('headers',{}).get('If-Match') or kwargs['headers']['If-Match']=='*'):
            raise StateError('canary_drive_write_forbidden')
        return http(uri,method=method,**kwargs)
    v2._http.request=intent_http
    store=CanaryStore(ConditionalDriveStateTransportV2(v2,binding),manifest)
    if any(r['phase']=='pending' for r in store.value['records'].values()):
        raise StateError('canary_pending_reconciliation_required')
    projection=store_from_environment(SID,env)
    if projection is None:raise StateError('canary_projection_binding_required')
    journal=ProjectionJournal(projection);journal.read()
    journal_id=projection.backing._file('journal')
    if not journal_id:raise StateError('canary_projection_journal_required')
    projection_http=projection.backing.service._http.request
    def projection_fence(uri,method='GET',**kwargs):
        if method.upper()!='GET' and (method.upper()!='PATCH'
                or urlsplit(uri).path!='/upload/drive/v3/files/'+journal_id):
            raise StateError('canary_projection_write_forbidden')
        return projection_http(uri,method=method,**kwargs)
    projection.backing.service._http.request=projection_fence
    svc=sheets_service();sheet_http=svc._http.request;requests=[]
    def sheet_fence(uri,method='GET',**kwargs):
        if method.upper()!='GET':
            path=unquote(urlsplit(uri).path)
            allowed=['/v4/spreadsheets/'+SID+'/values/'+t+'!A:A:append' for t in ('レシート','支出明細','取込データ')]
            if method.upper()!='POST' or path not in allowed or parse_qs(urlsplit(uri).query).get('valueInputOption')!=['RAW']:
                raise StateError('canary_sheets_write_forbidden')
            requests.append({'method':method,'table':next(t for t in ('レシート','支出明細','取込データ') if t in path)})
        return sheet_http(uri,method=method,**kwargs)
    svc._http.request=sheet_fence
    db=SheetsDB(SID,service=svc,read_pacer=SheetsReadPacer(),projection_journal=journal)
    db._restore_expense_category_validation_for_append=lambda _:None
    def verify_fresh(n,uid):
        unit,_,_=selected_unit(authority.load(),expected['source_file_id'],n,expected)
        if unit.unit_id!=uid or sha256(source(unit.source_file_id)).hexdigest()!=SOURCE_HASH:
            raise StateError('canary_source_or_authority_changed')
    def fresh(n):
        saved=rows[n]
        if (saved['status']!='would_import' or saved['validation_issues'] or saved['unit_id']!=expected['unit_ids'][n]
                or not saved['checks']['reread_stable'] or saved['checks']['policy_version']!=POLICY_VERSION):
            raise StateError('canary_candidate_unqualified')
        parsed=ReceiptResult.model_validate(saved['candidate']);png=[]
        def capture(payload,cats,**kwargs):
            png.append(payload);return parsed.model_copy(deep=True),[parsed.model_copy(deep=True)]
        local=ReadonlyPdfReceipts(authority.load,source,capture,db.categories(),expected,model='validated-actions-candidate')
        result=local.run(n)
        if result['status']!='would_import' or len(png)!=1 or result['date']!=saved['date'] or result['total']!=saved['total']:
            raise StateError('canary_fresh_privacy_or_validation_failed')
        proof={k:result[k] for k in ('unit_id','page_identity','page_number','source_content_hash',
            'human_classification','effective_classification')}
        proof.update(confirmation_digest=expected['confirmation_digest'],grouping_revision=expected['grouping_revision'])
        return result['unit_id'],png[0],ReceiptResult.model_validate(result['parsed']),db.categories(),proof
    stage=env['PDF_CANARY_STAGE'];reports=[]
    pages={'preflight':[],'p11':[11],'small':[5,6,8],'remaining':[2,7,9,12,13]}[stage]
    if stage not in {'preflight','p11'}:
        reports.append(run_canary(store,db,11,fresh,verify_fresh))
    for n in pages:
        if rows[n]['status']!='would_import':continue
        reports.append(run_canary(store,db,n,fresh,verify_fresh))
        count=len(requests);payload=store.payload;tag=store.tag
        reports.append(run_canary(store,db,n,fresh,verify_fresh))
        if len(requests)!=count or (store.payload,store.tag)!=(payload,tag):raise StateError('canary_replay_mutation')
    if authority.load()!=value or (authority.payload,authority.tag)!=(original_bytes,original_tag):
        raise StateError('canary_grouping_authority_changed')
    diagnostic={'schema':'pdf-unit-write-canary-diagnostic-v1','stage':stage,'run_id':env['GITHUB_RUN_ID'],
        'code_sha':head,'analysis_sha':env['PDF_CANARY_ANALYSIS_SHA'],'state_id':INTENT_ID,
        'results':reports,'append_requests':requests,'medical_calls':0,'gemini_calls':0,'source_moves':0,
        'archive':0,'p1_rendered':0,'p1_submitted':0,'updates':0,'deletes':0,'authority_unchanged':True,
        'projection_dirty_append':journal.read()['append'],'projection_journal_writes':projection.metrics['writes']}
    return encrypted(diagnostic,pem),{'status':'preflight_ok' if stage=='preflight' else 'canary_complete',
        'imported':sum(r['status']=='imported' for r in reports),'replayed':sum(r['status']=='replayed' for r in reports),
        'append_requests':len(requests),'medical_calls':0,'gemini_calls':0,'source_moves':0}


def main():
    logging.disable(logging.CRITICAL)
    try:
        with redirect_stdout(StringIO()),redirect_stderr(StringIO()):
            head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
            artifact,summary=execute(dict(os.environ),head)
            path=Path(os.environ['RUNNER_TEMP'])/'pdf-unit-write-canary'
            path.mkdir(mode=0o700,exist_ok=True)
            (path/OUTPUT).write_text(json.dumps(artifact,separators=(',',':')),encoding='utf-8')
        print(json.dumps(summary,separators=(',',':')));return 0
    except Exception as error:
        code=str(error) if isinstance(error,StateError) and re.fullmatch('[a-z_]+',str(error)) else 'canary_stopped'
        print(json.dumps({'status':'stopped','reason':code}));return 1

if __name__=='__main__':raise SystemExit(main())
