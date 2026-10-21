"""Real Chromium with actual authenticated Flask handlers, zero live transport.

Every browser request is intercepted before network and delivered in process.
Ubuntu runs Chromium and pytest inside a loopback-only network namespace too.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from app.iphone_sheet_handoff_host import PREFIX
from sheet_handoff_host_fixtures import HostRig
from unified_shadow_fixtures import ORIGIN, DOMAIN, inputs


@pytest.fixture(scope='module')
def browser():
    evidence=dict(os_egress_isolation=False, iphone_device_verified=False)
    if os.environ.get('HANDOFF_OUTER_NETNS'):
        assert os.readlink('/proc/self/ns/net') != os.environ['HANDOFF_OUTER_NETNS']
        import subprocess
        interfaces = json.loads(subprocess.check_output(['ip', '-j', 'link'], text=True))
        assert {i['ifname'] for i in interfaces} == {'lo'}
        for family in ('-4', '-6'):
            routes=json.loads(subprocess.check_output(['ip', family, '-j', 'route'], text=True))
            assert all(route.get('dev')=='lo' and route.get('dst')!='default' for route in routes)
        evidence.update(os_egress_isolation=True, loopback_only=True, external_interfaces=0,
                        external_routes=0, namespace=os.readlink('/proc/self/ns/net'))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=['--disable-background-networking',
            '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=<-loopback>',
            '--host-resolver-rules=MAP * ~NOTFOUND'])
        yield browser
        if evidence['os_egress_isolation']:
            processes=browser.new_browser_cdp_session().send('SystemInfo.getProcessInfo')['processInfo']
            assert any(p['type']=='browser' for p in processes)
            for process in processes:
                try: namespace=os.readlink('/proc/'+str(process['id'])+'/ns/net')
                except FileNotFoundError:
                    assert process['type']!='browser'  # exited renderer
                    continue
                assert namespace==evidence['namespace']
            evidence['chromium_namespace_verified']=True
        output=Path('.private/handoff-ui');output.mkdir(parents=True,exist_ok=True)
        (output/'network-evidence.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
        browser.close()


@pytest.fixture(params=[1365, 390])
def ui(request, browser, monkeypatch, tmp_path):
    from googleapiclient import discovery
    from google.cloud import firestore
    def forbidden(*args, **kwargs): pytest.fail('live_client_forbidden')
    monkeypatch.setattr(discovery, 'build', forbidden)
    monkeypatch.setattr(firestore, 'Client', forbidden)
    r = HostRig(monkeypatch, tmp_path); r.h.login()
    context = browser.new_context(viewport={'width':request.param, 'height':844}, service_workers='block')
    state = SimpleNamespace(r=r, context=context, page=context.new_page(), width=request.param,
        requests=[], errors=[], external=[], fault=None)
    state.page.on('pageerror', lambda error:state.errors.append(str(error)))
    def bridge(route):
        req=route.request; url=urlsplit(req.url)
        if url.scheme+'://'+url.netloc != ORIGIN:
            state.external.append(req.url);route.abort('blockedbyclient');return
        state.requests.append((req.method, url.path))
        if state.fault and state.fault(route, url.path, False): return
        response=r.h.client.open(url.path, method=req.method, data=req.post_data, base_url=ORIGIN,
            headers={k:v for k,v in req.headers.items() if k in {'origin','x-csrf-token','content-type'}})
        if state.fault and state.fault(route, url.path, True): return
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.get_data())
    context.route('**/*', bridge)
    state.page.goto(ORIGIN+PREFIX)
    yield state
    assert not state.errors and not state.external
    assert {cid:r.old.frozen(cid) for cid in r.old.native} == r.before
    assert all(c.readers.ledger.writes == 0 for c in r.old.native.values())
    assert not any('/r1/request' in path for _,path in state.requests)
    context.close()


def select(ui, cid='SYN-G101'):
    ui.page.locator('#search').fill(cid)
    row=ui.page.locator('tbody tr:visible');expect(row).to_have_count(1)
    return row


def imported(ui, cid='SYN-G101'):
    row=select(ui,cid)
    row.locator('[data-action=import_draft]').click()
    expect(row.locator('.save-state')).to_have_text('下書き取込済み・未確定')
    return select(ui,cid)


def compare(ui,cid='SYN-G101'):
    row=imported(ui,cid);row.locator('[data-action=compare]').click()
    expect(row.locator('.review')).to_be_visible()
    return row


def reauthenticate(ui):
    h=ui.r.h;link=urlsplit(h.rt.seed());h.start_path=link.path+'?'+link.query
    response=h.get(h.start_path)
    html=response.get_data(as_text=True).split('<form method="post" action="/start">')[1].split('</form>')[0]
    fields=inputs(h.app.response_class(html));fields['login']='reauth'
    assert h.post_form('/start',fields).status_code==303
    h.ticket=h.rt.tickets.load(h.client.get_cookie(h.web.COOKIE,domain=DOMAIN).value)
    assert h.post_form('/oauth/callback',{'code':'synthetic-code','state':h.ticket.state},'https://accounts.google.com').status_code==303


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
def test_full_flow_separate_import_compare_confirm_result_and_sticky_id(ui,cid):
    row=select(ui,cid)
    expect(row.locator('[data-action=compare]')).to_be_disabled()
    expect(row.locator('[data-field=memo]')).to_have_attribute('readonly','')
    with ui.page.expect_popup() as opened: row.get_by_text('合成原本を確認',exact=True).click()
    original=opened.value;original.wait_for_load_state()
    expect(original.locator('img')).to_be_visible()
    assert original.locator('img').evaluate('i=>i.complete&&i.naturalWidth===620')
    original.close()
    shots=Path('.private/handoff-ui');shots.mkdir(parents=True,exist_ok=True)
    ui.page.screenshot(path=str(shots/f'{ui.width}-{cid}-list.png'))
    row=imported(ui,cid)
    assert len([p for m,p in ui.requests if m=='POST']) == 2
    assert not ui.r.old.ui._requests(cid)
    row.locator('[data-action=compare]').click()
    expect(row.locator('.compare')).to_contain_text(cid)
    expect(row.locator('.compare')).to_contain_text('原本revision')
    expect(row.locator('.compare')).to_contain_text('変更差分')
    expect(row.locator('.compare')).to_contain_text(ui.r.old.native[cid].rig.f.fields['merchant'])
    post=row.locator('[data-action=confirm_handoff]');expect(post).to_be_disabled()
    row.locator('.explicit').check();expect(post).to_be_enabled()
    post.scroll_into_view_if_needed()
    box=row.locator('td').first.bounding_box();assert 0<=box['x']<ui.width
    expect(row.locator('td strong').first).to_be_in_viewport()
    assert ui.page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    ui.page.screenshot(path=str(shots/f'{ui.width}-{cid}-compare.png'))
    # Two actual events while first request is in flight; one request only.
    post.evaluate("b=>{b.click();b.dispatchEvent(new MouseEvent('click',{bubbles:true}));}")
    expect(row.locator('.result')).to_have_text('確認済み・未記帳')
    assert len([p for m,p in ui.requests if m=='POST' and p==PREFIX+'/request']) == 2
    row.locator('.recovery').click();expect(ui.page.locator('h2')).to_have_text('確認済み・未記帳')
    before=len(ui.requests);ui.page.reload();ui.page.go_back()
    assert all(m=='GET' for m,_ in ui.requests[before:])
    ui.page.goto(ORIGIN+PREFIX);row=select(ui,cid)
    expect(row.locator('[data-action=import_draft]')).to_be_disabled()
    expect(row.locator('.result')).to_have_text('確認済み・未記帳')
    (shots/f'{ui.width}-{cid}-evidence.json').write_text(json.dumps(dict(width=ui.width,case_id=cid,
        requests=ui.requests,writer_dispatches=0,automatic_retries=0,external_requests=ui.external,
        result='FINANCIAL_BOUNDARY_STOPPED',iphone_device_verified=False),ensure_ascii=False),encoding='utf8')


@pytest.mark.parametrize('fault',['before_receive','response_lost','attempt_unknown'])
@pytest.mark.parametrize('action',['import_draft','confirm_handoff'])
def test_unknown_persists_through_reload_and_relogin_only_result_lookup(ui,monkeypatch,fault,action):
    row=select(ui) if action=='import_draft' else compare(ui)
    if action=='confirm_handoff':row.locator('.explicit').check()
    if fault=='attempt_unknown':
        def fail(*args,**kwargs): raise RuntimeError('synthetic terminal CAS unavailable')
        original=ui.r.adapter._save
        def save(*args,**kwargs):
            if kwargs['confirmation']['requests'] and any(r['status']!='ATTEMPTING' for r in kwargs['confirmation']['requests'].values()): fail()
            return original(*args,**kwargs)
        monkeypatch.setattr(ui.r.adapter,'_save',save)
    def broken(route,path,after):
        if path==PREFIX+'/request' and ((fault=='before_receive' and not after) or (fault=='response_lost' and after)):
            route.abort('connectionreset');return True
    ui.fault=broken
    row.locator(f'[data-action={action}]').click()
    expect(row.locator('.result')).to_contain_text('UNKNOWN')
    writes=len([p for m,p in ui.requests if m=='POST']);ui.fault=None
    ui.page.reload();row=select(ui)
    expect(row.locator('[data-action=import_draft]')).to_be_disabled()
    other=select(ui,'SYN-M101');expect(other.locator('[data-action=import_draft]')).to_be_disabled()
    ui.r.adapter.uncertain=False  # process restart still retains journal + browser hints
    reauthenticate(ui)
    ui.page.reload();row=select(ui)
    row.locator('.recovery').click()
    expect(ui.page.locator('h2')).to_have_text('UNKNOWN：結果不明・要照合' if fault!='response_lost' else
        ('下書き取込済み・未確定' if action=='import_draft' else '確認済み・未記帳'))
    ui.page.goto(ORIGIN+PREFIX);row=select(ui)
    if fault!='response_lost':expect(row.locator('[data-action=import_draft]')).to_be_disabled()
    assert len([p for m,p in ui.requests if m=='POST'])==writes


@pytest.mark.parametrize('change',['sheet','source','draft','expired','duplicate'])
def test_changed_snapshot_expired_and_duplicate_reject_without_financial_dispatch(ui,change):
    row=compare(ui);row.locator('.explicit').check()
    if change=='sheet':
        cells=next(r['values'] for r in ui.r.raw['sheets'][0]['data'][0]['rowData'][1:] if r['values'][0]['userEnteredValue']['stringValue']=='SYN-G101')
        cells[12]['userEnteredValue']={'stringValue':'別操作の合成メモ'}
    elif change=='source':
        from draft_runtime_fixtures import set_value
        from app.receipt_confirmation_contract import load
        port=ui.r.old.native['SYN-G101'].readers.source;v=load(port.raw);v['source']['drive_revision_id']='replaced';set_value(port,v)
    elif change=='draft':
        from draft_runtime_fixtures import set_value
        from app.unified_receipt_review import digest
        d=ui.r.old.ui._draft('SYN-G101')[0];d['revision']+=1;d['value']['fields']['memo']='別の下書き';d['input_digest']=digest(d['value']);set_value(ui.r.old.native['SYN-G101'].readers.draft,d)
    elif change=='expired':ui.r.h.now+=601
    else:
        p=ui.r.prepare('SYN-G101','confirm_handoff');assert ui.r.send(p,True).status_code==200
    row.locator('[data-action=confirm_handoff]').click()
    expect(row.locator('.result')).to_contain_text('期限切れ' if change=='expired' else '競合')
    expect(row.locator('[data-action=import_draft]')).to_be_disabled()
    assert not ui.r.old.ui._requests('SYN-G101')


def test_holds_and_partial_manual_input_preserved(ui):
    for cid in ('SYN-G103','SYN-H301','SYN-P201','SYN-P202','SYN-M102','SYN-G102'):
        row=select(ui,cid);assert row.locator('button').count()==0
        expect(row).to_contain_text('保留')
    # The synthetic Sheet is the input surface. Incomplete manual medical data
    # can import, but cannot compare/confirm. No browser-side guessed category.
    cells=next(r['values'] for r in ui.r.raw['sheets'][0]['data'][0]['rowData'][1:] if r['values'][0]['userEnteredValue']['stringValue']=='SYN-M101')
    cells[6]['userEnteredValue']={'stringValue':''};cells[12]['userEnteredValue']={'stringValue':'手入力途中の合成メモ'}
    row=imported(ui,'SYN-M101')
    expect(row.locator('[data-field=amount]')).to_have_value('')
    expect(row.locator('[data-field=memo]')).to_have_value('手入力途中の合成メモ')
    row.locator('[data-action=compare]').click();expect(row.locator('.result')).to_contain_text('保留')
    ui.page.reload();row=select(ui,'SYN-M101')
    expect(row.locator('[data-field=memo]')).to_have_value('手入力途中の合成メモ')


def test_reauthentication_discards_old_comparison_and_keeps_saved_input(ui,monkeypatch):
    from datetime import datetime,timezone
    from google.auth import _helpers
    monkeypatch.setattr(_helpers,'utcnow',lambda:datetime.fromtimestamp(ui.r.h.now,timezone.utc))
    row=compare(ui,'SYN-M101');old_memo=row.locator('[data-field=memo]').input_value()
    ui.r.h.now+=601
    # Existing synthetic RSA/OAuth/session, with a new actual ticket/session.
    reauthenticate(ui)
    ui.page.reload();row=select(ui,'SYN-M101')
    expect(row.locator('.review')).to_be_hidden()
    expect(row.locator('[data-field=memo]')).to_have_value(old_memo)
    row.locator('[data-action=compare]').click();expect(row.locator('.review')).to_be_visible()
    expect(row.locator('.explicit')).not_to_be_checked()


def test_prepare_communication_failure_is_readonly_across_reload(ui):
    def fail(route,path,after):
        if '/prepare/' in path and not after:route.abort('timedout');return True
    ui.fault=fail;row=select(ui);row.locator('[data-action=import_draft]').click()
    expect(row.locator('.result')).to_contain_text('UNKNOWN')
    ui.fault=None;before=len(ui.requests)
    ui.page.reload();row=select(ui)
    expect(row.locator('[data-action=import_draft]')).to_be_disabled()
    row.locator('.recovery').click();expect(ui.page.locator('h2')).to_contain_text('UNKNOWN')
    assert all(method=='GET' for method,_ in ui.requests[before:])
    assert not ui.r.adapter._state()[1]['requests']


def test_unavailable_recovery_storage_fails_closed_without_post(ui):
    ui.page.add_init_script("Storage.prototype.setItem=()=>{throw new Error('synthetic unavailable storage')}")
    ui.page.reload();row=select(ui);row.locator('[data-action=import_draft]').click()
    expect(row.locator('[data-action=import_draft]')).to_be_disabled()
    assert not any(method=='POST' and path==PREFIX+'/request' for method,path in ui.requests)
    assert not ui.r.adapter._state()[1]['requests']
