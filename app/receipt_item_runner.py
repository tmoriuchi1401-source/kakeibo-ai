"""Receipt-item requests through the existing PDF manual runner, PLAN ONLY.

No workflow/schedule changes, new Secrets, image or accounting writes. GitHub
receives only the existing queue UUID, private Drive is the source of truth.
"""
import json
from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_item_review import SCHEMA,check_snapshot,card,evaluate
from .receipt_item_confirmation import ConfirmedItems,binding

CONTEXT_NAME='kakeibo-receipt-item-review-context-v1.json'


def detect(snapshot):
    return isinstance(snapshot,dict) and snapshot.get('identity',{}).get('schema')==SCHEMA


def accounting_plan(candidate,validation,categories,*,timestamp):
    """Same ReceiptPipeline materializer; only an in-memory PlanningDB."""
    from .pdf_receipt_write_canary import PlanningDB
    from .receipt_pipeline import ReceiptPipeline
    from .models import ReceiptResult
    from .receipt_validation import validate_receipt_result
    from .pdf_page_review import original_uri
    if validation['status']!='ready_to_confirm':raise StateError('item_runner_not_ready')
    result=ReceiptResult.model_validate(validation['parsed'])
    if result.transaction_kind!='purchase' or not validate_receipt_result(result,categories)[0]:
        raise StateError('item_runner_validation_failed')
    db=PlanningDB(categories);pipeline=ReceiptPipeline(db,None,clock=lambda:timestamp)
    ident=candidate['legacy']['identity']
    pipeline._materialize_result(result,ident['receipt_unit_id'],original_uri(ident['source_file_id'],ident['page_number']),[])
    return db.plan


def validate_plan(readers,journal,request_id,snapshot,owner_actor_id):
    candidate,current,categories=readers.read()
    if snapshot!=current:raise StateError('item_runner_snapshot_stale')
    raw,tag=journal.read_versioned();state=json.loads(raw);request=state['requests'].get(request_id)
    if (not request or request['snapshot']!=snapshot or request['binding']!=binding(candidate,snapshot,request_id)
            or request['proof']['actor_id']!=owner_actor_id):
        raise StateError('item_runner_authenticated_request_required')
    proof=ConfirmedItems(**request['proof']);inputs=check_snapshot(snapshot,card(candidate,categories=categories))
    if proof.request_id!=request_id or proof.snapshot_digest!=digest(snapshot):raise StateError('item_runner_proof_stale')
    if proof.authority_digest!=digest([request['binding'],proof.actor_id,proof.verified_at]):raise StateError('item_runner_proof_stale')
    plan=evaluate(candidate,inputs,categories,confirmation=proof)
    if request['plan']!={'input':inputs,'validation':plan}:raise StateError('item_runner_plan_changed')
    if journal.read_versioned()!=(raw,tag):raise StateError('item_runner_journal_changed')
    return candidate,plan


def private_context(folder,info):
    """Reached only after existing hosted-main/CI/UUID/concurrency guards."""
    from googleapiclient.discovery import build
    from .google_clients import credentials,READ_ONLY_SCOPES
    from .pdf_grouping_review import GroupingBinding,preflight_permissions
    from services.human_general.receipt_review_readers import validate_config_review
    svc=build('drive','v3',credentials=credentials(READ_ONLY_SCOPES),cache_discovery=False)
    result=svc.files().list(q="'"+folder+"' in parents and trashed=false and name='"+CONTEXT_NAME+"'",
        fields='files(id),nextPageToken',pageSize=2).execute(num_retries=0)
    if not result.get('files'):return None
    if result.get('nextPageToken') or len(result.get('files',[]))!=1:raise StateError('item_runner_private_context_ambiguous')
    fid=result['files'][0]['id'];context=json.loads(svc.files().get_media(fileId=fid).execute(num_retries=0))
    if set(context)!={'config','owner_actor_id','schema'} or context['schema']!='receipt-item-runner-context-v1':
        raise StateError('item_runner_context_invalid')
    from .receipt_audit import checked_hash
    checked_hash(context['owner_actor_id']);config=validate_config_review(context['config'])
    if config['drive']['folder']!=folder:raise StateError('item_runner_folder_changed')
    for target in (fid,config['candidate_file'],config['journal_file']):
        preflight_permissions(svc,GroupingBinding(folder,target),config['drive']['owner_digest'],info['client_email'])
    return context


def authenticated_snapshot(folder,info,request_id):
    """Private authenticated request lookup before legacy queue handling."""
    from services.human_general.receipt_review_readers import Readers,Journal
    context=private_context(folder,info)
    if context is None:return None
    readers=Readers(context['config'],info,owner_actor_id=context['owner_actor_id'])
    request=json.loads(Journal(readers).read_versioned()[0])['requests'].get(request_id)
    return request['snapshot'] if request else None


