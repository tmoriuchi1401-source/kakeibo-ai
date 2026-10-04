from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from hashlib import sha256

import pytest
from app.pdf_production_authority import DrivePdfAuthority
from app.pdf_page_kind import PageKindConfirmation
from app import pdf_grouping_authority_v2 as v2
from app.drive_run_state import StateError
from app.receipt_pdf_units import _digest
from pdf_production_test_support import context, request, BINDING, NOW
from test_receipt_pdf_units import local_ocr, synthetic_pdf
from test_conditional_drive_state_v2 import FakeV2, adapter


def setup(kinds=('normal','normal'),human=None):
    g,live,drive=context(kinds);view=g.display('drive-source-id')
    if human:PageKindConfirmation(g).confirm(view['proposal'],human)
    g.review(request(view))
    provider=DrivePdfAuthority(g.store,lambda sid:live.content)
    return g,live,drive,provider


def test_real_drive_authority_normal_does_not_require_extra_kind_confirmation(local_ocr):
    g,live,drive,provider=setup();before=drive.payload;writes=len(drive.updates)
    current=provider.current('drive-source-id')
    assert current.status=='grouping_confirmed' and len(current.units)==2
    assert all(s['human_classifications']==['normal'] and s['page_kind_digests']==[''] for s in current.units)
    assert all(provider.verify(s) for s in current.units)
    assert current.source_content_hash==sha256(live.content).hexdigest()
    assert drive.payload==before and len(drive.updates)==writes


def test_human_normal_never_erases_automatic_unknown_or_medical(local_ocr):
    g,live,drive,provider=setup(('unknown','normal'),{1:'一般',2:'一般'})
    current=provider.current('drive-source-id')
    assert current.units[0]['automatic_classifications']==['sensitive_unknown']
    assert current.units[0]['human_classifications']==['normal']
    assert current.units[0]['page_kind_digests']!=['']
    assert current.units[1]['automatic_classifications']==['normal']


def test_missing_or_unconfirmed_proposal_has_no_normal_units(local_ocr):
    g,live,drive=context();provider=DrivePdfAuthority(g.store,lambda sid:live.content)
    assert provider.current('drive-source-id').units==()
    g.display('drive-source-id')
    assert provider.current('drive-source-id').units==()


def test_local_cache_or_ui_cannot_be_durable_authority():
    with pytest.raises(StateError,match='durable_authority_required'):
        DrivePdfAuthority(SimpleNamespace(load=lambda:{}),lambda _:b'')


def test_a_local_transport_cannot_impersonate_a_drive_store(local_ocr):
    g,live,drive,provider=setup()
    g.store.transport=SimpleNamespace(read_versioned=lambda:(drive.payload,drive.tag))
    with pytest.raises(StateError,match='durable_authority_required'):
        DrivePdfAuthority(g.store,lambda _:live.content)


def test_duplicate_json_keys_in_drive_authority_stop_before_source_read(local_ocr):
    g,live,drive,provider=setup();source=Mock(return_value=live.content);provider.load_source=source
    drive.payload=b'{"schema":"wrong",'+drive.payload[1:]
    with pytest.raises(StateError,match='state_unavailable'):provider.current('drive-source-id')
    source.assert_not_called()


@pytest.mark.parametrize('change',['bytes','count','file_id'])
def test_source_changes_cannot_verify_old_units(local_ocr,change):
    g,live,drive,provider=setup();s=provider.current('drive-source-id').units[0]
    if change=='bytes':live.content+=b'changed'
    elif change=='count':live.content=synthetic_pdf(('normal',)*3)
    else:s={**s,'source_file_id':'different-file-id'}
    if change=='file_id':assert provider.verify(s) is False
    else:
        with pytest.raises(StateError,match='source_changed'):provider.verify(s)


def test_hold_and_revision_change_invalidate_old_units_without_any_writer(local_ocr):
    g,live,drive,provider=setup();before=provider.current('drive-source-id').units[0]
    view=g.view(g.store.load()['records'][_digest('drive-source-id')])
    g.review(request(view,operation='edit',partition=[[1,2]],number=2))
    assert provider.verify(before) is False
    view=g.view(g.store.load()['records'][_digest('drive-source-id')]);g.review(request(view,number=3))
    assert provider.verify(before) is False
    combined=provider.current('drive-source-id').units[0]
    assert combined['page_numbers']==[1,2] and combined['unit_id']!=before['unit_id']
    g.review(request(view,operation='hold',number=4))
    assert provider.verify(combined) is False


