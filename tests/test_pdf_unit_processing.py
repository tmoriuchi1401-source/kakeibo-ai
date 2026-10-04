from copy import deepcopy
from unittest.mock import Mock
import json

import pytest
from app.drive_run_state import StateError
from app.pdf_unit_processing import (DriveUnitProcessingStore, digest, encoded,
    empty_state, medical_page_spec, unit_spec, validate)
from test_conditional_drive_state_v2 import FakeV2, adapter

BINDING = 'b' * 64
STAMP = '2026-10-04T12:00:00+00:00'


def spec(n=4, automatic='sensitive_unknown', human='normal', revision=2):
    return unit_spec('synthetic-source', 'a'*64, 14, [n], [automatic], [human],
        revision=revision, proposal_digest='c'*64, confirmation_digest='d'*64,
        kind_digests=['e'*64])


def context():
    drive = FakeV2(); drive.payload = encoded(empty_state(BINDING))
    preflight = Mock()
    store = DriveUnitProcessingStore(adapter(drive), BINDING, preflight=preflight,
                                   clock=lambda: STAMP)
    return drive, store, preflight


def plan():
    return {'支出明細': [['MAN-synthetic', '2026-09-24', '', '手入力', 500,
        '食費', '外食', '', 'manual', '', 'manual:synthetic', '', 'active']]}


def reserve(store, s=None, route='general_manual', rows=None, fresh=None):
    return store.reserve(s or spec(), route, 'f'*64,
        plan() if rows is None else rows, '1'*64,
        verify_current=fresh or Mock(return_value=True))


def test_pending_precedes_accounting_completion_and_replay_does_not_write_state():
    drive, store, acl = context(); fresh = Mock(return_value=True)
    r, created = reserve(store, fresh=fresh)
    assert created and r['phase'] == 'pending' and len(drive.updates) == 1
    readback = Mock(return_value=False)
    with pytest.raises(StateError, match='accounting_readback'):
        store.complete(r['unit']['unit_id'], r['intent_digest'],
            verify_current=fresh, verify_readback=readback)
    assert len(drive.updates) == 1
    readback.return_value = True
    done = store.complete(r['unit']['unit_id'], r['intent_digest'],
                         verify_current=fresh, verify_readback=readback)
    assert done['phase'] == 'applied' and done['completed_at'] == STAMP
    before = drive.payload
    assert reserve(store)[0] == done
    assert store.complete(r['unit']['unit_id'], r['intent_digest'],
        verify_current=fresh, verify_readback=readback) == done
    assert drive.payload == before and len(drive.updates) == 2
    assert acl.call_count > 4


def test_existing_pending_is_reconciliation_only_no_second_claim_or_append():
    drive, store, _ = context(); first = reserve(store)
    second = reserve(store)
    assert first[1] is True and second == (first[0], False)
    assert len(drive.updates) == 1


@pytest.mark.parametrize('change', ['amount', 'request', 'classification', 'confirmation'])
def test_same_unit_id_different_intent_is_never_overwritten(change):
    drive, store, _ = context(); reserve(store); s = spec(); rows = plan()
    if change == 'amount': rows['支出明細'][0][4] = 501
    if change == 'request': rows['支出明細'][0][10] = 'manual:replacement'
    if change == 'classification': s['automatic_classifications'] = ['normal']
    if change == 'confirmation': s['confirmation_digest'] = '0'*64
    with pytest.raises(StateError, match='intent_changed'): reserve(store, s, rows=rows)
    assert len(drive.updates) == 1


def test_new_grouping_revision_cannot_claim_an_already_claimed_page():
    drive, store, _ = context(); reserve(store)
    with pytest.raises(StateError, match='page_already_claimed'):
        reserve(store, spec(revision=3))
    assert len(drive.updates) == 1


