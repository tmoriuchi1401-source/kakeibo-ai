from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import json

from PIL import Image,PngImagePlugin
import pytest

from app import pdf_grouping_authority_v2 as v2
from app import pdf_receipt_readonly as ro
from app import pdf_unit_readonly_analysis as runner
from app.drive_run_state import StateError
from test_receipt_pdf_units import local_ocr,synthetic_pdf
from test_pdf_unit_readonly_analysis import live_context
from test_private_state_bindings import key

NOW='2026-10-03T11:50:00+00:00'
BINDING=v2._digest(['private-folder','new-v2-file','management-sheet'])


def setup_v2(key,monkeypatch):
    g,t,old,opener=live_context(key,monkeypatch)
    source=opener({})[1];legacy=g.store.load();payload=g.store.payload
    value=v2.migrate(legacy,payload,old['source_file_id'],source(old['source_file_id']),binding=BINDING,
        legacy_file_id='old-v1-file',expected_confirmation=old['confirmation_digest'],
        expected_partition=[[n] for n in range(2,15)],expected_human_kinds=['medical']+['normal']*13,migrated_at=NOW)
    record=value['records'][v2._digest(old['source_file_id'])];p=record['proposal'];a=record['confirmation']
    expected={k:p[k] for k in ('source_file_id','source_content_hash','page_count','proposal_digest')}
    expected.update(binding=BINDING,grouping_revision=2,confirmation_digest=a['confirmation_digest'],
        unit_ids={u.page_numbers[0]:u.unit_id for u in v2.units(value)})
    return value,expected,g,t,source,legacy,payload,old


def test_migration_preserves_human_intent_and_flags_without_render(local_ocr,key,monkeypatch):
    value,expected,g,t,source,legacy,payload,old=setup_v2(key,monkeypatch)
    before=t.writes
    v2.validate_current(value,BINDING,legacy,payload,'old-v1-file')
    a=next(iter(value['records'].values()))['confirmation']
    prior=next(iter(legacy['records'].values()))['confirmation']
    assert a['confirmed_at']==prior['confirmed_at'] and a['grouping_revision']==2
    assert a['confirmed_partition']==[[n] for n in range(2,15)]
    assert value['migration']['legacy_confirmation_digest']==prior['confirmation_digest']
    assert value['migration']['legacy_confirmed_at']==a['confirmed_at']
    assert a['confirmation_digest']!=prior['confirmation_digest']
    assert all(a[k] is False for k in ('gemini_allowed','accounting_allowed','medical_handoff_allowed','archive_allowed'))
    assert [k['human_classification'] for k in value['page_kinds'].values()]==['medical']+['normal']*13
    assert t.payload==payload and t.writes==before
    assert b'PRIVATE_MEDICAL' not in v2.encoded(value) and b'PRIVATE_ATTACHMENT' not in v2.encoded(value)


def test_migration_replay_is_same_when_using_saved_migration_timestamp(local_ocr,key,monkeypatch):
    value,expected,g,t,source,legacy,payload,old=setup_v2(key,monkeypatch)
    again=v2.migrate(legacy,payload,old['source_file_id'],source(old['source_file_id']),binding=BINDING,
        legacy_file_id='old-v1-file',expected_confirmation=old['confirmation_digest'],
        expected_partition=[[n] for n in range(2,15)],expected_human_kinds=['medical']+['normal']*13,migrated_at=value['migration']['migrated_at'])
    assert again==value and v2.units(again)==v2.units(value)


