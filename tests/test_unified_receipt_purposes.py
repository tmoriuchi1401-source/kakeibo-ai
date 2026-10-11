"""Purpose isolation; real pinned validators, memory transports, no cloud IO."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from threading import Barrier
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.unified_receipt_review import digest, LEGACY_UNMAPPED_HOLD
from app.unified_receipt_purposes import (STRUCTURE_REVIEW, METADATA_REVIEW, RECONCILIATION_REVIEW,
    ACCOUNTING_POST, RECONCILIATION_SELECTIONS, PurposeBoundary, project_current_snapshot, validate_observation)
from unified_purpose_fixtures import existing_review_fixture, gate, readonly, native_modules, OWNER, NOW

ACTIONS = ['confirm_grouping', 'confirm_page_kind', 'confirm_privacy', 'link_existing']


@pytest.mark.parametrize('action', ACTIONS)
def test_metadata_minimal_evidence_real_validator_no_financial_permission(action):
    f = existing_review_fixture(action)
    o = f.observe(f.case_id, f.purpose, action, f.rid)
    assert 'receipt_index' not in o.evidence and 'posting_authority' not in o.evidence
    assert 'receipt_unit_id' not in o.evidence
    request = f.prepare()
    assert f.io.writes == 0 and f.native_calls == 0  # prepare never seeds authority
    result = f.boundary.submit(request, {})
    assert result['state'] == 'complete' and result['accounting_writes'] == 0
    assert f.accounting_appends == f.ai_sends == f.source_moves == 0
    # Another card must resolve to this same native operation, not execute again.
    other = f.boundary.prepare('another-display-card',f.purpose,action,str(uuid4()),f.io.revision,{})['operation_targets'][0]
    assert other['operation_identity']==request['operation_identity']
    before=(f.io.raw,f.io.writes,f.native_calls)
    with pytest.raises(StateError,match='unified_operation_conflict'):
        f.boundary.submit(other,{})
    assert before==(f.io.raw,f.io.writes,f.native_calls)


@pytest.mark.parametrize('action', ACTIONS)
@pytest.mark.parametrize('missing', ['source', 'pages', 'target_revision'])
def test_metadata_missing_common_evidence_denied_before_cas(action, missing):
    f = existing_review_fixture(action)
    def missing_field(o):
        e = deepcopy(o.evidence); e.pop(missing)
        return replace(o, evidence=e)
    f.change = missing_field
    with pytest.raises(StateError, match='evidence_invalid'):
        f.prepare()
    assert f.io.writes == f.native_calls == 0


@pytest.mark.parametrize('action,missing', [('confirm_grouping','proposal_digest'), ('confirm_page_kind','observation_digest'),
    ('confirm_privacy','selection'), ('link_existing','ledger_snapshot_digest'), ('link_existing','identity')])
def test_purpose_specific_evidence_cannot_be_inferred(action, missing):
    f = existing_review_fixture(action)
    f.change = lambda o: replace(o, evidence={k:v for k,v in o.evidence.items() if k != missing})
    with pytest.raises(StateError):
        f.prepare()
    assert f.io.writes == f.native_calls == 0


@pytest.mark.parametrize('action', ACTIONS)
@pytest.mark.parametrize('extra', ['receipt_unit_id','receipt_index','posting_authority','authority'])
def test_no_placeholder_unit_or_foreign_authority_accepted(action, extra):
    f = existing_review_fixture(action)
    f.change = lambda o: replace(o, evidence={**o.evidence, extra:'foreign-object'})
    with pytest.raises(StateError):
        f.prepare()
    assert f.io.writes == f.native_calls == 0


@pytest.mark.parametrize('stage', ['prepare','submit'])
@pytest.mark.parametrize('bad,value', [('actor_id',''), ('actor_id',digest(['another-owner'])), ('csrf_valid',False),
    ('session_active',False), ('method','GET'), ('purpose',ACCOUNTING_POST), ('request_id',str(uuid4())),
    ('binding_digest',digest(['other-purpose-authority'])), ('expected_revision',9), ('created_at',NOW-600),
    ('expires_at',NOW), ('expires_at',NOW+601), ('issuer','https://evil.example')])
def test_post_auth_owner_csrf_ttl_purpose_native_binding_and_revision(stage, bad, value):
    f = existing_review_fixture('confirm_grouping')
    r = f.prepare()
    with pytest.raises(StateError):
        if stage == 'prepare':
            f.boundary.prepare(f.case_id, f.purpose, f.action, f.rid, 0, {bad:value})
        else:
            f.boundary.submit(r, {bad:value})
    assert f.io.writes == f.native_calls == 0


@pytest.mark.parametrize('read', ['list','detail'])
def test_get_all_write_spies_zero_including_native_display(monkeypatch, read):
    f = existing_review_fixture('confirm_grouping')
    f.snapshot.append(dict(case_id='legacy-only', purpose=RECONCILIATION_REVIEW, disposition=LEGACY_UNMAPPED_HOLD,
                           state='held', display={'reason':'旧案件・対応確認保留','unsafe':'<script>bad()</script>'}))
    before = (f.io.raw, f.io.writes, f.state_io.raw, f.state_io.writes, deepcopy(f.raw))
    forbidden = []
    def denied(*a, **kw):
        forbidden.append((a, kw)); pytest.fail('GET must have ZERO writes and no native display/prepare')
    for obj, methods in [(f.io,['replace_versioned']), (f.state_io,['replace_versioned']),
                          (f.grouping,['display','review']), (f.grouping.store,['save']),
                          (f.port,['execute']), (f.boundary,['prepare','submit'])]:
        for method in methods:
            monkeypatch.setattr(obj, method, denied)
    # Forbidden capabilities of all external transports; GET has none of them.
    for capability in ('authority_issue','ledger_append','accounting_append','move_source','ai_send'):
        monkeypatch.setattr(f, capability, denied, raising=False)
    result = f.boundary.get_list({}) if read == 'list' else f.boundary.get_detail('legacy-only', {})
    rows = result if isinstance(result,list) else [result]
    assert all(r['operation_targets']==[] for r in rows)
    assert not forbidden and before == (f.io.raw,f.io.writes,f.state_io.raw,f.state_io.writes,f.raw)
    result2 = project_current_snapshot(deepcopy(f.snapshot))
    result2[0]['display']['原本'] = 'changed-copy'
    assert f.snapshot[0]['display']['原本'] == 'fixture'


@pytest.mark.parametrize('bad', [{'actor_id':''},{'actor_id':digest(['other'])},{'session_active':False},{'expires_at':NOW}])
def test_read_auth_owner_logout_expiry_required(bad):
    f = existing_review_fixture('confirm_grouping')
    with pytest.raises(StateError):
        f.boundary.get_list(bad)
    assert f.io.writes == f.native_calls == 0


def test_off_never_returns_operations_and_post_is_refused():
    f = existing_review_fixture('confirm_grouping')
    f.boundary.enabled = False
    assert f.boundary.get_list({})[0]['operation_targets'] == []
    with pytest.raises(StateError):
        f.prepare()
    with pytest.raises(StateError):
        f.boundary.submit({}, {})
    assert f.io.writes == f.native_calls == 0


def test_legacy_hold_metadata_visible_but_never_operable():
    f = existing_review_fixture('confirm_grouping')
    f.change = lambda o: replace(o, disposition=LEGACY_UNMAPPED_HOLD)
    with pytest.raises(StateError):
        f.prepare()
    assert f.io.writes == f.native_calls == 0


@pytest.mark.parametrize('decision,status', [('同一レシート','reconciled_existing'),('別のレシート','confirmed_distinct'),('判断できない','reconciliation_required')])
def test_real_reconciliation_selection_identity_revision_and_state_no_append(decision, status):
    f = existing_review_fixture('link_existing', decision)
    assert RECONCILIATION_SELECTIONS == native_modules().receipt_reconciliation_authority.DECISIONS
    r = f.prepare(); f.boundary.submit(r, {})
    assert f.repo.get_current(f.ident)['status'] == status
    assert f.native_record['binding']['selected_decision'] == RECONCILIATION_SELECTIONS[decision]
    assert f.native_record['binding']['current_state_revision'] == 0
    assert f.accounting_appends == f.ai_sends == f.source_moves == 0
    assert not any('ACCOUNTING_POST' == op['request'].get('purpose') for op in f.store.read()[0]['operations'].values())


def test_same_existing_is_not_a_native_selection():
    f = existing_review_fixture('link_existing')
    o = f.observe(f.case_id, f.purpose, f.action, f.rid)
    f.change = lambda o: replace(o, evidence={**o.evidence,'selection':'same_existing'})
    with pytest.raises(StateError):
        f.prepare()
    # Directly prove the actual validator refuses the retired fake selection.
    with pytest.raises(f.native.receipt_reconciliation_authority.StateError, match='reconciliation_request_tampered'):
        f.reconciliation.confirm(f.rid, selected_decision='same_existing', method='POST', explicit_post=True)
    assert f.repo.writes == f.io.writes == f.native_calls == 0


@pytest.mark.parametrize('field', ['candidate','identity','revision','actor','ttl'])
def test_actual_reconciliation_validator_refuses_changed_native_contract(field):
    f = existing_review_fixture('link_existing'); f.observe(f.case_id,f.purpose,f.action,f.rid)
    if field == 'candidate':
        f.candidate['snapshot_digest'] = digest(['changed-ledger'])
    elif field == 'identity':
        f.ident['review_identity'] = digest(['changed-review'])
    elif field == 'revision':
        current = f.native.receipt_audit.pending(f.ident); current['state_revision']=1
        # Changing binding's claimed revision must be detected before write.
        f.native_record['binding']['current_state_revision']=1
    elif field == 'actor':
        f.actor = replace(f.actor, subject='102')
    else:
        f.native_record['expires_at']='2026-01-01T00:00:00+00:00'
    with pytest.raises(f.native.receipt_reconciliation_authority.StateError):
        f.reconciliation.confirm(f.rid, selected_decision='同一レシート', method='POST', explicit_post=True)
    assert f.repo.writes == 0


@pytest.mark.parametrize('other_action', ['confirm_page_kind','confirm_privacy','link_existing'])
def test_canonical_request_id_cannot_cross_purpose_or_operation(other_action):
    first = existing_review_fixture('confirm_grouping'); first.boundary.submit(first.prepare(), {})
    second = existing_review_fixture(other_action); second.rid = first.rid
    second.boundary.requests = first.store
    with pytest.raises(StateError, match='unified_request_id_conflict'):
        second.boundary.prepare(second.case_id,second.purpose,second.action,second.rid,first.io.revision,{})
    assert second.native_calls == 0


def test_foreign_purpose_evidence_cannot_be_reused():
    structure = existing_review_fixture('confirm_grouping')
    metadata = existing_review_fixture('confirm_page_kind')
    o = structure.observe(structure.case_id,structure.purpose,structure.action,structure.rid)
    with pytest.raises(StateError):
        validate_observation(replace(o,purpose=METADATA_REVIEW,requested_action='confirm_page_kind'),structure.rid)
    with pytest.raises(StateError):
        validate_observation(replace(o,purpose=RECONCILIATION_REVIEW,requested_action='link_existing'),structure.rid)
    assert structure.io.writes == metadata.io.writes == 0


@pytest.mark.parametrize('action', ACTIONS)
def test_new_old_concurrent_same_operation_executes_once(action):
    f = existing_review_fixture(action); r = f.prepare(); barrier = Barrier(8)
    def send(i):
        barrier.wait(timeout=10)
        try:
            return f.boundary.submit(r,{},entry='legacy' if i%2 else 'unified')
        except StateError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(send,range(8)))
    assert any(x and x['state']=='complete' for x in results)
    assert f.native_calls == 1 and len(f.store.read()[0]['operations']) == 1
    assert f.accounting_appends == f.ai_sends == f.source_moves == 0


@pytest.mark.parametrize('mode', ['partial','lost'])
def test_metadata_partial_unknown_readback_only_no_redispatch(mode):
    f = existing_review_fixture('confirm_grouping'); r = f.prepare(); f.mode=mode
    with pytest.raises(StateError,match='unified_partial_unknown_readback_only'):
        f.boundary.submit(r,{})
    assert f.boundary.submit(r,{})['state']=='partial_unknown'
    before=f.state_io.writes
    result=f.boundary.recover_readback(r,{})
    assert result['state']==('complete' if mode=='lost' else 'partial_unknown')
    assert f.native_calls==1 and f.state_io.writes==before and result['accounting_writes']==0


@pytest.mark.parametrize('change', ['revision','hash','version','snapshot'])
def test_metadata_stale_denied_before_claim(change):
    f=existing_review_fixture('confirm_grouping');r=f.prepare()
    def altered(o):
        e=deepcopy(o.evidence)
        if change=='revision':
            e['target_revision']+=1
        elif change=='hash':
            e['source']['source_content_hash']=digest(['changed-source'])
        elif change=='version':
            e['source']['source_version']='2'
        else:
            e['proposal_digest']=digest(['changed-proposal'])
        return replace(o,evidence=e)
    f.change=altered
    with pytest.raises(StateError):
        f.boundary.submit(r,{})
    assert f.io.writes==f.native_calls==0


def test_cas_conflict_no_fallback_or_native_call(monkeypatch):
    f=existing_review_fixture('confirm_grouping');r=f.prepare()
    def conflict(*args):
        raise StateError('HTTP_412')
    monkeypatch.setattr(f.io,'replace_versioned',conflict)
    with pytest.raises(StateError,match='HTTP_412'):
        f.boundary.submit(r,{})
    assert f.native_calls==f.io.writes==0


def accounting_fixture():
    from test_unified_receipt_write import pdf_native
    from app.unified_receipt_controller import UnifiedReceiptController
    c,b,p,r,db,runner=pdf_native()
    controller=UnifiedReceiptController(b,lambda:[c],lambda _:True)
    boundary=PurposeBoundary(OWNER,gate,readonly,lambda:[],b.requests,accounting=controller,enabled=True,clock=lambda:NOW)
    return boundary,c,b,p,r,db


def test_accounting_purpose_minimal_native_evidence_prepares_without_append():
    boundary,c,b,p,r,db=accounting_fixture()
    prepared=boundary.prepare(c.case_id,ACCOUNTING_POST,'post',r['request_id'],0,{})
    assert prepared['operation_targets']==[r] and db.writes==[]
    assert b.requests.read()[0]['operations']=={}


@pytest.mark.parametrize('missing', ['unit','index','snapshot','authority'])
def test_accounting_purpose_never_relaxes_unit_snapshot_posting_permission(missing):
    boundary,c,b,p,r,db=accounting_fixture()
    if missing=='unit':c.identity.pop('receipt_unit_id')
    elif missing=='index':c.identity.pop('receipt_index')
    elif missing=='snapshot':c.fields[0][2]='changed unsent snapshot'
    if missing=='authority':
        original=p.runner.store.load();original['pages'][p.key]['units'][p.uid]['posting_authority']=None
        original['generation']+=1;p.runner.store.save(original)
        # Preserve the original trusted inspection; native authority check must deny.
        from test_unified_receipt_write import FakeNative
        fake=FakeNative(c,target=r['operation_identity']);p.reader=lambda *_:fake.inspect(c,'post')
    try:
        denied=boundary.prepare(c.case_id,ACCOUNTING_POST,'post',r['request_id'],0,{})
    except StateError:
        denied={'operation_targets':[]}
    assert denied['operation_targets']==[]
    assert not db.writes and b.requests.read()[0]['operations']=={}


def test_metadata_and_accounting_share_canonical_request_fence():
    boundary,c,b,p,r,db=accounting_fixture()
    f=existing_review_fixture('confirm_grouping');f.rid=r['request_id']
    f.boundary.requests=b.requests
    f.boundary.submit(f.prepare(),{})
    with pytest.raises(StateError,match='unified_request_id_conflict'):
        boundary.prepare(c.case_id,ACCOUNTING_POST,'post',r['request_id'],b.requests.read()[0]['revision'],{})
    with pytest.raises(StateError,match='unified_request_id_conflict'):
        b.submit(c,r)
    assert not db.writes


def test_separate_stores_for_purposes_are_refused():
    boundary,c,b,p,r,db=accounting_fixture()
    f=existing_review_fixture('confirm_grouping')
    with pytest.raises(StateError,match='unified_shared_request_store_missing'):
        PurposeBoundary(OWNER,gate,readonly,lambda:[],f.store,accounting=boundary.accounting,clock=lambda:NOW)
