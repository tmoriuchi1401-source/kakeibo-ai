"""Operator-pinned item review on existing OIDC screens; no accounting or AI."""
from dataclasses import asdict
import json
from uuid import uuid4
from itsdangerous import URLSafeTimedSerializer
from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app.human_general_auth_transport import AuthRequestStore,empty_auth_state,TTL
from app.receipt_item_review import card,evaluate
from app.receipt_item_confirmation import binding
from app.receipt_audit import event
from app.receipt_audit_firestore import FirestoreAuditRepository
from app.receipt_item_auth_transport import AuthenticatedItemReview,validate_item_auth
from .backend import FirestoreConditionalState,canonical,timestamp
from .runtime import SyntheticRuntime
from .receipt_review_readers import Readers,Journal,validate_config_review,validate_journal


class ReceiptReviewRuntime(SyntheticRuntime):
    def __init__(self,client,settings,key,config,info,**kwargs):
        super().__init__(client,settings,key,**kwargs)
        self.config=validate_config_review(config);self.readers=Readers(config,info,self.identity.owner_subject)
        self.drive=self.readers.drive;self.journal=Journal(self.readers)
        self.audit=FirestoreAuditRepository(client,digest(['receipt-item-confirmation-permanent-v1',config['scope']]),write_enabled=True)
        self._stages=lambda *_a,**_k:None
    def state(self,rid,kind):
        if kind=='requests':return FirestoreConditionalState(self.client.collection('receipt_item_auth_requests').document(rid),clock=self.clock)
        if kind=='authorities':return self.journal
        raise StateError('item_confirmation_state_forbidden')
    def gateway(self,rid):
        return AuthenticatedItemReview(AuthRequestStore(self.state(rid,'requests'),self.config['scope'],
            preflight=lambda:None,validator=validate_item_auth),self.identity,readers=self.readers.read,
            redirect_uri=self.origin+'/oauth/callback',exchange_code=self.exchange,clock=self.clock)
    def factory(self,rid,expected_tag=None):
        runtime=self
        class Confirmation:
            def confirm(self,candidate,snapshot,current,proof):
                before,tag=runtime.journal.read_versioned()
                if tag!=expected_tag:raise StateError('HTTP_412')
                value=json.loads(before)
                if rid in value['requests'] or value['requests']:raise StateError('item_confirmation_existing_request_requires_readback')
                # Fresh input, master, HGA, source and original unit immediately
                # before CAS; never use the earlier screen as fresh authority.
                c,s,cats=runtime.readers.read()
                if c!=candidate or s!=snapshot:raise StateError('item_confirmation_snapshot_stale')
                plan=evaluate(c,current,cats,confirmation=proof)
                if plan['status']!='ready_to_confirm':raise StateError('item_confirmation_not_ready')
                value['requests'][rid]={'binding':binding(c,s,rid),'proof':asdict(proof),'snapshot':s,
                    'plan':{'input':current,'validation':plan},'status':'validated_not_written'}
                value['generation']+=1;after=canonical(value);runtime.journal.replace(before,tag,after)
                original=c['legacy']['identity']
                identity={k:original[k] for k in ('source_file_id','source_content_hash','page_count','page_number','receipt_unit_id','review_identity')}
                identity.update(page_identity=original['stable_page_identity'],revision=original['authority_revision'])
                audit=event(identity,request_id=rid,request_digest=digest(binding(c,s,rid)),
                    event_type='item_structure_confirmed',decision='confirmed',actor_id=proof.actor_id,
                    confirmed_at=timestamp(proof.verified_at).isoformat(),authority_digest=proof.authority_digest,
                    reason_code='owner_verified_item_snapshot')
                runtime.audit.commit(audit,runtime.audit.get_current(identity)['state_revision'])
                # The existing runner captures this exact authenticated UUID
                # under its production concurrency lock. Cloud Run does not
                # race a runner with an unconditional Sheets queue append.
                return {'authority_digest':proof.authority_digest}
        def build(actor):
            if actor.actor_id!=runtime.readers.owner_actor_id:
                raise StateError('item_confirmation_actor_rejected')
            return Confirmation()
        return build
    def label(self,rid):return 'p14 商品明細・記帳要求の確認'
    def start_heading(self,rid):return '商品明細と記帳要求の本人確認'
    def confirmation_text(self,rid):return {'heading':'商品明細を確認して記帳要求を保存',
        'detail':'原本と商品名・金額・値引き・カテゴリを照合済みです。今回は記帳planの検証までで、会計には書き込みません。',
        'button':'原本と明細を確認し記帳要求を保存'}
    def success_label(self,rid):return 'p14の明細確認と記帳要求を保存しました（会計writeは未実行）'
    def validate_result(self,rid,record):
        value=json.loads(self.journal.read_versioned()[0]);r=value['requests'].get(rid)
        if not r or r['proof']['authority_digest']!=record['authority_digest']:raise StateError('item_confirmation_readback_mismatch')
        marker=self.audit.lookup(rid)
        if not marker or self.audit.get_event(marker['event_ref'])['authority_digest']!=record['authority_digest']:
            raise StateError('item_confirmation_audit_readback_required')
    def reconcile(self,rid):
        try:return 'written' if rid in json.loads(self.journal.read_versioned()[0])['requests'] else 'not_written'
        except Exception:return 'unknown'
    def seed(self):
        candidate,snapshot,categories=self.readers.read();rid=str(uuid4());now=int(self.clock())
        if json.loads(self.journal.read_versioned()[0])['requests']:raise StateError('item_confirmation_existing_request_requires_readback')
        self.client.collection('receipt_item_auth_requests').document(rid).create(
            {'payload':canonical(empty_auth_state(self.config['scope'])),'expires_at':timestamp(now+TTL)},retry=None,timeout=10)
        self.gateway(rid).prepare(binding(candidate,snapshot,rid),request_id=rid)
        proof=URLSafeTimedSerializer(self.key,salt='hga-link-v1').dumps({'request':rid})
        return self.origin+'/start?request='+rid+'&proof='+proof
