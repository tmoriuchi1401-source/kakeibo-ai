"""Existing Google OIDC browser flow, separate reconciliation-only operation.

No Medical/HGA/writer call. Trusted host supplies immutable request binding,
fresh readers and a permanent audit repository. Default HGA validators still
reject this operation. Origin/state/nonce/PKCE/CSRF remain the existing core.
"""
from copy import deepcopy
from datetime import datetime,timezone
from app.drive_run_state import StateError
from .human_general_auth_transport import (AuthenticatedGeneralConfirmation,AuthRequestStore,
    validate_auth_state,TTL,same,fingerprint)
from .receipt_reconciliation_authority import ACTION,binding,DECISIONS,ReconciliationConfirmation
from .receipt_audit import digest


def validate_reconciliation_auth(value,scope):
    validate_auth_state(value,scope,allowed_actions={ACTION})
    for rid,record in value['requests'].items():
        b=record['binding']
        expected=binding(b['identity'],request_id=rid,candidate_ledger_id=b['candidate_ledger_id'],
            ledger_snapshot_digest=b['ledger_snapshot_digest'],decision=b['selected_decision'],
            current_state_revision=b['current_state_revision'])
        if b!=expected:raise StateError('reconciliation_request_tampered')
    return value


class AuthenticatedReconciliation(AuthenticatedGeneralConfirmation):
    def __init__(self,store,identity,current_identity,load_source_hash,ledger_readback,repository,*,current_decision=None,**kwargs):
        # Constructor verifies redirect; general permission is never invoked.
        super().__init__(store,identity,lambda *_:None,lambda *_:None,**kwargs)
        self.requested_action=ACTION
        self.current_identity,self.source_hash,self.ledger=current_identity,load_source_hash,ledger_readback
        self.repository=repository
        self.current_decision=current_decision

    def fresh(self,record):
        b=record['binding'];ident=b['identity']
        if self.current_decision is not None and self.current_decision()!=b['selected_decision']:
            raise StateError('reconciliation_selection_stale')
        if (self.current_identity(ident['source_file_id'],ident['page_number'],ident['receipt_unit_id'])!=ident
                or self.source_hash(ident['source_file_id'])!=ident['source_content_hash']):
            raise StateError('reconciliation_source_page_review_stale')
        target=self.ledger(b['candidate_ledger_id'])
        if target!={'ledger_id':b['candidate_ledger_id'],'snapshot_digest':b['ledger_snapshot_digest'],
                    'active':True,'readback_complete':True}:
            raise StateError('reconciliation_ledger_stale_or_ambiguous')
        return ident

    def prepare(self,expected,*,request_id=None):
        rid=expected['request_id']
        if request_id is not None and request_id!=rid:raise StateError('reconciliation_request_tampered')
        now=int(self.clock());record={'request_id':rid,'binding':deepcopy(expected),'digest':digest(expected),
            'created_at':now,'expires_at':now+TTL,'status':'prepared'}
        validate_reconciliation_auth({'schema':'human-general-auth-requests-v1','binding':self.store.binding,
            'requests':{rid:record}},self.store.binding)
        self.fresh(record);state=self.store.load()
        if rid in state['requests'] or self.repository.lookup(rid):raise StateError('reconciliation_request_reuse')
        state['requests'][rid]=record;self.store.save(state);return rid

    def confirm(self,ticket,*,cookie,csrf,origin,method,confirmation_factory):
        state,record=self.record(ticket.request_id,'authenticated');self.browser(record,ticket,cookie)
        if (method!='POST' or not same(origin,self.origin) or not same(csrf,ticket.csrf)
                or not same(fingerprint(csrf),record['session']['csrf'])):
            raise StateError('human_general_auth_csrf_invalid')
        self.fresh(record)
        record['status']='claimed';self.store.save(state)
        actor=self.verified_actor(ticket.request_id)
        # Host checks the current strong validator before entering this call.
        service=confirmation_factory(actor)
        decision=next(k for k,v in DECISIONS.items() if v==record['binding']['selected_decision'])
        result=service.confirm(ticket.request_id,selected_decision=decision,method='POST',explicit_post=True)
        if result['event'] is None:proof=digest(['reconciliation-undecided',record['digest']])
        else:proof=result['event']['authority_digest']
        state,record=self.record(ticket.request_id,'claimed');record.update(status='complete',authority_digest=proof)
        self.store.save(state)
        return {'status':'complete','authority_digest':proof,'accounting_write':0}

    def confirmation(self,owner_actor_id):
        def trusted(rid):
            _,r=self.record(rid,'claimed')
            return {'binding':r['binding'],'digest':r['digest'],
                'created_at':datetime.fromtimestamp(r['created_at'],timezone.utc).isoformat(),
                'expires_at':datetime.fromtimestamp(r['expires_at'],timezone.utc).isoformat()}
        return ReconciliationConfirmation(self.repository,trusted_request=trusted,verified_actor=self.verified_actor,
            current_identity=self.current_identity,load_source_hash=self.source_hash,ledger_readback=self.ledger,
            owner_actor_id=owner_actor_id,clock=lambda:datetime.fromtimestamp(self.clock(),timezone.utc))
