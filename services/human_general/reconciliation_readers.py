"""Read-only pinned p1 original, owner inputs and exact existing accounting.

No candidate OCR, Medical writer, arbitrary spreadsheet or image transport.
Existing service-account credentials remain mounted, never logged or copied.
"""
import json
from hashlib import sha256
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account
from app.drive_run_state import StateError
from app.receipt_audit import validate_identity,digest,checked_hash
from .real_page import RealPageDrive,SOURCE,HASH,validate_config

SID='1G44cDDUryVpZazTDwuCT4eZrir5KJb2WVm9baHTRPow'
# Read-only wire contracts. Do not import UI/processor modules into this host.
SCHEMA='pdf-page-review-v1'
SOURCE_KEY='7bef7c636cf64f9a8cd40aefefcf0a668f2fdc4564217ddbee45ff69ee6f8d84'


def validate_reconciliation_config(config):
    keys={'target_identity','drive','medical_token','reconciliation_token','input_digest','comparison_ledger_id',
          'ledger_snapshot_digest','linked_rows_digest','receipt_id','import_id'}
    try:
        if set(config)!=keys:raise ValueError()
        identity=validate_identity(config['target_identity']);validate_config(config['drive'])
        if (identity['source_file_id']!=SOURCE or identity['source_content_hash']!=HASH
                or identity['page_number']!=1 or identity['page_count']!=14
                or identity['receipt_unit_id']!='' or identity['revision']!=2):raise ValueError()
        for key in ('medical_token','reconciliation_token','input_digest','ledger_snapshot_digest','linked_rows_digest'):
            checked_hash(config[key])
        if (config['comparison_ledger_id']!='R-1U23lqFfSFJNVFfdeFMlrUR28059HHThI-01'
                or config['receipt_id']!='R-1U23lqFfSFJNVFfdeFMlrUR28059HHThI'
                or config['import_id']!='receipt:1U23lqFfSFJNVFfdeFMlrUR28059HHThI'):raise ValueError()
    except Exception:raise StateError('reconciliation_configuration_invalid') from None
    return config


