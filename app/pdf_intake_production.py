"""Existing three-hour receipt flow: bounded registered PDF pages only.

Config is a private Drive reference, not a GitHub binding or session dump.
Scanner has no AI key. Runner uses separate page and posting capabilities.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from time import monotonic
from time import time
from pathlib import Path
from urllib.parse import urlencode

from .drive_run_state import StateError
from .pdf_intake_authority import digest, page_key, eligible_for_hga
from .pdf_intake_registry import DriveRegistryIO, RegistryStore, TERMINAL
from .pdf_intake_runner import PageIntake, observe_source
from .receipt_item_review import card
from .receipt_item_review_ui import SCHEMA, encoded_rows, requests, read_snapshot, cell_range
from .pdf_review_fields import SHEET_ID, original_uri

CONFIG_SCHEMA='pdf-intake-production-config-v1'
PAGE_MARKER='pdf-intake-page-review-v1'
MAX_FILES=3
MAX_NEW_PAGE_ANALYSES=1
PROCESS_BUDGET_SECONDS=600


def page_count(raw):
    from pypdf import PdfReader
    if type(raw) is not bytes or len(raw)>50*1024*1024:raise StateError('pdf_intake_source_size_limit')
    try:
        reader=PdfReader(BytesIO(raw),strict=True)
        if reader.is_encrypted:raise ValueError()
        count=len(reader.pages)
        if count>50:raise ValueError()
        return count
    except Exception:raise StateError('pdf_intake_source_structure_invalid') from None


class Context:
    def __init__(self,env,settings,db,*,writable):
        from .settings import service_account_source
        from .google_clients import read_only_drive_service,download_drive_file
        path,info=service_account_source();self.info=info or json.loads(Path(path).read_bytes())
        fid=env.get('PDF_INTAKE_CONFIG_FILE_ID','')
        import re
        if not re.fullmatch('[A-Za-z0-9_-]{10,150}',fid):raise StateError('pdf_intake_config_missing')
        self.reader=read_only_drive_service()
        meta=self.reader.files().get(fileId=fid,fields='id,mimeType,parents,owners(emailAddress),permissions(type,role,emailAddress,deleted)').execute(num_retries=0)
        raw=download_drive_file(fid,self.reader)
        if len(raw)>4096:raise StateError('pdf_intake_config_invalid')
        config=json.loads(raw)
        if (set(config)!={'schema','registry','origin','spreadsheet_id'} or config['schema']!=CONFIG_SCHEMA
                or config['spreadsheet_id']!=settings.spreadsheet_id or meta.get('mimeType')!='application/json'
                or meta.get('parents')!=[config['registry']['folder_id']]):raise StateError('pdf_intake_config_invalid')
        owner=[o.get('emailAddress') for o in meta.get('owners',[])]
        if (len(owner)!=1 or digest(owner[0])!=config['registry']['owner_digest']
                or {(p.get('type'),p.get('role'),p.get('emailAddress')) for p in meta.get('permissions',[]) if not p.get('deleted')}
                !={('user','owner',owner[0]),('user','writer',self.info['client_email'])}):raise StateError('pdf_intake_config_acl_changed')
        from .drive_receipts import normalize_folder_id
        if (config['registry']['inbox_id']!=normalize_folder_id(settings.receipt_drive_folder_id)
                or config['registry']['processed_id']!=normalize_folder_id(settings.processed_drive_folder_id)):
            raise StateError('pdf_intake_folder_binding_changed')
        self.config,self.db=config,db
        self.io=DriveRegistryIO(config['registry'],self.info,writable=writable)
        self.store=RegistryStore(self.io,self.io.anchor);self.store.load()
        from .pdf_intake_proof_client import ProofClient
        self.proof=ProofClient(config['origin'],self.info)

    def source(self,sid):
        from .google_clients import download_drive_file,read_only_drive_service
        # OCR/AI can take minutes. Freshness checks use a fresh transport
        # rather than a connection left idle during local processing.
        reader=read_only_drive_service()
        fields='id,mimeType,parents,version,trashed,size'
        before=reader.files().get(fileId=sid,fields=fields).execute(num_retries=0)
        if (before.get('id')!=sid or before.get('mimeType')!='application/pdf' or before.get('trashed')
                or before.get('parents') not in ([self.config['registry']['inbox_id']],[self.config['registry']['processed_id']])):
            raise StateError('pdf_intake_source_location_changed')
        if not 0<int(before.get('size',0))<=50*1024*1024:raise StateError('pdf_intake_source_size_limit')
        raw=download_drive_file(sid,reader)
        if len(raw)>50*1024*1024 or reader.files().get(fileId=sid,fields=fields).execute(num_retries=0)!=before:
            raise StateError('pdf_intake_source_changed')
        return raw

    def runner(self,settings):
        def analyzer(permission):
            from .gemini_ai import GeminiAI
            from .page_receipt_ai import GeminiPageReceipts
            ai=GeminiAI(settings.gemini_api_key,settings.normal_receipt_gemini_model,request_attempts=1)
            return GeminiPageReceipts(ai.client,ai.model,permission)
        from .utils import now_jst_string
        return PageIntake(self.store,self.source,self.db,self.proof,analyzer,clock=now_jst_string,unit_limit=3)


class Projection:
    """Append to the existing sheet once. Never overwrite owner input."""
    def __init__(self,context):self.context=context;self.db=context.db
    def rows(self):
        rows=self.db.get_raw("'PDFページ確認'!A1:W5000")
        if len(rows)>=5000:raise StateError('pdf_intake_review_extent_limit')
        return rows
    def batch(self,requests):
        if requests:self.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.db.sid,body={'requests':requests}).execute(num_retries=0)
    def geometry(self):
        result=self.db.svc.spreadsheets().get(spreadsheetId=self.db.sid,fields='sheets(properties)').execute(num_retries=0)
        p=next((s['properties'] for s in result['sheets'] if s['properties']['title']=='PDFページ確認'),None)
        if p is None or p['sheetId']!=SHEET_ID:raise StateError('pdf_intake_review_sheet_changed')
        return p['gridProperties']
    def link(self,index,uri):
        self.batch([{'repeatCell':{'range':cell_range(index,1),
            'cell':{'userEnteredFormat':{'textFormat':{'link':{'uri':uri}}}},'fields':'userEnteredFormat.textFormat.link'}}])
    def pending(self,key):
        record=self.context.store.load()['pages'][key];page=record['page'];rows=self.rows()
        if any(len(r)>18 and r[17]==PAGE_MARKER and r[18]==key for r in rows):return
        kind=page['automatic_classification'];allowed=eligible_for_hga(page)
        label='医療：AI送信せず手入力確認待ち' if kind=='medical' else '給与：専用経路で処理' if kind=='payroll' else '一般レシートの送信許可待ち' if allowed else 'ページの原本確認が必要です'
        values=[]
        for f,l,v in [('target','対象',f"p{page['page_number']}"),('state','状態',label),('original','原本','ページ全体を見る'),('permission','送信許可','一般レシートとしてGemini送信を許可' if allowed else '外部AI送信は停止中')]:
            row=['']*23;row[0:2]=[l,v];row[17:21]=[PAGE_MARKER,key,json.dumps(page,separators=(',',':')),f];values.append(row)
        start=max(len(rows),145);geometry=self.geometry();batch=[]
        if geometry['rowCount']<start+len(values):batch.append({'appendDimension':{'sheetId':SHEET_ID,'dimension':'ROWS','length':start+len(values)-geometry['rowCount']}})
        if geometry['columnCount']<23:batch.append({'appendDimension':{'sheetId':SHEET_ID,'dimension':'COLUMNS','length':23-geometry['columnCount']}})
        batch.append({'updateCells':{'range':{'sheetId':SHEET_ID,'startRowIndex':start,'endRowIndex':start+len(values),'startColumnIndex':0,'endColumnIndex':23},
            'rows':[{'values':[{'userEnteredValue':{'stringValue':str(v)}} for v in row]} for row in values],'fields':'userEnteredValue'}})
        batch.append({'mergeCells':{'range':{'sheetId':SHEET_ID,'startRowIndex':start,'endRowIndex':start+len(values),'startColumnIndex':1,'endColumnIndex':17},'mergeType':'MERGE_ROWS'}})
        if self.rows()!=rows:raise StateError('pdf_intake_review_changed')
        self.batch(batch);self.link(start+2,original_uri(page['source']['source_file_id'],page['page_number']))
        if allowed:self.link(start+3,self.context.config['origin']+'/review?'+urlencode({'target':key}))
    def unit(self,key,uid):
        state=self.context.store.load();unit=state['pages'][key]['units'][uid];candidate=unit['candidate'];rows=self.rows()
        view=candidate['view'];found=[i for i,r in enumerate(rows) if len(r)>18 and r[17]==SCHEMA and r[18]==view['token']]
        if found:
            read_snapshot(rows,view,original_uri(state['pages'][key]['page']['source']['source_file_id'],state['pages'][key]['page']['page_number']))
        else:
            values=encoded_rows(view);start=max(len(rows),145);geometry=self.geometry()
            # Reuse the semantic formatter on a virtual, equal-length placeholder.
            virtual=deepcopy(rows)+[[] for _ in range(start-len(rows))]+deepcopy(values)
            batch,_,actual,extra=requests(virtual,deepcopy(virtual),[view],self.db.categories(),column_count=geometry['columnCount'],row_count=geometry['rowCount'])
            if actual!=start or extra:raise StateError('pdf_intake_review_projection_invalid')
            if geometry['rowCount']<start+len(values):batch.insert(0,{'appendDimension':{'sheetId':SHEET_ID,'dimension':'ROWS','length':start+len(values)-geometry['rowCount']}})
            if self.rows()!=rows:raise StateError('pdf_intake_review_changed')
            self.batch(batch)
            read_snapshot(self.rows(),view,original_uri(state['pages'][key]['page']['source']['source_file_id'],state['pages'][key]['page']['page_number']))
        if not candidate.get('ui_projected'):
            latest=self.context.store.load()
            if latest['pages'][key]!=state['pages'][key]:raise StateError('pdf_intake_review_candidate_changed')
            latest['pages'][key]['units'][uid]['candidate']['ui_projected']=True;latest['generation']+=1;self.context.store.save(latest)
    def snapshot(self,key,uid):
        candidate=self.context.store.load()['pages'][key]['units'][uid]['candidate']
        return read_snapshot(self.rows(),candidate['view'],original_uri(candidate['view']['identity']['source_file_id'],candidate['view']['identity']['page_number']))
    def confirmation_link(self,key,uid):
        view=self.context.store.load()['pages'][key]['units'][uid]['candidate']['view'];rows=self.rows()
        indices=[i for i,r in enumerate(rows) if len(r)>20 and r[17]==SCHEMA and r[18]==view['token'] and r[20]=='result']
        if len(indices)!=1:raise StateError('pdf_intake_review_result_missing')
        self.link(indices[0],self.context.config['origin']+'/review?'+urlencode({'target':key,'unit':uid}))
    def hide(self,key,uid):
        view=self.context.store.load()['pages'][key]['units'][uid]['candidate']['view'];rows=self.rows()
        indices=[i for i,r in enumerate(rows) if len(r)>18 and r[17]==SCHEMA and r[18]==view['token']]
        if not indices:return
        if indices!=list(range(indices[0],indices[-1]+1)):raise StateError('pdf_intake_review_fragmented')
        self.batch([{'updateDimensionProperties':{'range':{'sheetId':SHEET_ID,'dimension':'ROWS','startIndex':indices[0],'endIndex':indices[-1]+1},'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}}])

    def hide_pending(self,key):
        indices=[i for i,r in enumerate(self.rows()) if len(r)>18 and r[17]==PAGE_MARKER and r[18]==key]
        if indices:
            if indices!=list(range(indices[0],indices[-1]+1)):raise StateError('pdf_intake_review_fragmented')
            self.batch([{'updateDimensionProperties':{'range':{'sheetId':SHEET_ID,'dimension':'ROWS','startIndex':indices[0],'endIndex':indices[-1]+1},'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}}])


def process(context,settings,plans,*,apply):
    runner=context.runner(settings);projection=Projection(context)
    counts={'found':0,'written':0,'needs_review':0,'unchanged':0,'failure':0}
    started=monotonic();analyzed=0
    if len(plans)>MAX_FILES:raise StateError('pdf_intake_file_limit')
    sources={p['source_id'] for p in plans}
    pending=[key for key,r in context.store.load()['pages'].items()
             if r['page']['source']['source_file_id'] in sources and not r['analysis']
             and r['status']!='privacy_observation_changed' and r['page']['observation_complete']
             and not r['page']['clearly_sensitive']
             and (r['page']['automatic_classification']=='normal' or r['authority'].get('single_page_ai'))]
    pending.sort()
    # Rotate the bounded AI slot each three-hour window. A single failing
    # page must not indefinitely starve other independently eligible pages.
    offset=(int(time())//10800)%len(pending) if pending else 0
    selected=set((pending[offset:]+pending[:offset])[:MAX_NEW_PAGE_ANALYSES])
    for source in plans:
        # The scanner plan is NOT permission. Runtime rereads registry and source.
        try:
            raw=context.source(source['source_id'])
            if sha256(raw).hexdigest()!=source['sha256'] or page_count(raw)!=source['page_count']:
                raise StateError('pdf_intake_source_changed')
            state=context.store.load();keys=[k for k,r in state['pages'].items() if r['page']['source']['source_file_id']==source['source_id']]
            if len(keys)!=source['page_count']:raise StateError('pdf_intake_source_pages_missing')
        except Exception:counts['failure']+=1;continue
        for key in keys:
            record=context.store.load()['pages'][key]
            new_analysis=key in pending
            if (monotonic()-started>=PROCESS_BUDGET_SECONDS
                    or new_analysis and key not in selected
                    or new_analysis and analyzed>=MAX_NEW_PAGE_ANALYSES
                    or new_analysis and runner.written>=runner.unit_limit):
                counts['deferred']=counts.get('deferred',0)+1;continue
            try:
                if new_analysis:analyzed+=1
                result=runner.analyze(key)
                if not context.store.load()['pages'][key]['units']:
                    counts['needs_review']+=1
                    if apply:projection.pending(key)
                    continue
                if apply:projection.hide_pending(key)
            except StateError:
                counts['needs_review']+=1
                if apply:projection.pending(key)
                continue
            except Exception:counts['failure']+=1;continue
            for uid in context.store.load()['pages'][key]['units']:
                counts['found']+=1
                try:
                    unit=context.store.load()['pages'][key]['units'][uid]
                    if unit['candidate'].get('ui_projected'):
                        snapshot=projection.snapshot(key,uid)
                        outcome=runner.post_manual(key,uid,snapshot,apply=apply)
                        if outcome.get('status')=='posting_authority_required' and apply:
                            prepared=runner.prepare_manual(key,uid,snapshot)
                            if prepared['status']=='awaiting_authenticated_confirmation':projection.confirmation_link(key,uid)
                    else:outcome=runner.post_normal(key,uid,apply=apply)
                    status=outcome['status']
                    if status=='imported':counts['written']+=1;projection.hide(key,uid)
                    elif status in {'complete','terminal'}:
                        counts['unchanged']+=1
                        if apply and context.store.load()['pages'][key]['units'][uid]['status'] in TERMINAL:projection.hide(key,uid)
                    elif status=='unit_limit':counts['unchanged']+=1
                    else:
                        counts['needs_review']+=1
                        if apply:projection.unit(key,uid)
                except StateError:
                    counts['needs_review']+=1
                    if apply:projection.unit(key,uid)
                except Exception:counts['failure']+=1
        if apply:
            try:
                current=context.store.load();pages=[r for r in current['pages'].values() if r['page']['source']['source_file_id']==source['source_id']]
                if len(pages)==source['page_count'] and all(r['units'] and all(u['status'] in TERMINAL for u in r['units'].values()) for r in pages):
                    if archive_terminal(context,source['source_id'],source['sha256']):counts['archived']=counts.get('archived',0)+1
            except StateError:counts['archive_held']=counts.get('archive_held',0)+1
    return counts


def archive_terminal(context,sid,source_hash):
    """Attest exact ledger history first, move once, compact ONLY current data."""
    registered=context.store.load()
    pages={k:r for k,r in registered['pages'].items() if r['page']['source']['source_file_id']==sid}
    proof=context.proof.archive(sid,source_hash,attest=bool(pages))
    if proof['archive_allowed'] is not True:return False
    before=context.source(sid)
    if sha256(before).hexdigest()!=source_hash:raise StateError('pdf_intake_archive_source_changed')
    from .google_clients import drive_service
    drive=drive_service();fields='id,parents,mimeType,version,trashed'
    meta=drive.files().get(fileId=sid,fields=fields).execute(num_retries=0)
    inbox=context.config['registry']['inbox_id'];processed=context.config['registry']['processed_id']
    if meta.get('parents')==[inbox]:
        # Any lost response is resolved by one read-back, never an automatic retry.
        try:drive.files().update(fileId=sid,addParents=processed,removeParents=inbox,fields=fields).execute(num_retries=0)
        except Exception:pass
    after=drive.files().get(fileId=sid,fields=fields).execute(num_retries=0)
    if after.get('id')!=sid or after.get('parents')!=[processed] or after.get('trashed'):
        raise StateError('pdf_intake_archive_move_outcome_unknown')
    if sha256(context.source(sid)).hexdigest()!=source_hash:raise StateError('pdf_intake_archive_source_changed')
    verified=context.proof.archive(sid,source_hash)
    if verified['archive_allowed'] is not True or verified['summary_digest']!=proof['summary_digest']:
        raise StateError('pdf_intake_archive_history_changed')
    latest=context.store.load()
    if {k:r for k,r in latest['pages'].items() if k in pages}!=pages:
        raise StateError('pdf_intake_archive_current_changed')
    if pages:
        for k in pages:del latest['pages'][k]
        latest['generation']+=1;context.store.save(latest)
    return True
