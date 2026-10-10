from copy import deepcopy
from types import SimpleNamespace
import pytest

from app.drive_run_state import StateError
from app.pdf_intake_runner import observe_source
from app.pdf_intake_registry import RegistryStore
from test_pdf_intake_registry import Memory
from test_pdf_intake_authority import RAW, page


def registered():
    store=RegistryStore(Memory(),'anchor')
    keys=[store.register(page(n,classification='normal')) for n in range(1,4)]
    return store,keys


def test_unchanged_complete_registration_replay_does_not_ocr_or_write(monkeypatch):
    import app.pdf_intake_production as production
    monkeypatch.setattr(production,'page_count',lambda _:3)
    store,keys=registered();before=store.io.raw,store.io.tag,store.io.writes
    assert observe_source(RAW,'new-pdf',store,reuse_complete=True,
                          observer=lambda *_:pytest.fail('no redundant OCR'))==keys
    assert (store.io.raw,store.io.tag,store.io.writes)==before
    with pytest.raises(StateError,match='source_changed'):
        observe_source(b'changed','new-pdf',store,reuse_complete=True)


def test_incomplete_registration_cannot_skip_privacy_observation(monkeypatch):
    import app.pdf_intake_production as production
    monkeypatch.setattr(production,'page_count',lambda _:3)
    store,_=registered();value=store.load();value['pages'].pop(next(iter(value['pages'])))
    value['generation']+=1;store.save(value)
    with pytest.raises(RuntimeError,match='observer reached'):
        observe_source(RAW,'new-pdf',store,reuse_complete=True,
                       observer=lambda *_:(_ for _ in ()).throw(RuntimeError('observer reached')))


def test_bounded_analysis_rotates_without_privacy_upgrade_or_write(monkeypatch):
    import app.pdf_intake_production as production
    store,keys=registered();before=deepcopy(store.load());calls=[]
    runner=SimpleNamespace(written=0,unit_limit=3,analyze=lambda key:calls.append(key))
    context=SimpleNamespace(store=store,source=lambda _:RAW,runner=lambda _:runner)
    monkeypatch.setattr(production,'Projection',lambda _:SimpleNamespace(pending=lambda _:None))
    monkeypatch.setattr(production,'page_count',lambda _:3)
    monkeypatch.setattr(production,'monotonic',lambda:0)
    plans=[{'source_id':'new-pdf','sha256':page()['source']['source_content_hash'],'page_count':3}]
    monkeypatch.setattr(production,'time',lambda:0)
    first=production.process(context,None,plans,apply=False)
    monkeypatch.setattr(production,'time',lambda:10800)
    second=production.process(context,None,plans,apply=False)
    assert len(calls)==2 and calls[0]!=calls[1]
    assert first['deferred']==second['deferred']==2
    assert store.load()==before


def test_unknown_without_permission_never_consumes_normal_analysis_slot(monkeypatch):
    import app.pdf_intake_production as production
    store=RegistryStore(Memory(),'anchor');unknown=store.register(page(1));normal=store.register(page(2,classification='normal'))
    store.register(page(3,classification='medical'));calls=[]
    runner=SimpleNamespace(written=0,unit_limit=3,analyze=lambda key:calls.append(key))
    context=SimpleNamespace(store=store,source=lambda _:RAW,runner=lambda _:runner)
    monkeypatch.setattr(production,'Projection',lambda _:SimpleNamespace(pending=lambda _:None))
    monkeypatch.setattr(production,'page_count',lambda _:3)
    monkeypatch.setattr(production,'monotonic',lambda:0)
    production.process(context,None,[{'source_id':'new-pdf','sha256':page()['source']['source_content_hash'],'page_count':3}],apply=False)
    assert normal in calls and unknown in calls
    assert store.load()['pages'][unknown]['authority']=={}