@pytest.mark.parametrize('failure',['source_byte','source_id','confirmation','partition','kind','count'])
def test_migration_rejects_changed_source_structure_or_intent(local_ocr,key,monkeypatch,failure):
    value,expected,g,t,source,legacy,payload,old=setup_v2(key,monkeypatch)
    sid=old['source_file_id'];content=source(sid);confirmation=old['confirmation_digest']
    partition=[[n] for n in range(2,15)];kinds=['medical']+['normal']*13
    if failure=='source_byte':content=content[:-1]+bytes([content[-1]^1])
    if failure=='source_id':sid='other-source'
    if failure=='confirmation':confirmation='0'*64
    if failure=='partition':partition=[[2,3]]+[[n] for n in range(4,15)]
    if failure=='kind':kinds[0]='normal'
    if failure=='count':content=synthetic_pdf(('medical',)+('normal',)*14)
    before=t.payload;writes=t.writes
    with pytest.raises(StateError):v2.migrate(legacy,payload,sid,content,binding=BINDING,
        legacy_file_id='old-v1-file',expected_confirmation=confirmation,expected_partition=partition,
        expected_human_kinds=kinds,migrated_at=NOW)
    assert t.payload==before and t.writes==writes


@pytest.mark.parametrize('failure',['flag','page_identity','member_identity','revision','human','partition','binding','legacy_digest'])
def test_v2_corruption_fail_closed(local_ocr,key,monkeypatch,failure):
    value,expected,*_=setup_v2(key,monkeypatch);bad=deepcopy(value)
    record=next(iter(bad['records'].values()));p=record['proposal'];a=record['confirmation']
    if failure=='flag':a['accounting_allowed']=True
    if failure=='page_identity':p['pages'][1]['page_identity']='0'*64
    if failure=='member_identity':p['groups'][0]['member_page_identities'][0]='0'*64
    if failure=='revision':record['revision']=3
    if failure=='human':p['pages'][0]['human_classification']='normal'
    if failure=='partition':a['confirmed_partition']=[[1]]
    if failure=='binding':bad['binding']='0'*64
    if failure=='legacy_digest':bad['migration']['legacy_confirmation_digest']='0'*64
    with pytest.raises(StateError):v2.validate(bad,BINDING)


def test_png_encode_and_chunks_do_not_enter_page_or_unit_identity(local_ocr,key,monkeypatch):
    value,expected,g,t,source,legacy,payload,old=setup_v2(key,monkeypatch)
    fingerprints=[]
    with Image.new('RGB',(120,80),'white') as image:
        for compression,ancillary in [(0,False),(9,False),(9,True)]:
            output=BytesIO();chunks=PngImagePlugin.PngInfo()
            if ancillary:chunks.add_text('diagnostic','synthetic')
            image.save(output,format='PNG',compress_level=compression,pnginfo=chunks)
            fingerprints.append(sha256(output.getvalue()).hexdigest())
    assert len(set(fingerprints))==3
    identity=v2.page_identity(old['source_content_hash'],2,14)
    assert identity==next(iter(value['records'].values()))['proposal']['pages'][1]['page_identity']
    assert len({v2.page_identity(old['source_content_hash'],2,14) for _ in fingerprints})==1
    before=v2.units(value)
    v2.validate_current(value,BINDING,legacy,payload,'old-v1-file')
    assert v2.units(value)==before
    assert identity!=v2.page_identity(sha256(source(old['source_file_id'])+b'changed').hexdigest(),2,14)
    assert identity!=v2.page_identity(old['source_content_hash'],2,15)


@pytest.mark.parametrize('mode',['different_compression','metadata','rgba','different_source','page_count','medical_page'])
def test_v2_render_difference_permitted_only_with_fresh_exact_gate(local_ocr,key,monkeypatch,mode):
    value,expected,g,t,source,*_=setup_v2(key,monkeypatch)
    before_writes=t.writes
    from app.models import ReceiptResult
    result=ReceiptResult(date='2026-09-01',merchant='Synthetic shop',total=100,
        items=[dict(name='商品',amount=100,major_category='食費',minor_category='食品')])
    analyze=Mock(return_value=(result,[result]))
    original=ro._render_png
    def render(*args,**kwargs):
        png=original(*args,**kwargs)
        with Image.open(BytesIO(png)) as image:
            output=BytesIO();chunks=PngImagePlugin.PngInfo()
            if mode=='metadata':chunks.add_text('synthetic','untrusted')
            if mode=='rgba':image=image.convert('RGBA')
            image.save(output,format='PNG',compress_level=0,pnginfo=chunks)
            return output.getvalue()
    monkeypatch.setattr(ro,'_render_png',render)
    if mode=='different_source':source=lambda _:synthetic_pdf(('normal',)*14)
    if mode=='page_count':source=lambda _:synthetic_pdf(('medical',)+('normal',)*14)
    svc=ro.ReadonlyPdfReceipts(lambda:value,source,analyze,[('食費','食品')],expected,model='synthetic')
    row=svc.run(1 if mode=='medical_page' else 2)
    if mode=='different_compression':
        assert row['status'] in {'would_import','would_need_review'} and analyze.call_count==1
        assert row['payload_sha256']!=row['observation_render_hash']
        assert analyze.call_args.kwargs['expected_payload_sha256']==row['payload_sha256']
        assert svc.budget.peak_live_pages==1
    else:analyze.assert_not_called();assert row['status'] in {'authority_held','privacy_blocked'}
    assert t.writes==before_writes


