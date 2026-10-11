"""Real application factory / Google RSA path, zero live resources."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from app.drive_run_state import StateError
from app.unified_receipt_shadow import PREFIX, BLUEPRINT, ShadowConfig, make_isolated_shadow_application
from app.unified_receipt_purposes import ACCOUNTING_POST
from unified_shadow_fixtures import HostRig, Document, DOMAIN, ORIGIN, inputs, add_native_unit_cards

ACTIONS = ['confirm_grouping', 'confirm_page_kind', 'confirm_privacy', 'link_existing']


@pytest.fixture(autouse=True)
def no_production_client_or_transport(monkeypatch):
    from google.cloud import firestore
    from googleapiclient import discovery
    def forbidden(*args, **kwargs): pytest.fail('production client construction forbidden')
    monkeypatch.setattr(firestore, 'Client', forbidden)
    monkeypatch.setattr(discovery, 'build', forbidden)


def test_real_factory_default_off_has_only_unchanged_routes(monkeypatch):
    h = HostRig(monkeypatch, enabled=False)
    assert BLUEPRINT not in h.app.blueprints
    assert all(not r.rule.startswith(PREFIX) for r in h.app.url_map.iter_rules())
    assert h.get(PREFIX).status_code == 404
    assert h.get('/health').status_code == 200
    assert h.login().status_code == 303
    assert h.get('/confirm').status_code == 200
    before = h.db.writes
    assert h.get('/logout').status_code == 200 and h.db.writes == before


def test_shadow_explicit_on_unique_namespace_and_templates(monkeypatch):
    h = HostRig(monkeypatch)
    rules = list(h.app.url_map.iter_rules())
    endpoints = [r.endpoint for r in rules]
    assert len(endpoints) == len(set(endpoints))
    shadow = [r for r in rules if r.rule.startswith(PREFIX)]
    assert len(shadow) == 4 and all(r.endpoint.startswith(BLUEPRINT+'.') for r in shadow)
    assert h.login().status_code == 303
    assert h.get(PREFIX).status_code == 200
    assert h.get(PREFIX+'/cases/'+h.f.case_id).status_code == 200
    assert h.factory_calls > 0


@pytest.mark.parametrize('bad', ['environment', 'memory'])
def test_isolation_setting_cannot_enable_live_runtime(monkeypatch, bad):
    h = HostRig(monkeypatch, enabled=False)
    config = ShadowConfig(True, 'production' if bad == 'environment' else 'isolated_synthetic')
    if bad == 'memory': h.db.shadow_memory_only = False
    with pytest.raises(StateError, match='isolation_required'):
        make_isolated_shadow_application(h.web.make_application, h.configured, h.boundary, config=config,
            cookies={'login':h.web.LOGIN_COOKIE, 'ticket':h.web.COOKIE})


@pytest.mark.parametrize('path', ['', '/cases/isolated-purpose-case', '/STRUCTURE_REVIEW/prepare', '/STRUCTURE_REVIEW/submit'])
def test_unauthenticated_get_and_post_denied(monkeypatch, path):
    h = HostRig(monkeypatch); before = h.db.writes
    response = h.get(PREFIX+path) if 'REVIEW' not in path else h.client.post(PREFIX+path, json={}, base_url=ORIGIN)
    assert response.status_code == 403 and h.db.writes == before and h.f.native_calls == 0


def test_non_owner_google_signature_path_denied(monkeypatch):
    h = HostRig(monkeypatch); h.claim_changes = {'sub':'999999'}
    assert h.login().status_code == 403
    assert h.get(PREFIX).status_code == 403 and h.f.native_calls == 0


@pytest.mark.parametrize('mutation', ['signature', 'audience', 'nonce'])
def test_existing_google_verification_not_bypassed(monkeypatch, mutation):
    h = HostRig(monkeypatch)
    if mutation == 'signature': h.public = 'invalid certificate'
    elif mutation == 'audience': h.claim_changes = {'aud':'another.apps.googleusercontent.com'}
    else: h.claim_changes = {'nonce':'wrong'}
    assert h.login().status_code == 403 and h.f.native_calls == 0


@pytest.mark.parametrize('stage', ['prepare', 'submit'])
@pytest.mark.parametrize('csrf', [False, 'wrong'])
def test_existing_ticket_csrf_required(monkeypatch, stage, csrf):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0] if stage == 'submit' else dict(
        case_id=h.f.case_id, requested_action=h.f.action, request_id=h.f.rid, expected_revision=0)
    before = h.db.writes
    response = h.post(PREFIX+'/'+h.f.purpose+'/'+stage, v, csrf=csrf)
    assert response.status_code == 400 and h.db.writes == before and h.f.io.writes == h.f.native_calls == 0


@pytest.mark.parametrize('origin', ['null', 'https://other.test', ORIGIN+'.evil'])
def test_exact_origin_remains_required(monkeypatch, origin):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    assert h.submit(v, origin=origin).status_code == 400 and h.f.native_calls == 0


@pytest.mark.parametrize('stage', ['GET', 'prepare', 'submit'])
def test_600_second_request_expiry_denied(monkeypatch, stage):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    h.now += 600
    response = h.get(PREFIX) if stage == 'GET' else h.prepare() if stage == 'prepare' else h.submit(v)
    assert response.status_code == 410 and h.f.io.writes == h.f.native_calls == 0


@pytest.mark.parametrize('stage', ['GET', 'prepare', 'submit'])
def test_logout_revokes_already_prepared_request(monkeypatch, stage):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    assert h.post_form('/logout', inputs(h.get('/logout'))).status_code == 200
    response = h.get(PREFIX) if stage == 'GET' else h.prepare() if stage == 'prepare' else h.submit(v)
    assert response.status_code == 403 and h.f.native_calls == 0


@pytest.mark.parametrize('action', ACTIONS)
def test_get_repeated_pure_projection_and_prepare_all_writes_zero(monkeypatch, action):
    h = HostRig(monkeypatch, action); assert h.login().status_code == 303
    before = deepcopy(h.db.data); raw = h.f.io.raw; state_raw = h.f.state_io.raw if h.f.state_io else None
    def forbidden(*a, **kw): pytest.fail('GET/prepare wrote or called display')
    with monkeypatch.context() as spy:
        spy.setattr(Document, 'create', forbidden); spy.setattr(Document, 'update', forbidden)
        spy.setattr(h.f.io, 'replace_versioned', forbidden)
        if h.f.state_io: spy.setattr(h.f.state_io, 'replace_versioned', forbidden)
        if hasattr(h.f, 'grouping'): spy.setattr(h.f.grouping, 'display', forbidden)
        spy.setattr(h.f.port, 'submit_native', forbidden)
        for _ in range(3):
            assert h.get(PREFIX).status_code == 200
            assert h.get(PREFIX+'/cases/'+h.f.case_id).status_code == 200
        prepared = h.prepare()
        assert prepared.status_code == 200
    assert h.db.data == before and h.f.io.raw == raw and h.f.native_calls == 0
    if h.f.state_io: assert h.f.state_io.raw == state_raw
    assert h.f.accounting_appends == h.f.ai_sends == h.f.source_moves == 0


@pytest.mark.parametrize('action', ACTIONS)
def test_real_metadata_services_without_unit_posting_permission(monkeypatch, action):
    h = HostRig(monkeypatch, action); assert h.login().status_code == 303
    response = h.prepare(); assert response.status_code == 200
    v = response.json['operation_targets'][0]
    assert 'receipt_unit_id' not in v and 'posting_authority' not in v
    result = h.submit(v)
    assert result.status_code == 200 and result.json['state'] == 'complete'
    assert h.f.native_calls == 1 and h.f.accounting_appends == h.f.ai_sends == h.f.source_moves == 0
    assert h.submit(v).status_code == 200 and h.f.native_calls == 1
    assert '確認操作完了' in h.get(PREFIX+'/cases/'+h.f.case_id).get_data(as_text=True)


@pytest.mark.parametrize('decision,status', [('同一レシート','reconciled_existing'), ('別のレシート','confirmed_distinct'), ('判断できない','reconciliation_required')])
def test_native_reconciliation_exact_three_choices_never_append(monkeypatch, decision, status):
    h = HostRig(monkeypatch, 'link_existing', decision); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    assert h.submit(v).status_code == 200
    assert h.f.repo.get_current(h.f.ident)['status'] == status
    assert h.f.accounting_appends == 0


@pytest.mark.parametrize('extra', ['selection', 'authority', 'receipt_unit_id', 'posting_authority'])
def test_foreign_evidence_and_fake_reconciliation_choice_rejected(monkeypatch, extra):
    h = HostRig(monkeypatch, 'link_existing'); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]; v[extra] = 'same_existing'
    assert h.submit(v).status_code == 400 and h.f.native_calls == h.f.io.writes == 0


def test_purpose_route_and_canonical_request_reuse_denied(monkeypatch):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    assert h.post(PREFIX+'/METADATA_REVIEW/submit', v).status_code == 400
    assert h.submit(v).status_code == 200
    # Existing coordinator rejects the same canonical ID for another operation.
    other = deepcopy(v); other['case_id'] = 'another-display-card'
    assert h.submit(other).status_code == 409 and h.f.native_calls == 1


@pytest.mark.parametrize('stage', ['prepare', 'submit'])
def test_stale_revision_refused_without_write(monkeypatch, stage):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    if stage == 'prepare': response = h.prepare(expected_revision=1)
    else:
        v = h.prepare().json['operation_targets'][0]; v['expected_state_revision'] = 1; response = h.submit(v)
    assert response.status_code == 409 and h.f.native_calls == h.f.io.writes == 0


def test_cas_conflict_has_no_fallback_or_execution(monkeypatch):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    def conflict(*args): raise StateError('HTTP_412')
    monkeypatch.setattr(h.f.io, 'replace_versioned', conflict)
    response = h.submit(v)
    assert response.status_code == 412 and h.f.native_calls == 0
    assert '再送信せず' in response.get_data(as_text=True)


@pytest.mark.parametrize('action', ACTIONS)
def test_concurrent_http_submit_at_most_one_native_execution(monkeypatch, action):
    h = HostRig(monkeypatch, action); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    cookies = [(name, h.client.get_cookie(name, domain=DOMAIN).value) for name in (h.web.COOKIE, h.web.LOGIN_COOKIE)]
    def submit(_):
        client = h.app.test_client()
        for name, value in cookies: client.set_cookie(name, value, domain=DOMAIN)
        return h.submit(v, client=client).status_code
    with ThreadPoolExecutor(max_workers=8) as pool: statuses = list(pool.map(submit, range(8)))
    assert 200 in statuses and set(statuses) <= {200, 409, 412}
    assert h.f.native_calls == 1 and h.f.accounting_appends == h.f.ai_sends == h.f.source_moves == 0


@pytest.mark.parametrize('missing', ['unit', 'index', 'snapshot', 'authority'])
def test_accounting_host_contract_rejects_incomplete_evidence(monkeypatch, missing):
    h = HostRig(monkeypatch, accounting=True); assert h.login().status_code == 303
    if missing == 'unit': h.case.identity.pop('receipt_unit_id')
    elif missing == 'index': h.case.identity.pop('receipt_index')
    elif missing == 'snapshot': h.case.fields[0][2] = 'unsent changed snapshot'
    else:
        state = h.financial_port.runner.store.load()
        state['pages'][h.financial_port.key]['units'][h.financial_port.uid]['posting_authority'] = None
        state['generation'] += 1; h.financial_port.runner.store.save(state)
    v = dict(case_id=h.case.case_id, requested_action='post', request_id=h.financial['request_id'], expected_revision=0)
    response = h.post(PREFIX+'/ACCOUNTING_POST/prepare', v)
    assert response.status_code in {200,400,409}
    if response.status_code == 200: assert response.json['operation_targets'] == []
    assert h.financial_db.writes == [] and h.bridge.requests.read()[0]['operations'] == {}


def test_complete_accounting_fixture_validates_but_cannot_reach_writer(monkeypatch):
    h = HostRig(monkeypatch, accounting=True); assert h.login().status_code == 303
    def forbidden(*args, **kwargs): pytest.fail('shadow reached financial bridge.submit')
    monkeypatch.setattr(h.bridge, 'submit', forbidden)
    response = h.post(PREFIX+'/ACCOUNTING_POST/prepare', dict(case_id=h.case.case_id, requested_action='post',
        request_id=h.financial['request_id'], expected_revision=0))
    assert response.status_code == 200 and response.json['operation_targets'] == [h.financial]
    assert h.post(PREFIX+'/ACCOUNTING_POST/submit', h.financial).status_code == 400
    assert h.financial_db.writes == [] and h.bridge.requests.read()[0]['operations'] == {}


def test_cards_boundaries_escape_and_unsent_input_preservation(monkeypatch):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    add_native_unit_cards(h)
    before = deepcopy(h.snapshot)
    for _ in range(3):
        body = h.get(PREFIX).get_data(as_text=True)
        assert body.count('<article class="case"') == 5
        assert body.count('ここまで：案件') == 5
        assert body.count('<section class="unit"') == 3
        assert '<script>unsafe()' not in body and '&lt;script&gt;' in body
        assert '旧案件・対応確認保留' in body and '<form' not in body
        assert '入力途中：消さない' in body
    assert h.snapshot == before


def test_host_request_id_cannot_cross_purpose(monkeypatch):
    import unified_purpose_fixtures as fixtures
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    v = h.prepare().json['operation_targets'][0]
    assert h.submit(v).status_code == 200
    other = fixtures.existing_review_fixture('confirm_page_kind')
    other.rid = h.f.rid
    h.boundary.ports[(other.purpose, other.action)] = other.port
    response = h.post(PREFIX+'/'+other.purpose+'/prepare', dict(case_id=other.case_id,
        requested_action=other.action, request_id=other.rid, expected_revision=h.f.io.revision))
    assert response.status_code == 409 and other.native_calls == 0


def test_get_hold_detail_never_generates_form_or_operation(monkeypatch):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    body = h.get(PREFIX+'/cases/isolated-legacy-hold').get_data(as_text=True)
    assert '旧確認と新Receipt Unitの対応を証明できない' in body
    assert '<form' not in body and '<button' not in body


def test_unknown_purpose_and_payload_never_leak_error_data(monkeypatch):
    h = HostRig(monkeypatch); assert h.login().status_code == 303
    response = h.post(PREFIX+'/STRUCTURE_REVIEW/prepare', {'token':'secret', 'medical':'private facility'})
    assert response.status_code == 400
    body = response.get_data(as_text=True)
    assert 'secret' not in body and 'private facility' not in body and h.ticket.csrf not in body
    assert h.post(PREFIX+'/WRONG/submit', {}).status_code == 400
