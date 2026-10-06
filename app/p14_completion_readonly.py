"""Authenticated p14 only, two bounded read-only analyses with frozen IDs.

No writer, authority save or projection mutations. Config is encrypted to the
existing SA key in a manual-only Actions Variable. Secret stays in the runner.
"""
from hashlib import sha256
import json
from .drive_run_state import StateError
from .page_receipt_model import digest,page_key
from .human_general_authority import validate_grant
from .page_receipt_ai import authorize_payload
from .page_receipt_readonly import ReadonlyPageReceipts


def corroborate_replay(first,second):
    """Blank fields that disagree across successful read-only runs too.

    Replayed geometry fixes identity, not field values or accounting authority.
    Structural item/transaction changes cannot be repaired by filling a total.
    """
    from copy import deepcopy
    from .general_receipt_completion import validate_draft,FIELDS
    validate_draft(first);validate_draft(second)
    if first['identity']!=second['identity']:raise StateError('readonly_receipt_identity_changed')
    result=deepcopy(second);issues=set(first['hard_issues'])|set(second['hard_issues'])
    for field in FIELDS:
        if first['prefill'][field]!=second['prefill'][field]:
            result['prefill'][field]=''
            result['blank_reasons'][field]=sorted(set(first['blank_reasons'][field]+second['blank_reasons'][field]+['replay_field_disagreement']))
        result['provenance'][field]='gemini' if result['prefill'][field]!='' else 'missing'
    a,b=first['parsed'],second['parsed']
    if a and b:
        shape=lambda r:[(i['name'],i['quantity'],i['amount']) for i in r['items']]
        if shape(a)!=shape(b):issues.add('replay_item_structure_changed')
        if a['transaction_kind']!=b['transaction_kind']:issues.add('replay_transaction_kind_changed')
    elif a!=b:issues.add('replay_item_structure_changed')
    result['hard_issues']=sorted(issues)
    result.pop('candidate_digest');result['candidate_digest']=digest(result)
    return validate_draft(result)


def context(env,pem,expected,current,*,drive_factory=None):
    from .pdf_unit_readonly_analysis import decrypted
    from services.human_general.real_page import RealPageDrive,validate_verified
    try:
        config=decrypted(json.loads(env['PDF_HGA_READONLY_BINDING']),pem)
        if set(config)!={'drive','actor_id','authority_sha256'}:raise ValueError()
        drive=(drive_factory or RealPageDrive)(config['drive'],json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON']))
        # Defense in depth: mutation interface is removed before any read.
        request=drive.request
        def get(fid,**kw):
            if kw.get('put') is not None:raise StateError('readonly_cloud_write_forbidden')
            return request(fid,**kw)
        drive.request=get
        drive.acl();page=drive.fresh()
        if page!=current(expected['source_file_id'],14):raise StateError('readonly_hga_page_changed')
        def read():
            raw,tag=drive.read(config['drive']['authority_file'])
            if sha256(raw).hexdigest()!=config['authority_sha256']:raise StateError('readonly_hga_changed')
            value=json.loads(raw)
            if len(value['actor_evidence'])!=1:raise StateError('readonly_verified_actor_missing')
            actor=next(iter(value['actor_evidence'].values()))['actor']
            if actor['actor_id']!=config['actor_id'] or digest([actor['issuer'],actor['subject']])!=config['actor_id']:
                raise StateError('readonly_actor_not_allowlisted')
            validate_verified(value,config['drive'],actor['subject'])
            grant=value['authority']['grants'][page_key(page)]
            validate_grant(grant,page)
            return grant,raw,tag
        grant,initial,tag=read()
        def load_grant(p):
            if p!=drive.fresh():raise StateError('readonly_hga_page_changed')
            return read()[0]
        def unchanged():
            _,raw,latest=read()
            if (raw,latest)!=(initial,tag):raise StateError('readonly_hga_changed')
            drive.fresh()
        return page,load_grant,unchanged
    except StateError:raise
    except Exception:raise StateError('readonly_hga_configuration_invalid') from None


def execute_p14(env,checkout_sha,store,source,categories,expected,pem,*,factory=None,open_hga=context):
    from .page_receipt_actions import load_page,page_analyzer
    from .pdf_unit_readonly_analysis import encrypted
    initial=store.load();initial_bytes,tag=store.payload,store.tag
    current=lambda sid,n:load_page(store,expected,sid,n) if n==14 else forbidden_page()
    page,grant,unchanged=open_hga(env,pem,expected,current)
    permission=lambda p,png:authorize_payload(p,png,current_page=current,load_source=source,load_grant=grant)
    model=env.get('NORMAL_RECEIPT_GEMINI_MODEL','gemini-3.5-flash-lite')
    analyzer=(factory or page_analyzer)(env['GEMINI_API_KEY'],model,permission)
    runner=ReadonlyPageReceipts(current,source,grant,analyzer,categories,completion_drafts=True)
    rows=[]
    for _ in range(2):
        unchanged()
        result=runner.run(expected['source_file_id'],14,previous=rows[0] if rows else None)
        rows.append(result)
        if result['status'] not in {'would_import','would_need_review'}:break
    replay=(len(rows)==2 and bool(rows[0].get('manifest')) and rows[0]['manifest']==rows[1].get('manifest'))
    drafts=[]
    if replay:
        first,second=(r['completion_drafts'] for r in rows)
        if len(first)!=len(second):raise StateError('readonly_receipt_identity_changed')
        drafts=[corroborate_replay(a,b) for a,b in zip(first,second)]
    unchanged()
    if store.load()!=initial or (store.payload,store.tag)!=(initial_bytes,tag):raise StateError('readonly_authority_changed')
    if sha256(source(expected['source_file_id'])).hexdigest()!=page.source.source_content_hash:raise StateError('readonly_source_changed')
    value={'schema':'p14-completion-readonly-v1','mode':'page_p14','run_id':env['GITHUB_RUN_ID'],'code_sha':checkout_sha,
        'page':page.model_dump(),'results':rows,'gemini_model':model,'gemini_calls':analyzer.calls,
        'receipt_manifest_replay':replay,'completion_drafts':drafts,'hga_unchanged':True,'grouping_unchanged':True,
        'authority_writes':0,'cloud_writes':0,'medical_calls':0,'source_moves':0,'p1_rendered':0,'p1_submitted':0,
        'accounting_authority':False,'budgets':runner.budget.metadata()}
    serial=json.dumps(value,ensure_ascii=False)
    info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    if any(s and s in serial for s in (env['GEMINI_API_KEY'],info['private_key'],env['GOOGLE_SERVICE_ACCOUNT_JSON'])):
        raise StateError('readonly_diagnostic_rejected')
    accepted=bool(rows) and all(r['status'] in {'would_import','would_need_review','receipt_segmentation_review','privacy_blocked'} for r in rows)
    return encrypted(value,pem),{'status':'analysis_complete' if accepted else 'analysis_incomplete',
        'gemini_calls':analyzer.calls,'receipt_manifest_replay':replay,
        'counts':{s:sum(r['status']==s for r in rows) for s in ('would_import','would_need_review','receipt_segmentation_review','privacy_blocked','analysis_failed','authority_held')}}


def forbidden_page():raise StateError('readonly_p14_only')
