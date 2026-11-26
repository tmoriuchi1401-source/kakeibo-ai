from copy import deepcopy
from hashlib import sha256
import pytest

from app.drive_run_state import StateError
from app.receipt_hosted import ReceiptOwner
from test_receipt_hosted import setup,RAW,SID


@pytest.mark.parametrize('source_fences,hash_fences', [([SID],[]),([], [sha256(RAW).hexdigest()])])
def test_carried_legacy_fence_blocks_before_ai_review_or_writer(monkeypatch,source_fences,hash_fences):
    runtime,transport,ai,review,writer,gate,args=setup(monkeypatch)
    state=deepcopy(runtime.owner.store.value)
    state['receipt_hosted_v1'].update(legacy_source_fences=source_fences,legacy_hash_fences=hash_fences)
    runtime.owner.store.save(state)
    before=deepcopy(runtime.owner.store.value)
    with pytest.raises(StateError,match='legacy_attempt_fenced'):runtime.process_bytes(RAW,'image/png',SID)
    assert ai.analyze_receipt.call_count==0 and review.intakes==0
    assert runtime.owner.store.value==before


@pytest.mark.parametrize('fields', [dict(legacy_source_fences=['x']),
    dict(legacy_source_fences=[],legacy_hash_fences=['bad']),
    dict(legacy_source_fences=['x','x'],legacy_hash_fences=[]),
    dict(legacy_source_fences='x',legacy_hash_fences=[])])
def test_incomplete_or_malformed_carried_fences_fail_closed(monkeypatch,fields):
    runtime,*_=setup(monkeypatch)
    runtime.owner.store.value['receipt_hosted_v1'].update(fields)
    with pytest.raises(StateError):ReceiptOwner(runtime.owner.store)


def test_carried_fences_do_not_weaken_existing_global_unknown(monkeypatch):
    runtime,*_=setup(monkeypatch)
    state=runtime.owner.store.value['receipt_hosted_v1']
    state.update(legacy_source_fences=[],legacy_hash_fences=[])
    state['stopped']=True
    with pytest.raises(StateError,match='unknown_no_retry'):ReceiptOwner(runtime.owner.store)


@pytest.mark.parametrize('remove_key',[True,False])
def test_runtime_save_cannot_drop_carried_permanent_fences(monkeypatch,remove_key):
    runtime,*_=setup(monkeypatch)
    state=runtime.owner.read()
    state.update(legacy_source_fences=['old-source'],legacy_hash_fences=[])
    runtime.owner.save(state)
    changed=runtime.owner.read()
    if remove_key:changed.pop('legacy_source_fences')
    else:changed['legacy_source_fences']=[]
    with pytest.raises(StateError,match='removal_forbidden'):runtime.owner.save(changed)


def test_production_factory_requires_carried_inventory_before_review_connection(monkeypatch):
    from app import receipt_hosted as hosted, receipt_hosted_review as review
    from types import SimpleNamespace
    monkeypatch.setattr('app.production_flow.verify_execution_boundary', lambda *_: None)
    store=SimpleNamespace(value={hosted.KEY:{}})
    monkeypatch.setattr('app.receipt_confirmation_production.open_context', lambda *_a,**_k:(None,store,None,None))
    def forbidden(*_a,**_k):
        pytest.fail('review connection must not open before fence inventory exists')
    monkeypatch.setattr(review,'open_review',forbidden)
    with pytest.raises(StateError,match='legacy_fence_inventory_required'):
        hosted.open_pipeline(None,None,[],{'RUNNER_ENVIRONMENT':'github-hosted','RUNNER_OS':'Linux'})
