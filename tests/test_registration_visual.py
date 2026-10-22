"""Static native cell values only. No new product page or real Sheets claim."""
import pytest

from registration_fixtures import Rig
from sheets_only_fixtures import no_clients, evaluate
from test_iphone_handoff_browser import browser
from test_sheets_only_visual import render_cells


@pytest.mark.parametrize('width',[1365,390])
@pytest.mark.parametrize('scenario',['registered','medical_partial','permission','unknown','sync_wait'])
def test_registration_native_cell_view(browser,width,scenario,monkeypatch,tmp_path):
    no_clients(monkeypatch)
    r=Rig(monkeypatch,tmp_path)
    cid='SYN-M101' if scenario=='medical_partial' else 'SYN-G101'
    if scenario=='medical_partial':r.set(cid,'amount','')
    if scenario=='permission':r.policy(lambda v:v.update(permissions_verified=False))
    if scenario=='unknown':
        def fail(*a,**k):raise TimeoutError('synthetic response loss')
        monkeypatch.setattr(r.old.old.native[cid].readers.draft,'replace_versioned',fail)
    if scenario=='sync_wait':
        r.request(cid,'登録 1');assert r.worker.poll()[-1]['status']=='SYNC_WAIT'
        assert '受付待ち' in evaluate(r.sheet,cid)
    else:
        expected={'registered':'VALIDATED_UNPOSTED','medical_partial':'HELD',
                  'permission':'PERMISSION_UNVERIFIED','unknown':'UNKNOWN'}[scenario]
        assert r.register(cid)['status']==expected
    render_cells(browser,r,cid,width,'registration-'+scenario)
