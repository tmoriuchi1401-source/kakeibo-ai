"""Machine-only, metadata archive on the existing year-partitioned repository.

No session reads, finance writes, image retention or original moves. The server
rereads raw ledger rows/formulas and the registered source before attesting.
"""
from datetime import datetime,timezone
import base64
import json
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from app.drive_run_state import StateError
from app.pdf_intake_authority import digest
from app.receipt_audit import event
from app.receipt_audit_firestore import FirestoreAuditRepository
from .generic_intake import verify_machine_token,RegisteredPageDrive
from .backend import timestamp


def authenticate(router,bearer):
    if not router.intake_config or not isinstance(bearer,str) or not bearer.startswith('Bearer '):
        raise StateError('pdf_intake_machine_identity_rejected')
    verify_machine_token(bearer[7:],router.origin,router.info['client_id'],clock=router.clock)


def root(router):
    return router.client.collection('kakeibo_pdf_sources').document(digest(['pdf-source-history-v1',router.intake_config]))


def summary(router,query):
    if not isinstance(query,dict) or set(query)!={'source_id','source_hash','mode'} or query['mode'] not in {'lookup','attest'}:
        raise StateError('pdf_intake_archive_query_invalid')
    from app.pdf_intake_registry import DriveRegistryIO,RegistryStore,TERMINAL
    io=DriveRegistryIO(router.intake_config,router.info,writable=False);store=RegistryStore(io,io.anchor)
    reference=root(router).collection('sources').document(digest(query['source_id']))
    previous=reference.get(retry=None,timeout=10)
    if previous.exists:
        saved=previous.to_dict()
        if (saved.get('schema_version')!=1 or saved.get('retention_class')!='permanent'
                or saved.get('summary_digest')!=digest({k:v for k,v in saved.items() if k!='summary_digest'})):
            raise StateError('pdf_intake_archive_history_invalid')
        if saved['source_file_id']!=query['source_id'] or saved['source_content_hash']!=query['source_hash']:
            raise StateError('pdf_intake_archive_source_changed')
        return {'archive_allowed':True,'summary_digest':saved['summary_digest'],'replayed':True}
    if query['mode']=='lookup':return {'archive_allowed':False,'summary_digest':None,'replayed':False}
    state=store.load();pages=[(k,r) for k,r in state['pages'].items() if r['page']['source']['source_file_id']==query['source_id']]
    if not pages or len(pages)!=pages[0][1]['page']['source']['page_count']:
        raise StateError('pdf_intake_archive_pages_incomplete')
    expected=pages[0][1]['page']['source']
    if expected['source_content_hash']!=query['source_hash']:raise StateError('pdf_intake_archive_source_changed')
    for k,r in pages:
        if r['page']['source']!=expected or not r['units'] or any(u['status'] not in TERMINAL for u in r['units'].values()):
            raise StateError('pdf_intake_archive_not_terminal')
    drive=RegisteredPageDrive.__new__(RegisteredPageDrive)
    drive.io=io;drive._source_cache=None;drive.page=type('SourcePage',(),{'source':type('Source',(),expected)()})()
    drive.source(query['source_id'])
    session=AuthorizedSession(service_account.Credentials.from_service_account_info(router.info,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']))
    tables={}
    for title,width in [('レシート','I'),('支出明細','M'),('取込データ','L')]:
        modes=[]
        for mode in ['UNFORMATTED_VALUE','FORMULA']:
            response=session.get('https://sheets.googleapis.com/v4/spreadsheets/'+router.intake_spreadsheet+'/values/'+f"'{title}'!A1:{width}10000",
                params={'valueRenderOption':mode,'dateTimeRenderOption':'SERIAL_NUMBER'},timeout=10,allow_redirects=False)
            if response.status_code!=200:raise StateError('pdf_intake_archive_ledger_unavailable')
            rows=response.json().get('values',[])
            if not rows or len(rows)>=10000:raise StateError('pdf_intake_archive_ledger_extent')
            modes.append(rows)
        tables[title]=modes
    compact=[];scope=digest(['pdf-intake-permanent-v1',io.anchor])
    audit=FirestoreAuditRepository(router.client,scope,write_enabled=True)
    for key,record in pages:
        page=record['page'];units=[]
        for uid,unit in record['units'].items():
            intent=unit['intent'];readback=unit['readback']
            if intent is None or intent['status']!='complete' or readback is None:
                raise StateError('pdf_intake_archive_exact_readback_required')
            for operation in intent['plan']['operations']:
                title=operation['table'];width={'レシート':9,'支出明細':13,'取込データ':12}[title]
                pad=lambda x:list(x)+['']*max(0,width-len(x))
                for row in operation['rows']:
                    for table in tables[title]:
                        matching=[r for r in table[1:] if r and str(r[0])==str(row[0])]
                        if len(matching)!=1 or pad(matching[0])!=pad(row):raise StateError('pdf_intake_archive_ledger_mismatch')
            proof=unit['posting_authority'];actor=digest(['service-account',router.info['client_id']])
            if proof is not None:
                from app.pdf_intake_authority import DecisionSealer
                sealer=DecisionSealer(base64.urlsafe_b64decode(router.key),digest(['https://accounts.google.com',router.identity.owner_subject]))
                verified=sealer.verify(proof,proof['decision']['binding']);actor=verified['actor_id']
            identity={**{k:page['source'][k] for k in ('source_file_id','source_content_hash','page_count')},
                'page_number':page['page_number'],'page_identity':page['stable_page_identity'],'receipt_unit_id':uid,
                'review_identity':page['review_identity'],'revision':page['authority_revision']}
            when=intent['claimed_at']
            # Runner persists a JST wall-clock stamp, never an arbitrary browser time.
            from zoneinfo import ZoneInfo
            when=datetime.fromisoformat(when).replace(tzinfo=ZoneInfo('Asia/Tokyo')) if '+' not in when and not when.endswith('Z') else datetime.fromisoformat(when)
            value=event(identity,request_id=intent['request_id'],request_digest=digest(intent),event_type='imported',
                ledger_id=readback['receipt_id'],decision='confirmed',actor_id=actor,confirmed_at=when.isoformat(),
                authority_digest=digest(proof) if proof else intent['plan']['digest'],reason_code='writer_exact_readback')
            audit.commit(value,audit.get_current(identity)['state_revision'])
            units.append({'receipt_unit_id':uid,'ledger_id':readback['receipt_id'],'import_id':readback['import_id'],
                          'event_ref':audit.lookup(intent['request_id'])['event_ref'],'authority_digest':value['authority_digest']})
        saved_hga=record['authority'].get('single_page_ai')
        # Keep only sealed decision metadata, never the request/session/raw input.
        compact.append({'page':page,'hga_decision':None if saved_hga is None else saved_hga['proof'],'units':units})
    saved={**expected,'schema_version':1,'retention_class':'permanent','pages':compact}
    saved['summary_digest']=digest(saved)
    if len(json.dumps(saved))>512*1024:raise StateError('pdf_intake_archive_summary_size')
    if store.load()!=state:raise StateError('pdf_intake_archive_state_changed')
    reference.create(saved,retry=None,timeout=10)
    if reference.get(retry=None,timeout=10).to_dict()!=saved:raise StateError('pdf_intake_archive_readback_required')
    return {'archive_allowed':True,'summary_digest':saved['summary_digest'],'replayed':False}


def preflight(router,query):
    if not isinstance(query,dict) or set(query)!={'source_id'}:raise StateError('pdf_intake_preflight_invalid')
    from app.pdf_intake_registry import DriveRegistryIO,RegistryStore
    io=DriveRegistryIO(router.intake_config,router.info,writable=False)
    state=RegistryStore(io,io.anchor).load()
    fields='id,parents(id),mimeType,labels(trashed),etag'
    meta=io.metadata(query['source_id'],fields)
    if meta.get('parents') not in ([{'id':io.config['inbox_id']}],[{'id':io.config['processed_id']}]) or meta.get('mimeType')!='application/pdf' or meta.get('labels',{}).get('trashed'):
        raise StateError('pdf_intake_preflight_source_invalid')
    raw=io._request('GET','https://www.googleapis.com/drive/v2/files/'+query['source_id'],params={'alt':'media'}).content
    if io.metadata(query['source_id'],fields)!=meta:raise StateError('pdf_intake_preflight_source_changed')
    from hashlib import sha256
    return {'status':'ok','source_sha256':sha256(raw).hexdigest(),'registry_read':True,'write':0}
