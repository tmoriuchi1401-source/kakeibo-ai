from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app import p14_completion_readonly as canary,page_receipt_actions as actions,pdf_unit_readonly_analysis as runner
from app.page_receipt_ai import GeminiPageReceipts,wire_value
from app.drive_run_state import StateError
from app.general_receipt_completion import evaluate
from test_page_receipts import page,reading,confirmation,CATEGORIES
from test_receipt_pdf_units import local_ocr,synthetic_pdf
from test_private_state_bindings import key
from test_pdf_unit_readonly_analysis import environment
from test_human_general_auth_transport import keys,OWNER


def run(key,monkeypatch,*,blocked=False,changing=False):
    kinds=['medical']+['normal']*12+['unknown']
    p,raw=page('unknown',14,kinds);svc,_=confirmation(p)
    grant=svc.confirm(p,operation='confirm_general_receipt_ai',request_id='00000000-0000-4000-8000-000000000001')
    store=Mock(payload=b'fixed',tag='"tag"');store.load.return_value={'fixed':True}
    source=Mock(return_value=raw)
    monkeypatch.setattr(actions,'load_page',lambda _s,_e,sid,n:p)
    calls=Mock(side_effect=[SimpleNamespace(output_text=json.dumps(wire_value(reading(1)))) for _ in range(4)])
    factory=Mock(side_effect=lambda key,model,permission:GeminiPageReceipts(SimpleNamespace(interactions=SimpleNamespace(create=calls)),model,permission))
    expected={'source_file_id':p.source.source_file_id}
    env={**environment(),'PDF_READONLY_MODE':'page_p14','GOOGLE_SERVICE_ACCOUNT_JSON':json.dumps({'private_key':key})}
    def hga(*_):
        if blocked:raise StateError('readonly_verified_actor_missing')
        def fresh():
            if changing and calls.call_count:raise StateError('readonly_hga_changed')
        return p,lambda _:grant,fresh
    value,summary=canary.execute_p14(env,'a'*40,store,source,CATEGORIES,expected,key,factory=factory,open_hga=hga)
    return runner.decrypted(value,key),summary,calls


def test_only_p14_two_independent_readings_twice_and_blank_optional(local_ocr,key,monkeypatch):
    value,summary,calls=run(key,monkeypatch)
    assert calls.call_count==4 and summary['receipt_manifest_replay']
    assert all(r['payload_pages']==[14] and r['payload_mime']=='image/png' for r in value['results'])
    assert value['results'][0]['manifest']==value['results'][1]['manifest']
    record=value['results'][0]['completion_drafts'][0]
    assert record['prefill']['amount']==100 and record['prefill']['payment']==''
    assert evaluate(record,record['prefill'],CATEGORIES)['status']=='ready_to_confirm'
    assert all(value[k]==0 for k in ('authority_writes','cloud_writes','medical_calls','source_moves','p1_rendered','p1_submitted'))
    assert not value['accounting_authority']


def test_missing_verified_hga_before_any_gemini(local_ocr,key,monkeypatch):
    with pytest.raises(StateError,match='verified_actor'):run(key,monkeypatch,blocked=True)


def test_changed_durable_hga_after_first_analysis_stops_replay(local_ocr,key,monkeypatch):
    with pytest.raises(StateError,match='hga_changed'):run(key,monkeypatch,changing=True)


def test_p14_manual_mode_uses_unchanged_no_writer_flags():
    env={**environment(),'PDF_READONLY_MODE':'page_p14'}
    runner.require_context(env,'a'*40)
    env['PDF_ACCOUNTING_ENABLED']='true'
    with pytest.raises(StateError):runner.require_context(env,'a'*40)


@pytest.mark.parametrize('change',['date','amount','items'])
def test_replay_field_disagreement_blanks_and_structural_change_stays_review(change):
    from test_general_receipt_completion import candidate
    from app.page_receipt_model import digest
    first,*_=candidate();second=deepcopy(first)
    if change=='date':second['prefill']['date']='2026-10-06';second['parsed']['date']='2026-10-06'
    elif change=='amount':second['prefill']['amount']=1000;second['parsed']['total']=1000
    else:second['parsed']['items'][0]['name']='different item'
    second.pop('candidate_digest');second['candidate_digest']=digest(second)
    merged=canary.corroborate_replay(first,second)
    if change in {'date','amount'}:
        assert merged['prefill'][change]=='' and merged['provenance'][change]=='missing'
        assert evaluate(merged,merged['prefill'],CATEGORIES)['status']=='needs_human_completion'
    else:
        assert 'replay_item_structure_changed' in merged['hard_issues']
        assert evaluate(merged,merged['prefill'],CATEGORIES)['status']=='needs_review'


@pytest.mark.parametrize('tamper',[None,'actor','hash','binding','scope','source'])
def test_verified_drive_readonly_context_bound_and_tamper_rejected(monkeypatch,keys,key,tamper):
    from test_human_general_real_page import real_http
    from app.page_receipt_model import digest
    from hashlib import sha256
    rig,drive,http,cfg=real_http(monkeypatch,keys)
    rig.start();assert rig.callback().status_code==303;assert rig.confirm().status_code==303
    raw=http.payloads[cfg['authority_file']]
    config={'drive':cfg,'actor_id':digest(['https://accounts.google.com',OWNER]),'authority_sha256':sha256(raw).hexdigest()}
    if tamper=='actor':config['actor_id']='f'*64
    elif tamper=='hash':config['authority_sha256']='f'*64
    elif tamper=='binding':config['drive']={**cfg,'binding':'f'*64}
    elif tamper=='scope':
        value=json.loads(raw);next(iter(value['actor_evidence'].values()))['explicit_consent']='general_receipt'
        http.payloads[cfg['authority_file']]=json.dumps(value).encode()
        config['authority_sha256']=sha256(http.payloads[cfg['authority_file']]).hexdigest()
    elif tamper=='source':http.raw+=b'changed'
    blob=runner.encrypted(config,key)
    env={'PDF_HGA_READONLY_BINDING':json.dumps(blob),'GOOGLE_SERVICE_ACCOUNT_JSON':'{}'}
    writes=len([r for r in http.requests if r[0]=='PUT'])
    def call():return canary.context(env,key,{'source_file_id':drive.page.source.source_file_id},lambda *_:drive.page,drive_factory=lambda *_:drive)
    if tamper:
        with pytest.raises(StateError):call()
    else:
        p,grant,check=call();assert 'single_page_ai' in grant(p)['authority_scope'];check()
        with pytest.raises(StateError):drive.request(cfg['authority_file'],put=b'{}',tag='"v2"')
    assert len([r for r in http.requests if r[0]=='PUT'])==writes
