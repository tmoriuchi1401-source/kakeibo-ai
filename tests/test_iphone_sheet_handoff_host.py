from copy import deepcopy

import pytest

from app.iphone_sheet_handoff_host import PREFIX, make_isolated_sheet_handoff_application
from app.unified_receipt_shadow import ShadowConfig
from sheet_handoff_host_fixtures import HostRig
from unified_shadow_fixtures import ORIGIN, DOMAIN


@pytest.fixture(autouse=True)
def no_clients(monkeypatch):
    from googleapiclient import discovery
    from google.cloud import firestore
    def forbidden(*args, **kwargs): pytest.fail('live_client_forbidden')
    monkeypatch.setattr(discovery, 'build', forbidden)
    monkeypatch.setattr(firestore, 'Client', forbidden)


@pytest.mark.parametrize('cid', ['SYN-G101', 'SYN-M101'])
def test_real_authenticated_host_import_confirm_and_result_without_financial_write(cid, monkeypatch, tmp_path):
    r = HostRig(monkeypatch, tmp_path); assert r.h.login().status_code == 303
    auth = deepcopy(r.h.db.data)
    imported = r.prepare(cid, 'import_draft'); response = r.send(imported)
    assert response.status_code == 200, response.json
    assert response.json['status'] == 'DRAFT_IMPORTED'
    p = r.prepare(cid, 'confirm_handoff')
    assert r.send(p, False).status_code == 409
    response = r.send(p, True)
    assert response.status_code == 200, response.json
    assert response.json['status'] == 'FINANCIAL_BOUNDARY_STOPPED'
    assert response.json['writer_dispatches'] == 0 and response.json['native_authority'] is False
    journal = r.journal.read_versioned()
    get = r.h.get(PREFIX+'/results/'+p['request_id'])
    assert get.status_code == 200 and get.json == response.json
    assert get.headers['Cache-Control'] == 'no-store'
    assert r.journal.read_versioned() == journal
    assert r.send(p, True).status_code == 409
    assert r.h.db.data == auth
    assert {cid:r.old.frozen(cid) for cid in r.old.native} == r.before
    # The reused one-tab UI now exists; the financial R1 endpoint still does not.
    page = r.h.get(PREFIX)
    assert page.status_code == 200 and '確認済み・未記帳' in page.get_data(as_text=True)
    assert r.h.client.post('/isolated-synthetic/iphone-confirmation/r1/request',base_url=ORIGIN).status_code == 404


@pytest.mark.parametrize('attack', ['unauthenticated', 'csrf', 'origin', 'null_origin', 'cross_site', 'expired', 'ticket', 'login', 'owner'])
def test_security_fails_before_fresh_reader_or_journal_access(attack, monkeypatch, tmp_path):
    r = HostRig(monkeypatch, tmp_path)
    if attack != 'unauthenticated': r.h.login()
    headers = {'Origin':ORIGIN,'X-CSRF-Token':r.h.ticket.csrf if attack != 'unauthenticated' else ''}
    if attack == 'csrf': headers['X-CSRF-Token'] = 'wrong'
    elif attack == 'origin': headers['Origin'] = 'https://foreign.example'
    elif attack == 'null_origin': headers['Origin'] = 'null'
    elif attack == 'cross_site': headers['Sec-Fetch-Site'] = 'cross-site'
    elif attack == 'expired': r.h.now += 601
    elif attack in {'ticket', 'login'}:
        r.h.client.set_cookie(r.h.web.COOKIE if attack == 'ticket' else r.h.web.LOGIN_COOKIE, 'bad', domain=DOMAIN)
    elif attack == 'owner': r.adapter.confirmation.owner = '0'*64
    def forbidden(*args, **kwargs): pytest.fail('read_before_authentication')
    monkeypatch.setattr(r.reader, 'read', forbidden)
    monkeypatch.setattr(r.adapter, '_state', forbidden)
    response = r.h.client.post(PREFIX+'/prepare/SYN-M101/import_draft', json={}, base_url=ORIGIN, headers=headers)
    assert response.status_code in {403, 410}, response.json


def test_result_get_does_not_read_live_sheet_or_originals(monkeypatch, tmp_path):
    r = HostRig(monkeypatch, tmp_path); r.h.login()
    def forbidden(*args, **kwargs): pytest.fail('GET_must_not_capture_or_write')
    monkeypatch.setattr(r.reader, 'read', forbidden)
    monkeypatch.setattr(r.io, 'replace_versioned', forbidden)
    for c in r.old.native.values(): monkeypatch.setattr(c.readers, 'observe', forbidden)
    assert r.h.get(PREFIX+'/results/unknown').json['status'] == 'UNKNOWN'


def test_default_off_does_not_inspect_or_register_adapter():
    from flask import Flask
    def forbidden(): pytest.fail('runtime_must_not_be_constructed')
    app = make_isolated_sheet_handoff_application(lambda:Flask(__name__), forbidden, object(), config=ShadowConfig())
    assert all(not route.rule.startswith(PREFIX) for route in app.url_map.iter_rules())
