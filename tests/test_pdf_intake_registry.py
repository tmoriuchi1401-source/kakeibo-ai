from copy import deepcopy
from types import SimpleNamespace
import json

import pytest

from app.drive_run_state import StateError
from app.pdf_intake_authority import canonical, page_key
from app.pdf_intake_registry import RegistryStore, empty_registry, validate, DriveRegistryIO
from test_pdf_intake_authority import page


class Memory:
    def __init__(self):
        self.raw=canonical(empty_registry('anchor')); self.tag='"0"'; self.writes=0
    def read_versioned(self):return self.raw,self.tag
    def replace_versioned(self,before,tag,after):
        if before!=self.raw or tag!=self.tag:raise StateError('HTTP_412')
        self.raw=after; self.writes+=1; self.tag='"'+str(self.writes)+'"'


def test_register_and_replay_exact_readback_no_extra_write():
    io=Memory(); store=RegistryStore(io,'anchor'); key=store.register(page())
    before=io.raw,io.tag,io.writes
    assert store.register(page())==key
    assert (io.raw,io.tag,io.writes)==before
    assert store.load()['pages'][key]['authority']=={}


def test_another_page_failure_does_not_remove_independent_page():
    io=Memory(); store=RegistryStore(io,'anchor')
    first=store.register(page(1)); store.register(page(2))
    prior=store.load()['pages'][first]
    changed=page(2); changed['processing_status']='tampered'
    with pytest.raises(StateError):store.register(changed)
    assert store.load()['pages'][first]==prior


def test_source_hash_changed_is_source_wide_gate():
    io=Memory(); store=RegistryStore(io,'anchor'); store.register(page())
    value=page(1); value['source']['source_content_hash']='e'*64
    from app.receipt_plan.identity import page_identity
    from app.pdf_intake_authority import review_identity
    value['stable_page_identity']=page_identity('e'*64,1,3)
    value['review_identity']=review_identity(value)
    with pytest.raises(StateError,match='source_changed'):store.register(value)
    assert io.writes==1


def test_stale_etag_rejects_without_unconditional_fallback():
    io=Memory(); store=RegistryStore(io,'anchor'); current=store.load()
    current['generation']+=1
    key=page_key(page()); current['pages'][key]={'page':page(),'status':'observed','authority':{},'units':{},'analysis':{}}
    io.tag='"concurrent"'
    with pytest.raises(StateError,match='HTTP_412'):store.save(current)
    assert io.writes==0


def test_unknown_save_outcome_is_not_retried():
    io=Memory(); store=RegistryStore(io,'anchor')
    original=io.replace_versioned
    def ambiguity(*args):
        original(*args)
        raise StateError('transport_response_lost')
    io.replace_versioned=ambiguity
    with pytest.raises(StateError,match='transport_response_lost'):store.register(page())
    assert io.writes==1
    # Reconciliation is read-only; callers cannot automatically retry a PUT.
    assert page_key(page()) in store.load()['pages']


@pytest.mark.parametrize('field',['id_token','cookie','nonce','private_key','raw_response','image_bytes','pdf_bytes'])
def test_registry_refuses_secrets_and_raw_media(field):
    io=Memory(); store=RegistryStore(io,'anchor'); store.register(page())
    current=store.load(); current['pages'][page_key(page())]['analysis'][field]='unsafe'
    current['generation']+=1
    with pytest.raises(StateError,match='registry_invalid'):store.save(current)
    assert io.writes==1


def test_readonly_credential_transport_has_no_write_path():
    io=object.__new__(DriveRegistryIO); io.writable=False
    with pytest.raises(StateError,match='conditional_write_forbidden'):
        io.replace_versioned(b'before','"tag"',b'after')


def test_acl_broad_permission_is_rejected():
    from app.pdf_intake_authority import digest
    io=object.__new__(DriveRegistryIO)
    io.sa='synthetic-sa@example.invalid'; io.config={'folder_id':'folder','file_id':'file','owner_digest':digest('owner@example.invalid')}
    def metadata(fid,*_):return {'id':fid,'owners':[{'emailAddress':'owner@example.invalid'}],
                                 'permissions':[{'type':'user','role':'owner','emailAddress':'owner@example.invalid'},
                                                {'type':'user','role':'writer','emailAddress':io.sa},
                                                {'type':'anyone','role':'reader'}]}
    io.metadata=metadata
    with pytest.raises(StateError,match='private_acl_changed'):io.acl()


def test_duplicate_json_keys_rejected():
    io=Memory(); store=RegistryStore(io,'anchor')
    io.raw=b'{"schema":"pdf-intake-current-v1","anchor":"anchor","generation":0,"generation":1,"pages":{}}'
    with pytest.raises(StateError,match='registry_invalid'):store.load()
