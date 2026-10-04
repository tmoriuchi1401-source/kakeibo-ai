"""Manual request service using existing bindings, credentials and backend.

The existing dispatch gate stays off on the Draft branch. No schedule, new
secret, OAuth scope, image storage, general receipt pipeline or move hook.
"""
from .drive_run_state import StateError
from .pdf_page_kind import PageKindConfirmation
from .pdf_page_review import PageReviewSheet, process_page_request, publish_current
from .pdf_page_medical import PdfManualConfirmation, manual_source
from .receipt_confirmation import review_id
from .receipt_pdf_units import _digest


def medical_factory(kinds, store, db, inbox):
    """Existing receipt confirmation Drive正本 + existing Medical writer."""
    def verify(source, folder):
        proof=source.get('pdf_page')
        if not proof or folder!=inbox:raise StateError('confirmation_source_changed')
        try:
            _,proposal,answers=kinds.current(proof['original_file_id'])
            number=proof['page_number']
            page=proposal['pages'][number-1]
            if source!=manual_source(page,answers.get(number)):
                raise StateError('confirmation_source_changed')
        except Exception:raise StateError('confirmation_source_changed') from None
    def create(source, read_inputs):
        return PdfManualConfirmation(store,db,source,inbox,verify,read_inputs)
    return create


def medical_results(kinds, store, db, inbox):
    """Projection of durable manual outcomes; never believe a status cell."""
    result={}
    for key,item in store.value.get('confirmation_items',{}).items():
        source=item.get('source',{})
        if not source.get('pdf_page'):continue
        if item['status']=='applied':
            # Common read-back validator, with no writer invocation.
            review=PdfManualConfirmation(store,db,source,inbox,lambda *_:None,lambda:['']*8)
            if not review._complete(item.get('plan',[])) or not item.get('plan'):
                raise StateError('confirmation_readback_mismatch')
            result[key]='医療確定済み'
        elif item['status']=='pending':result[key]='医療反映確認待ち'
        elif item.get('error'):result[key]=item['error']
    return result


def execute_shared(env, grouping, legacy_sheet, reader, mode, *, verified_pages=None):
    """Legacy dispatcher delegates only when the shared sheet schema is active."""
    if env.get('GEMINI_API_KEY'):raise StateError('medical_process_must_not_receive_ai_key')
    from .receipt_confirmation_production import open_context
    settings,store,db,_=open_context(env,True)
    # Existing state has exact read-back in ReimportStore.save. No new binding.
    from .drive_receipts import normalize_folder_id
    inbox=normalize_folder_id(settings.receipt_drive_folder_id)
    from .pdf_grouping_authority import DurablePdfGrouping
    selection=None
    if mode=='review':
        _,capture=legacy_sheet.request(env.get('PDF_GROUPING_REQUEST_ID',''))
        if isinstance(capture,dict) and capture.get('identity',{}).get('kind') in {'medical','page_kind'}:
            selection=capture['identity'].get('page_numbers')
    # A migrated source may use its independent v2 source/ordinal verifier.
    # Unmigrated legacy callers keep the original strict pixel verification.
    verifier=verified_pages or reader.verify_pages
    verified=DurablePdfGrouping(grouping.store,lambda sid,previous:verifier(sid,previous,numbers=selection),
                               proposer=grouping.proposer,clock=grouping.clock)
    kinds=PageKindConfirmation(verified)
    sheet=PageReviewSheet(legacy_sheet.service,legacy_sheet.sid,db.categories(),
        load_medical_results=lambda:medical_results(kinds,store,db,inbox))
    if mode=='install':
        sheet.install();publish_current(kinds,sheet);return {'ui_installed':1}
    if mode=='refresh':
        displayed=held=0
        for source_id in reader.source_ids():
            view=grouping.display(source_id)
            if view['proposal'] is None:held+=1
            else:displayed+=1
        publish_current(kinds,sheet)
        return {'displayed':displayed,'held':held}
    if mode=='review':
        result=process_page_request(kinds,sheet,env.get('PDF_GROUPING_REQUEST_ID',''),
            medical_factory=medical_factory(kinds,store,db,inbox))
        # Medical errors may contain manually supplied facility/duplicate IDs;
        # only a fixed outcome code reaches workflow logs.
        return {'result':'review_processed','gemini_calls':0,'archive_moves':0}
    raise StateError('grouping_operation_invalid')
