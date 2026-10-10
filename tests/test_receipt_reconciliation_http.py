from copy import deepcopy
from urllib.parse import urlsplit,parse_qs
import pytest
from app.receipt_audit import MemoryAuditRepository,digest
from services.human_general.reconciliation_runtime import ReconciliationRuntime
from services.human_general.web import create_app
from test_human_general_http import HttpRig
from test_human_general_auth_transport import keys
from test_receipt_retention_audit import identity


def rig(keys,decision='same'):
    r=HttpRig(keys);r.ident=identity();r.repo=MemoryAuditRepository()
    r.target=dict(ledger_id='R-existing-01',snapshot_digest=digest('exact existing rows'),active=True,readback_complete=True)
    r.runtime=ReconciliationRuntime(r.db,r.settings,r.key,r.ident,lambda *_:r.ident,
        lambda _:r.ident['source_content_hash'],lambda _:r.target,r.repo,
        clock=lambda:r.now,identity=r.runtime.identity,exchange=r.exchange)
    r.app=create_app(lambda:r.runtime);r.app.testing=True;r.client=r.app.test_client()
    link=r.runtime.seed_decision(r.target['ledger_id'],r.target['snapshot_digest'],decision)
    parts=urlsplit(link);r.start_path=parts.path+'?'+parts.query;r.rid=parse_qs(parts.query)['request'][0]
    return r


@pytest.mark.parametrize('decision,state,count',[
    ('same','reconciled_existing',1),('different','confirmed_distinct',1),('unknown','reconciliation_required',0)])
def test_existing_oidc_screen_reconciliation_no_ai_or_writer(keys,decision,state,count):
    r=rig(keys,decision)
    assert r.start().status_code==303 and r.callback().status_code==303
    screen=r.get('/confirm');assert screen.status_code==200
    assert '記帳済みレシートとの対応確認' in screen.get_data(as_text=True)
    assert 'Gemini' not in screen.get_data(as_text=True)
    assert r.repo.writes==0
    assert r.confirm().status_code==303 and r.get('/result').status_code==200
    assert r.repo.writes==count and r.repo.get_current(r.ident)['status']==state
    before=deepcopy((r.repo.events,r.repo.current,r.repo.receipts))
    assert r.confirm().status_code==r.get(r.start_path).status_code==409
    assert (r.repo.events,r.repo.current,r.repo.receipts)==before


def test_stale_etag_cannot_decide_or_append(keys):
    r=rig(keys);r.start();r.callback()
    assert r.confirm(etag='"stale"').status_code==412
    assert r.repo.writes==0


@pytest.mark.parametrize('negative',['actor','source','ledger','origin','csrf'])
def test_rejections_preserve_permanent_history(keys,negative):
    r=rig(keys)
    if negative=='actor':r.claim_changes={'sub':'987654321'}
    assert r.start().status_code==303
    response=r.callback()
    if negative=='actor':assert response.status_code==403
    else:
        assert response.status_code==303
        if negative=='source':r.ident={**r.ident,'review_identity':digest('changed review')}
        elif negative=='ledger':r.target={**r.target,'snapshot_digest':digest('modified rows')}
        if negative=='origin':
            _,tag=r.runtime.state(r.rid,'authorities').read_versioned()
            response=r.post('/confirm',dict(action='confirm',csrf=r.ticket.csrf,etag=tag),origin='null')
        else:response=r.confirm(csrf='bad') if negative=='csrf' else r.confirm()
        assert response.status_code in {400,409,412}
    assert r.repo.writes==0
