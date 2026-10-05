"""Actions-only fixed PDF canary. Secrets stay in runner memory.

No production-flow, ledger, Medical writer, mover or authority-save API.
Public logs contain configuration booleans/counts only. Detailed diagnostics
are encrypted to the existing service-account key before artifact upload.
"""
from contextlib import redirect_stdout, redirect_stderr
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
import base64
import json
import logging
import os
import re
import subprocess
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image

from .drive_run_state import StateError
from .pdf_grouping_authority import DriveGroupingStore
from .conditional_drive_state_v2 import ConditionalDriveStateTransportV2
from .pdf_grouping_review import GroupingBinding, DrivePdfReader, preflight_permissions
from .pdf_page_kind import current_answer
from .pdf_receipt_readonly import ReadonlyPdfReceipts, selected_unit
from .receipt_pdf_grouping import ConfirmedDocumentUnit
from .receipt_pdf_units import _digest
from .receipt_privacy_gate import require_receipt_ai_permission
from .private_state_bindings import unwrap

REPO='tmoriuchi1401-source/kakeibo-ai'
WORKFLOW='.github/workflows/pdf-unit-readonly.yml'
BRANCH='codex/pdf-page-privacy'
SOURCE_KEY='7bef7c636cf64f9a8cd40aefefcf0a668f2fdc4564217ddbee45ff69ee6f8d84'
SOURCE_HASH='ca1b8cba60addc14a364438d691c40164e250708103bce76a320f671629491bf'
CONFIRMATION='7a1900acc176b393f9c793e9c4e3aadb1087aedf1fd63eabb15c5cd6ff9e915b'
BINDING='d8a7d0f3dfc61cb3c3710741bbd88090afa6e8599568fa375de3b42e1ca0c290'
LABEL=b'pdf-unit-readonly-diagnostic-v1'
ARTIFACT='pdf-unit-readonly-encrypted'
OUTPUT='diagnostic.enc.json'
CHECKS={'local-ocr-runtime','test (general, not payroll and not medical)',
        'test (sensitive-synthetic, payroll or medical)'}


def require_context(env,checkout_sha):
    branch=env.get('GITHUB_REF')
    if (env.get('GITHUB_ACTIONS')!='true' or env.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
            or env.get('GITHUB_REPOSITORY')!=REPO or env.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or env.get('RUNNER_OS')!='Linux' or branch not in {'refs/heads/main','refs/heads/'+BRANCH}
            or env.get('GITHUB_WORKFLOW_REF')!=REPO+'/'+WORKFLOW+'@'+str(branch)
            or not re.fullmatch(r'[0-9a-f]{40}',env.get('PDF_READONLY_APPROVED_SHA',''))
            or checkout_sha!=env['PDF_READONLY_APPROVED_SHA']
            or env.get('PDF_READONLY_MODE') not in {'preflight','p2','remaining','page_p2','page_remaining','page_replay','page_multi'}
            or env.get('PDF_READONLY_CONFIRM')!='READ_ONLY'
            or any(env.get(k)!='false' for k in ('PDF_ACCOUNTING_ENABLED','PDF_MEDICAL_ENABLED','PDF_ARCHIVE_ENABLED'))
            or env.get('ACTIONS_STEP_DEBUG')=='true' or env.get('RUNNER_DEBUG')=='1'
            or not re.fullmatch(r'[1-9][0-9]*',env.get('GITHUB_RUN_ID',''))
            or env.get('GOOGLE_SERVICE_ACCOUNT_FILE') or env.get('GOOGLE_APPLICATION_CREDENTIALS')
            or env.get('GOOGLE_GENAI_USE_VERTEXAI') or env.get('GEMINI_BASE_URL')):
        raise StateError('readonly_runner_context_required')
    if env['PDF_READONLY_MODE'] in {'remaining','page_remaining','page_replay','page_multi'} and not re.fullmatch(r'[1-9][0-9]*',env.get('PDF_READONLY_P2_RUN_ID','')):
        raise StateError('readonly_p2_proof_required')
    if env['PDF_READONLY_MODE']=='page_remaining' and env.get('PDF_READONLY_PAGE_BATCH') not in {'p3-p6','p7-p10','p11-p14'}:
        raise StateError('readonly_page_batch_required')


