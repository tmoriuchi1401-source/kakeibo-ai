"""Synthetic existing-app HTTP, independent readers and private-journal restart.

No real OAuth/Google data, deployment, listener, financial write or Sheet write.
"""
from copy import deepcopy
from html.parser import HTMLParser
import json
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.page_receipt_model import digest
from receipt_registration_preview.reconciliation import parse_import_rows
from receipt_registration_preview.category_rules import parse_rules
from services.human_general.backend import EncryptedTickets, canonical
from services.human_general.web import create_app, COOKIE, LOGIN_COOKIE
from services.human_general.receipt_correction_io import (
    IndependentReceiptReader, ResultJournal, empty_results, project_result,
)
from services.human_general.receipt_correction_web import registration_routes
from test_receipt_correction_registration import Rig, keys, RID, SOURCE
from test_page_receipts import MemoryTransport

ORIGIN = 'https://confirmation.example.test'
DOMAIN = 'confirmation.example.test'
OWNER, SERVICE = 'owner@example.test', 'service@example.test'
SHEET, PRIVATE, RESULTS, FOLDER = 'synthetic-sheet', 'synthetic-cases', 'synthetic-results', 'synthetic-folder'


class Sources:
    def __init__(self, r):
        p = r.packet
        self.meta = {}
        for fid,mime in [(SHEET,'application/vnd.google-apps.spreadsheet'), (PRIVATE,'application/json'),
                         (RESULTS,'application/json'), (SOURCE,'image/png')]:
            self.meta[fid] = dict(id=fid,version='7',mimeType=mime,trashed=False,parents=[FOLDER],
                owners=[dict(emailAddress=OWNER)], permissions=[dict(type='user',role='owner',emailAddress=OWNER),
                dict(type='user',role='writer',emailAddress=SERVICE)])
        self.sheets = dict(sheet='確認', imports=[deepcopy(p['tx'].row)], candidates=deepcopy(p['indexed_rows']),
            categories=deepcopy(p['categories']), rules=[], cases=[dict(case_id=p['tx'].import_id,
            source=deepcopy(p['source']),operations=deepcopy(p['operations']),memo='編集中メモ',submission=None)])
        self.catalog = dict(cases={p['tx'].import_id:dict(source=deepcopy(p['source']),
            candidate_digest=digest([row for _,row in p['indexed_rows']]), import_digest=digest(p['tx'].row),
            total_confirmation=deepcopy(p['total_confirmation']))})
        self.raw, self.sheet_reads = p['source_bytes'], 0

    def metadata(self, fid):return deepcopy(self.meta[fid])
    def sheet_values(self):
        self.sheet_reads += 1
        return deepcopy(self.sheets)
    def private_cases(self):return deepcopy(self.catalog), '"private-cases-1"'
    def reader(self):
        return IndependentReceiptReader(metadata=self.metadata, source_bytes=lambda _:self.raw,
            sheet_values=self.sheet_values,private_cases=self.private_cases,spreadsheet_id=SHEET,
            private_id=PRIVATE,source_folder=FOLDER,owner_email=OWNER,service_email=SERVICE,
            parse_imports=parse_import_rows,parse_rules=parse_rules)


