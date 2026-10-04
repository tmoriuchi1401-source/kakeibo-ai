from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.error import HTTPError
import base64
import json
import zipfile

import pytest
import yaml
from PIL import Image

from app import pdf_unit_readonly_analysis as runner
from app.drive_run_state import StateError
from app.pdf_page_kind import PageKindConfirmation
from app.receipt_pdf_grouping import PageEvidence
from app.models import ReceiptResult
from test_pdf_grouping_authority import context,request
from test_receipt_pdf_units import local_ocr,synthetic_pdf
from test_private_state_bindings import key


def environment():
    return {'GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REPOSITORY':runner.REPO,
        'GITHUB_REF':'refs/heads/main','GITHUB_WORKFLOW_REF':runner.REPO+'/'+runner.WORKFLOW+'@refs/heads/main',
        'RUNNER_ENVIRONMENT':'github-hosted','RUNNER_OS':'Linux','PDF_READONLY_APPROVED_SHA':'a'*40,
        'PDF_READONLY_MODE':'p2','PDF_READONLY_CONFIRM':'READ_ONLY','GITHUB_RUN_ID':'100',
        'PDF_ACCOUNTING_ENABLED':'false','PDF_MEDICAL_ENABLED':'false','PDF_ARCHIVE_ENABLED':'false',
        'GITHUB_TOKEN':'synthetic-github-token','GEMINI_API_KEY':'synthetic-gemini-secret',
        'GOOGLE_SERVICE_ACCOUNT_JSON':'synthetic','PDF_GROUPING_BINDING':'synthetic-binding'}


@pytest.mark.parametrize('change',[{'GITHUB_ACTIONS':'false'},{'GITHUB_EVENT_NAME':'schedule'},
    {'GITHUB_REPOSITORY':'other/repo'},{'GITHUB_REF':'refs/heads/other'},
    {'GITHUB_WORKFLOW_REF':'other.yml'}, {'PDF_READONLY_APPROVED_SHA':'not-sha'},
    {'RUNNER_ENVIRONMENT':'self-hosted'},{'RUNNER_OS':'Windows'},
    {'PDF_READONLY_CONFIRM':''},{'PDF_ACCOUNTING_ENABLED':'true'},{'PDF_MEDICAL_ENABLED':'true'},
    {'PDF_ARCHIVE_ENABLED':'true'},{'RUNNER_DEBUG':'1'},{'ACTIONS_STEP_DEBUG':'true'},
    {'GOOGLE_SERVICE_ACCOUNT_FILE':'local-key.json'},{'PDF_READONLY_MODE':'remaining'}])
def test_context_rejected_before_secrets_or_ai(change):
    env={**environment(),**change};open_=Mock();ai=Mock()
    with pytest.raises(StateError):runner.execute(env,'a'*40,opener=open_,analyzer=ai,approve=Mock())
    open_.assert_not_called();ai.assert_not_called()


@pytest.mark.parametrize('missing',['GEMINI_API_KEY','GOOGLE_SERVICE_ACCOUNT_JSON','PDF_GROUPING_BINDING'])
def test_unavailable_config_never_opens_or_analyzes(missing):
    env=environment();env[missing]='';open_=Mock();ai=Mock()
    with pytest.raises(StateError,match='configuration_unavailable'):
        runner.execute(env,'a'*40,opener=open_,analyzer=ai,approve=Mock())
    open_.assert_not_called();ai.assert_not_called()


def live_context(key,monkeypatch):
    kinds=('medical',)+('normal',)*13
    g,live,t=context(kinds);view=g.display('drive-source-id')
    PageKindConfirmation(g).confirm(view['proposal'],{n:'医療' if n==1 else '一般' for n in range(1,15)})
    view=g.regenerate_general('drive-source-id',lambda p,ns:{n:PageEvidence() for n in ns})
    g.review(request(view));p=view['proposal'];a=g.store.load()['records'][runner._digest('drive-source-id')]['confirmation']
    units=g.confirmed_units('drive-source-id')
    expected={k:p[k] for k in ('source_file_id','source_content_hash','page_count','proposal_digest')}
    expected.update(binding=g.store.binding,grouping_revision=2,confirmation_digest=a['confirmation_digest'],
        unit_ids={u.page_numbers[0]:u.unit_id for u in units})
    monkeypatch.setattr(runner,'SOURCE_HASH',p['source_content_hash']);monkeypatch.setattr(runner,'CONFIRMATION',a['confirmation_digest'])
    return g,t,expected,lambda _:(g.store,lambda _:synthetic_pdf(kinds),[('食費','食品')],expected,key)