def github_get(path,token,*,binary=False):
    if not path.startswith('/repos/'+REPO+'/'):
        raise StateError('readonly_github_target_invalid')
    request=Request('https://api.github.com'+path,headers={'Authorization':'Bearer '+token,
        'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'})
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*_):return None
    try:
        response=build_opener(NoRedirect()).open(request,timeout=30)
    except HTTPError as error:
        if not binary or error.code!=302:raise
        location=error.headers.get('Location','');target=urlsplit(location)
        if (target.scheme!='https' or target.username or target.password or target.port not in (None,443)
                or not target.hostname or not target.hostname.endswith(('.blob.core.windows.net','.githubusercontent.com'))):
            raise StateError('readonly_artifact_destination_invalid') from None
        # Never forward the repository token to signed artifact storage.
        response=build_opener(NoRedirect()).open(Request(location),timeout=30)
    with response:
        data=response.read(16*1024*1024+1)
    if len(data)>16*1024*1024:raise StateError('readonly_proof_too_large')
    return data if binary else json.loads(data)


def approved_pr(env,get=github_get):
    token=env.get('GITHUB_TOKEN','')
    if not token:raise StateError('readonly_github_auth_missing')
    p=get('/repos/'+REPO+'/pulls/91',token)
    if (p['state']!='open' or p['draft'] is not True or p['head']['repo']['full_name']!=REPO
            or p['head']['ref']!=BRANCH or p['base']['ref']!='main'
            or p['head']['sha']!=env['PDF_READONLY_APPROVED_SHA']):
        raise StateError('readonly_pr_changed')
    checks=get('/repos/'+REPO+'/commits/'+p['head']['sha']+'/check-runs?per_page=100',token)
    runs=[r for r in checks.get('check_runs',[]) if r['name'] in CHECKS]
    if (len(runs)!=3 or {r['name'] for r in runs}!=CHECKS
            or any(r['status']!='completed' or r['conclusion']!='success' for r in runs)):
        raise StateError('readonly_ci_required')


def readonly_http(service):
    original=service._http.request
    def request(uri,method='GET',**kwargs):
        if method.upper()!='GET':raise StateError('readonly_cloud_write_forbidden')
        return original(uri,method=method,**kwargs)
    service._http.request=request


def encrypted(value,pem):
    private=serialization.load_pem_private_key(pem.encode('ascii'),password=None)
    key=AESGCM.generate_key(bit_length=256);nonce=os.urandom(12)
    ciphertext=AESGCM(key).encrypt(nonce,json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),LABEL)
    wrapped=private.public_key().encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
        algorithm=hashes.SHA256(),label=LABEL))
    return {'schema':'pdf-unit-readonly-encrypted-v1',**{k:base64.b64encode(v).decode('ascii')
        for k,v in {'wrapped_key':wrapped,'nonce':nonce,'ciphertext':ciphertext}.items()}}


def decrypted(value,pem):
    if set(value)!= {'schema','wrapped_key','nonce','ciphertext'} or value['schema']!='pdf-unit-readonly-encrypted-v1':
        raise StateError('readonly_proof_invalid')
    private=serialization.load_pem_private_key(pem.encode('ascii'),password=None)
    key=private.decrypt(base64.b64decode(value['wrapped_key'],validate=True),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=LABEL))
    return json.loads(AESGCM(key).decrypt(base64.b64decode(value['nonce'],validate=True),
        base64.b64decode(value['ciphertext'],validate=True),LABEL))