class Flow:
    def __init__(self, keys, *, unresolved=False):
        from cryptography.fernet import Fernet
        self.r = Rig(keys)
        self.sources = Sources(self.r)
        if unresolved:self.sources.sheets['cases'][0]['operations']=[]
        self.reader = self.sources.reader()
        self.r.gateway.read_case = self.reader
        self.transport = MemoryTransport()
        self.transport.payload = canonical(empty_results())
        self.journal = self.new_journal()
        self.r.gateway.journal = self.journal
        self.actor = self.r.authenticate()
        self.login = self.r.logins.create(self.actor)
        self.tickets = EncryptedTickets(self.r.db, Fernet.generate_key(), clock=lambda:self.r.now)
        self.tickets.save(self.r.ticket,self.r.now+600)
        snapshot = self.r.state()['binding']['snapshot']
        self.sources.sheets['cases'][0]['submission'] = dict(request_id=RID,snapshot_digest=snapshot['digest'])
        # The logical result binding is metadata, not part of the editable operations.
        self.app = create_app(lambda:SimpleNamespace(origin=ORIGIN))
        self.app.register_blueprint(registration_routes(gateway_for=self.gateway,tickets=self.tickets,
                                   logins=self.r.logins,journal=self.journal))
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.set_cookie(COOKIE,self.r.ticket.cookie,domain=DOMAIN,secure=True)
        self.client.set_cookie(LOGIN_COOKIE,self.login,domain=DOMAIN,secure=True)

    def new_journal(self):
        return ResultJournal(self.transport,preflight=lambda:self.reader.checked_metadata(RESULTS),clock=lambda:self.r.now)
    def gateway(self, rid, login_cookie):
        assert rid == RID
        g = self.r.gateway
        # Bind even first-code login to its existing encrypted shared session.
        record = g.store.load()['requests'][rid]
        if 'login_evidence' not in record:
            # Production host does this at OAuth callback, never on a display GET.
            # Fixture initialization below installs it before requests are served.
            raise StateError('receipt_result_login_required')
        g.verify_login = lambda record:self.r.logins.verify_bound(record,login_cookie)
        return g
    def bind_login(self):
        self.r.gateway.verify_login = lambda record:self.r.logins.verify_bound(record,self.login)
        self.r.gateway.bind_login(RID,self.r.logins.evidence(self.login))
    def get(self, path):return self.client.get(path,base_url=ORIGIN)
    def form(self):
        response = self.get('/receipt/register/'+RID)
        assert response.status_code == 200
        fields = {}
        class Parser(HTMLParser):
            def handle_starttag(self,tag,attrs):
                attrs=dict(attrs)
                if tag=='input':fields[attrs['name']]=attrs['value']
        Parser().feed(response.get_data(as_text=True))
        return fields,response
    def post(self, fields, origin=ORIGIN):
        return self.client.post('/receipt/register/'+RID,data=fields,base_url=ORIGIN,headers={'Origin':origin})


@pytest.fixture
def flow(keys):
    f = Flow(keys)
    f.bind_login()
    return f


def test_authenticated_existing_app_one_POST_persistent_result_and_no_input_writes(flow):
    before = deepcopy(flow.sources.sheets)
    fields,screen = flow.form()
    assert flow.transport.writes == 0
    html = screen.get_data(as_text=True)
    assert screen.headers['Cache-Control']=='no-store'
    assert flow.client.get('/receipt/register/'+RID,base_url='http://'+DOMAIN).status_code==400
    assert 'initial-scale=1' in html and 'max-width:420px' in html
    assert html.count('<button') == 1 and 'この内容を登録' in html
    response = flow.post(fields)
    assert response.status_code == 303
    result = flow.get(response.location)
    assert result.status_code == 200 and '検証済み・未記帳' in result.get_data(as_text=True)
    assert flow.transport.writes == 3
    assert flow.sources.sheets == before
    saved = flow.journal.query(RID,logins=flow.r.logins,cookie=flow.login)
    projected = project_result(before['cases'],saved)
    assert projected[0]['result']['state'] == '検証済み・未記帳'
    assert projected[0]['memo'] == before['cases'][0]['memo']
    assert projected[0]['operations'] == before['cases'][0]['operations']
    writes = flow.transport.writes
    assert flow.post(fields).status_code == 409
    assert flow.transport.writes == writes


def test_result_survives_registration_expiry_and_new_journal_instance(flow):
    fields,_ = flow.form()
    flow.post(fields)
    flow.r.now += 601
    del flow.r.db.data['registration_requests/'+RID]  # Auth TTL cleanup.
    flow.client.delete_cookie(COOKIE,domain=DOMAIN)
    fresh = flow.new_journal()
    before = flow.transport.payload
    record = fresh.query(RID,logins=flow.r.logins,cookie=flow.login)
    assert record['state'] == '検証済み・未記帳'
    assert flow.get('/receipt/result/'+RID).status_code == 200
    assert flow.transport.payload == before
    with pytest.raises(StateError):flow.r.confirm()


