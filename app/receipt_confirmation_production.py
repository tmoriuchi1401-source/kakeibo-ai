"""Existing Actions receipt intake, with no AI credentials in this process."""
import json,os,sys
from hashlib import sha256
from pathlib import Path

from .drive_run_state import DriveStateTransport,StateError
from .receipt_reimport_production import ResultBinding,ReimportStore
from .receipt_confirmation import ReceiptConfirmation,TITLE,CHOICES,medical_categories
from .receipt_privacy_gate import evaluate_receipt_privacy
from .drive_receipts import normalize_folder_id,is_supported_receipt_mime


def configure_ui(db,validation_only=False):
    meta=db.svc.spreadsheets().get(spreadsheetId=db.sid,fields='sheets(properties)').execute(num_retries=0)
    sheet=next(s['properties'] for s in meta['sheets'] if s['properties']['title']==TITLE)
    sid=sheet['sheetId'];requests=[]
    def validation(col,choices):
        return {'setDataValidation':{'range':{'sheetId':sid,'startRowIndex':1,'startColumnIndex':col,'endColumnIndex':col+1},
            'rule':{'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':v} for v in choices]},'strict':True,'showCustomUi':True}}}
    choices=medical_categories(db.categories())
    if not choices:raise StateError('medical_category_master_missing')
    requests.extend([validation(12,CHOICES),validation(10,['｜'.join(c) for c in choices])])
    if validation_only:
        db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid,body={'requests':requests}).execute(num_retries=0)
        return
    requests += [{'updateSheetProperties':{'properties':{'sheetId':sid,'gridProperties':{'frozenRowCount':1,'frozenColumnCount':3}},'fields':'gridProperties.frozenRowCount,gridProperties.frozenColumnCount'}},
        {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':0,'endRowIndex':1,'endColumnIndex':16},'cell':{'userEnteredFormat':{'backgroundColor':{'red':.94,'green':.95,'blue':.96},'textFormat':{'bold':True},'wrapStrategy':'WRAP'}},'fields':'userEnteredFormat'}},
        {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':1,'endColumnIndex':16},'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP'}},'fields':'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment'}},
        {'repeatCell':{'range':{'sheetId':sid,'startRowIndex':1,'startColumnIndex':7,'endColumnIndex':15},'cell':{'userEnteredFormat':{'backgroundColor':{'red':1,'green':.98,'blue':.88}}},'fields':'userEnteredFormat.backgroundColor'}},
        {'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'COLUMNS','startIndex':0,'endIndex':1},'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}}]
    for a,b,width in [(1,3,110),(3,4,180),(4,7,300),(7,13,160),(13,16,240)]:
        requests.append({'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'COLUMNS','startIndex':a,'endIndex':b},'properties':{'pixelSize':width},'fields':'pixelSize'}})
    db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid,body={'requests':requests}).execute(num_retries=0)


def verify_receipt_source(source, original_folder, inbox_folder, processed_folder, read_metadata, read_bytes):
    """Bind a review to unchanged bytes in an allowed receipt lifecycle location."""
    sid=source['source_id']
    def metadata():
        m=read_metadata(sid)
        if (m.get('id')!=sid or m.get('trashed') or m.get('mimeType')!=source['mime_type']
                or not isinstance(m.get('parents'),list) or len(m['parents'])!=1):
            raise StateError('confirmation_source_changed')
        return m
    before=metadata()
    parents=before['parents']
    in_original=parents==[original_folder]
    moved_to_processed=(original_folder==inbox_folder and bool(processed_folder)
                        and parents==[processed_folder] and processed_folder!=inbox_folder)
    if not (in_original or moved_to_processed):
        raise StateError('confirmation_source_changed')
    saved_hash=source.get('sha256')
    if not saved_hash:
        # The intake scanner has not downloaded this new source yet. Its
        # metadata must still match exactly; the caller then hashes the bytes.
        if not in_original or before.get('version')!=source.get('version'):
            raise StateError('confirmation_source_changed')
    else:
        if (not isinstance(saved_hash,str) or len(saved_hash)!=64
                or any(c not in '0123456789abcdef' for c in saved_hash)):
            raise StateError('confirmation_source_changed')
        if sha256(read_bytes(sid)).hexdigest()!=saved_hash:
            raise StateError('confirmation_source_changed')
    if metadata()!=before:
        raise StateError('confirmation_source_changed')
    return before


def open_context(env,apply):
    from .google_clients import drive_service,read_only_drive_service,read_only_sheets_service,download_drive_file
    from .settings import Settings,service_account_source
    from .private_state_bindings import unwrap
    from .sheets import SheetsDB, SheetsReadPacer
    settings=Settings();settings.validate(need_sheet=True,need_drive=True)
    path,info=service_account_source();info=info or json.loads(Path(path).read_bytes())
    config=json.loads(env['RECEIPT_CONFIRMATION_BINDING'])
    fid=unwrap('RECEIPT_REIMPORT_FILE_ID',config['file'],info['private_key'])
    if fid in [env.get(n) for n in ('AMAZON_STATE_FILE_ID','AUPAY_CARD_STATE_FILE_ID','BANK_STATE_FILE_ID','KAKEIBO_RUN_LEDGER_FILE_ID')]:
        raise StateError('confirmation_store_must_be_separate')
    drive=drive_service() if apply else read_only_drive_service()
    store=ReimportStore(DriveStateTransport(drive,ResultBinding(env['KAKEIBO_STATE_FOLDER_ID'],fid)),config['manifest'],settings.spreadsheet_id)
    for target in (env['KAKEIBO_STATE_FOLDER_ID'],fid):
        meta=drive.files().get(fileId=target,fields='owners(emailAddress),permissions(type,role,emailAddress,deleted)').execute(num_retries=0)
        owner=store.value['manifest']['owner_email'];sa=store.value['manifest']['sa_email']
        if [o['emailAddress'] for o in meta['owners']]!=[owner] or {(p['type'],p['role'],p['emailAddress']) for p in meta['permissions'] if not p.get('deleted')}!={('user','owner',owner),('user','writer',sa)}:
            raise StateError('confirmation_store_permissions_changed')
    reader=read_only_drive_service()
    inbox=normalize_folder_id(settings.receipt_drive_folder_id)
    processed_folder=getattr(settings,'processed_drive_folder_id','')
    processed=normalize_folder_id(processed_folder) if processed_folder else ''
    def metadata(source,folder):
        return verify_receipt_source(source,folder,inbox,processed,
            lambda sid:reader.files().get(fileId=sid,fields='id,parents,mimeType,version,trashed').execute(num_retries=0),
            lambda sid:download_drive_file(sid,reader))
    db=SheetsDB(settings.spreadsheet_id,service=None if apply else read_only_sheets_service(),
        read_pacer=SheetsReadPacer() if apply else None,read_retry_base=20)
    return settings,store,db,metadata


def sync_review_visibility(review):
    """Archive completed/obsolete rows by hiding, preserving every owner cell."""
    rows=review.ui_rows()
    if not rows:return
    db=review.db
    result=db.svc.spreadsheets().get(spreadsheetId=db.sid,ranges=[f"'{TITLE}'!A1:P{max(n for n,_ in rows.values())}"],
        fields='sheets(properties(sheetId),data(startRow,rowMetadata(hiddenByUser)))').execute(num_retries=0)
    sheet=result['sheets'][0];sid=sheet['properties']['sheetId'];hidden={}
    for grid in sheet.get('data',[]):
        for i,value in enumerate(grid.get('rowMetadata',[]),grid.get('startRow',0)+1):hidden[i]=value.get('hiddenByUser',False)
    requests=[]
    for key,(n,row) in rows.items():
        hide=not review.needs_attention(key)
        if hidden.get(n,False)!=hide:
            requests.append({'updateDimensionProperties':{'range':{'sheetId':sid,'dimension':'ROWS','startIndex':n-1,'endIndex':n},
                'properties':{'hiddenByUser':hide},'fields':'hiddenByUser'}})
    from .receipt_confirmation_ui import dropdown_requests
    requests.extend(dropdown_requests(review,sid,rows))
    if requests:db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid,body={'requests':requests}).execute(num_retries=0)


def execute(env,apply):
    if env.get('GEMINI_API_KEY'):raise StateError('medical_process_must_not_receive_ai_key')
    from .google_clients import read_only_drive_service,download_drive_file
    settings,store,db,metadata=open_context(env,apply)
    reader=read_only_drive_service()
    review=ReceiptConfirmation(store,db,metadata)
    from .medical_auto_posting import AUTO_POLICIES,apply_automatic,in_scope,owner_blocked
    from .medical_local_owner import blocked as local_owner_blocked
    from .receipt_local_ocr import enabled as local_ocr_enabled
    policy=env.get('MEDICAL_DERIVED_AI_POLICY','');automatic=policy in AUTO_POLICIES
    if not apply:
        from copy import deepcopy
        from types import SimpleNamespace
        # Exercise the real validator with current owner input and read-only
        # clients. Only the in-memory store receives diagnostic state changes.
        memory=SimpleNamespace(value=deepcopy(store.value))
        memory.save=lambda value:setattr(memory,'value',deepcopy(value))
        preview=ReceiptConfirmation(memory,db,metadata)
        preview.capture_inputs()
        eligible=preview.apply_confirmations(dry_run=True)
        closed=sum(x['status']=='closed_user' and review.items[k]['status']!='closed_user' for k,x in preview.items.items())
        report={'found':len(review.items),'written':0,'failure':0,'review_eligible':eligible,
            'review_closable':closed,**preview.review_counts()}
        if env.get('KAKEIBO_RUN_LEDGER_FILE_ID'):
            from .drive_run_state import StateBinding
            from .production_ledger import ProductionLedger
            from .receipt_recovery_audit import audit_rows
            binding=StateBinding('production_run',settings.spreadsheet_id,env['KAKEIBO_STATE_FOLDER_ID'],env['KAKEIBO_RUN_LEDGER_FILE_ID'])
            ledger=ProductionLedger(DriveStateTransport(reader,binding),binding)
            tables=review.tables()
            raw={'spreadsheet_id':settings.spreadsheet_id,'imports':tables['import_rows'],'expenses':tables['expense_rows']}
            report['recovery_audit']=audit_rows(raw,tables['receipt_rows'],review,ledger)
            if store.transport.read()!=store.payload or ledger.transport.read()!=ledger.payload:
                raise StateError('receipt_audit_state_changed')
        return report
    review.capture_inputs()
    review.prepare_general()
    review.resolve_general_without_writes()
    def archive():
        from .receipt_confirmation_archive import archive_confirmations
        from .google_clients import drive_service
        destination=getattr(settings,'processed_drive_folder_id','')
        if not destination:return 0
        return archive_confirmations(review,normalize_folder_id(settings.receipt_drive_folder_id),
            normalize_folder_id(destination),drive_service(),lambda sid:download_drive_file(sid,reader))
    resumed_archives=archive()
    if env.get('MEDICAL_FINALIZE_ONLY')=='true':
        written=review.apply_confirmations();auto_written=0
        if automatic:
            from .settings import service_account_source
            from .medical_crop_review import identity_key
            key_path,key_info=service_account_source();key_info=key_info or json.loads(Path(key_path).read_bytes())
            from .medical_auto_posting import WRITE_LIMIT
            remaining=max(0,WRITE_LIMIT-int(env.get('MEDICAL_LOCAL_WRITTEN','0')))
            auto_written=apply_automatic(review,identity_key=identity_key(key_info['private_key']),policy=policy,write_limit=remaining)
        written+=auto_written;archived=resumed_archives+archive();rows=review.render()
        if store.value.get('confirmation_ui_version')!=2:
            configure_ui(db,validation_only=True)
            from copy import deepcopy
            value=deepcopy(store.value);value['confirmation_ui_version']=2;store.save(value)
        sync_review_visibility(review)
        if review.refresh_needed():
            from .expense_view import ExpenseViewPipeline
            ExpenseViewPipeline(db).refresh();review.mark_refreshed()
        return {'found':rows,'written':written,'archived':archived,'medical_auto_written':auto_written,'failure':0,**review.review_counts(),
            'medical_pending':sum(x['kind']=='medical' and x['status']=='waiting' for x in review.items.values())}
    folder=normalize_folder_id(settings.receipt_drive_folder_id)
    result=reader.files().list(q=f"'{folder}' in parents and trashed=false",pageSize=100,orderBy='createdTime',
        fields='nextPageToken,files(id,mimeType,version)',supportsAllDrives=True,includeItemsFromAllDrives=True).execute(num_retries=0)
    if result.get('nextPageToken'):raise StateError('receipt_inbox_collection_incomplete')
    plans=[];medical_plans=[];blocked_sources=set();counts={'found':0,'medical_detected':0,'blocked':0,'written':0,'medical_local_written':0,'failure':0,'archived':resumed_archives}
    previous_medical={x['source']['source_id'] for x in review.items.values() if x['kind']=='medical'}
    for f in result.get('files',[]):
        if not is_supported_receipt_mime(f['mimeType']):continue
        counts['found']+=1
        source=dict(source_id=f['id'],mime_type=f['mimeType'],version=f['version'])
        before=metadata(source,folder)
        payload=download_drive_file(f['id'],reader)
        if before!=metadata(source,folder):raise StateError('confirmation_source_changed')
        source['sha256']=sha256(payload).hexdigest()
        owner_route=review.route_owner_intake(source,folder)
        if owner_route:
            # A kind answer is not authority to send pixels or infer amounts.
            # Medical confirmation uses the same explicit input writer below.
            if owner_route!='医療' or not (automatic and local_ocr_enabled()):
                if owner_route=='医療':counts['medical_detected']+=1
                continue
        gate=evaluate_receipt_privacy(payload,f['mimeType'],**({'known_source_classification':'medical'} if f['id'] in previous_medical or owner_route=='医療' else {}))
        if gate.classification=='medical':
            review.observe_medical(source,folder);counts['medical_detected']+=1
            review.resolve_intake_kind(source,folder,gate)
            if env.get('MEDICAL_PREPARE_DIR'):
                from .medical_candidate_preparation import prepare
                from .receipt_confirmation import review_id
                import base64
                key=base64.b64decode(env['MEDICAL_CROP_ATTESTATION_KEY'],validate=True)
                rid=review_id('medical',source)
                if review.items[rid]['status'] in {'applied','pending','closed_user'}:
                    continue
                # Automatic mode is independent of saved UI coordinates and
                # owner attestations, including malformed/old manual records.
                crop_review=None if automatic else store.value.get('medical_crop_reviews',{}).get(rid)
                review_key=None
                if crop_review is not None or automatic:
                    from .settings import service_account_source
                    from .medical_crop_review import identity_key
                    path,info=service_account_source();info=info or json.loads(Path(path).read_bytes())
                    review_key=identity_key(info['private_key'])
                owner_guard=local_owner_blocked if local_ocr_enabled() else owner_blocked
                if automatic and (not in_scope(source,store.value,policy) or owner_guard(source,store.value)):
                    packet,crop={'source':source,'fields':{},'status':'held','reason':'automatic_scope_or_owner_input'},None
                else:
                    packet,crop=prepare(source,payload,key,crop_review=crop_review,review_key=review_key,automatic=automatic,
                                        document_key=review_key if automatic else None)
                if packet['status']=='local_ready':
                    from .medical_local_reading import apply_local
                    from .medical_auto_posting import WRITE_LIMIT
                    from .models import ReceiptResult
                    review.render()
                    if counts['medical_local_written']<WRITE_LIMIT:
                        parsed=ReceiptResult.model_validate(packet['local_parsed'])
                        from .medical_local_duplicate import existing_reader
                        destination=normalize_folder_id(settings.processed_drive_folder_id) if getattr(settings,'processed_drive_folder_id','') else ''
                        read_existing=existing_reader(reader,lambda sid:download_drive_file(sid,reader),destination,review_key,review)
                        if apply_local(review,source,folder,parsed,packet['local_provenance'],read_existing=read_existing):
                            counts['medical_local_written']+=1;counts['written']+=1
                            counts['archived']+=archive()
                            continue
                        reason='existing_accounting_or_review_conflict'
                    else:reason='automatic_run_limit'
                    packet.pop('local_parsed',None);packet.update(status='held',reason=reason)
                packet['review_id']=review_id('medical',source)
                if crop is not None:
                    # Derived pixels only; no original or OCR file is written.
                    path=Path(env['MEDICAL_PREPARE_DIR'])/(packet['review_id']+'.png')
                    path.write_bytes(crop);path.chmod(0o600)
                    packet['crop_file']=path.name
                medical_plans.append(packet)
        elif gate.classification=='normal' and gate.gemini_allowed:
            review.resolve_intake_kind(source,folder,gate)
            if env.get('RECEIPT_SCAN_PLAN'):
                directory=Path(env['RECEIPT_SCAN_PLAN']).parent
                original=directory/(sha256(f['id'].encode()).hexdigest()+'.bin')
                original.write_bytes(payload);original.chmod(0o600)
                plans.append(dict(source,path=str(original)))
        else:
            counts['blocked']+=1;blocked_sources.add(f['id'])
            review.observe_intake_hold(source,folder,gate)
    review.finish_intake_scan(blocked_sources)
    if not env.get('MEDICAL_PREPARE_DIR'):
        counts['written']+=review.apply_confirmations()
        counts['archived']+=archive()
    counts['medical_pending']=sum(x['kind']=='medical' and x['status']=='waiting' for x in review.items.values())
    create_ui=TITLE not in db.sheet_titles()
    counts['review_rows']=review.render()
    if create_ui or not store.value.get('confirmation_ui_configured'):
        configure_ui(db)
        from copy import deepcopy
        value=deepcopy(store.value);value['confirmation_ui_configured']=True;store.save(value)
    sync_review_visibility(review)
    counts.update(review.review_counts())
    if env.get('MEDICAL_PREPARE_DIR'):
        path=Path(env['MEDICAL_PREPARE_DIR'])/'medical-plan.json'
        path.write_text(json.dumps(medical_plans,ensure_ascii=True),encoding='utf-8');path.chmod(0o600)
    if env.get('RECEIPT_SCAN_PLAN'):
        path=Path(env['RECEIPT_SCAN_PLAN']);path.write_text(json.dumps({'sources':plans}),encoding='utf-8');path.chmod(0o600)
    if review.refresh_needed():
        # Existing display writer, only after actual accounting changes.
        from .expense_view import ExpenseViewPipeline
        ExpenseViewPipeline(db).refresh()
        review.mark_refreshed()
    return counts


def main():
    try:
        from .production_flow import verify_execution_boundary,REPO
        import subprocess
        env=dict(os.environ);head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
        verify_execution_boundary(env,head)
        if len(sys.argv)!=2 or sys.argv[1] not in {'preview','apply'}:raise StateError('confirmation_mode_invalid')
        print(json.dumps(execute(env,sys.argv[1]=='apply'),sort_keys=True))
    except Exception as error:
        from .production_source import source_error_code
        print(json.dumps({'failure':1,'error':source_error_code(error)}));raise SystemExit(1)

if __name__=='__main__':main()