def prior_p2(env,pem,expected,get=github_get):
    run_id=env['PDF_READONLY_P2_RUN_ID'];token=env['GITHUB_TOKEN']
    run=get('/repos/'+REPO+'/actions/runs/'+run_id,token)
    if (run['event']!='workflow_dispatch' or run['status']!='completed' or run['conclusion']!='success'
            or run['path'].split('@')[0]!=WORKFLOW or run['repository']['full_name']!=REPO):
        raise StateError('readonly_p2_run_invalid')
    meta=get('/repos/'+REPO+'/actions/runs/'+run_id+'/artifacts?per_page=100',token)
    artifacts=[a for a in meta.get('artifacts',[]) if a['name']==ARTIFACT and not a['expired']]
    if len(artifacts)!=1:raise StateError('readonly_p2_proof_missing')
    data=get('/repos/'+REPO+'/actions/artifacts/'+str(artifacts[0]['id'])+'/zip',token,binary=True)
    with zipfile.ZipFile(BytesIO(data)) as archive:
        if archive.namelist()!=[OUTPUT] or archive.getinfo(OUTPUT).file_size>8*1024*1024:
            raise StateError('readonly_p2_proof_invalid')
        proof=decrypted(json.loads(archive.read(OUTPUT)),pem)
    check_p2(proof,env,expected)
    return proof


def check_p2(proof,env,expected):
    if env['PDF_READONLY_MODE'] in {'page_remaining','page_replay','page_multi'}:
        from .page_receipt_actions import check_page_p2
        return check_page_p2(proof,env,expected)
    rows=proof.get('results',[])
    if (proof.get('schema')!='pdf-unit-readonly-diagnostic-v2' or proof.get('mode')!='p2'
            or proof.get('run_id')!=env['PDF_READONLY_P2_RUN_ID']
            or proof.get('code_sha')!=env['PDF_READONLY_APPROVED_SHA']
            or proof.get('confirmation_digest')!=expected['confirmation_digest']
            or proof.get('source_content_hash')!=expected['source_content_hash']
            or proof.get('proposal_digest')!=expected['proposal_digest']
            or proof.get('grouping_revision')!=expected['grouping_revision']
            or (expected.get('legacy_confirmation_digest') is not None
                and proof.get('legacy_confirmation_digest')!=expected['legacy_confirmation_digest'])
            or proof.get('authority_unchanged') is not True or len(rows)!=1
            or rows[0].get('page_number')!=2 or rows[0].get('unit_id')!=expected['unit_ids'][2]
            or rows[0].get('status') not in {'would_import','would_need_review'}
            or rows[0].get('privacy',{}).get('classification')!='normal'
            or rows[0].get('privacy',{}).get('complete') is not True
            or any(proof.get(k)!=0 for k in ('cloud_writes','medical_calls','source_moves','p1_rendered','p1_submitted'))):
        raise StateError('readonly_p2_proof_stale')