def test_source_or_authority_change_prevents_claim_and_completion():
    drive, store, _ = context()
    with pytest.raises(StateError, match='authority_or_source_changed'):
        reserve(store, fresh=Mock(return_value=False))
    assert drive.updates == []
    r, _ = reserve(store)
    fresh = Mock(side_effect=[True, False])
    with pytest.raises(StateError, match='authority_or_source_changed'):
        store.complete(r['unit']['unit_id'], r['intent_digest'],
            verify_current=fresh, verify_readback=Mock(return_value=True))
    assert store.load()['records'][r['unit']['unit_id']]['phase'] == 'pending'


@pytest.mark.parametrize('automatic,human', [('sensitive_unknown','normal'),
    ('medical','normal'), ('payroll','normal'), ('normal','medical')])
def test_receipt_route_never_uses_human_normal_to_clear_automatic_restrictions(automatic, human):
    drive, store, _ = context()
    receipt_plan = {'レシート': [['']*9], '支出明細': [['']*13], '取込データ': [['']*12]}
    with pytest.raises(StateError, match='state_invalid'):
        reserve(store, spec(automatic=automatic, human=human), 'receipt', receipt_plan)
    assert drive.updates == []


@pytest.mark.parametrize('automatic,human,kinds', [('medical','normal',['e'*64]),
    ('payroll','normal',['e'*64]), ('sensitive_unknown','normal',['']), ('normal','medical',['e'*64])])
def test_general_manual_is_only_explicit_human_normal_not_medical_or_payroll(automatic, human, kinds):
    drive, store, _ = context(); s=spec(automatic=automatic, human=human);s['page_kind_digests']=kinds
    with pytest.raises(StateError, match='state_invalid'): reserve(store,s)
    assert drive.updates == []


def test_medical_completion_is_reference_only_with_no_medical_payload_or_calls():
    drive, store, _ = context(); s=spec(n=1,human='medical')
    with pytest.raises(StateError, match='state_invalid'):
        reserve(store,s,'medical_manual',{'支出明細':plan()['支出明細']})
    r,_=reserve(store,s,'medical_manual',{})
    assert r['planned_rows']=={} and r['writer_reference']=='1'*64
    # No writer, AI, source mover, accounting permission, OCR or Medical fields.
    assert set(json.loads(drive.payload)['audit'][0])=={'operation','timestamp','unit_id',
        'source_file_id','source_content_hash','grouping_revision','proposal_digest',
        'confirmation_digest','intent_digest','result','page_number'}
    assert '医療' not in drive.payload.decode() and 'amount' not in drive.payload.decode()


def test_server_412_never_retries_or_falls_back():
    drive, store, _=context();drive.update_race=True
    with pytest.raises(StateError,match='changed_since_read'):reserve(store)
    assert len(drive.updates)==1 and json.loads(drive.payload)['records']=={}


def test_success_response_without_state_readback_does_not_confirm():
    drive,store,_=context();drive.ignore_write=True
    with pytest.raises(StateError,match='readback_mismatch'):reserve(store)
    assert len(drive.updates)==1 and json.loads(drive.payload)['records']=={}


@pytest.mark.parametrize('payload',[b'',b'{',b'null',b'{"schema":1,"schema":2}'])
def test_missing_corrupt_or_duplicate_json_keys_never_use_local_authority(payload):
    drive,store,_=context();drive.payload=payload
    with pytest.raises(StateError):store.load()
    assert drive.updates==[]


def test_acl_failure_prevents_even_state_read():
    drive,store,acl=context();acl.side_effect=StateError('private_acl_changed')
    with pytest.raises(StateError,match='private_acl_changed'):store.load()
    assert drive.reads==0 and drive.updates==[]


def test_applied_flag_without_completion_audit_is_rejected():
    drive,store,_=context();r,_=reserve(store);value=store.load()
    value['records'][r['unit']['unit_id']].update(phase='applied',completed_at=STAMP)
    with pytest.raises(StateError,match='state_invalid'):validate(value,BINDING)


