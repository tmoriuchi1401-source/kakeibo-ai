"""Real signature verification; synthetic source/ledger, no live write."""
from copy import deepcopy
from uuid import uuid4
import pytest
from app.drive_run_state import StateError
from app.human_general_auth_transport import AuthRequestStore, validate_auth_state
from app.receipt_reconciliation_auth_transport import AuthenticatedReconciliation, validate_reconciliation_auth
from app.receipt_reconciliation_authority import binding
from app.receipt_audit import MemoryAuditRepository, digest
from test_human_general_auth_transport import Rig, keys, OWNER
from test_receipt_retention_audit import identity


def setup(keys, decision='same'):
    r=Rig(keys); r.ident=identity(); r.source_hash=r.ident['source_content_hash']
    r.repo=MemoryAuditRepository(); r.rid=str(uuid4())
    r.expected=binding(r.ident,request_id=r.rid,candidate_ledger_id='R-existing-01',
        ledger_snapshot_digest=digest('exact existing rows'),decision=decision)
    r.target=dict(ledger_id='R-existing-01',snapshot_digest=r.expected['ledger_snapshot_digest'],
        active=True,readback_complete=True)
    r.gateway=AuthenticatedReconciliation(AuthRequestStore(r.transport,r.store.binding,
        preflight=lambda:None,validator=validate_reconciliation_auth),r.policy,
        lambda *_:r.ident,lambda _:r.source_hash,lambda _:r.target,r.repo,
        redirect_uri='https://confirmation.example.test/oauth/callback',exchange_code=r.exchange,
        clock=lambda:r.now)
    r.gateway.prepare(r.expected)
    r.ticket=r.gateway.begin(r.rid)
    return r


def callback(r):
    return r.gateway.callback(r.ticket,cookie=r.ticket.cookie,state=r.ticket.state,code='synthetic-code')


def confirm(r, **changes):
    args=dict(cookie=r.ticket.cookie,csrf=r.ticket.csrf,origin=r.gateway.origin,method='POST',
        confirmation_factory=lambda actor:r.gateway.confirmation(actor.actor_id))
    args.update(changes)
    return r.gateway.confirm(r.ticket,**args)


@pytest.mark.parametrize('decision,status,count',[
    ('same','reconciled_existing',1),('different','confirmed_distinct',1),
    ('unknown','reconciliation_required',0)])
def test_authenticated_explicit_decisions_and_replay(keys,decision,status,count):
    r=setup(keys,decision); callback(r)
    assert r.repo.writes==0  # Authentication alone cannot decide.
    assert confirm(r)['accounting_write']==0
    current=r.repo.get_current(r.ident)
    assert current['status']==status and r.repo.writes==count
    before=deepcopy((r.repo.events,r.repo.current,r.repo.receipts))
    with pytest.raises(StateError,match='replay'):confirm(r)
    assert (r.repo.events,r.repo.current,r.repo.receipts)==before


@pytest.mark.parametrize('change',[
    {'sub':'987654321'},{'aud':'wrong'},{'iss':'https://evil.example'},
    {'email_verified':False},{'nonce':'wrong'}])
def test_google_actor_rejected_before_authority(keys,change):
    r=setup(keys);r.claim_changes=change
    with pytest.raises(StateError):callback(r)
    assert r.repo.writes==0


@pytest.mark.parametrize('change',[
    {'origin':'null'},{'origin':''},{'origin':'https://evil.example'},
    {'csrf':'wrong'},{'method':'GET'}])
def test_origin_csrf_post_rejections(keys,change):
    r=setup(keys);callback(r)
    with pytest.raises(StateError):confirm(r,**change)
    assert r.repo.writes==0


@pytest.mark.parametrize('change',['source','page','review','revision','ledger','expired'])
def test_fresh_source_page_review_and_ledger(keys,change):
    r=setup(keys);callback(r)
    if change=='source':r.source_hash=digest('changed source')
    elif change=='page':r.ident=identity(4)
    elif change=='review':r.ident['review_identity']=digest('new review')
    elif change=='revision':r.ident['revision']+=1
    elif change=='ledger':r.target['snapshot_digest']=digest('changed ledger')
    elif change=='expired':r.now+=601
    with pytest.raises(StateError):confirm(r)
    assert r.repo.writes==0


def test_general_store_never_accepts_medical_reconciliation_operation(keys):
    r=setup(keys)
    with pytest.raises(StateError):validate_auth_state(r.gateway.store.load(),r.store.binding)
    assert r.repo.writes==0


def test_request_id_and_decision_tampering_rejected(keys):
    r=setup(keys);value=r.gateway.store.load()
    value['requests'][r.rid]['binding']['selected_decision']='different'
    with pytest.raises(StateError):validate_reconciliation_auth(value,r.store.binding)
    assert r.repo.writes==0
