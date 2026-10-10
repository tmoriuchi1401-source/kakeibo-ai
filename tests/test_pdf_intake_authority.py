from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.receipt_plan.identity import SourceRef, PageUnit, stable_page
from app.pdf_intake_authority import (
    AuthorityAdapter, DecisionSealer, binding, digest, review_identity, page_key,
)

RAW = b'%PDF-synthetic-new-source'
OWNER = digest(['https://accounts.google.com', 'synthetic-owner'])


def page(number=2, classification='unknown', source_id='new-pdf'):
    source = SourceRef(source_file_id=source_id, source_content_hash=sha256(RAW).hexdigest(), page_count=3)
    result = PageUnit(source=source, page_number=number, stable_page_identity=stable_page(source, number),
                     automatic_classification=classification, automatic_reason='privacy_unresolved',
                     observation_complete=True, extraction_status='extracted',
                     observation_render_hash='a'*64, review_identity='0'*64, authority_revision=1).model_dump()
    result['review_identity'] = review_identity(result)
    return result


def proof(expected, *, method='google_oidc_shared_session_v2'):
    rid = str(uuid4())
    actor = SimpleNamespace(request_id=rid, actor_id=OWNER, method=method,
                            request_digest=digest({'request_id': rid, **expected}), verified_at=100)
    sealer = DecisionSealer(b'x'*32, OWNER)
    return sealer, sealer.seal(expected, actor, rid, 101)


def adapter(value, sealed=None, verifier=None):
    return AuthorityAdapter(lambda *_: value, lambda _: RAW, lambda *_: sealed,
                            verifier or (lambda *_: pytest.fail('proof must not be used')))


def test_new_source_page_accepts_only_owner_sealed_exact_capability():
    value = page()
    expected = binding(value, 'single_page_ai')
    sealer, sealed = proof(expected)
    result = adapter(value, sealed, sealer.verify).authorize(value)
    assert result['valid'] and result['actor_id'] == OWNER
    assert result['authority_digest'] == digest(sealed)
    assert 'email' not in str(sealed) and 'subject' not in str(sealed)


def test_normal_requires_no_human_authority_or_session():
    value = page(classification='normal')
    result = adapter(value).authorize(value)
    assert result['basis'] == 'automatic_normal'


def test_unknown_without_decision_is_held():
    with pytest.raises(StateError, match='authority_missing'):
        adapter(page()).authorize(page())


@pytest.mark.parametrize('classification', ['medical', 'payroll'])
def test_sensitive_rejected_before_source_read(classification):
    value = page(classification=classification)
    gate = adapter(value)
    gate.load_source = lambda _: pytest.fail('sensitive source must not reach transport')
    with pytest.raises(StateError, match='sensitive_page_forbidden'):
        gate.authorize(value)
    sealer = DecisionSealer(b'x'*32, OWNER)
    rid = str(uuid4()); expected = binding(value, 'single_page_ai')
    actor = SimpleNamespace(request_id=rid, actor_id=OWNER, method='google_oidc_code_pkce_v1',
                            request_digest=digest({'request_id': rid, **expected}), verified_at=100)
    with pytest.raises(StateError, match='sensitive_hga_forbidden'):
        sealer.seal(expected, actor, rid, 101)


@pytest.mark.parametrize('field', ['source', 'page_number', 'review_identity', 'authority_revision', 'processing_status'])
def test_changed_fresh_binding_rejects_before_authority(field):
    value = page(); current = deepcopy(value)
    current[field] = {'source_file_id': 'copy', **{k:v for k,v in value['source'].items() if k!='source_file_id'}} if field == 'source' else (
        'b'*64 if field=='review_identity' else 'changed' if field=='processing_status' else 3)
    current['review_identity'] = review_identity(current)
    with pytest.raises(StateError):
        adapter(current).authorize(value)


@pytest.mark.parametrize('change', ['page', 'source', 'actor', 'request', 'operation', 'revoked', 'seal'])
def test_tamper_and_cross_page_reuse_reject(change):
    value=page(); expected=binding(value,'single_page_ai'); sealer,sealed=proof(expected)
    other=deepcopy(sealed)
    if change=='page':other['decision']['binding']['page']['page_number']=1
    elif change=='source':other['decision']['binding']['page']['source']['source_file_id']='another-source'
    elif change=='actor':other['decision']['actor_id']='c'*64
    elif change=='request':other['decision']['request_id']=str(uuid4())
    elif change=='operation':other['decision']['binding']['operation']='receipt_posting'
    elif change=='revoked':other['decision']['revoked']=True
    else:other['seal']='d'*64
    with pytest.raises(StateError,match='authority_proof_invalid'):
        adapter(value,other,sealer.verify).authorize(value)


def test_ai_permission_cannot_expand_into_posting():
    value=page(); expected=binding(value,'single_page_ai'); sealer,sealed=proof(expected)
    unit={'receipt_unit_id':'page-receipt-v1:'+'f'*64,'segmentation_digest':'e'*64,'item_identities':['d'*64]}
    with pytest.raises(StateError,match='authority_proof_invalid'):
        adapter(value,sealed,sealer.verify).authorize(value,'receipt_posting',unit=unit,
                                                     snapshot_digest='c'*64,plan_digest='b'*64)


def test_source_bytes_changed_rejects():
    gate=adapter(page()); gate.load_source=lambda _: b'changed'
    with pytest.raises(StateError,match='source_stale'):gate.authorize(page())


def test_backend_reply_alone_cannot_change_binding_or_proof_digest():
    value=page(); expected=binding(value,'single_page_ai'); _,sealed=proof(expected)
    with pytest.raises(StateError,match='authority_verification_failed'):
        adapter(value,sealed,lambda *_:{'valid':True,'binding_digest':digest(expected),
                                      'authority_digest':'0'*64,'actor_id':OWNER,'request_id':str(uuid4())}).authorize(value)


def test_page_changes_during_verification_rejects():
    value=page(); expected=binding(value,'single_page_ai'); sealer,sealed=proof(expected)
    gate=adapter(value,sealed,sealer.verify)
    def verify(*args):
        gate.current_page=lambda *_: page(1)
        return sealer.verify(*args)
    gate.verify_proof=verify
    with pytest.raises(StateError,match='page_stale'):gate.authorize(value)


def test_actor_digest_includes_one_time_request_identity():
    value=page(); expected=binding(value,'single_page_ai'); rid=str(uuid4())
    actor=SimpleNamespace(request_id=rid,actor_id=OWNER,method='google_oidc_code_pkce_v1',
                          request_digest=digest(expected),verified_at=100)
    with pytest.raises(StateError,match='verified_actor_required'):
        DecisionSealer(b'x'*32,OWNER).seal(expected,actor,rid,101)


def test_readonly_replay_never_appends_or_claims():
    value=page(); expected=binding(value,'single_page_ai'); sealer,sealed=proof(expected)
    before=deepcopy(sealed); gate=adapter(value,sealed,sealer.verify)
    assert gate.authorize(value)==gate.authorize(value)
    assert sealed==before and page_key(value)==page_key(value)
