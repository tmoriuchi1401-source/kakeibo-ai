"""Static rendering of native fixture cells, NOT a product HTML/Safari UI.

No route, scripts, menu or custom button is added. Values come from the actual
Sheet projection formulas evaluated independently. Native Sheets/iPhone remain
unverified: these screenshots check wrapping and the proposed column widths.
"""
from html import escape
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from sheets_only_fixtures import Rig, evaluate, no_clients
from test_iphone_handoff_browser import browser  # existing sealed Chromium harness
from app.iphone_sheet_reader import raw_value


@pytest.mark.parametrize('width',[1365,390])
@pytest.mark.parametrize('scenario',['general_compare','medical_compare','medical_request','unknown','pdf_hold'])
def test_native_fixture_values_wrap_and_identity_remains_visible(browser,width,scenario,monkeypatch,tmp_path):
    no_clients(monkeypatch);r=Rig(monkeypatch,tmp_path)
    cid='SYN-M101' if scenario.startswith('medical') else 'SYN-P201' if scenario=='pdf_hold' else 'SYN-G101'
    r.request(cid,'比較 1')
    if scenario=='unknown':
        def fail(*args,**kwargs):raise OSError('synthetic lost draft response')
        monkeypatch.setattr(r.old.old.native[cid].readers.draft,'replace_versioned',fail)
    r.worker.poll()
    if scenario=='medical_request':r.request(cid,r.token(cid));r.worker.poll()
    text=evaluate(r.sheet,cid)
    assert text==r.sheet.view_text(cid)
    cells=r.sheet.row(cid)
    headers=r.sheet.raw['sheets'][0]['data'][0]['rowData'][0]['values']
    values=[raw_value(c) for c in cells];values[2]=text
    # Display-only fixture serialization. Never parse or execute original URLs.
    values=[('原本リンク（保持）' if i==4 else str(v)) for i,v in enumerate(values)]
    widths=[x['updateDimensionProperties']['properties']['pixelSize'] for x in r.sheet.format_plan[:3]]
    cols=''.join(f'<col style="width:{widths[i] if i<3 else 180}px">' for i in range(19))
    html='''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{box-sizing:border-box}body{margin:0;padding:8px;font:15px/1.5 "Noto Sans CJK JP",system-ui,sans-serif;color:#17313d}
p{margin:8px 0} .grid{max-width:100%;overflow:auto;border:1px solid #acbdc4;max-height:85vh}table{border-spacing:0;table-layout:fixed;width:3246px}
th,td{border:1px solid #c9d7dc;padding:6px;white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top}th{position:sticky;top:0;background:#e1edf1;z-index:2}
td:first-child,th:first-child{position:sticky;left:0;background:#e1edf1;z-index:1}th:first-child{z-index:3}.chip{display:block;min-height:44px;background:#eef3f6;border:1px solid #819ba8;padding:8px 2px}
</style><p>合成「確認」1タブ・セル表示検証（実機未確認）</p><div class="grid"><table><colgroup>'''+cols+'</colgroup><thead><tr>'
    html+=''.join('<th>'+escape(raw_value(h))+'</th>' for h in headers)+'</tr></thead><tbody><tr>'
    html+=''.join('<td>'+('<span class="chip">'+escape(v)+' ▾</span>' if i==1 else escape(v))+'</td>' for i,v in enumerate(values))+'</tr></tbody></table></div>'
    context=browser.new_context(viewport=dict(width=width,height=844),service_workers='block')
    external=[]
    def deny(route):external.append(route.request.url);route.abort('blockedbyclient')
    context.route('**/*',deny)
    page=context.new_page();page.set_content(html)
    row=page.locator('tbody tr');expect(row.locator('td').nth(2)).to_contain_text(text)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    # First three columns are sufficient to request and inspect results.
    for index in range(3):
        box=row.locator('td').nth(index).bounding_box()
        assert box['x']>=0 and box['x']+box['width']<=width
    assert row.locator('.chip').bounding_box()['height']>=44
    output=Path('.private/sheets-only');output.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(output/f'{width}-{scenario}.png'))
    page.locator('.grid').evaluate('g=>g.scrollLeft=1500')
    expect(row.locator('td').first).to_be_in_viewport()
    assert not external
    r.assert_safe()
    (output/f'{width}-{scenario}-evidence.json').write_text(json.dumps(dict(
        width=width,scenario=scenario,case_id=cid,text=text,columns=widths,
        native_google_verified=False,iphone_device_verified=False,external_requests=external,
        writer_dispatches=0,authority_dispatches=0),ensure_ascii=False),encoding='utf8')
    context.close()
