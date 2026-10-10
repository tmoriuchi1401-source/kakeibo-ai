"""New registered Receipt Unit on the existing item-confirmation/OIDC flow."""
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import base64
import json

from itsdangerous import URLSafeTimedSerializer
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.drive_run_state import StateError
from app.pdf_intake_authority import DecisionSealer, binding as posting_binding, digest
from app.page_receipt_model import page_key
from app.human_general_authority import validate_grant
from app.general_receipt_completion import unit_identity
from app.receipt_item_review import validate, card, check_snapshot, evaluate
from app.receipt_item_review_ui import read_snapshot
from app.receipt_item_confirmation import binding, ConfirmedItems
from app.receipt_audit_firestore import FirestoreAuditRepository
from app.pdf_review_fields import original_uri
from app.human_general_auth_transport import empty_auth_state, TTL
from .generic_intake import RegisteredPageDrive
from .runtime import SyntheticRuntime
from .receipt_review_runtime import ReceiptReviewRuntime
from .receipt_review_readers import validate_journal
from .backend import canonical, timestamp


class RegisteredReaders:
    def __init__(self,config,target,uid,info,owner_sub,sid):
        self.drive=RegisteredPageDrive(config,target,info);self.uid=uid;self.sid=sid
        self.owner_actor_id=digest(['https://accounts.google.com',owner_sub])
        self.sheets=AuthorizedSession(service_account.Credentials.from_service_account_info(info,
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']))
        self.cache=None
        candidate=self.candidate()
        self.config={'scope':digest(['registered-item-confirmation-v1',self.drive.config['binding'],uid,candidate['record']['digest']])}

    def candidate(self):
        record=self.drive.store.load()['pages'][self.drive.target]
        unit=record['units'].get(self.uid)
        if unit is None or unit['status'] in {'imported','duplicate_confirmed'}:
            raise StateError('human_general_auth_replay_or_unknown_request')
        return unit['candidate']

    def sheet_rows(self,region):
        response=self.sheets.get('https://sheets.googleapis.com/v4/spreadsheets/'+self.sid+'/values/'+region,
            params={'valueRenderOption':'UNFORMATTED_VALUE','dateTimeRenderOption':'SERIAL_NUMBER'},timeout=10,allow_redirects=False)
        if response.status_code!=200:raise StateError('item_confirmation_sheet_unavailable')
        rows=response.json().get('values',[])
        if len(rows)>=5000:raise StateError('item_confirmation_sheet_truncated')
        return rows

    def read(self):
        if self.cache is not None:return deepcopy(self.cache)
        page=self.drive.fresh();candidate=self.candidate();record=validate(candidate['record'])
        if unit_identity(page,candidate['manifest'],self.uid)!=record['legacy']['identity']:
            raise StateError('item_confirmation_unit_stale')
        stored=self.drive.store.load()['pages'][self.drive.target]
        if page.automatic_classification!='normal':
            saved=stored['authority'].get('single_page_ai')
            if saved is None:raise StateError('item_confirmation_hga_missing')
            self.sealer.verify(saved['proof'],posting_binding(page.model_dump(),'single_page_ai'))
            validate_grant(saved['state']['grants'].get(page_key(page)),page)
        categories=[tuple(r[:2]) for r in self.sheet_rows("'カテゴリ'!A1:B1000")[1:]
                    if len(r)>=2 and all(isinstance(v,str) and v for v in r[:2])]
        if not categories:raise StateError('item_confirmation_category_master_unavailable')
        snapshot=read_snapshot(self.sheet_rows("'PDFページ確認'!A1:W5000"),candidate['view'],
                               original_uri(page.source.source_file_id,page.page_number))
        prepared=candidate.get('posting')
        if prepared is None or prepared['snapshot']!=snapshot:
            raise StateError('item_confirmation_snapshot_stale')
        self.cache=(record,snapshot,categories)
        return deepcopy(self.cache)


class RegisteredJournal:
    def __init__(self,runtime):self.runtime=runtime;self.readers=runtime.readers;self.actor=None
    def read_versioned(self):
        drive=self.readers.drive;current=drive.store.load()
        unit=current['pages'][drive.target]['units'][self.readers.uid]
        value=unit['candidate'].get('confirmation_journal',
            {'schema':'receipt-item-confirmations-v1','scope':self.readers.config['scope'],'requests':{},'generation':0})
        validate_journal(value,self.readers.config['scope'])
        return canonical(value),drive.store.tag
    def replace(self,before,tag,after):
        if self.actor is None:raise StateError('registered_item_verified_actor_required')
        if self.read_versioned()!=(before,tag):raise StateError('HTTP_412')
        value=validate_journal(json.loads(after),self.readers.config['scope'])
        if len(value['requests'])!=1:raise StateError('registered_item_one_request_only')
        rid,confirmation=next(iter(value['requests'].items()))
        self.readers.cache=None;self.readers.drive._source_cache=None
        candidate,snapshot,categories=self.readers.read()
        if confirmation['binding']!=binding(candidate,snapshot,rid) or self.actor.request_id!=rid:
            raise StateError('item_confirmation_snapshot_stale')
        proof=ConfirmedItems(**confirmation['proof'])
        current=check_snapshot(snapshot,card(candidate,categories=categories))
        validation=evaluate(candidate,current,categories,confirmation=proof)
        prepared=self.readers.candidate()['posting']
        if (validation['status']!='ready_to_confirm' or validation['parsed']!=prepared['parsed']
                or confirmation['plan']!={'input':current,'validation':validation}
                or self.actor.request_digest!=digest(binding(candidate,snapshot,rid))):
            raise StateError('registered_item_plan_stale')
        expected=posting_binding(self.readers.drive.expected,'receipt_posting',
            unit=prepared['unit_binding'],snapshot_digest=digest(snapshot),plan_digest=prepared['plan_digest'])
        bound=SimpleNamespace(**{k:getattr(self.actor,k) for k in ('request_id','actor_id','method','verified_at')},
                              request_digest=digest({'request_id':rid,**expected}))
        sealed=self.runtime.sealer.seal(expected,bound,rid,int(self.runtime.clock()))
        updated=self.readers.drive.store.load()
        if self.readers.drive.store.tag!=tag:raise StateError('HTTP_412')
        unit=updated['pages'][self.readers.drive.target]['units'][self.readers.uid]
        if unit['posting_authority'] is not None:raise StateError('human_general_auth_replay_or_unknown_request')
        unit['posting_authority']=sealed;unit['candidate']['confirmation_journal']=value
        updated['generation']+=1;self.readers.drive.store.save(updated)


class RegisteredReviewRuntime(ReceiptReviewRuntime):
    def __init__(self,client,settings,key,config,target,uid,info,sid,**kwargs):
        SyntheticRuntime.__init__(self,client,settings,key,**kwargs)
        self.readers=RegisteredReaders(config,target,uid,info,self.identity.owner_subject,sid)
        self.config=self.readers.config;self.drive=self.readers.drive;self.journal=RegisteredJournal(self)
        self.sealer=DecisionSealer(base64.urlsafe_b64decode(key),self.readers.owner_actor_id)
        self.readers.sealer=self.sealer
        self.audit=FirestoreAuditRepository(client,digest(['receipt-item-confirmation-permanent-v1',self.config['scope']]),write_enabled=True)
        self._stages=lambda *_a,**_k:None

    def factory(self,rid,expected_tag=None):
        existing=super().factory(rid,expected_tag)
        def build(actor):
            self.journal.actor=actor
            return existing(actor)
        return build

    def confirmation_text(self,rid):
        prepared=self.readers.candidate()['posting']
        return {'heading':'原本と商品明細を確認して記帳を許可',
            'detail':f"このレシートの{prepared['parsed']['total']:,}円・{len(prepared['parsed']['items'])}明細を確認しました。受付後、runnerが原本・入力・重複を再照合して記帳します。",
            'button':'このレシートの明細を確認し記帳を許可'}

    def success_label(self,rid):return f'p{self.drive.page.page_number}の記帳要求を保存しました（記帳結果はシートで確認）'

    def seed(self):
        link=super().seed()
        from urllib.parse import urlsplit,parse_qs
        rid=parse_qs(urlsplit(link).query)['request'][0]
        profile=digest([self.readers.candidate()['record']['digest'],self.readers.candidate()['posting']])
        self.client.collection('real_request_profiles').document(rid).create(
            {'profile':'registered_review','profile_digest':profile,'target':self.drive.target,
             'unit_id':self.readers.uid,'expires_at':timestamp(int(self.clock())+TTL)},retry=None,timeout=10)
        return link