def exercise(key,monkeypatch,mode='p2',proof=None):
    g,t,expected,opener=live_context(key,monkeypatch)
    result=ReceiptResult(date='2026-09-01',merchant='Synthetic shop',total=100,payment_method='現金',
        items=[dict(name='商品',amount=100,major_category='食費',minor_category='食品')])
    evidence={'calls':0};pages=[]
    def analyze(png,categories):evidence['calls']+=1;pages.append(png);return result,[result]
    factory=Mock(return_value=(analyze,evidence));env=environment()
    env.update(GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps({'private_key':key}),PDF_READONLY_MODE=mode)
    if mode=='remaining':env['PDF_READONLY_P2_RUN_ID']='99'
    before=t.payload;writes=t.writes
    prior=Mock(side_effect=lambda env,pem,current:runner.check_p2(proof,env,current)) if mode=='remaining' else Mock()
    artifact,summary=runner.execute(env,'a'*40,opener=opener,analyzer=factory,prior=prior,approve=Mock())
    assert t.payload==before and t.writes==writes
    return artifact,summary,env,expected,pages


def test_p2_only_encrypted_diagnostic_no_secret_ocr_image_or_cloud_write(local_ocr,key,monkeypatch):
    artifact,summary,env,expected,images=exercise(key,monkeypatch)
    assert summary['status']=='analysis_complete' and summary['gemini_calls']==1 and len(images)==1
    text=json.dumps(artifact)
    assert all(value not in text for value in (env['GEMINI_API_KEY'],key,'Synthetic shop','現金','PRIVATE_MEDICAL'))
    value=runner.decrypted(artifact,key)
    assert [r['page_number'] for r in value['results']]==[2]
    assert value['p1_rendered']==value['p1_submitted']==value['medical_calls']==value['cloud_writes']==value['source_moves']==0
    assert 'parsed' not in value['results'][0]
    assert value['schema']=='pdf-unit-readonly-diagnostic-v2'
    candidate=value['results'][0]['candidate']
    assert candidate['total']==100 and len(candidate['items'])==1
    assert 'note' not in candidate and 'note' not in candidate['items'][0]
    assert 'text' not in value['results'][0] and value['budgets']['peak_live_pages']==1


def test_remaining_requires_same_successful_p2_proof_and_is_sequential(local_ocr,key,monkeypatch):
    artifact,_,_,_,_=exercise(key,monkeypatch);proof=runner.decrypted(artifact,key);proof['run_id']='99'
    artifact,summary,_,_,images=exercise(key,monkeypatch,'remaining',proof)
    assert summary['gemini_calls']==len(images)==12
    value=runner.decrypted(artifact,key)
    assert [r['page_number'] for r in value['results']]==list(range(3,15))
    assert value['budgets']['peak_live_pages']==1


@pytest.mark.parametrize('change',[{'mode':'remaining'},{'code_sha':'b'*40},{'authority_unchanged':False},
    {'confirmation_digest':'0'*64},{'proposal_digest':'0'*64},{'grouping_revision':3},
    {'cloud_writes':1},{'p1_submitted':1},{'results':[]}])
def test_stale_p2_proof_never_expands(local_ocr,key,monkeypatch,change):
    artifact,_,env,expected,_=exercise(key,monkeypatch);proof=runner.decrypted(artifact,key)
    env['PDF_READONLY_P2_RUN_ID']=proof['run_id'];proof.update(change)
    with pytest.raises(StateError,match='p2_proof_stale'):runner.check_p2(proof,env,expected)