class ReconciliationReaders:
    def __init__(self,config,info,*,drive=None,sheets=None):
        self.config=validate_reconciliation_config(config)
        self.drive=drive or RealPageDrive(config['drive'],info)
        original=self.drive.request
        def get(fid,**kwargs):
            if kwargs.get('put') is not None:raise StateError('reconciliation_drive_write_forbidden')
            return original(fid,**kwargs)
        self.drive.request=get
        auth=service_account.Credentials.from_service_account_info(info,
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
        self.sheets=sheets or AuthorizedSession(auth);self.cache=None
        begin,end=self.drive.begin_request,self.drive.end_request
        def reset_begin():self.cache=None;begin()
        def reset_end():self.cache=None;end()
        self.drive.begin_request=reset_begin;self.drive.end_request=reset_end

    def rows(self):
        if self.cache is not None:return self.cache
        # Bounded pages/ledgers. A result at the exact upper boundary is held;
        # do not silently infer uniqueness from a truncated accounting table.
        ranges=["'PDFページ確認'!A1:P1000","'支出明細'!A1:M10000",
                "'レシート'!A1:I10000","'取込データ'!A1:L10000"]
        response=self.sheets.get('https://sheets.googleapis.com/v4/spreadsheets/'+SID+'/values:batchGet',
            params={'ranges':ranges,'valueRenderOption':'UNFORMATTED_VALUE','dateTimeRenderOption':'SERIAL_NUMBER'},
            timeout=10,allow_redirects=False)
        if response.status_code!=200:raise StateError('reconciliation_sheet_read_unavailable')
        values=response.json()['valueRanges']
        if len(values)!=4:raise StateError('reconciliation_sheet_read_invalid')
        tables=[v.get('values',[]) for v in values]
        if any(len(rows)>=limit for rows,limit in zip(tables,(1000,10000,10000,10000))):
            raise StateError('reconciliation_sheet_read_truncated')
        self.cache=tables;return tables

    def current(self,sid,number,receipt_id):
        ident=self.config['target_identity']
        if (sid,number,receipt_id)!=(SOURCE,1,''):raise StateError('reconciliation_target_forbidden')
        self.drive.acl();self.drive.fresh()
        raw,_=self.drive.read('1atHszVu7J-OXPbJkhCMhsvhiyz6QEdsR')
        proposal=json.loads(raw)['records'][SOURCE_KEY]['proposal'];page=proposal['pages'][0]
        if (proposal['source_content_hash']!=HASH or proposal['page_count']!=14
                or page['page_identity']!=ident['page_identity'] or page['human_classification']!='medical'):
            raise StateError('reconciliation_page_kind_stale')
        inputs={};identity=None
        for row in self.rows()[0]:
            if len(row)>5 and row[2]==SCHEMA and row[3]==self.config['medical_token']:
                found=json.loads(row[4])
                if identity is not None and identity!=found:raise StateError('reconciliation_owner_input_stale')
                identity=found
                if row[5] in {'date','facility','amount','category','payment','memo'}:
                    if row[5] in inputs:raise StateError('reconciliation_owner_input_stale')
                    inputs[row[5]]=row[1] if len(row)>1 else ''
        if (set(inputs)!={'date','facility','amount','category','payment','memo'} or digest(inputs)!=self.config['input_digest']
                or not identity or identity.get('source_file_id')!=SOURCE or identity.get('page_numbers')!=[1]):
            raise StateError('reconciliation_owner_input_stale')
        return ident

    def decision(self):
        from app.receipt_reconciliation_authority import DECISIONS
        rows=[r for r in self.rows()[0] if len(r)>5 and r[2]==SCHEMA
            and r[3]==self.config['reconciliation_token'] and r[5]=='reconciliation_decision']
        if len(rows)!=1:raise StateError('reconciliation_selection_stale')
        identity=json.loads(rows[0][4])
        expected=self.config['target_identity']
        if any(identity.get(k)!=v for k,v in expected.items()):raise StateError('reconciliation_selection_stale')
        if (identity.get('candidate_ledger_id')!=self.config['comparison_ledger_id']
                or identity.get('ledger_snapshot_digest')!=self.config['ledger_snapshot_digest']):
            raise StateError('reconciliation_selection_stale')
        return DECISIONS.get(rows[0][1])

    def source_hash(self,sid):
        return sha256(self.drive.source(sid)).hexdigest()

    def ledger(self,ledger_id):
        if ledger_id!=self.config['comparison_ledger_id']:raise StateError('reconciliation_ledger_target_forbidden')
        tables=self.rows();matches=[]
        for rows,key in zip(tables[1:],('comparison_ledger_id','receipt_id','import_id')):
            found=[r for r in rows[1:] if r and r[0]==self.config[key]]
            if len(found)!=1:raise StateError('reconciliation_ledger_stale_or_ambiguous')
            matches.append(found[0])
        row=list(matches[0])+['']*13;row=row[:13]
        if (digest(row)!=self.config['ledger_snapshot_digest'] or digest(matches)!=self.config['linked_rows_digest']
                or row[12]!='active' or row[9]!=self.config['receipt_id'] or row[10]!=self.config['import_id']):
            raise StateError('reconciliation_ledger_stale_or_ambiguous')
        return dict(ledger_id=ledger_id,snapshot_digest=self.config['ledger_snapshot_digest'],active=True,readback_complete=True)

    def refresh(self):
        self.cache=None;self.drive.refresh_before_write()
        self.current(SOURCE,1,'');self.ledger(self.config['comparison_ledger_id'])


class FreshAuditRepository:
    """Before the existing atomic permanent append, freshly re-read evidence."""
    def __init__(self,repository,readers):self.repo,self.readers=repository,readers
    def __getattr__(self,name):return getattr(self.repo,name)
    def commit(self,event,revision):
        self.readers.refresh()
        return self.repo.commit(event,revision)
