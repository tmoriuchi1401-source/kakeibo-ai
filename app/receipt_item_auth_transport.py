"""Existing OIDC transport for a receipt snapshot; no accounting capability."""
from copy import deepcopy
from .drive_run_state import StateError
from .human_general_auth_transport import (AuthenticatedGeneralConfirmation,
    validate_auth_state, TTL, same, fingerprint)
from .receipt_item_confirmation import ACTION,binding,attest
from .receipt_item_review import check_snapshot,card,evaluate
from .page_receipt_model import digest


def validate_item_auth(value,scope):
    validate_auth_state(value,scope,allowed_actions={ACTION})
    for record in value['requests'].values():
        b=record['binding']
        if (set(b)!={'requested_action','request_id','identity','candidate_digest','snapshot_digest','item_ids','adjustment_targets'}
                or len(b['item_ids'])!=len(set(b['item_ids']))):
            raise StateError('item_confirmation_binding_invalid')
    return value


class AuthenticatedItemReview(AuthenticatedGeneralConfirmation):
    def __init__(self,store,identity,*,readers,**kwargs):
        super().__init__(store,identity,lambda *_:None,lambda *_:None,**kwargs)
        self.requested_action=ACTION;self.readers=readers

    def fresh(self,record):
        candidate,snapshot,categories=self.readers()
        identity=candidate['legacy']['identity']
        if identity['automatic_classification'] in {'medical','payroll'} or identity['clearly_sensitive'] or identity['human_page_kind'] in {'medical','payroll'}:
            raise StateError('item_confirmation_sensitive_page')
        current=check_snapshot(snapshot,card(candidate,categories=categories))
        if binding(candidate,snapshot,record['request_id'])!=record['binding']:
            raise StateError('item_confirmation_snapshot_stale')
        if current['action']!='記帳する':raise StateError('item_confirmation_action_stale')
        return candidate,snapshot,current,categories

    def prepare(self,expected,*,request_id):
        now=int(self.clock());record={'request_id':request_id,'binding':deepcopy(expected),
            'digest':digest(expected),'created_at':now,'expires_at':now+TTL,'status':'prepared'}
        validate_item_auth({'schema':'human-general-auth-requests-v1','binding':self.store.binding,
            'requests':{request_id:record}},self.store.binding)
        self.fresh(record);state=self.store.load()
        if request_id in state['requests']:raise StateError('item_confirmation_request_reuse')
        state['requests'][request_id]=record;self.store.save(state)
        return request_id

    def confirm(self,ticket,*,cookie,csrf,origin,method,confirmation_factory):
        state,record=self.record(ticket.request_id,'authenticated');self.browser(record,ticket,cookie)
        if (method!='POST' or not same(origin,self.origin) or not same(csrf,ticket.csrf)
                or not same(fingerprint(csrf),record['session']['csrf'])):
            raise StateError('human_general_auth_csrf_invalid')
        candidate,snapshot,current,categories=self.fresh(record)
        record['status']='claimed';self.store.save(state)
        actor=self.verified_actor(ticket.request_id)
        # Full Google-verified evidence belongs only to the existing short-lived
        # auth session. Permanent/Drive proofs contain the hashed actor ID only.
        proof=attest(candidate,snapshot,current,actor,request_id=ticket.request_id,
            owner_actor_id=digest(['https://accounts.google.com',self.identity.owner_subject]),
            now=int(self.clock()),categories=categories)
        result=confirmation_factory(actor).confirm(candidate,snapshot,current,proof)
        state,record=self.record(ticket.request_id,'claimed')
        record.update(status='complete',authority_digest=result['authority_digest']);self.store.save(state)
        return {'status':'complete','accounting_write':0,'authority_digest':result['authority_digest']}