def test_cloud_mutation_fence_precedes_http_call():
    http=SimpleNamespace(request=Mock(return_value='read'))
    service=SimpleNamespace(_http=http);runner.readonly_http(service)
    assert http.request('https://example.test','GET')=='read'
    for method in ('POST','PATCH','PUT','DELETE'):
        with pytest.raises(StateError,match='cloud_write_forbidden'):http.request('https://example.test',method)


def test_debug_and_exception_bodies_never_print_secrets(monkeypatch,capsys):
    monkeypatch.setenv('GEMINI_API_KEY','SYNTHETIC-PRIVATE-KEY')
    def failure(*_):
        print('SYNTHETIC-PRIVATE-KEY');raise ValueError('SYNTHETIC-PRIVATE-KEY')
    monkeypatch.setattr(runner,'execute',failure)
    monkeypatch.setattr(runner.subprocess,'check_output',lambda *_ ,**__: 'a'*40)
    assert runner.main()==1
    output=capsys.readouterr()
    assert 'Gemini: configured' in output.out and 'Read-only analysis: stopped' in output.out
    assert 'SYNTHETIC-PRIVATE-KEY' not in output.out+output.err


def test_manual_workflow_new_path_only_and_secret_scope():
    path=Path('.github/workflows/pdf-unit-readonly.yml');text=path.read_text(encoding='utf-8')
    workflow=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(workflow['on'])=={'workflow_dispatch'}
    assert all(value=='read' for value in workflow['permissions'].values())
    assert 'production_flow' not in text and 'PROCESSED_DRIVE_FOLDER_ID' not in text
    steps=workflow['jobs']['readonly']['steps']
    secret_steps=[s for s in steps if 'secrets.' in json.dumps(s)]
    assert len(secret_steps)==1 and secret_steps[0]['run']=='python -m app.pdf_unit_readonly_analysis'
    assert all(secret_steps[0]['env'][k]=='false' for k in ('PDF_ACCOUNTING_ENABLED','PDF_MEDICAL_ENABLED','PDF_ARCHIVE_ENABLED'))
    assert workflow['on']['workflow_dispatch']['inputs']['mode']['default']=='p2'
    upload=next(s for s in steps if s.get('uses','').startswith('actions/upload-artifact'))
    assert upload['with']['path'].endswith('/'+runner.OUTPUT) and upload['with']['retention-days']=='1'
    assert 'set -x' not in text and 'echo "$GEMINI_API_KEY"' not in text


@pytest.mark.parametrize('failure',['draft','head','repo','branch','base','ci_failed','ci_pending','ci_missing','ci_duplicate'])
def test_reviewed_pr_and_ci_gate_rejects_changed_metadata(failure):
    env=environment()
    p={'state':'open','draft':True,'base':{'ref':'main'},
       'head':{'repo':{'full_name':runner.REPO},'ref':runner.BRANCH,'sha':'a'*40}}
    runs=[{'name':n,'status':'completed','conclusion':'success'} for n in runner.CHECKS]
    if failure=='draft':p['draft']=False
    if failure=='head':p['head']['sha']='b'*40
    if failure=='repo':p['head']['repo']['full_name']='other/repo'
    if failure=='branch':p['head']['ref']='main'
    if failure=='base':p['base']['ref']='other'
    if failure=='ci_failed':runs[0]['conclusion']='failure'
    if failure=='ci_pending':runs[0]['status']='in_progress'
    if failure=='ci_missing':runs.pop()
    if failure=='ci_duplicate':runs.append(runs[0])
    with pytest.raises(StateError):runner.approved_pr(env,Mock(side_effect=[p,{'check_runs':runs}]))


def test_approved_pr_exact_head_and_ci():
    p={'state':'open','draft':True,'base':{'ref':'main'},
       'head':{'repo':{'full_name':runner.REPO},'ref':runner.BRANCH,'sha':'a'*40}}
    checks={'check_runs':[{'name':n,'status':'completed','conclusion':'success'} for n in runner.CHECKS]}
    get=Mock(side_effect=[p,checks]);runner.approved_pr(environment(),get)
    assert get.call_args_list[1].args[0].endswith('/'+'a'*40+'/check-runs?per_page=100')


@pytest.mark.parametrize('failure',['expired','duplicate','failed_run','other_workflow','schedule','wrong_repo',
                                    'unexpected_zip','tampered_ciphertext'])