def test_current_legacy_change_invalidates_v2(local_ocr,key,monkeypatch):
    value,expected,g,t,source,legacy,payload,old=setup_v2(key,monkeypatch)
    with pytest.raises(StateError,match='legacy_intent_stale'):
        v2.validate_current(value,BINDING,legacy,payload+b' ','old-v1-file')
    with pytest.raises(StateError,match='legacy_intent_stale'):
        v2.validate_current(value,BINDING,legacy,payload,'other-v1-file')


def test_v2_store_has_no_mutation_api_and_checks_both_files(local_ocr,key,monkeypatch):
    value,expected,g,t,source,legacy,payload,old=setup_v2(key,monkeypatch)
    transport=SimpleNamespace(read_versioned=Mock(return_value=(v2.encoded(value),'"synthetic"')))
    check=Mock();store=v2.DriveGroupingV2Store(transport,BINDING,g.store,'old-v1-file',preflight=check)
    assert store.load()==value and check.call_count==1 and not hasattr(store,'save')
    transport.read_versioned.return_value=(b'{broken','"synthetic"')
    with pytest.raises(StateError):store.load()


def test_discovery_rejects_missing_or_duplicate_state():
    for files in [[],[{'id':'a'},{'id':'b'}]]:
        service=Mock();service.files().list().execute.return_value={'files':files}
        with pytest.raises(StateError):v2.discover(service,'private-folder','a'*64)


@pytest.mark.parametrize('stage',['after_gate_before_adapter','inside_sdk_input'])
def test_exact_payload_mutation_never_reaches_sdk(monkeypatch,stage):
    from app import gemini_ai
    sdk=Mock()
    client=SimpleNamespace(interactions=SimpleNamespace(create=sdk),
        _api_client=SimpleNamespace(_http_options=SimpleNamespace(base_url='https://generativelanguage.googleapis.com/')))
    monkeypatch.setattr(gemini_ai.genai,'Client',Mock(return_value=client))
    monkeypatch.setattr(gemini_ai,'require_receipt_ai_permission',Mock())
    final_gate=Mock();monkeypatch.setattr(runner,'require_receipt_ai_permission',final_gate)
    output=BytesIO()
    with Image.new('RGB',(10,10),'white') as image:image.save(output,format='PNG')
    png=output.getvalue();changed=png[:-1]+bytes([png[-1]^1])
    expected_hash=sha256(png).hexdigest()
    if stage=='inside_sdk_input':
        import base64
        def tamper(self,content,mime,categories,**kwargs):
            return self.client.interactions.create(input=[{'type':'text','text':'synthetic'},
                {'type':'image','mime_type':'image/png','data':base64.b64encode(changed).decode()}])
        monkeypatch.setattr(gemini_ai.GeminiAI,'analyze_receipt',tamper)
    analyze,_=runner.receipt_analyzer('synthetic-key','synthetic')
    with pytest.raises(StateError):
        analyze(changed if stage=='after_gate_before_adapter' else png,[('食費','食品')],expected_payload_sha256=expected_hash)
    sdk.assert_not_called();final_gate.assert_not_called()
