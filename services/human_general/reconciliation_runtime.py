"""Reconciliation on the existing OIDC screens; trusted readers are required.

Not automatically enabled by the live configuration loader. Host must supply
fresh original/ledger/input readers; no Sheet value can grant authority.
"""
from uuid import uuid4
from itsdangerous import URLSafeTimedSerializer
from app.drive_run_state import StateError
from app.human_general_auth_transport import AuthRequestStore,empty_auth_state,TTL,UUID
from app.receipt_reconciliation_auth_transport import AuthenticatedReconciliation,validate_reconciliation_auth
from app.receipt_reconciliation_authority import binding,DECISIONS
from app.receipt_audit import digest,checked_hash
from .backend import FirestoreConditionalState,canonical,timestamp
from .runtime import SyntheticRuntime


class CurrentReadback:
    def __init__(self,repo,identity):self.repo,self.identity=repo,identity
    def read_versioned(self):
        state=self.repo.get_current(self.identity)
        return canonical(state),'"reconciliation-'+digest(state)+'"'


class ReconciliationRuntime(SyntheticRuntime):
    def __init__(self,client,settings,key,target_identity,current_identity,load_source_hash,ledger_readback,repository,*,current_decision=None,**kwargs):
        super().__init__(client,settings,key,**kwargs)
        self.page_identity=target_identity;self.current_identity=current_identity;self.source_hash=load_source_hash
        self.ledger=ledger_readback;self.repository=repository
        self.current_decision=current_decision
        self.binding=digest(['medical-reconciliation-auth-v1',target_identity])
        self.mode='reconciliation_only';self.owner_id=digest(['https://accounts.google.com',self.identity.owner_subject])

    def state(self,rid,kind):
        if not isinstance(rid,str) or not UUID.fullmatch(rid):raise StateError('human_general_auth_request_invalid')
        if kind=='requests':return FirestoreConditionalState(self.client.collection('reconciliation_requests').document(rid),clock=self.clock)
        if kind=='authorities':return CurrentReadback(self.repository,self.page_identity)
        raise StateError('reconciliation_state_forbidden')

    def gateway(self,rid):
        return AuthenticatedReconciliation(AuthRequestStore(self.state(rid,'requests'),self.binding,
            preflight=lambda:None,validator=validate_reconciliation_auth),self.identity,
            self.current_identity,self.source_hash,self.ledger,self.repository,
            redirect_uri=self.origin+'/oauth/callback',exchange_code=self.exchange,clock=self.clock,current_decision=self.current_decision)

    def factory(self,rid,expected_tag=None):
        def build(actor):
            if expected_tag is None or expected_tag=='*' or self.state(rid,'authorities').read_versioned()[1]!=expected_tag:
                raise StateError('HTTP_412')
            if actor.actor_id!=self.owner_id:raise StateError('reconciliation_actor_rejected')
            return self.gateway(rid).confirmation(self.owner_id)
        return build

    def label(self,rid):return 'p'+str(self.page_identity['page_number'])+'・既存記帳との対応確認'
    def start_heading(self,rid):return '記帳済みレシートとの対応確認'
    def confirmation_text(self,rid):
        _,r=self.gateway(rid).record(rid,'authenticated')
        selection=next(k for k,v in DECISIONS.items() if v==r['binding']['selected_decision'])
        return {'heading':'記帳済みレシートとの対応確認','detail':'選択した判定：'+selection+'。会計への追加・変更はしません。',
                'button':selection+'として確定'}
    def success_label(self,rid):
        _,r=self.gateway(rid).record(rid,'complete')
        return {'same':'既存記帳と同一のレシートとして対応付けました',
                'different':'この既存記帳とは別のレシートとして確認しました',
                'unknown':'判断できないため、対応確認待ちを維持しました'}[r['binding']['selected_decision']]

    def validate_result(self,rid,record):
        b=record['binding'];marker=self.repository.lookup(rid)
        if b['selected_decision']=='unknown':
            expected=digest(['reconciliation-undecided',record['digest']])
            if marker is not None or record['authority_digest']!=expected:raise StateError('reconciliation_readback_required')
            return
        if not marker or marker['request_digest']!=record['digest'] or marker['actor_id']!=self.owner_id:
            raise StateError('reconciliation_readback_required')
        event=self.repository.get_event(marker['event_ref'])
        if event['authority_digest']!=record['authority_digest'] or event['event_digest']!=marker['event_digest']:
            raise StateError('reconciliation_readback_required')

    def seed_decision(self,ledger_id,ledger_snapshot_digest,decision):
        # Operator pins a fresh dropdown selection. HTTP has no prepare route.
        checked_hash(ledger_snapshot_digest)
        current=self.repository.get_current(self.page_identity)
        if current['status']!='reconciliation_required':raise StateError('reconciliation_current_stale')
        rid,now=str(uuid4()),int(self.clock())
        expected=binding(self.page_identity,request_id=rid,candidate_ledger_id=ledger_id,
            ledger_snapshot_digest=ledger_snapshot_digest,decision=decision,current_state_revision=current['state_revision'])
        self.client.collection('reconciliation_requests').document(rid).create(
            {'payload':canonical(empty_auth_state(self.binding)),'expires_at':timestamp(now+TTL)},retry=None,timeout=10)
        self.gateway(rid).prepare(expected)
        proof=URLSafeTimedSerializer(self.key,salt='hga-link-v1').dumps({'request':rid})
        return self.origin+'/start?request='+rid+'&proof='+proof


class LiveReconciliationRuntime(ReconciliationRuntime):
    def __init__(self,client,settings,key,config,info,**kwargs):
        from .reconciliation_readers import ReconciliationReaders,FreshAuditRepository
        from app.receipt_audit_firestore import FirestoreAuditRepository
        from .stages import Stages
        readers=ReconciliationReaders(config,info)
        ident=config['target_identity']
        repository=FreshAuditRepository(FirestoreAuditRepository(client,
            digest(['p1-reconciliation-permanent-v1',ident['source_file_id'],ident['source_content_hash'],ident['page_identity']]),
            write_enabled=True),readers)
        super().__init__(client,settings,key,config['target_identity'],readers.current,readers.source_hash,
            readers.ledger,repository,current_decision=readers.decision,**kwargs)
        self.readers=readers;self.drive=readers.drive
        self._stages=lambda *_args,**_kwargs:None
        self.stage_factory=Stages

    def gateway(self,rid):
        self._stages=self.stage_factory(rid)
        return super().gateway(rid)

    def reconcile(self,rid):
        try:
            marker=self.repository.lookup(rid)
            if marker is None:return 'not_written'
            event=self.repository.get_event(marker['event_ref'])
            if event['event_digest']==marker['event_digest']:return 'written'
        except Exception:pass
        return 'unknown'
