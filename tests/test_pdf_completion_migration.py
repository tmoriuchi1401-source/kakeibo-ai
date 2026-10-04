from copy import deepcopy
from unittest.mock import Mock
from hashlib import sha256

import pytest
from app import pdf_grouping_authority_v2 as v2
from app.pdf_completion_migration import adopt_canary_intents
from app.pdf_production_authority import DrivePdfAuthority
from app.pdf_receipt_materialization import materialize
from app.pdf_unit_payload import rendered_unit
from app.pdf_bounded_rendering import WorkBudget
from app.pdf_unit_processing import encoded
from app.receipt_reimport_production import digest as legacy_digest
from app.receipt_pdf_units import _digest
from app.receipt_validation import POLICY_VERSION
from app.drive_run_state import StateError
from test_pdf_production_authority import setup
from test_pdf_unit_processing import context as completion_context
from test_pdf_receipt_materialization import DB,result
from test_conditional_drive_state_v2 import FakeV2,adapter
from test_receipt_pdf_units import local_ocr
from pdf_production_test_support import NOW


def fixture():
    g,live,drive,provider=setup(human={1:'一般',2:'一般'})
    old=g.store.load();confirmation=old['records'][_digest('drive-source-id')]['confirmation']
    migrated=v2.migrate(old,g.store.payload,'drive-source-id',live.content,binding='c'*64,
        legacy_file_id='old-v1-file',expected_confirmation=confirmation['confirmation_digest'],
        expected_partition=[[1],[2]],expected_human_kinds=['normal','normal'],migrated_at=NOW)
    v2drive=FakeV2();v2drive.payload=v2.encoded(migrated)
    provider=DrivePdfAuthority(g.store,lambda _:live.content,migrated=v2.DriveGroupingV2Store(
        adapter(v2drive),'c'*64,g.store,'old-v1-file',preflight=lambda:None))
    s=provider.current('drive-source-id').units[0];db=DB();_,temporary,_=completion_context()
    with rendered_unit(live.content,s,live.observations,WorkBudget()) as (png,h):
        materialize(db,temporary,s,png,h,result(),verify_current=provider.verify,clock=lambda:'2026-10-04 12:00:00')
    plan=[[title,row] for title,rows in db.rows.items() for row in rows]
    record={'unit_id':s['unit_id'],'page_number':1,'phase':'applied','policy_version':POLICY_VERSION,
        'timestamp':'2026-10-04 12:00:00','parsed_digest':legacy_digest(result().model_dump()),
        'payload_sha256':h,'plan':plan,'plan_digest':legacy_digest(plan),
        'proof':{'unit_id':s['unit_id'],'page_identity':s['member_page_identities'][0],'page_number':1,
            'source_content_hash':s['source_content_hash'],'confirmation_digest':s['confirmation_digest'],
            'grouping_revision':s['grouping_revision'],'human_classification':'normal','effective_classification':'normal'}}
    manifest={'source_file_id':s['source_file_id'],'source_content_hash':s['source_content_hash'],
        'confirmation_digest':s['confirmation_digest'],'grouping_revision':s['grouping_revision'],
        'authority_binding':'c'*64,'policy_version':POLICY_VERSION,'scope':'operator_receipt_write_canary',
        'archive_allowed':False,'medical_handoff_allowed':False,'unit_ids':{'1':s['unit_id']},
        'page_identities':{'1':s['member_page_identities'][0]}}
    legacy=FakeV2();legacy.payload=encoded({'schema':'pdf-receipt-write-canary-v1',
        'manifest':manifest,'records':{s['unit_id']:record}})
    newdrive,completion,_=completion_context()
    return legacy,provider,completion,db,s,newdrive,live


def run(args,preflight=None):
    legacy,provider,completion,db,s,*_=args
    return adopt_canary_intents(adapter(legacy),preflight or Mock(),provider,completion,db,s['source_file_id'])


def test_drive_intent_adoption_preserves_rows_unit_ids_and_replay(local_ocr):
    args=fixture();legacy,provider,store,db,s,newdrive,_=args;before=deepcopy(db.rows)
    out=run(args);assert out['adopted'][0]['status']=='applied'
    assert len(db.appends)==3 and legacy.updates==[] and db.rows==before
    saved=newdrive.payload;assert run(args)['adopted'][0]['replayed']
    assert newdrive.payload==saved and len(newdrive.updates)==2 and len(db.appends)==3
    # Different historic JSON fingerprints are preserved, never mistaken for
    # source hashes or forcibly made equal during migration.
    import json
    r=next(iter(json.loads(legacy.payload)['records'].values()))
    assert r['parsed_digest']!=r['plan'][-1][1][10]
    assert store.load()['records'][s['unit_id']]['planned_rows']['取込データ'][0]==r['plan'][-1][1]


@pytest.mark.parametrize('change',['pending','hash','binding','revision','human','unit','row','schema','payload'])
def test_invalid_legacy_proof_cannot_create_even_pending_completion(change,local_ocr):
    import json
    args=fixture();legacy,provider,store,db,s,newdrive,_=args
    value=json.loads(legacy.payload);old=value['records'][s['unit_id']]
    if change=='pending':old['phase']='pending'
    elif change=='hash':value['manifest']['source_content_hash']='f'*64
    elif change=='binding':value['manifest']['authority_binding']='f'*64
    elif change=='revision':old['proof']['grouping_revision']+=1
    elif change=='human':old['proof']['human_classification']='medical'
    elif change=='unit':old['unit_id']='different'
    elif change=='row':old['plan'][0][1][3]=101;old['plan_digest']=legacy_digest(old['plan'])
    elif change=='schema':value['schema']='wrong'
    elif change=='payload':old['payload_sha256']='wrong'
    legacy.payload=encoded(value);before=deepcopy(db.rows)
    with pytest.raises(StateError,match='proof_invalid'):run(args)
    assert not newdrive.updates and not legacy.updates and db.rows==before and len(db.appends)==3


def test_missing_accounting_readback_is_not_adopted(local_ocr):
    args=fixture();args[3].rows['支出明細']=[]
    with pytest.raises(StateError,match='readback_missing'):run(args)
    assert not args[5].updates and not args[0].updates


def test_missing_drive_acl_or_concurrent_old_state_change_stops_adoption(local_ocr):
    args=fixture()
    with pytest.raises(StateError,match='synthetic_acl'):
        run(args,preflight=Mock(side_effect=StateError('synthetic_acl')))
    assert not args[5].updates
    calls=0
    def acl():
        nonlocal calls
        calls+=1
        if calls==2:args[0].revision+=1
    with pytest.raises(StateError,match='legacy_state_changed'):run(args,preflight=acl)
    assert not args[5].updates and len(args[3].appends)==3


def test_local_json_or_unconfigured_v2_cannot_restore_old_accounting_authority(local_ocr):
    args=list(fixture());args[1].migrated=None
    with pytest.raises(StateError,match='drive_proof_required'):run(args)
    assert not args[5].updates