def open_legacy_context(env):
    # No service-account file is created. Reuse existing JSON in runner memory.
    from googleapiclient.discovery import build
    from .google_clients import credentials, READ_ONLY_SCOPES
    info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    config=json.loads(env['PDF_GROUPING_BINDING'])
    pem=info['private_key']
    folder=unwrap('KAKEIBO_STATE_FOLDER_ID',config['folder'],pem)
    file_id=unwrap('PDF_GROUPING_STATE_FILE_ID',config['file'],pem)
    binding=GroupingBinding(folder,file_id)
    sid=env['SPREADSHEET_ID']
    if _digest([folder,file_id,sid])!=BINDING:raise StateError('readonly_binding_mismatch')
    auth=credentials(READ_ONLY_SCOPES)
    drive=build('drive','v3',credentials=auth,cache_discovery=False)
    v2=build('drive','v2',credentials=auth,cache_discovery=False)
    sheets=build('sheets','v4',credentials=auth,cache_discovery=False)
    for client in (drive,v2,sheets):readonly_http(client)
    permissions=lambda:preflight_permissions(drive,binding,config['owner_digest'],info['client_email'])
    store=DriveGroupingStore(ConditionalDriveStateTransportV2(v2,binding),BINDING,preflight=permissions)
    # Replace the mutation interface even though the HTTP fence already blocks it.
    def forbidden(*_,**__):raise StateError('readonly_authority_write_forbidden')
    store.save=store.transport.replace_versioned=forbidden
    state=store.load();p=state['records'][SOURCE_KEY]['proposal'];a=state['records'][SOURCE_KEY]['confirmation']
    if (not a or p['source_content_hash']!=SOURCE_HASH or p['page_count']!=14 or p['grouping_version']!=2
            or a['confirmation_digest']!=CONFIRMATION or a['confirmed_partition']!=[[n] for n in range(2,15)]
            or current_answer(state,p['pages'][0])['human_classification']!='medical'):
        raise StateError('readonly_authority_changed')
    units=[ConfirmedDocumentUnit(p['source_file_id'],SOURCE_HASH,tuple(g['page_numbers']),tuple(g['member_page_hashes']),
        tuple(g['page_classifications']),2,p['proposal_digest']) for g in p['groups']]
    expected={k:p[k] for k in ('source_file_id','source_content_hash','page_count','proposal_digest')}
    expected.update(binding=BINDING,grouping_revision=2,confirmation_digest=CONFIRMATION,
        unit_ids={u.page_numbers[0]:u.unit_id for u in units})
    for n in range(2,15):selected_unit(state,p['source_file_id'],n,expected)
    reader=DrivePdfReader(drive,env['RECEIPT_DRIVE_FOLDER_ID'])
    def source(source_id):
        if source_id!=p['source_file_id']:raise StateError('readonly_source_not_fixed')
        before=reader.metadata(source_id)
        content=drive.files().get_media(fileId=source_id,supportsAllDrives=True).execute(num_retries=0)
        if reader.metadata(source_id)!=before:raise StateError('readonly_source_changed')
        return content
    # Fixed source freshness before even resolving the Gemini adapter.
    if sha256(source(p['source_file_id'])).hexdigest()!=SOURCE_HASH:raise StateError('readonly_source_changed')
    categories=[tuple(row) for row in sheets.spreadsheets().values().get(spreadsheetId=sid,
        range="'カテゴリ'!A2:B500").execute(num_retries=0).get('values',[]) if len(row)==2 and all(row)]
    if not categories:raise StateError('readonly_categories_missing')
    return store,source,categories,expected,pem


def open_context(env):
    """v1 stays untouched; a uniquely bound private v2 file is the new正本."""
    from googleapiclient.discovery import build
    from .google_clients import credentials, READ_ONLY_SCOPES
    from .pdf_grouping_authority_v2 import DriveGroupingV2Store, discover, units, validate
    legacy,source,categories,old,pem=open_legacy_context(env)
    config=json.loads(env['PDF_GROUPING_BINDING']);info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    folder=unwrap('KAKEIBO_STATE_FOLDER_ID',config['folder'],pem)
    legacy_id=unwrap('PDF_GROUPING_STATE_FILE_ID',config['file'],pem)
    auth=credentials(READ_ONLY_SCOPES)
    drive=build('drive','v3',credentials=auth,cache_discovery=False)
    v2=build('drive','v2',credentials=auth,cache_discovery=False)
    for client in (drive,v2):readonly_http(client)
    target=discover(drive,folder,old['binding']);binding=GroupingBinding(folder,target)
    digest=_digest([folder,target,env['SPREADSHEET_ID']])
    preflight=lambda:preflight_permissions(drive,binding,config['owner_digest'],info['client_email'])
    transport=ConditionalDriveStateTransportV2(v2,binding)
    def forbidden(*_,**__):raise StateError('readonly_authority_write_forbidden')
    transport.replace_versioned=forbidden
    store=DriveGroupingV2Store(transport,digest,legacy,legacy_id,preflight=preflight)
    value=store.load();record=value['records'][SOURCE_KEY];p=record['proposal'];a=record['confirmation']
    if value['migration']['legacy_confirmation_digest']!=CONFIRMATION:raise StateError('readonly_migration_intent_changed')
    expected={k:p[k] for k in ('source_file_id','source_content_hash','page_count','proposal_digest')}
    expected.update(binding=digest,grouping_revision=record['revision'],confirmation_digest=a['confirmation_digest'],
        legacy_confirmation_digest=CONFIRMATION,unit_ids={u.page_numbers[0]:u.unit_id for u in units(value)})
    for number in range(2,15):selected_unit(value,p['source_file_id'],number,expected)
    return store,source,categories,expected,pem