@pytest.mark.parametrize('cookie',['missing','tampered','revoked'])
def test_past_query_requires_current_verified_login(flow,cookie):
    fields,_ = flow.form()
    flow.post(fields)
    if cookie=='revoked':flow.r.logins.revoke(flow.login)
    else:flow.client.set_cookie(LOGIN_COOKIE,'' if cookie=='missing' else 'wrong',domain=DOMAIN,secure=True)
    assert flow.get('/receipt/result/'+RID).status_code == 403


@pytest.mark.parametrize('change',['original','version','acl','metadata_missing','sheet','catalog','candidate','import','duplicate'])
def test_reader_rejects_changed_or_unbound_data_without_result(flow,change):
    fields,_ = flow.form()
    s = flow.sources
    if change=='original':s.raw=b'changed'
    elif change=='version':s.meta[SOURCE]['version']='8'
    elif change=='acl':s.meta[SHEET]['permissions'].append(dict(type='anyone',role='reader'))
    elif change=='metadata_missing':s.meta[SOURCE].pop('trashed')
    elif change=='sheet':s.sheets['cases'][0]['operations'][0]['changes']['amount']+=1
    elif change=='catalog':s.catalog['cases']['receipt:'+SOURCE]['source']['sha256']='e'*64
    elif change=='candidate':s.sheets['candidates'][0][1][4]='changed'
    elif change=='import':s.sheets['imports'][0][6]=999
    else:s.sheets['cases'].append(deepcopy(s.sheets['cases'][0]))
    assert flow.post(fields).status_code == 409
    assert flow.transport.writes == 0


def test_concurrent_edit_during_double_read_stops(flow):
    original = flow.sources.sheet_values
    count = 0
    def changing():
        nonlocal count
        count+=1
        if count==2:flow.sources.sheets['cases'][0]['memo']='concurrent edit'
        return original()
    flow.reader.sheet_values = changing
    with pytest.raises(StateError):flow.reader('receipt:'+SOURCE)


@pytest.mark.parametrize('kind',['csrf','origin','digest','extra'])
def test_HTTP_registration_rejects_untrusted_form_or_origin(flow,kind):
    fields,_ = flow.form()
    origin=ORIGIN
    if kind=='csrf':fields['csrf']='wrong'
    elif kind=='origin':origin='null'
    elif kind=='digest':fields['snapshot_digest']='f'*64
    else:fields['actor_id']='self-asserted'
    assert flow.post(fields,origin).status_code in {403,409}
    assert flow.transport.writes==0


def test_HTTP_auth_expiry_has_no_validation_or_automatic_replay(flow):
    fields,_=flow.form()
    flow.r.now+=600
    assert flow.post(fields).status_code==409
    assert flow.transport.writes==0


@pytest.mark.parametrize('state',['受付済み','検証中','結果不明・照合待ち'])
def test_all_pending_and_unknown_states_display_read_only(flow,state):
    record=flow.r.state()
    flow.journal.transition(record,'受付済み')
    if state!='受付済み':flow.journal.transition(record,state)
    before=flow.transport.payload
    response=flow.get('/receipt/result/'+RID)
    assert response.status_code==200 and state in response.get_data(as_text=True)
    assert flow.transport.payload==before


def test_unresolved_items_show_correction_required(keys):
    flow=Flow(keys,unresolved=True)
    flow.bind_login()
    fields,_=flow.form()
    response=flow.post(fields)
    assert response.status_code==303
    assert '修正が必要' in flow.get(response.location).get_data(as_text=True)


