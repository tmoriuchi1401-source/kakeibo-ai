"""New cell wording only; no new product UI or repeated device acceptance."""
import pytest

from registration_fixtures import Rig
from sheets_only_fixtures import no_clients, evaluate
from test_iphone_handoff_browser import browser
from test_sheets_only_visual import render_cells


@pytest.mark.parametrize('width', [1365, 390])
@pytest.mark.parametrize('scenario', ['missing_amount', 'multiple', 'conflict', 'validated', 'received'])
def test_medical_changed_cell_display(browser, width, scenario, monkeypatch, tmp_path):
    no_clients(monkeypatch); r = Rig(monkeypatch, tmp_path); cid = 'SYN-M101'
    if scenario in {'missing_amount', 'multiple'}:
        for field in (['amount'] if scenario == 'missing_amount' else ['date', 'merchant', 'amount', 'category']):
            r.set(cid, field, '')
    r.register(cid, 2 if scenario == 'conflict' else 1)
    if scenario == 'received':
        r.register(cid, 2)
    text = evaluate(r.sheet, cid)
    assert '入力修正だけでは登録検証・記帳されません' in text
    if scenario in {'conflict', 'received'}:
        assert '再登録せず' in text and '登録 3' not in text
    render_cells(browser, r, cid, width, 'medical-'+scenario)