def test_audit_rejects_private_fields_and_unknown_schema_or_binding():
    drive,store,_=context();reserve(store);value=store.load()
    for field,data in [('binding','c'*64),('schema','other')]:
        changed=deepcopy(value);changed[field]=data
        with pytest.raises(StateError,match='state_invalid'):validate(changed,BINDING)
    value['audit'][0]['ocr']='private'
    with pytest.raises(StateError,match='state_invalid'):validate(value,BINDING)


def test_medical_page_completion_is_independent_of_general_partition_and_contains_no_inputs():
    drive,store,_=context()
    s=medical_page_spec('synthetic-source','a'*64,14,1,'sensitive_unknown','e'*64)
    r,_=reserve(store,s,'medical_manual',{})
    assert r['unit']['grouping_revision']==0 and r['unit']['proposal_digest']==''
    assert r['unit']['unit_id'].startswith('pdf-page-kind-unit-v1:')
    assert r['planned_rows']=={} and r['writer_reference']=='1'*64
    with pytest.raises(StateError,match='state_invalid'):
        reserve(store,s,'general_manual')
    assert len(drive.updates)==1


def test_existing_grouping_unit_ids_remain_byte_identical():
    from hashlib import sha256
    s=spec()
    old=sha256(json.dumps(['pdf-unit-v2',s['source_file_id'],s['source_content_hash'],
        s['page_numbers'],s['member_page_identities'],s['grouping_revision'],s['proposal_digest']],
        ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
    assert s['unit_id']=='pdf-confirmed-unit-v2:'+old


def test_privacy_hold_survives_grouping_edit_and_never_becomes_receipt_authority():
    drive,store,_=context();s=spec(automatic='normal')
    store.block(s,'sensitive_unknown','ocr_incomplete',verify_current=lambda _:True)
    saved=drive.payload
    assert store.restrictions(s['source_file_id'],s['source_content_hash'])=={4:'sensitive_unknown'}
    store.block(s,'sensitive_unknown','ocr_incomplete',verify_current=lambda _:True)
    assert drive.payload==saved and len(drive.updates)==1
    changed=spec(automatic='normal',revision=3)
    rid='R-'+changed['unit_id'];iid='receipt:'+changed['unit_id']
    receipt_plan={'レシート':[[rid,'2026-09-24','',100,'','','解析済','stamp','']],
        '支出明細':[[rid+'-01','2026-09-24','','商品',100,'食費','外食','','receipt',rid,iid,'','active']],
        '取込データ':[[iid,'stamp','receipt',changed['unit_id'],'2026-09-24','',100,'','解析済','','f'*64,'']]}
    with pytest.raises(StateError,match='sticky_privacy_hold'):reserve(store,changed,'receipt',receipt_plan)
    assert len(drive.updates)==1


def test_unknown_followed_by_medical_keeps_both_and_blocks_general_manual():
    drive,store,_=context();s=spec(automatic='normal')
    for kind in ('sensitive_unknown','medical'):
        store.block(s,kind,'synthetic_signal',verify_current=lambda _:True)
    assert store.restrictions(s['source_file_id'],s['source_content_hash'])=={4:'sensitive_unknown'}
    assert len(store.load()['privacy_holds'])==2
    with pytest.raises(StateError,match='human_kind_required'):reserve(store,s)
    assert len(drive.updates)==2


def test_unknown_can_use_explicit_general_manual_but_never_ai_permission():
    drive,store,_=context();s=spec(automatic='normal')
    store.block(s,'sensitive_unknown','render_failed',verify_current=lambda _:True)
    r,created=reserve(store,s)
    assert created and r['route']=='general_manual'
    assert store.load()['privacy_holds'] and r['phase']=='pending'


def test_privacy_hold_cannot_be_recorded_against_stale_source_or_grant_normal():
    drive,store,_=context()
    with pytest.raises(StateError,match='authority_or_source_changed'):
        store.block(spec(),'medical','synthetic_signal',verify_current=lambda _:False)
    with pytest.raises(StateError,match='state_invalid'):
        store.block(spec(),'normal','synthetic_signal',verify_current=lambda _:True)
    assert drive.updates==[]
