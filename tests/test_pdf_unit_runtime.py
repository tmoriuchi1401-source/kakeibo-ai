from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import pdf_unit_runtime as runtime,pdf_unit_intake as intake,pdf_grouping_authority_v2 as v2
from app.pdf_unit_processing import empty_state,encoded
from app.receipt_pdf_units import _digest
from app.drive_run_state import StateError
from test_pdf_unit_intake import setup
from test_receipt_pdf_units import local_ocr,synthetic_pdf
from test_conditional_drive_state_v2 import DriveRequest
from test_pdf_receipt_materialization import DB
from pdf_production_test_support import NOW


def test_preview_observes_mixed_pages_without_any_mutation_or_ai(local_ocr):
    service,live,g,db,sdk,state=setup(('normal','medical'),{2:'医療'})
    before=(state.payload,deepcopy(db.rows),g.store.load())
    view=service.preview(live.content,'drive-source-id')
    assert [u['status'] for u in view['units']]==['new_eligible','medical_pending']
    assert runtime.counts(view)==dict(written=0,unchanged=0,needs_review=1,failure=0,new_eligible=1)
    assert (state.payload,db.rows,g.store.load())==before and sdk.call_count==0 and not db.appends
    assert 'image' not in json.dumps(view['pdf_preflight']) and 'OCR' not in json.dumps(view['pdf_preflight'])
    service.process(live.content,'drive-source-id',preflight=view['pdf_preflight'])
    before=(state.payload,sdk.call_count,len(db.appends))
    assert service.preview(live.content,'drive-source-id')['units'][0]['status']=='unchanged'
    assert before==(state.payload,sdk.call_count,len(db.appends))


@pytest.mark.parametrize('change',['hash','page_count','identity','authority','structure'])
def test_runner_snapshot_mismatch_stops_before_sdk_or_writer(change,local_ocr):
    service,live,g,db,sdk,state=setup();p=service.preview(live.content,'drive-source-id')['pdf_preflight']
    if change=='hash':p['source_content_hash']='f'*64
    elif change=='page_count':p['page_count']+=1
    elif change=='identity':p['pages'][0]['page_identity']='f'*64
    elif change=='authority':p['authority_digest']='f'*64
    else:p['pages'].reverse()
    before=state.payload
    with pytest.raises(StateError,match='source_or_authority_changed'):
        service.process(live.content,'drive-source-id',preflight=p)
    assert sdk.call_count==0 and not db.appends and state.payload==before


