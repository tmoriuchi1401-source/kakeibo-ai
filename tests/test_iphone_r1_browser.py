"""Real Chromium DOM/events; every request goes through the actual Flask host.

In-process HTTP bridge avoids exposing a listener. It does not mock service
responses or writer execution. No external browser requests are allowed.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from app.iphone_confirmation_host import PREFIX
from iphone_r1_fixtures import Rig
from unified_shadow_fixtures import ORIGIN


@pytest.mark.parametrize('width',[1365,390])
@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
def test_real_events_full_flow_and_sticky_identity(monkeypatch,tmp_path,width,cid):
    r=Rig(monkeypatch,tmp_path);r.h.login();errors=[];requests=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        context=browser.new_context(viewport={'width':width,'height':844})
        def bridge(route):
            req=route.request;u=urlsplit(req.url)
            assert u.netloc=='confirmation.example.test'
            requests.append((req.method,u.path))
            response=r.h.client.open(u.path,method=req.method,data=req.post_data,base_url=ORIGIN,
                headers={k:v for k,v in req.headers.items() if k in {'origin','x-csrf-token','content-type'}})
            route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.get_data())
        context.route('**/*',bridge)
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(ORIGIN+PREFIX)
        page.locator('#search').fill(cid)
        row=page.locator('tbody tr:visible');expect(row).to_have_count(1)
        expect(row.locator('td').first).to_have_text(cid)
        with page.expect_popup() as opened:
            row.get_by_text('合成原本を確認',exact=True).click()
        original=opened.value;original.wait_for_load_state();assert cid in original.locator('body').inner_text();original.close()
        fields=r.native[cid].rig.f.fields
        for name,value in fields.items():
            loc=row.locator(f'[data-field="{name}"]')
            if loc.count():loc.fill(str(value))
        if cid=='SYN-G101':
            for i,item in enumerate(r.native[cid].rig.f.items):
                fs=row.locator('fieldset').nth(i)
                for key in ('name','amount','category','note'):fs.locator(f'[data-item="{key}"]').fill(str(item[key]))
                fs.locator('[data-item=state]').select_option('entered')
        # Partial medical input survives save/reload before full manual entry.
        if cid=='SYN-M101':
            row.locator('[data-field=amount]').fill('')
            row.locator('[data-action=save_draft]').click()
            expect(row.locator('.save-state')).to_have_text('下書き保存済み')
            page.reload();page.locator('#search').fill(cid);row=page.locator('tbody tr:visible')
            expect(row.locator('[data-field=merchant]')).to_have_value(fields['merchant'])
            row.locator('[data-field=amount]').fill('135')
        row.locator('[data-action=save_draft]').click()
        expect(row.locator('.save-state')).to_have_text('下書き保存済み')
        page.reload();page.locator('#search').fill(cid);row=page.locator('tbody tr:visible')
        expect(row.locator('[data-field=merchant]')).to_have_value(fields['merchant'])
        row.locator('[data-action=compare]').click()
        expect(row.locator('.review')).to_be_visible()
        expect(row.locator('.compare')).to_contain_text('修正後（実際の記帳対象）')
        expect(row.locator('.compare')).to_contain_text(fields['merchant'])
        row.locator('.explicit').check()
        # Scroll far right and verify the ID is still within the viewport.
        row.locator('[data-action=post]').scroll_into_view_if_needed()
        box=row.locator('td').first.bounding_box();assert 0<=box['x']<width and box['width']>=120
        shot=Path('.private/r1-browser');shot.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(shot/f'{width}-{cid}-compare.png'))
        # Two actual DOM click events while the first POST is in flight.
        row.locator('[data-action=post]').evaluate("b=>{b.click();b.dispatchEvent(new MouseEvent('click',{bubbles:true}));}")
        expect(row.locator('.result')).to_have_text('合成検証での記帳成功')
        row.locator('.recovery').click()
        expect(page.locator('h2')).to_have_text('合成検証での記帳成功')
        assert r.native[cid].readers.ledger.writes==(4 if cid=='SYN-G101' else 3)
        assert sum(method=='POST' for method,path in requests)==(2 if cid=='SYN-G101' else 3)
        assert not errors
        (shot/f'{width}-{cid}.json').write_text(json.dumps(dict(viewport=width,case=cid,requests=requests,errors=errors,
            original_roundtrip=True,save_reload=True,comparison=True,explicit_post=True,result='COMPLETE'),ensure_ascii=False),encoding='utf8')
        browser.close()


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
@pytest.mark.parametrize('width',[1365,390])
def test_lost_financial_response_is_inspected_without_retry(monkeypatch,tmp_path,cid,width):
    r=Rig(monkeypatch,tmp_path);r.h.login();r.save(cid);posts=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':width,'height':844})
        def bridge(route):
            req=route.request;u=urlsplit(req.url);assert u.netloc=='confirmation.example.test'
            res=r.h.client.open(u.path,method=req.method,data=req.post_data,base_url=ORIGIN,
                headers={k:v for k,v in req.headers.items() if k in {'origin','x-csrf-token','content-type'}})
            if req.method=='POST':posts.append(1);route.abort('connectionreset');return
            route.fulfill(status=res.status_code,headers=dict(res.headers),body=res.get_data())
        page.route('**/*',bridge);page.goto(ORIGIN+PREFIX);page.locator('#search').fill(cid)
        row=page.locator('tbody tr:visible');row.locator('[data-action=compare]').click()
        row.locator('.explicit').check();row.locator('[data-action=post]').click()
        expect(row.locator('.result')).to_contain_text('UNKNOWN')
        expect(row.locator('[data-action=compare]')).to_be_disabled()
        row.locator('.recovery').click();expect(page.locator('h2')).to_have_text('合成検証での記帳成功')
        assert len(posts)==1
        assert r.native[cid].readers.ledger.writes==(4 if cid=='SYN-G101' else 3)
        browser.close()


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
@pytest.mark.parametrize('width',[1365,390])
def test_expired_session_reauth_preserves_in_progress_input(monkeypatch,tmp_path,cid,width):
    r=Rig(monkeypatch,tmp_path);r.h.login()
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':width,'height':844})
        def bridge(route):
            req=route.request;u=urlsplit(req.url);assert u.netloc=='confirmation.example.test'
            res=r.h.client.open(u.path,method=req.method,data=req.post_data,base_url=ORIGIN,
                headers={k:v for k,v in req.headers.items() if k in {'origin','x-csrf-token','content-type'}})
            route.fulfill(status=res.status_code,headers=dict(res.headers),body=res.get_data())
        page.route('**/*',bridge);page.goto(ORIGIN+PREFIX);page.locator('#search').fill(cid)
        row=page.locator('tbody tr:visible');row.locator('[data-field=memo]').fill('再認証中の入力を保持')
        r.h.now+=600;row.locator('[data-action=save_draft]').click()
        expect(row.locator('.result')).not_to_be_empty()
        expect(row.locator('[data-field=memo]')).to_have_value('再認証中の入力を保持')
        link=urlsplit(r.h.rt.seed());r.h.start_path=link.path+'?'+link.query
        r.h.login()
        row.locator('[data-action=save_draft]').click()
        expect(row.locator('.save-state')).to_have_text('下書き保存済み')
        assert r.native[cid].readers.ledger.writes==0
        browser.close()


@pytest.mark.parametrize('width',[1365,390])
def test_network_loss_blocks_double_tap_and_preserves_input(monkeypatch,tmp_path,width):
    r=Rig(monkeypatch,tmp_path);r.h.login()
    with sync_playwright() as pw:
        browser=pw.chromium.launch();page=browser.new_page(viewport={'width':width,'height':844})
        def bridge(route):
            req=route.request;u=urlsplit(req.url)
            assert u.netloc=='confirmation.example.test'
            if req.method=='POST':route.abort('timedout');return
            res=r.h.get(u.path);route.fulfill(status=res.status_code,headers=dict(res.headers),body=res.get_data())
        page.route('**/*',bridge);page.goto(ORIGIN+PREFIX);page.locator('#search').fill('SYN-G101')
        row=page.locator('tbody tr:visible');row.locator('[data-field=memo]').fill('通信断でも保持')
        row.locator('[data-action=save_draft]').click()
        expect(row.locator('.result')).to_contain_text('UNKNOWN')
        expect(row.locator('[data-field=memo]')).to_have_value('通信断でも保持')
        expect(row.locator('[data-action=save_draft]')).to_be_disabled()
        assert r.native['SYN-G101'].readers.ledger.writes==0
        browser.close()
