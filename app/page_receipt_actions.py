"""New array-model canary within the existing manual-only, GET-fenced runner.

Independent diagnostic schema cannot be consumed by legacy accounting canaries.
No new Secret, workflow, schedule, binding or authority provision is performed.
Unknown pages without newly authenticated explicit AI consent stay held.
"""
from hashlib import sha256
import json
from urllib.parse import urlsplit
from .drive_run_state import StateError
from .page_receipt_model import SourceRef,PageUnit,digest
from .human_general_authority import review_identity
from .page_receipt_ai import GeminiPageReceipts,authorize_payload
from .page_receipt_readonly import ReadonlyPageReceipts
from .pdf_receipt_readonly import selected_unit
from .receipt_pdf_units import _digest

SCHEMA='page-receipt-readonly-diagnostic-v1'

def check_page_p2(proof,env,expected):
    rows=proof.get('results',[])
    if (proof.get('schema')!=SCHEMA or proof.get('mode')!='page_p2'
            or proof.get('run_id')!=env['PDF_READONLY_P2_RUN_ID']
            or proof.get('code_sha')!=env['PDF_READONLY_APPROVED_SHA']
            or any(proof.get(k)!=expected[k] for k in ('source_content_hash','proposal_digest','confirmation_digest','grouping_revision'))
            or len(rows)!=1 or rows[0].get('page_number')!=2
            or rows[0].get('legacy_unit_id')!=expected['unit_ids'][2]
            or rows[0].get('status') not in {'would_import','would_need_review'} or not rows[0].get('units')
            or rows[0].get('privacy',{}).get('basis')!='automatic_normal'
            or rows[0].get('privacy',{}).get('exact_classification')!='normal'
            or proof.get('authority_unchanged') is not True
            or any(proof.get(k)!=0 for k in ('cloud_writes','medical_calls','source_moves','p1_rendered','p1_submitted'))):
        raise StateError('readonly_page_p2_proof_stale')

def load_page(store,expected,source_id,number):
    value=store.load()
    _,observed,proposal=selected_unit(value,source_id,number,expected)
    source=SourceRef(source_file_id=source_id,source_content_hash=proposal['source_content_hash'],page_count=proposal['page_count'])
    data=dict(source=source,page_number=number,stable_page_identity=observed['page_identity'],
        automatic_classification=observed['automatic_classification'],automatic_reason=observed['reason_code'],
        human_page_kind='general_receipt' if observed['human_classification']=='normal' else observed['human_classification'],
        observation_complete=observed['observation_metadata']['observation_complete'],
        extraction_status=observed['extraction_status'],observation_render_hash=observed['observation_render_hash'],
        review_identity='0'*64,authority_revision=expected['grouping_revision'])
    page=PageUnit(**data);data['review_identity']=review_identity(page)
    return PageUnit(**data)

def page_analyzer(key,model,permission):
    from .gemini_ai import GeminiAI
    owner=GeminiAI(key,model,request_attempts=1)
    destination=urlsplit(owner.client._api_client._http_options.base_url)
    if (destination.scheme!='https' or destination.hostname!='generativelanguage.googleapis.com'
            or destination.port not in (None,443) or destination.username or destination.password):
        raise StateError('readonly_gemini_destination_mismatch')
    adapter=GeminiPageReceipts(owner.client,model,permission)
    adapter.sdk_owner=owner # keep Client lifetime; no use of single-receipt parser
    return adapter

def execute_pages(env,checkout_sha,store,source,categories,expected,pem,*,prior,factory=page_analyzer):
    from .pdf_unit_readonly_analysis import encrypted
    initial=store.load();initial_bytes,initial_tag=store.payload,store.tag
    mode=env['PDF_READONLY_MODE']
    proof=prior(env,pem,expected) if mode in {'page_remaining','page_replay'} else None
    current=lambda sid,n:load_page(store,expected,sid,n)
    # No live Human General state has been provisioned or authenticated here.
    # Never promote legacy human-normal answers to a new external-AI scope.
    def no_grant(_):raise StateError('human_general_authority_missing')
    permission=lambda p,png:authorize_payload(p,png,current_page=current,load_source=source,load_grant=no_grant)
    analyzer=factory(env['GEMINI_API_KEY'],env.get('NORMAL_RECEIPT_GEMINI_MODEL','gemini-3.5-flash-lite'),permission)
    runner=ReadonlyPageReceipts(current,source,no_grant,analyzer,categories)
    rows=[]
    batch={'p3-p6':range(3,7),'p7-p10':range(7,11),'p11-p14':range(11,15)}
    for number in ([2] if mode in {'page_p2','page_replay'} else batch[env['PDF_READONLY_PAGE_BATCH']]):
        previous=proof['results'][0] if mode=='page_replay' else None
        result=runner.run(expected['source_file_id'],number,previous=previous)
        for u in result.get('units',[]):
            parsed=u['parsed']
            u['parsed']={k:parsed[k] for k in ('date','merchant','total','payment_method','transaction_kind')}
            u['parsed']['items']=[{k:x[k] for k in ('name','quantity','amount','major_category','minor_category')}
                for x in parsed['items']]
        result['legacy_unit_id']=expected['unit_ids'][number]
        rows.append(result)
        if result['status']=='authority_held':break
    if store.load()!=initial or store.payload!=initial_bytes or store.tag!=initial_tag:
        raise StateError('readonly_authority_changed')
    if sha256(source(expected['source_file_id'])).hexdigest()!=expected['source_content_hash']:
        raise StateError('readonly_source_changed')
    value={'schema':SCHEMA,'mode':mode,'run_id':env['GITHUB_RUN_ID'],'code_sha':checkout_sha,
        **{k:expected[k] for k in ('source_content_hash','proposal_digest','confirmation_digest','grouping_revision')},
        'authority_unchanged':True,'cloud_writes':0,'medical_calls':0,'source_moves':0,
        'p1_rendered':0,'p1_submitted':0,'gemini_calls':analyzer.calls,'results':rows,'budgets':runner.budget.metadata(),
        'accounting_authority':False,'new_human_general_authority_created':False}
    serial=json.dumps(value,ensure_ascii=False)
    info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    if any(secret and secret in serial for secret in (env['GEMINI_API_KEY'],info['private_key'],env['GOOGLE_SERVICE_ACCOUNT_JSON'])):
        raise StateError('readonly_diagnostic_rejected')
    statuses={'would_import','would_need_review','privacy_blocked','human_general_authority_required','receipt_segmentation_review'}
    complete=bool(rows) and all(r['status'] in statuses for r in rows)
    if mode=='page_p2':complete=complete and rows[0]['status'] in {'would_import','would_need_review'}
    return encrypted(value,pem),{'status':'analysis_complete' if complete else 'analysis_incomplete',
        'gemini_calls':analyzer.calls,'counts':{s:sum(r['status']==s for r in rows) for s in sorted(statuses|{'analysis_failed','authority_held'})}}