def test_prior_artifact_must_be_successful_exact_workflow_and_authenticated(local_ocr,key,monkeypatch,failure):
    artifact,_,env,expected,_=exercise(key,monkeypatch)
    env['PDF_READONLY_P2_RUN_ID']='100'
    run={'event':'workflow_dispatch','status':'completed','conclusion':'success','path':runner.WORKFLOW,
         'repository':{'full_name':runner.REPO}}
    artifacts=[{'id':7,'name':runner.ARTIFACT,'expired':False}]
    if failure=='expired':artifacts[0]['expired']=True
    if failure=='duplicate':artifacts.append(artifacts[0])
    if failure=='failed_run':run['conclusion']='failure'
    if failure=='other_workflow':run['path']='.github/workflows/kakeibo-production.yml'
    if failure=='schedule':run['event']='schedule'
    if failure=='wrong_repo':run['repository']['full_name']='other/repo'
    if failure=='tampered_ciphertext':
        raw=bytearray(base64.b64decode(artifact['ciphertext']));raw[0]^=1
        artifact['ciphertext']=base64.b64encode(raw).decode()
    buf=BytesIO()
    with zipfile.ZipFile(buf,'w') as archive:
        archive.writestr('other.json' if failure=='unexpected_zip' else runner.OUTPUT,json.dumps(artifact))
    with pytest.raises(Exception):
        runner.prior_p2(env,key,expected,Mock(side_effect=[run,{'artifacts':artifacts},buf.getvalue()]))


def test_prior_artifact_roundtrip_and_same_unit(local_ocr,key,monkeypatch):
    artifact,_,env,expected,_=exercise(key,monkeypatch);env['PDF_READONLY_P2_RUN_ID']='100'
    run={'event':'workflow_dispatch','status':'completed','conclusion':'success','path':runner.WORKFLOW,
         'repository':{'full_name':runner.REPO}}
    buf=BytesIO()
    with zipfile.ZipFile(buf,'w') as archive:archive.writestr(runner.OUTPUT,json.dumps(artifact))
    get=Mock(side_effect=[run,{'artifacts':[{'id':7,'name':runner.ARTIFACT,'expired':False}]},buf.getvalue()])
    assert runner.prior_p2(env,key,expected,get)['results'][0]['unit_id']==expected['unit_ids'][2]


def test_artifact_redirect_does_not_forward_github_token(monkeypatch):
    token='synthetic-github-token';requests=[]
    def open_(request,**_):
        requests.append(request)
        if len(requests)==1:
            raise HTTPError(request.full_url,302,'redirect',
                {'Location':'https://synthetic.blob.core.windows.net/artifact?signed=synthetic'},None)
        return BytesIO(b'zip-bytes')
    monkeypatch.setattr(runner,'build_opener',lambda *_:SimpleNamespace(open=open_))
    assert runner.github_get('/repos/'+runner.REPO+'/actions/artifacts/7/zip',token,binary=True)==b'zip-bytes'
    assert requests[0].get_header('Authorization')=='Bearer '+token
    assert requests[1].get_header('Authorization') is None
    assert token not in requests[1].full_url


@pytest.mark.parametrize('target',['http://synthetic.blob.core.windows.net/a',
    'https://attacker.test/a','https://blob.core.windows.net.attacker.test/a',
    'https://user:pass@synthetic.blob.core.windows.net/a'])
def test_artifact_redirect_untrusted_destination_rejected(monkeypatch,target):
    open_=Mock(side_effect=HTTPError('url',302,'redirect',{'Location':target},None))
    monkeypatch.setattr(runner,'build_opener',lambda *_:SimpleNamespace(open=open_))
    with pytest.raises(StateError,match='artifact_destination_invalid'):
        runner.github_get('/repos/'+runner.REPO+'/actions/artifacts/7/zip','synthetic',binary=True)
    assert open_.call_count==1