def hosted(env,folder,info,request_id,snapshot):
    """Reached only after existing hosted-main/CI/UUID/concurrency guards."""
    from googleapiclient.discovery import build
    from .google_clients import credentials,READ_ONLY_SCOPES
    from services.human_general.receipt_review_readers import Readers,Journal
    context=private_context(folder,info)
    if context is None:raise StateError('item_runner_private_context_missing')
    config=context['config'];readers=Readers(config,info,owner_actor_id=context['owner_actor_id'])
    # Every Drive operation is GET-only here, including the own journal.
    old=readers.private.request
    def get(target,**kw):
        if kw.get('put') is not None:raise StateError('item_runner_write_disabled')
        return old(target,**kw)
    readers.private.request=get;readers.drive.begin_request()
    try:candidate,plan=validate_plan(readers,Journal(readers),request_id,snapshot,context['owner_actor_id'])
    finally:readers.drive.end_request()
    # Exact existing queue only, after authenticated validation, inside this
    # workflow's existing kakeibo-production concurrency. No onEdit/new trigger.
    from .receipt_item_queue import capture
    from .pdf_review_fields import QUEUE
    from .sheets import SheetsDB
    from .utils import now_jst_string
    service=build('sheets','v4',credentials=credentials(),cache_discovery=False)
    original=service._http.request
    def fence(url,method='GET',**kwargs):
        if method.upper()!='GET':
            if method.upper()!='POST' or url.split('?')[0]!='https://sheets.googleapis.com/v4/spreadsheets/'+env['SPREADSHEET_ID']+'/values:batchUpdate':
                raise StateError('item_runner_queue_write_forbidden')
            body=json.loads(kwargs['body']);data=body.get('data',[])
            import re
            if (set(body)!={'valueInputOption','data'} or body['valueInputOption']!='RAW' or len(data)!=1
                or not re.fullmatch("'"+QUEUE+"'!A[0-9]+:F[0-9]+",data[0]['range'])
                or len(data[0]['values'])!=1 or data[0]['values'][0][0]!=request_id):
                raise StateError('item_runner_queue_write_forbidden')
        return original(url,method=method,**kwargs)
    service._http.request=fence
    from .pdf_grouping_ui import GroupingSheet
    captured=capture(GroupingSheet(service,env['SPREADSHEET_ID']),card(candidate,categories=readers.read()[2]),
        snapshot,request_id=request_id,clock=now_jst_string,write_enabled=True)
    if captured['request_id']!=request_id:raise StateError('item_runner_queue_request_conflict')
    # Existing accounting rows are read-only. Never infer duplicate from amount.
    from .sheets import SheetsDB
    db=SheetsDB(env['SPREADSHEET_ID'],service=build('sheets','v4',credentials=credentials(READ_ONLY_SCOPES),cache_discovery=False))
    from .models import ReceiptResult
    unit=candidate['legacy']['identity']['receipt_unit_id']
    tables=[db.get("'"+title+"'!A1:M10000") for title in ('レシート','支出明細','取込データ')]
    if any(len(rows)>=10000 for rows in tables):raise StateError('item_runner_ledger_truncated')
    collision=any(unit in str(row[0]) for rows in tables for row in rows[1:] if row)
    if collision:plan={**plan,'status':'needs_review','issues':plan['issues']+['ledger_identity_collision_or_duplicate']}
    if plan['status']=='ready_to_confirm':
        from .pdf_receipt_write_canary import check_duplicates
        try:check_duplicates(db,unit,ReceiptResult.model_validate(plan['parsed']))
        except StateError:plan={**plan,'status':'needs_review','issues':plan['issues']+['duplicate_candidate_requires_review']}
    # Reuse the existing row planner directly, never run image analysis/gate.
    result={'status':'item_plan_validated_not_written' if plan['status']=='ready_to_confirm' else 'item_plan_held',
        'validation_state':plan['status'],'item_count':len(candidate['items']),'accounting_appends':0,
        'medical_manual_completed':0,'gemini_calls':0,'source_moves':0,'archive':0,
        'duplicate_identity_collision':collision,'queue_status':captured['status'],'queue_appends':captured['appended']}
    if plan['status']=='ready_to_confirm':
        from .utils import now_jst_string
        rows=accounting_plan(candidate,plan,readers.read()[2],timestamp=now_jst_string())
        result.update(planned_receipts=sum(t=='レシート' for t,_ in rows),
            planned_expense_rows=sum(t=='支出明細' for t,_ in rows),planned_imports=sum(t=='取込データ' for t,_ in rows),
            planned_expense_total=sum(r[4] for t,r in rows if t=='支出明細'),plan_digest=digest(rows))
    return result
