from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation import ReceiptConfirmation, TITLE, review_id
from app.receipt_reimport_production import TABLES, target_snapshot
from tests.test_receipt_confirmation import Store, DB
from tests.test_receipt_reimport import fixture


def intake(answer='医療'):
    store,db=Store(),DB();verify=Mock()
    review=ReceiptConfirmation(store,db,verify)
    source=dict(source_id='synthetic',version='1',sha256='a'*64,mime_type='image/png')
    review.observe_intake_hold(source,'folder',SimpleNamespace(classification='sensitive_unknown'))
    review.render();db.rows[TITLE][0][14]=answer;review.capture_inputs()
    return review,store,db,verify,source


def test_medical_kind_answer_creates_manual_review_once_without_posting():
    review,store,db,verify,source=intake()
    assert review.route_owner_intake(source,'folder')=='医療'
    review.render()
    assert review.route_owner_intake(source,'folder')=='医療'
    assert review.apply_confirmations()==0
    assert len(review.items)==2
    assert review.items[review_id('medical',source)]['inputs']==['']*8
    assert db.rows[TITLE][0][14]=='医療'
    assert not db.rows['支出明細'] and not db.rows['取込データ']
    from app.medical_auto_posting import owner_blocked
    assert owner_blocked(source,store.value)


@pytest.mark.parametrize('answer',['一般の買物','再撮影が必要','医療かもしれない',''])
def test_kind_note_cannot_grant_normal_ai_authority(answer):
    review,store,db,verify,source=intake(answer)
    assert review.route_owner_intake(source,'folder')==''
    assert len(review.items)==1 and not db.rows['支出明細']


def test_owner_exclusion_does_not_create_receipt_or_move_source():
    review,store,db,verify,source=intake('対象外')
    assert review.route_owner_intake(source,'folder')=='対象外'
    assert len(review.items)==1 and review.apply_confirmations()==0
    assert not db.rows['レシート']


@pytest.mark.parametrize('conflict',['hold','typing','presentation','source'])
def test_medical_route_rejects_stale_answer_or_source(conflict):
    review,store,db,verify,source=intake()
    if conflict=='hold':
        db.rows[TITLE][0][12]='保留';review.capture_inputs()
    elif conflict=='typing':db.rows[TITLE][0][14]='対象外'
    elif conflict=='presentation':db.rows[TITLE][0][4]='changed'
    else:verify.side_effect=StateError('confirmation_source_changed')
    if conflict=='source':
        with pytest.raises(StateError):review.route_owner_intake(source,'folder')
    else:assert review.route_owner_intake(source,'folder')==''
    assert len(review.items)==1


def normal():
    parsed,rows=fixture();rows.pop('categories');parsed.items[0].name='New candidate label'
    store,db=Store(),DB()
    for key,title in TABLES.items():db.rows[title]=deepcopy(rows[key])
    source=dict(source_id='s1',version='1',sha256='b'*64,mime_type='application/pdf')
    store.value['manifest']={'sources':[source],'folder_id':'folder'}
    store.value['records']={'s1':dict(phase='complete',parsed=parsed.model_dump(),before=target_snapshot(rows,'s1'))}
    verify=Mock();review=ReceiptConfirmation(store,db,verify)
    review.prepare_general();review.render()
    db.rows[TITLE][0][12]='既存値を維持';review.capture_inputs()
    return review,store,db,verify,source


def test_keep_existing_can_close_obsolete_source_question_without_financial_write():
    review,store,db,verify,source=normal();key=review_id('normal',source)
    review.items[key].update(status='superseded',error='原本の版が変更')
    before=deepcopy({t:db.rows[t] for t in TABLES.values()})
    verify.side_effect=StateError('confirmation_source_changed')
    assert review.apply_confirmations()==0
    assert review.items[key]['status']=='closed_user' and 'error' not in review.items[key]
    assert {t:db.rows[t] for t in TABLES.values()}==before
    assert review.apply_confirmations()==0


@pytest.mark.parametrize('conflict',['ledger','typing','presentation','empty'])
def test_keep_existing_requires_current_ledger_and_live_owner_choice(conflict):
    review,store,db,verify,source=normal();key=review_id('normal',source)
    if conflict=='ledger':db.rows['支出明細'][0][11]='new owner note'
    elif conflict=='typing':db.rows[TITLE][0][12]='保留'
    elif conflict=='presentation':db.rows[TITLE][0][4]='different presentation'
    else:db.rows['支出明細']=[]
    before=deepcopy({t:db.rows[t] for t in TABLES.values()})
    assert review.apply_confirmations()==0
    assert review.items[key]['status']!='closed_user'
    assert {t:db.rows[t] for t in TABLES.values()}==before


@pytest.mark.parametrize('confirmed',[False,True])
def test_preview_uses_live_confirmation_validator_without_any_writes(monkeypatch,confirmed):
    from app import receipt_confirmation_production as runtime,google_clients
    from tests.test_receipt_confirmation import medical,confirm
    review,store,db,verify,source=medical()
    if confirmed:confirm(db)
    before_store,before_rows=deepcopy(store.value),deepcopy(db.rows)
    monkeypatch.setattr(runtime,'open_context',lambda *args:(SimpleNamespace(),store,db,verify))
    monkeypatch.setattr(google_clients,'read_only_drive_service',Mock())
    db.append_raw=Mock(side_effect=AssertionError('preview financial write'))
    db.update_row_raw=Mock(side_effect=AssertionError('preview financial write'))
    db.set_raw_range=Mock(side_effect=AssertionError('preview UI write'))
    store.save=Mock(side_effect=AssertionError('preview Drive write'))
    result=runtime.execute({},False)
    assert result['written']==0 and result['review_eligible']==int(confirmed)
    assert store.value==before_store and db.rows==before_rows


def test_preview_and_apply_agree_on_missing_medical_amount(monkeypatch):
    from app import receipt_confirmation_production as runtime,google_clients
    from tests.test_receipt_confirmation import medical
    review,store,db,verify,source=medical();db.rows[TITLE][0][12]='医療費を確定'
    monkeypatch.setattr(runtime,'open_context',lambda *args:(SimpleNamespace(),store,db,verify))
    monkeypatch.setattr(google_clients,'read_only_drive_service',Mock())
    assert runtime.execute({},False)['review_eligible']==0
    review.capture_inputs();assert review.apply_confirmations()==0
    assert '実支払額' in review.items[review_id('medical',source)]['error']