def receipt_analyzer(key,model):
    from .gemini_ai import GeminiAI
    from .models import ReceiptResult
    ai=GeminiAI(key,model,request_attempts=1)
    destination=urlsplit(ai.client._api_client._http_options.base_url)
    if (destination.scheme!='https' or destination.hostname!='generativelanguage.googleapis.com'
            or destination.port not in (None,443) or destination.username or destination.password):
        raise StateError('readonly_gemini_destination_mismatch')
    original=ai.client.interactions.create
    evidence={'calls':0,'responses':0,'readings':[],'fingerprint':None}
    def create(**kwargs):
        content=kwargs.get('input',[])
        if (len(content)!=2 or content[0].get('type')!='text' or content[1].get('type')!='image'
                or content[1].get('mime_type')!='image/png'):
            raise StateError('readonly_gemini_payload_invalid')
        png=base64.b64decode(content[1]['data'],validate=True)
        if sha256(png).digest()!=evidence['fingerprint'] or evidence['calls']>=3:
            raise StateError('readonly_gemini_payload_changed')
        with Image.open(BytesIO(png)) as image:
            if image.mode!='RGB' or image.info:raise StateError('readonly_gemini_payload_invalid')
        require_receipt_ai_permission(png,'image/png',known_source_classification='normal')
        evidence['calls']+=1
        response=original(**kwargs)
        evidence['responses']+=1
        try:evidence['readings'].append(ReceiptResult.model_validate_json(response.output_text))
        except Exception:pass
        return response
    # The bound Interactions resource does not own genai.Client. Keep its owner
    # alive: Client.__del__ otherwise closes HTTP before the first request.
    ai.client=SimpleNamespace(interactions=SimpleNamespace(create=create),sdk_owner=ai.client)
    def analyze(png,categories,*,expected_payload_sha256=None):
        if expected_payload_sha256 is not None and sha256(png).hexdigest()!=expected_payload_sha256:
            raise StateError('readonly_payload_changed')
        evidence.update(calls=0,responses=0,readings=[],fingerprint=sha256(png).digest())
        result=ai.analyze_receipt(png,'image/png',categories,known_source_classification='normal')
        readings=evidence['readings'];evidence['readings']=[]
        return result,readings
    return analyze,evidence