@pytest.mark.parametrize('stage',['before','after'])
def test_unknown_journal_commit_stops_resubmission_and_allows_independent_reconciliation(flow,stage):
    fields,_=flow.form()
    original=flow.transport.replace_versioned
    def lost(before,tag,after):
        value=json.loads(after)
        if value['results'][RID]['state']=='検証済み・未記帳':
            if stage=='after':original(before,tag,after)
            raise StateError('synthetic_result_write_unknown')
        original(before,tag,after)
    flow.transport.replace_versioned=lost
    assert flow.post(fields).status_code==409
    writes=flow.transport.writes
    assert flow.post(fields).status_code==409 and flow.transport.writes==writes
    flow.r.now+=601
    value=flow.new_journal().query(RID,logins=flow.r.logins,cookie=flow.login)
    assert value['state']==('検証済み・未記帳' if stage=='after' else '結果不明・照合待ち')


def test_projection_uses_identity_after_sort_and_preserves_new_draft(flow):
    fields,_=flow.form()
    flow.post(fields)
    value=flow.journal.query(RID,logins=flow.r.logins,cookie=flow.login)
    current=deepcopy(flow.sources.sheets['cases'])
    current[0]['memo']='受付後の新しいメモ'
    current[0]['operations'][0]['changes']['amount']=999
    held=dict(case_id='H301',source={},operations=[],memo='旧保留',submission=None)
    output=project_result([held,*current],value)
    assert output[0]==held and output[1]['operations']==current[0]['operations']
    assert output[1]['memo']=='受付後の新しいメモ'
    assert output[1]['result']['draft_notice']=='未登録の変更あり'
    for bad in ([current[0],current[0]], [dict(current[0],source={})],
                [dict(current[0],submission=dict(request_id='another',snapshot_digest='a'*64))]):
        with pytest.raises(StateError):project_result(bad,value)


def test_another_registrants_result_is_not_disclosed_to_current_login(flow):
    fields,_=flow.form()
    flow.post(fields)
    value=json.loads(flow.transport.payload)
    record=value['results'][RID]
    record['actor_id']='e'*64
    record['registration_digest']=digest([record['binding']['requested_action'],record['binding_digest'],
                                         record['actor_id'],record['validation']])
    flow.transport.payload=canonical(value)
    assert flow.get('/receipt/result/'+RID).status_code==403


def test_html_escapes_untrusted_item_names(keys):
    # Re-freeze a new request is required for a changed name; use a fresh fixture
    # reader before authenticate through a local Rig/Source pair.
    r=Rig(keys)
    sources=Sources(r)
    sources.sheets['cases'][0]['operations'][0]['changes']['name']='<script>alert(1)</script>'
    r.gateway.read_case=sources.reader()
    r.authenticate()
    view=r.gateway.details(r.ticket,cookie=r.ticket.cookie)
    from services.human_general.receipt_correction_web import detail_table
    html=detail_table(view['preview']['items'])
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_journal_CAS_failure_is_not_retried_or_posted(flow):
    fields,_=flow.form()
    count=0
    def conflict(*_):
        nonlocal count
        count+=1
        raise StateError('HTTP_412')
    flow.transport.replace_versioned=conflict
    assert flow.post(fields).status_code==409
    assert count==1 and flow.transport.writes==0


def test_result_journal_corruption_and_ACL_change_stop_query(flow):
    fields,_=flow.form()
    flow.post(fields)
    before=flow.transport.payload
    value=json.loads(before)
    value['results'][RID]['validation']['items'][0]['amount']+=1
    flow.transport.payload=canonical(value)
    assert flow.get('/receipt/result/'+RID).status_code==409
    flow.transport.payload=before
    flow.sources.meta[RESULTS]['permissions'].append(dict(type='anyone',role='reader'))
    assert flow.get('/receipt/result/'+RID).status_code==409


def test_route_refuses_short_lived_only_gateway_configuration(flow):
    flow.r.gateway.journal=None
    assert flow.get('/receipt/register/'+RID).status_code==409
    assert flow.transport.writes==0
