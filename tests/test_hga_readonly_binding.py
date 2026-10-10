from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from app.drive_run_state import StateError
from app import hga_readonly_binding as binding
from app.page_receipt_model import digest
from app.private_state_bindings import wrap
from test_private_state_bindings import key
from test_human_general_auth_transport import keys


def setup(monkeypatch,keys,key):
    from test_human_general_real_page import real_http
    rig,drive,http,cfg=real_http(monkeypatch,keys)
    config={'drive':cfg,'actor_id':'a'*64,'authority_sha256':'b'*64}
    fid='private-reference-id';folder=cfg['folder'];owner='owner@example.test';sa='service@example.test'
    cfg['owner_digest']=digest(owner)
    def metadata(file,mime):return {'id':file,'etag':'"fixed"','mimeType':mime,'labels':{'trashed':False},
        'owners':[{'emailAddress':owner}],'permissions':[{'type':'user','role':'owner','emailAddress':owner},{'type':'user','role':'writer','emailAddress':sa}]}
    meta=metadata(fid,'application/json');meta['parents']=[{'id':folder}]
    directory=metadata(folder,'application/vnd.google-apps.folder')
    data={'file':meta,'folder':directory,'raw':json.dumps(binding.document(config)).encode(),'calls':[]}
    def request(method,url,**kwargs):
        data['calls'].append((method,url,kwargs));assert method=='GET'
        if kwargs['params'].get('alt')=='media':return SimpleNamespace(status_code=200,content=data['raw'])
        value=data['folder'] if url.endswith('/'+folder) else data['file']
        return SimpleNamespace(status_code=200,content=json.dumps(value).encode(),json=lambda:deepcopy(value))
    env={'PDF_HGA_READONLY_BINDING_ID':fid,'PDF_GROUPING_BINDING':json.dumps({'folder':wrap('KAKEIBO_STATE_FOLDER_ID',folder,key),'owner_digest':digest(owner)})}
    reader=binding.PrivateBindingReader(env,key,{'client_email':sa},session=SimpleNamespace(request=request))
    return reader,data,config,env


def test_reference_only_fresh_private_gets(monkeypatch,keys,key):
    reader,data,config,env=setup(monkeypatch,keys,key)
    assert reader.read()[0]==config
    assert set(env)=={'PDF_HGA_READONLY_BINDING_ID','PDF_GROUPING_BINDING'}
    assert all(c[0]=='GET' and c[2]['allow_redirects'] is False for c in data['calls'])


@pytest.mark.parametrize('failure',['public','wrong_owner','wrong_parent','wrong_mime','weak_etag','changed_etag','size','tampered_digest','extra_secret','wrong_folder'])
def test_private_reference_rejects_and_never_writes(monkeypatch,keys,key,failure):
    reader,data,config,env=setup(monkeypatch,keys,key)
    if failure=='public':data['file']['permissions'].append({'type':'anyone','role':'reader'})
    elif failure=='wrong_owner':data['folder']['owners'][0]['emailAddress']='other@example.test'
    elif failure=='wrong_parent':data['file']['parents']=[{'id':'other-folder-id'}]
    elif failure=='wrong_mime':data['file']['mimeType']='application/pdf'
    elif failure=='weak_etag':data['file']['etag']='W/"weak"'
    elif failure=='size':data['raw']=b'x'*(binding.MAX_BYTES+1)
    elif failure=='changed_etag':
        original=reader.metadata;count=0
        def metadata(fid):
            nonlocal count
            value=original(fid)
            if fid==reader.fid:
                count+=1
                if count>1:value['etag']='"changed"'
            return value
        reader.metadata=metadata
    else:
        value=json.loads(data['raw'])
        if failure=='tampered_digest':value['digest']='c'*64
        elif failure=='extra_secret':value['config']['client_secret']='synthetic-secret'
        else:
            value['config']['drive']['folder']='different-private-folder'
            value['config']['drive']['binding']=digest(['human-general-verified-drive-canary-v1',
                value['config']['drive']['folder'],value['config']['drive']['authority_file'],value['config']['drive']['page']['source'],
                14,value['config']['drive']['page']['review_identity']])
            value=binding.document(value['config'])
        data['raw']=json.dumps(value).encode()
    with pytest.raises(StateError):reader.read()
    assert all(c[0]=='GET' for c in data['calls'])


@pytest.mark.parametrize('reference',['','https://drive.google.com/file/id','{"drive":{}}'])
def test_no_payload_or_url_in_reference_env(key,reference):
    with pytest.raises(StateError,match='reference_required'):
        binding.PrivateBindingReader({'PDF_HGA_READONLY_BINDING_ID':reference},key,{})


def test_workflow_carries_id_and_no_payload_variable():
    from pathlib import Path
    workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/pdf-unit-readonly.yml').read_text()
    assert 'PDF_HGA_READONLY_BINDING_ID: ${{ inputs.hga_binding_id }}' in workflow
    assert 'vars.PDF_HGA_READONLY_BINDING' not in workflow


@pytest.mark.parametrize('mode',['page_p4','page_p10','page_p14'])
def test_runner_requires_reference_before_cloud_read(mode):
    from app.pdf_unit_readonly_analysis import require_context
    from test_pdf_unit_readonly_analysis import environment
    env={**environment(),'PDF_READONLY_MODE':mode}
    with pytest.raises(StateError,match='reference_required'):require_context(env,'a'*40)
    env['PDF_HGA_READONLY_BINDING_ID']='private-reference-id'
    require_context(env,'a'*40)