def test_remaining_invalid_proof_never_constructs_gemini(key,monkeypatch):
    g,t,expected,opener=live_context(key,monkeypatch)
    env={**environment(),'PDF_READONLY_MODE':'remaining','PDF_READONLY_P2_RUN_ID':'99',
         'GOOGLE_SERVICE_ACCOUNT_JSON':json.dumps({'private_key':key})}
    ai=Mock();before=t.writes
    with pytest.raises(StateError):
        runner.execute(env,'a'*40,opener=opener,analyzer=ai,
            prior=Mock(side_effect=StateError('readonly_p2_proof_stale')),approve=Mock())
    ai.assert_not_called();assert t.writes==before


@pytest.mark.parametrize('blocked',[False,True])
def test_real_receipt_adapter_rechecks_exact_png_before_each_sdk_call(monkeypatch,blocked):
    from app import gemini_ai
    from app.receipt_privacy_gate import ReceiptPrivacyBlocked
    result=ReceiptResult(date='2026-09-01',merchant='Synthetic shop',total=100,
        items=[dict(name='商品',amount=100,major_category='食費',minor_category='食品')])
    sdk=Mock(return_value=SimpleNamespace(output_text=result.model_dump_json()))
    client=SimpleNamespace(interactions=SimpleNamespace(create=sdk),
        _api_client=SimpleNamespace(_http_options=SimpleNamespace(base_url='https://generativelanguage.googleapis.com/')))
    factory=Mock(return_value=client);monkeypatch.setattr(gemini_ai.genai,'Client',factory)
    initial_gate=Mock();monkeypatch.setattr(gemini_ai,'require_receipt_ai_permission',initial_gate)
    final_gate=Mock(side_effect=ReceiptPrivacyBlocked() if blocked else None)
    monkeypatch.setattr(runner,'require_receipt_ai_permission',final_gate)
    buf=BytesIO()
    with Image.new('RGB',(10,10)) as image:image.save(buf,format='PNG')
    payload=buf.getvalue();analyze,evidence=runner.receipt_analyzer('synthetic-key','synthetic-model')
    if blocked:
        with pytest.raises(ReceiptPrivacyBlocked):analyze(payload,[('食費','食品')])
        sdk.assert_not_called()
    else:
        assert analyze(payload,[('食費','食品')])[0]==result
        assert sdk.call_count==evidence['calls']==1
        content=sdk.call_args.kwargs['input']
        assert len(content)==2 and content[1]['mime_type']=='image/png'
        assert base64.b64decode(content[1]['data'])==payload
    assert initial_gate.call_args.args[:2]==final_gate.call_args.args[:2]==(payload,'image/png')
    assert factory.call_args.kwargs['http_options']['retry_options']=={'attempts':1}

def test_real_sdk_owner_survives_proxy_and_gc_before_http(monkeypatch):
    import gc
    import weakref
    import httpx
    from google import genai
    from app import gemini_ai
    result=ReceiptResult(date='2026-09-01',merchant='Synthetic shop',total=100,
        items=[dict(name='Synthetic item',amount=100,major_category='食費',minor_category='食品')])
    calls=[];owners=[];factory=genai.Client
    def handle(request):
        calls.append(request)
        return httpx.Response(200,json={'id':'synthetic','status':'completed','model':'synthetic',
            'steps':[{'type':'model_output','content':[{'type':'text','text':result.model_dump_json()}]}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        def construct(**kwargs):
            options={**kwargs.pop('http_options'),'httpx_client':http}
            owner=factory(**kwargs,http_options=options);owners.append(weakref.ref(owner))
            return owner
        monkeypatch.setattr(gemini_ai.genai,'Client',construct)
        monkeypatch.setattr(gemini_ai,'require_receipt_ai_permission',Mock())
        monkeypatch.setattr(runner,'require_receipt_ai_permission',Mock())
        analyze,evidence=runner.receipt_analyzer('synthetic-key','synthetic')
        gc.collect()
        assert owners[0]() is not None
        buffer=BytesIO()
        with Image.new('RGB',(10,10)) as image:image.save(buffer,format='PNG')
        assert analyze(buffer.getvalue(),[('食費','食品')])[0]==result
        assert len(calls)==evidence['calls']==evidence['responses']==1
        assert calls[0].url.path=='/v1/interactions'
        owners[0]().close()