def execute(env,checkout_sha,*,opener=open_context,analyzer=receipt_analyzer,prior=prior_p2,approve=approved_pr):
    require_context(env,checkout_sha);approve(env)
    configured={k:bool(env.get(k,'').strip()) for k in ('GEMINI_API_KEY','GOOGLE_SERVICE_ACCOUNT_JSON','PDF_GROUPING_BINDING')}
    if not all(configured.values()):raise StateError('readonly_configuration_unavailable')
    store,source,categories,expected,pem=opener(env)
    initial=store.load();initial_bytes=store.payload;initial_tag=store.tag
    if env['PDF_READONLY_MODE']=='preflight':return None,{'status':'preflight_ok','gemini_calls':0}
    if env['PDF_READONLY_MODE'] in {'page_p2','page_remaining','page_replay','page_multi'}:
        from .page_receipt_actions import execute_pages
        return execute_pages(env,checkout_sha,store,source,categories,expected,pem,prior=prior)
    if env['PDF_READONLY_MODE']=='remaining':prior(env,pem,expected)
    analyze,evidence=analyzer(env['GEMINI_API_KEY'],env.get('NORMAL_RECEIPT_GEMINI_MODEL','gemini-3.5-flash-lite'))
    runner=ReadonlyPdfReceipts(store.load,source,analyze,categories,expected,
        model=env.get('NORMAL_RECEIPT_GEMINI_MODEL','gemini-3.5-flash-lite'))
    rows=[];calls=0
    for number in ([2] if env['PDF_READONLY_MODE']=='p2' else range(3,15)):
        evidence['calls']=0;evidence['responses']=0
        result=runner.run(number);calls+=evidence['calls']
        result['gemini_response_count']=evidence.get('responses',0)
        # v2 preserves validated structured normal candidates for the separately
        # authorized accounting canary. Still no raw response, OCR or images.
        rows.append({k:v for k,v in result.items() if k in {'page_number','unit_id','source_content_hash','page_hash',
            'model','status','validation_issues','reason','date','merchant','total','item_count','checks','privacy',
            'effective_classification','human_classification','payload_sha256','payload_mime','payload_pages',
            'page_identity','observation_render_hash','gemini_api_status','analysis_failure_kind',
            'analysis_failure_class','analysis_failure_sites','gemini_response_count'}})
        if result['status']=='would_import':
            parsed=result['parsed']
            rows[-1]['candidate']={k:parsed[k] for k in ('date','merchant','total','payment_method','transaction_kind')}
            rows[-1]['candidate']['items']=[{k:x[k] for k in ('name','quantity','amount','major_category','minor_category')}
                                          for x in parsed['items']]
        if result['status']=='authority_held':break
    if store.load()!=initial or store.payload!=initial_bytes or store.tag!=initial_tag:
        raise StateError('readonly_authority_changed')
    value={'schema':'pdf-unit-readonly-diagnostic-v2','mode':env['PDF_READONLY_MODE'],'run_id':env['GITHUB_RUN_ID'],
        'code_sha':checkout_sha,'source_content_hash':SOURCE_HASH,'confirmation_digest':expected['confirmation_digest'],
        'legacy_confirmation_digest':expected.get('legacy_confirmation_digest'),
        'proposal_digest':expected['proposal_digest'],'grouping_revision':expected['grouping_revision'],
        'authority_unchanged':True,'cloud_writes':0,'medical_calls':0,'source_moves':0,'p1_rendered':0,'p1_submitted':0,
        'gemini_calls':calls,'results':rows,'budgets':runner.budget.metadata()}
    serial=json.dumps(value,ensure_ascii=False)
    info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    if any(secret and secret in serial for secret in (env['GEMINI_API_KEY'],info['private_key'],env['GOOGLE_SERVICE_ACCOUNT_JSON'])):
        raise StateError('readonly_diagnostic_rejected')
    artifact=encrypted(value,pem)
    success=(rows and all(r['status'] in {'would_import','would_need_review','privacy_blocked'} for r in rows)
        and (env['PDF_READONLY_MODE']!='p2' or rows[0]['status'] in {'would_import','would_need_review'}))
    return artifact,{'status':'analysis_complete' if success else 'analysis_incomplete','gemini_calls':calls,
        'counts':{k:sum(r['status']==k for r in rows) for k in ('would_import','would_need_review','privacy_blocked','analysis_failed','authority_held')}}


def main():
    env=dict(os.environ)
    for name,label in (('GEMINI_API_KEY','Gemini'),('GOOGLE_SERVICE_ACCOUNT_JSON','Google service account'),
                       ('PDF_GROUPING_BINDING','Authority binding')):
        print(label+(': configured' if env.get(name,'').strip() else ': unavailable'))
    logging.disable(logging.CRITICAL)
    try:
        # Suppress third-party debug/error bodies, including OCR and SDK output.
        with redirect_stdout(StringIO()),redirect_stderr(StringIO()):
            sha=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
            artifact,summary=execute(env,sha)
            if artifact:
                root=Path(env['RUNNER_TEMP'])/'pdf-unit-readonly'
                root.mkdir(mode=0o700,exist_ok=True)
                (root/OUTPUT).write_text(json.dumps(artifact,separators=(',',':')),encoding='utf-8')
        print(json.dumps(summary,separators=(',',':')))
        return 0 if summary['status'] in {'preflight_ok','analysis_complete'} else 1
    except Exception:
        print('Read-only analysis: stopped')
        return 1


if __name__=='__main__':raise SystemExit(main())