def test_sensitive_scan_snapshot_cannot_be_downgraded_by_later_normal_observation(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup(('normal','medical'),{2:'医療'});original=intake.observe_pdf
    def sensitive(*a,**kw):
        observed=original(*a,**kw)
        return replace(observed,pages=tuple(replace(p,classification='medical') if p.page_number==1 else p
            for p in observed.pages))
    monkeypatch.setattr(intake,'observe_pdf',sensitive)
    view=service.preview(live.content,'drive-source-id')
    monkeypatch.setattr(intake,'observe_pdf',original)
    result=service.process(live.content,'drive-source-id',preflight=view['pdf_preflight'])
    assert result['units'][0]['status']=='privacy_pending' and not db.appends and sdk.call_count==0
    assert service.completion.restrictions('drive-source-id',view['pdf_preflight']['source_content_hash'])=={1:'medical'}


def test_disabled_runtime_does_not_touch_keys_google_or_local_authority(monkeypatch):
    monkeypatch.setattr(runtime,'require_context',Mock(side_effect=AssertionError('No context or key reads')))
    assert runtime.open_intake(None,None,env={}) is None
    assert runtime.open_intake(None,None,env={'PDF_UNIT_PROCESSING_ENABLED':'yes'}) is None
    runtime.require_context.assert_not_called()


@pytest.mark.parametrize('patch',[
    {'GITHUB_SHA':'b'*40},{'GITHUB_REF':'refs/heads/codex/pdf-page-privacy'},
    {'RUNNER_ENVIRONMENT':'self-hosted'},{'GITHUB_EVENT_NAME':'pull_request'},
    {'GITHUB_WORKFLOW_REF':'other-workflow'},{'RUNNER_DEBUG':'1'},
])
def test_existing_validated_main_and_hosted_context_are_required_before_keys(patch,monkeypatch):
    from app import production_flow
    monkeypatch.setattr(runtime.subprocess,'check_output',lambda *a,**kw:'a'*40)
    env=dict(PDF_UNIT_PROCESSING_ENABLED='true',GITHUB_ACTIONS='true',GITHUB_SHA='a'*40,
        KAKEIBO_VALIDATED_MAIN_SHA='a'*40,KAKEIBO_PRODUCTION_ENABLED='true',GITHUB_REF='refs/heads/main',
        GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',RUNNER_ENVIRONMENT='github-hosted',RUNNER_OS='Linux',
        GITHUB_EVENT_NAME='schedule',GITHUB_RUN_ID='123',
        GITHUB_WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main')
    env.update(patch)
    with pytest.raises(StateError):runtime.open_intake(None,None,env=env)


@pytest.mark.parametrize('kind',['missing','duplicate','pagination','mime','parent'])
def test_completion_discovery_has_no_create_or_local_fallback(kind):
    api=Mock();name=runtime.processing_name('a'*64)
    f={'id':'synthetic-completion-id','name':name,'mimeType':'application/json','parents':['synthetic-folder']}
    result={'files':[f]}
    if kind=='missing':result['files']=[]
    elif kind=='duplicate':result['files']*=2
    elif kind=='pagination':result['nextPageToken']='more'
    elif kind=='mime':f['mimeType']='application/pdf'
    else:f['parents']=['another-folder']
    api.files().list().execute.return_value=result
    with pytest.raises(StateError,match='missing_or_ambiguous'):
        runtime.discover_processing(api,'synthetic-folder','a'*64)
    api.files().create.assert_not_called();api.files().update.assert_not_called()


@pytest.mark.parametrize('tag',[None,'*','W/"weak"','bare'])
def test_runtime_http_cannot_fall_back_to_unconditional_or_weak_writes(tag):
    api=SimpleNamespace(_http=SimpleNamespace(request=Mock()))
    runtime.http_fence(api,completion_id='synthetic-completion-id')
    with pytest.raises(StateError,match='cloud_write_forbidden'):
        api._http.request('https://www.googleapis.com/upload/drive/v2/files/synthetic-completion-id',
            method='PUT',headers={'If-Match':tag})


def test_runtime_http_only_allows_conditional_completion_upload_not_authority_or_move():
    original=Mock();api=SimpleNamespace(_http=SimpleNamespace(request=original))
    runtime.http_fence(api,completion_id='synthetic-completion-id')
    root='https://www.googleapis.com/upload/drive/v2/files/'
    api._http.request(root+'synthetic-completion-id',method='PUT',headers={'If-Match':'"strong"'})
    assert original.call_count==1
    for url,method in [(root+'legacy-authority-id','PUT'),(root+'source-pdf-id','PATCH'),
            ('https://www.googleapis.com/drive/v3/files/source-pdf-id?addParents=processed','PATCH')]:
        with pytest.raises(StateError):api._http.request(url,method=method,headers={'If-Match':'"strong"'})
    assert original.call_count==1


def factory_fixture(monkeypatch):
    service,live,g,db,sdk,state=setup(('normal','medical'),{1:'一般',2:'医療'})
    folder='synthetic-private-folder';old_id='synthetic-legacy-id';new_id='synthetic-v2-id';done_id='synthetic-completion-id'
    sid='synthetic-sheet';lb=_digest([folder,old_id,sid]);legacy=g.store.load();legacy['binding']=lb
    from app.pdf_grouping_authority import encoded as authority_bytes
    old_bytes=authority_bytes(legacy);answer=next(iter(legacy['records'].values()))['confirmation']
    vb=_digest([folder,new_id,sid])
    migrated=v2.migrate(legacy,old_bytes,'drive-source-id',live.content,binding=vb,legacy_file_id=old_id,
        expected_confirmation=answer['confirmation_digest'],expected_partition=[[1],[2]],
        expected_human_kinds=['normal','medical'],migrated_at=NOW)
    payloads={old_id:old_bytes,new_id:v2.encoded(migrated),done_id:encoded(empty_state(_digest([folder,done_id,sid])))}
    class Versioned:
        _rootDesc={'version':'v2'}
        def __init__(self):self._http=SimpleNamespace(request=Mock());self.writes=[]
        def files(self):return self
        def get(self,**kw):
            fid=kw['fileId']
            return DriveRequest(lambda headers:({'id':fid,'etag':'"strong-'+fid+'"',
                'parents':[{'id':folder}],'mimeType':'application/json','editable':True},{'etag':'"strong-'+fid+'"'}))
        def get_media(self,**kw):return DriveRequest(lambda headers:(payloads[kw['fileId']],{}))
        def update(self,**kw):raise AssertionError('Factory/preview cannot write state')
    clients=[];reader=Mock();reader._http=SimpleNamespace(request=Mock())
    def get(**kw):
        if kw['fields'].startswith('owners'):
            value={'owners':[{'emailAddress':'owner@example.invalid'}],'permissions':[
                {'type':'user','role':'owner','emailAddress':'owner@example.invalid'},
                {'type':'user','role':'writer','emailAddress':'sa@example.invalid'}]}
        else:value={'id':kw['fileId'],'parents':['synthetic-inbox'],'mimeType':'application/pdf','version':'1'}
        return SimpleNamespace(execute=lambda **kw:deepcopy(value))
    reader.files().get.side_effect=get
    reader.files().get_media.side_effect=lambda **kw:SimpleNamespace(execute=lambda **kw:live.content)
    def listed(**kw):
        name=kw['q'].split("name = '")[1].split("'")[0]
        fid=new_id if name==v2.state_name(lb) else done_id
        return SimpleNamespace(execute=lambda **kw:{'files':[{'id':fid,'name':name,'mimeType':'application/json','parents':[folder]}]})
    reader.files().list.side_effect=listed
    def build(name,version,**kw):
        if version=='v3':return reader
        client=Versioned();clients.append(client);return client
    monkeypatch.setattr('googleapiclient.discovery.build',build)
    monkeypatch.setattr('app.google_clients.credentials',lambda *a:object())
    monkeypatch.setattr('app.private_state_bindings.unwrap',lambda name,*a:folder if name=='KAKEIBO_STATE_FOLDER_ID' else old_id)
    monkeypatch.setattr(runtime,'require_context',lambda env:None)
    env={'PDF_UNIT_PROCESSING_ENABLED':'true','GOOGLE_SERVICE_ACCOUNT_JSON':json.dumps(
        {'private_key':'synthetic-in-memory-key','client_email':'sa@example.invalid'}),
        'PDF_GROUPING_BINDING':json.dumps({'folder':'ciphertext','file':'ciphertext','owner_digest':_digest('owner@example.invalid')})}
    db.sid=sid;settings=SimpleNamespace(spreadsheet_id=sid,receipt_drive_folder_id='synthetic-inbox')
    return env,settings,db,clients,reader,payloads


def test_factory_reuses_drive_binding_acl_v2_and_never_resolves_ai_in_preview(local_ocr,monkeypatch):
    env,settings,db,clients,reader,payloads=factory_fixture(monkeypatch);before=deepcopy(payloads)
    service=runtime.open_intake(settings,db,apply=False,env=env)
    current=service.authority.current('drive-source-id')
    assert current.units[0]['human_classifications']==['normal'] and current.medical_pages
    content=service.authority.load_source('drive-source-id')
    assert runtime.counts(service.preview(content,'drive-source-id'))['new_eligible']==1
    assert payloads==before and all(not client.writes for client in clients) and service.analyzer.ai is None
    with pytest.raises(StateError,match='preview_ai_forbidden'):service.analyzer.factory()


def test_factory_rejects_completion_schema_or_missing_projection_before_ai(local_ocr,monkeypatch):
    env,settings,db,clients,reader,payloads=factory_fixture(monkeypatch)
    with pytest.raises(StateError,match='projection_required'):
        runtime.open_intake(settings,db,apply=True,env=env)
    payloads['synthetic-completion-id']=b'{broken'
    with pytest.raises(StateError,match='read_unavailable'):
        runtime.open_intake(settings,db,apply=False,env=env)
    assert all(not client.writes for client in clients)


def test_private_acl_change_stops_factory_without_repair_or_sharing_change(local_ocr,monkeypatch):
    env,settings,db,clients,reader,payloads=factory_fixture(monkeypatch)
    get=reader.files().get.side_effect
    def changed(**kw):
        request=get(**kw)
        if kw['fields'].startswith('owners'):
            value=request.execute();value['permissions'].append({'type':'anyone','role':'reader'})
            return SimpleNamespace(execute=lambda **kw:value)
        return request
    reader.files().get.side_effect=changed
    with pytest.raises(StateError,match='permissions_mismatch'):
        runtime.open_intake(settings,db,apply=False,env=env)
    reader.permissions().create.assert_not_called();reader.files().update.assert_not_called()
    assert all(not client.writes for client in clients)


def test_completion_discovery_cannot_reuse_grouping_authority_file(local_ocr,monkeypatch):
    env,settings,db,clients,reader,payloads=factory_fixture(monkeypatch)
    listed=reader.files().list.side_effect
    def collision(**kw):
        result=listed(**kw).execute()
        if 'pdf-unit-processing-v1-' in kw['q']:result['files'][0]['id']='synthetic-legacy-id'
        return SimpleNamespace(execute=lambda **kw:result)
    reader.files().list.side_effect=collision
    with pytest.raises(StateError,match='must_be_separate'):
        runtime.open_intake(settings,db,apply=False,env=env)
    assert all(not client.writes for client in clients)


def test_page_scan_without_runtime_never_treats_page_boundary_as_a_transaction(local_ocr):
    normal=runtime.preview_pdf(synthetic_pdf(('normal','normal')),'drive-source-id')
    assert all(u['status']=='grouping_required' for u in normal['units'])
    assert runtime.counts(normal)['new_eligible']==0
    single=runtime.preview_pdf(synthetic_pdf(('normal',)),'drive-source-id')
    assert runtime.counts(single)['new_eligible']==1 and single['archive_allowed'] is False
