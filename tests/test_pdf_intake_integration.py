from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
import pytest

from app.drive_run_state import StateError
from app.pdf_intake_authority import binding,digest,DecisionSealer
from app.receipt_item_review import fields,card,snapshot,check_snapshot,evaluate
from app.receipt_item_confirmation import ConfirmedItems,annotated
from app.pdf_intake_production import page_count
from app.pdf_intake_runner import observe_source
from app.pdf_intake_registry import RegistryStore
from test_pdf_intake_runner import rig,CATEGORIES
from test_pdf_intake_registry import Memory
from test_pdf_intake_authority import RAW,OWNER,page


def selected(runner,key,uid):
    c=runner.store.load()['pages'][key]['units'][uid]['candidate']
    current=fields(c['record']);current['action']='記帳する'
    if 'structure_confirmation' in current:current['structure_confirmation']='確認済み'
    return snapshot(card(c['record'],categories=CATEGORIES),current)


def attest(runner,key,uid,s):
    value=runner.store.load();unit=value['pages'][key]['units'][uid];c=unit['candidate'];p=c['posting'];rid=str(uuid4())
    current=check_snapshot(s,card(c['record'],categories=CATEGORIES))
    proof=ConfirmedItems(rid,c['record']['digest'],digest(s),digest(current),digest(c['record']['legacy']['identity']),OWNER,100,'a'*64)
    c['confirmation_journal']={'requests':{rid:{'proof':vars(proof)}}}
    expected=binding(value['pages'][key]['page'],'receipt_posting',unit=p['unit_binding'],snapshot_digest=digest(s),plan_digest=p['plan_digest'])
    sealer=DecisionSealer(b'x'*32,OWNER)
    actor=SimpleNamespace(request_id=rid,actor_id=OWNER,method='google_oidc_shared_session_v2',verified_at=100,
                           request_digest=digest({'request_id':rid,**expected}))
    unit['posting_authority']=sealer.seal(expected,actor,rid,101);value['generation']+=1;runner.store.save(value)
    runner.proof_client.verify=sealer.verify


def test_sheet_intent_prepared_only_not_posting_permission():
    runner,key,uid,db=rig();s=selected(runner,key,uid)
    assert runner.prepare_manual(key,uid,s)['status']=='awaiting_authenticated_confirmation'
    assert runner.post_manual(key,uid,s,apply=True)['status']=='posting_authority_required'
    assert db.writes==[]
    before=runner.store.io.writes
    runner.prepare_manual(key,uid,s)
    assert runner.store.io.writes==before


def test_authenticated_exact_snapshot_posts_once_with_existing_writer():
    runner,key,uid,db=rig();s=selected(runner,key,uid);runner.prepare_manual(key,uid,s);attest(runner,key,uid,s)
    assert runner.post_manual(key,uid,s)['plan']['total']==730 and db.writes==[]
    assert runner.post_manual(key,uid,s,apply=True)['status']=='imported'
    before=deepcopy(db.data)
    assert runner.post_manual(key,uid,s,apply=True)['replay']
    assert db.data==before and len(db.writes)==3


def test_authorized_input_cannot_be_replaced_after_confirm():
    runner,key,uid,db=rig();s=selected(runner,key,uid);runner.prepare_manual(key,uid,s);attest(runner,key,uid,s)
    altered=deepcopy(s);altered['items'][0]['category']='日用品｜洗剤'
    with pytest.raises(StateError,match='confirmed_snapshot_changed'):runner.post_manual(key,uid,altered,apply=True)
    assert db.writes==[]


def test_ui_projection_disables_normal_default_auto_values():
    runner,key,uid,db=rig();v=runner.store.load();v['pages'][key]['units'][uid]['candidate']['ui_projected']=True
    v['generation']+=1;runner.store.save(v)
    assert runner.post_normal(key,uid,apply=True)['status']=='posting_authority_required'
    assert db.writes==[]


def test_changed_source_is_entire_source_gate_not_new_identity():
    runner,key,uid,db=rig();p=page(number=1,classification='normal',source_id='newPDFSourceFile01')
    runner.store.register(p)
    with pytest.raises(StateError,match='source_changed'):
        observe_source(b'other',p['source']['source_file_id'],runner.store,observer=lambda *_:SimpleNamespace(
            source_content_hash=__import__('hashlib').sha256(b'other').hexdigest(),pages=[SimpleNamespace(page_number=n) for n in range(1,4)]))
    assert db.writes==[]


def test_no_whole_pdf_fallback_or_schedule_change():
    from pathlib import Path
    scan=Path('app/receipt_confirmation_production.py').read_text('utf8')
    assert scan.index('if number>1:')<scan.index('owner_route=review.route_owner_intake')
    assert "'multipage_sources':multipage_plans" in scan
    wf=Path('.github/workflows/kakeibo-production.yml').read_text('utf8')
    assert "cron: '31 */3 * * *'" in wf and 'group: kakeibo-production' in wf
    assert 'PDF_INTAKE_CONFIG_FILE_ID: ${{ vars.PDF_INTAKE_CONFIG_FILE_ID }}' in wf


@pytest.mark.parametrize('raw',[b'not a pdf',b'%PDF-corrupt'])
def test_invalid_pdf_is_held_not_sent(raw):
    with pytest.raises(StateError):page_count(raw)