def test_medical_tracking_survives_general_grouping_hold_without_becoming_general(local_ocr):
    g,live,drive,provider=setup(('unknown','normal'),{1:'医療',2:'一般'})
    current=provider.current('drive-source-id');med=current.medical_pages[0]
    assert med['grouping_revision']==0 and med['human_classifications']==['medical']
    view=g.view(g.store.load()['records'][_digest('drive-source-id')])
    g.review(request(view,operation='hold',number=2))
    held=provider.current('drive-source-id')
    assert held.units==() and held.medical_pages==(med,) and provider.verify(med)


def test_current_migrated_unit_ids_and_digests_are_preserved(local_ocr):
    g,live,drive,provider=setup(('medical','normal'),{1:'医療',2:'一般'})
    old=g.store.load();payload=g.store.payload;a=old['records'][_digest('drive-source-id')]['confirmation']
    value=v2.migrate(old,payload,'drive-source-id',live.content,binding='c'*64,
        legacy_file_id='old-v1-file',expected_confirmation=a['confirmation_digest'],
        expected_partition=[[1],[2]],expected_human_kinds=['medical','normal'],migrated_at=NOW)
    migrated_drive=FakeV2();migrated_drive.payload=v2.encoded(value)
    migrated=v2.DriveGroupingV2Store(adapter(migrated_drive),'c'*64,g.store,'old-v1-file',preflight=Mock())
    provider=DrivePdfAuthority(g.store,lambda _:live.content,migrated=migrated)
    current=provider.current('drive-source-id')
    assert [s['unit_id'] for s in current.units]==[u.unit_id for u in v2.units(value)]
    assert current.units[1]['confirmation_digest']==next(iter(value['records'].values()))['confirmation']['confirmation_digest']
    assert len(migrated_drive.updates)==0
    migrated_drive.payload=b'{broken'
    with pytest.raises(StateError):provider.current('drive-source-id')


def test_authority_change_during_source_download_is_refused(local_ocr):
    g,live,drive,provider=setup()
    def source(sid):
        view=g.view(g.store.load()['records'][_digest(sid)])
        g.review(request(view,operation='hold',number=2))
        return live.content
    provider.load_source=source
    with pytest.raises(StateError,match='authority_changed'):provider.current('drive-source-id')


def test_an_unrelated_pdf_does_not_inherit_or_depend_on_old_migrated_intent(local_ocr):
    g,live,drive,provider=setup(human={1:'一般',2:'一般'})
    legacy=g.store.load();a=legacy['records'][_digest('drive-source-id')]['confirmation']
    value=v2.migrate(legacy,g.store.payload,'drive-source-id',live.content,binding='c'*64,
        legacy_file_id='old-v1-file',expected_confirmation=a['confirmation_digest'],
        expected_partition=[[1],[2]],expected_human_kinds=['normal','normal'],migrated_at=NOW)
    vd=FakeV2();vd.payload=v2.encoded(value)
    migrated=v2.DriveGroupingV2Store(adapter(vd),'c'*64,g.store,'old-v1-file',preflight=lambda:None)
    from app.receipt_pdf_units import observe_pdf
    other=synthetic_pdf(('normal','normal'),metadata='synthetic-other-document')
    observations=observe_pdf(other,'other-source-id')
    g.load_observations=lambda sid,old:observations if sid=='other-source-id' else live.observations
    view=g.display('other-source-id')
    g.review(request(view,number=8))
    original=g.view(g.store.load()['records'][_digest('drive-source-id')])
    g.review(request(original,operation='hold',number=9))
    provider=DrivePdfAuthority(g.store,lambda sid:other if sid=='other-source-id' else live.content,migrated=migrated)
    assert len(provider.current('other-source-id').units)==2
    assert provider.current('drive-source-id').units==()
    with pytest.raises(StateError,match='legacy_unconfirmed'):migrated.load()
    assert vd.updates==[]
