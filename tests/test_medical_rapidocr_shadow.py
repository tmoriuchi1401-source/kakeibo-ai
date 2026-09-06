"""Synthetic only; optional RapidOCR is not needed by the production test suite."""
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.medical_ocr_observation_shadow import (
    ReceiptImage, make_observation, project_observation, failed_observation,
)
from app.medical_payment_evidence import resolve_payment_evidence
from app.medical_rapidocr_shadow import ModelAsset, RapidOcrManifest, RapidOcrShadowAdapter
from app.medical_rapidocr_worker import capture_regions, verified_models, WorkerFailure, offline_guard, validate_runtime
from app.medical_rapidocr_shadow import SUPPORTED_ASSETS


def image(page=1):
    return ReceiptImage('synthetic_unit',page,b'synthetic image transport')


def row(text='領収金額 987円', confidence=.95, x=1):
    return {'text':text,'confidence':confidence,'detection_confidence':.8,
            'polygon':[[x,1],[x+10,1],[x+10,10],[x,10]]}


def observe(rows, page=1):
    return make_observation(image(page),'synthetic',('a'*64,),100,100,rows)


def adapter(tmp_path):
    asset=ModelAsset(str(tmp_path/'model.onnx'),'a'*64)
    return RapidOcrShadowAdapter(RapidOcrManifest(asset,asset,asset),
        python_executable=str(tmp_path/'python.exe'))


@pytest.mark.parametrize('content',[bytearray(b'image'),memoryview(b'image'),'',b''])
def test_reject_mutable_or_invalid_image(content):
    with pytest.raises(ValueError): ReceiptImage('synthetic',1,content)


def test_immutable_provenance_and_hidden_raw_repr():
    result=observe([row()])
    with pytest.raises(FrozenInstanceError): result.page=2
    assert '987' not in repr(result)
    assert '支払' not in repr(result.regions[0])
    assert result.regions[0].granularity=='text_region'
    assert result.regions[0].bbox==(1,1,10,9)
    assert result.regions[0].confidence==.95
    assert result.regions[0].detection_confidence==.8
    assert result.image_sha256==hashlib.sha256(image().image_bytes).hexdigest()


def test_duplicate_values_are_separate_regions_and_no_candidates():
    projection=project_observation(observe([row('987円'),row('987円',x=40)]))
    assert len(projection.source.regions)==len(projection.evidence.regions)==2
    assert projection.evidence.regions[0].region_index!=projection.evidence.regions[1].region_index
    assert all(r.candidates==() for r in projection.evidence.regions)
    assert resolve_payment_evidence(projection.evidence).status=='needs_review'


def test_valid_strong_region_is_still_shadow_only():
    projection=project_observation(observe([row()]))
    assert projection.evidence.regions[0].scope=='payment_region'
    assert projection.summary()['candidate_count']==0
    assert resolve_payment_evidence(projection.evidence).status=='needs_review'


def test_low_confidence_and_malformed_competitor_both_retained():
    projection=project_observation(observe([row(),row('支払金額 9.87円',.69)]))
    region=projection.evidence.regions[1]
    assert region.observations[0].state=='low_confidence_numeric'
    assert 'ambiguous_numeric_observations' in region.diagnostic_codes
    assert 'amount_observation_low_confidence' in region.diagnostic_codes
    assert resolve_payment_evidence(projection.evidence).status=='needs_review'


def test_multiple_runs_same_region_preserve_offsets_without_fake_word_box():
    projection=project_observation(observe([row('支払金額 987円 654円')]))
    region=projection.evidence.regions[0]
    assert len(region.observations)==len(projection.numeric_spans[0])==2
    assert all(o.bbox is None for o in region.observations)
    assert 'ambiguous_numeric_observations' in region.diagnostic_codes
    assert projection.summary()['competing_payment_regions']==1


@pytest.mark.parametrize('text,scope',[('小計 987円','excluded'),('自費 987円','possible_payment_region'),('987円','unassigned')])
def test_context_projection_does_not_discard_numbers(text,scope):
    projection=project_observation(observe([row(text)]))
    assert projection.evidence.regions[0].scope==scope
    assert len(projection.evidence.regions[0].observations)==1


@pytest.mark.parametrize('text',['',None])
def test_blank_or_missing_recognition_is_not_lost(text):
    result=observe([row(),row(text)])
    assert len(result.regions)==2
    assert not result.complete
    assert resolve_payment_evidence(project_observation(result).evidence).status=='needs_review'


@pytest.mark.parametrize('confidence',[None,float('nan'),-1,2])
def test_bad_confidence_is_unresolved(confidence):
    result=observe([row(confidence=confidence)])
    assert len(result.regions)==1
    assert 'invalid_confidence' in result.regions[0].issues
    assert not result.complete


def test_bad_geometry_keeps_observation_but_prevents_complete():
    result=observe([row(x=99)])
    assert len(result.regions)==1 and not result.complete
    assert result.regions[0].bbox is None


def test_self_crossing_polygon_remains_unresolved():
    malformed=row()
    malformed['polygon']=[[1,1],[10,10],[1,10],[10,1]]
    result=observe([malformed])
    assert len(result.regions)==1 and not result.complete
    assert 'invalid_geometry' in result.regions[0].issues


def test_pages_are_separate_projection_inputs():
    first=project_observation(observe([row()],page=1))
    second=project_observation(observe([row('小計 654円')],page=2))
    assert [r.page for r in first.evidence.regions]==[1]
    assert [r.page for r in second.evidence.regions]==[2]
    assert first.source.image_sha256==second.source.image_sha256
    assert first.source.page!=second.source.page


def test_failure_projects_to_incomplete_needs_review():
    projection=project_observation(failed_observation(image()))
    assert not projection.evidence.observation_complete
    assert resolve_payment_evidence(projection.evidence).status=='needs_review'


def test_upstream_mismatched_arrays_not_truncated_or_filtered():
    det=SimpleNamespace(scores=[.8,.9,.8])
    rec=SimpleNamespace(txts=['987円',''],scores=[.69,.2])
    regions,complete=capture_regions(det,rec,[row()['polygon']]*3)
    assert len(regions)==3 and not complete
    assert regions[0]['confidence']==.69
    assert regions[1]['text']=='' and regions[2]['text'] is None


def test_verified_models_returns_hashed_snapshot(tmp_path):
    path=tmp_path/'synthetic.onnx'; path.write_bytes(b'synthetic model')
    asset={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    result=verified_models({'models':dict.fromkeys(('det','cls','rec'),asset)})
    path.write_bytes(b'changed later')
    assert result['det']==b'synthetic model'
    with pytest.raises(WorkerFailure,match='model_verification_failed'):
        verified_models({'models':dict.fromkeys(('det','cls','rec'),asset)})


def test_missing_model_fails_without_download(tmp_path):
    asset={'path':str(tmp_path/'missing.onnx'),'sha256':'a'*64}
    with pytest.raises(WorkerFailure,match='model_verification_failed'):
        verified_models({'models':dict.fromkeys(('det','cls','rec'),asset)})


def test_adapter_uses_private_pipe_and_does_not_log(tmp_path,monkeypatch,capsys):
    def run(command,**kwargs):
        assert '-I' in command and '-B' in command
        assert kwargs['stdout']==subprocess.PIPE and kwargs['stderr']==subprocess.DEVNULL
        assert kwargs['input'].endswith(image().image_bytes)
        return SimpleNamespace(returncode=0,stdout=json.dumps({'status':'observed','width':100,
            'height':100,'complete':True,'regions':[row()]}).encode())
    monkeypatch.setattr(subprocess,'run',run)
    result=adapter(tmp_path).observe(image())
    assert result.regions[0].text=='領収金額 987円'
    assert capsys.readouterr().out==''


@pytest.mark.parametrize('response',[b'not json',b'{"status":"failed","reason":"private content"}',b'{}'])
def test_malformed_worker_response_fails_closed(tmp_path,monkeypatch,response):
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=response))
    result=adapter(tmp_path).observe(image())
    assert result.issues==('observation_incomplete',)
    assert 'private' not in repr(result)


def test_timeout_is_redacted(tmp_path,monkeypatch,capsys):
    def run(*args,**kwargs): raise subprocess.TimeoutExpired('private command',1,output=b'private OCR')
    monkeypatch.setattr(subprocess,'run',run)
    result=adapter(tmp_path).observe(image())
    assert not result.complete and capsys.readouterr().out==''


def test_production_modules_do_not_import_adapter():
    root=Path(__file__).resolve().parents[1]/'app'
    for name in ('receipt_pipeline.py','receipt_privacy_gate.py','gemini_ai.py','receipt_text_extraction.py'):
        assert 'medical_rapidocr' not in (root/name).read_text(encoding='utf-8')


def test_offline_guard_blocks_before_transport_and_restores_parent_io():
    import socket
    original=socket.socket.connect
    with offline_guard() as (_,deny,attempted):
        with socket.socket() as client:
            for action in (lambda:client.connect(('127.0.0.1',1)),
                    lambda:client.connect_ex(('127.0.0.1',1)),
                    lambda:client.sendto(b'synthetic',('127.0.0.1',1)),
                    lambda:socket.getaddrinfo('synthetic.invalid',80),
                    lambda:socket.create_connection(('127.0.0.1',1)),
                    lambda:deny()):
                with pytest.raises(WorkerFailure,match='offline_violation'): action()
        assert len(attempted)==6
    assert socket.socket.connect is original


def test_nonfinite_output_is_private_json_safe_but_remains_unresolved():
    rows,complete=capture_regions(SimpleNamespace(scores=[float('nan')]),
        SimpleNamespace(txts=['987円'],scores=[float('nan')]),[row()['polygon']])
    json.dumps(rows,allow_nan=False)
    assert complete
    result=observe(rows)
    assert not result.complete and len(result.regions)==1


def test_surplus_detector_observation_not_dropped():
    rows,complete=capture_regions(SimpleNamespace(scores=[.8,.9]),
        SimpleNamespace(txts=['987円'],scores=[.9]),[row()['polygon']])
    assert not complete and len(rows)==2
    assert rows[1]['text'] is None


def test_changed_bytes_same_unit_are_not_cached(tmp_path,monkeypatch):
    requests=[]
    def run(command,**kwargs):
        requests.append(kwargs['input'])
        return SimpleNamespace(returncode=0,stdout=b'{"status":"failed"}')
    monkeypatch.setattr(subprocess,'run',run)
    engine=adapter(tmp_path)
    first=engine.observe(ReceiptImage('same',1,b'first'))
    second=engine.observe(ReceiptImage('same',1,b'second'))
    assert len(requests)==2 and requests[0]!=requests[1]
    assert first.image_sha256!=second.image_sha256


@pytest.mark.parametrize('change',['requested_version','installed_version','asset_hash','missing_role'])
def test_unreviewed_runtime_or_assets_are_rejected(change):
    manifest={'rapidocr_version':'3.9.2','onnxruntime_version':'1.29.0',
              'models':{k:{'sha256':v} for k,v in SUPPORTED_ASSETS.items()}}
    versions={'rapidocr':'3.9.2','onnxruntime':'1.29.0'}
    if change=='requested_version': manifest['rapidocr_version']='0.0.0'
    if change=='installed_version': versions['onnxruntime']='0.0.0'
    if change=='asset_hash': manifest['models']['det']['sha256']='0'*64
    if change=='missing_role': del manifest['models']['rec']
    with pytest.raises(WorkerFailure): validate_runtime(manifest,versions.__getitem__)


def test_network_executable_path_rejected(tmp_path):
    asset=ModelAsset(str(tmp_path/'model.onnx'),'a'*64)
    with pytest.raises(ValueError):
        RapidOcrShadowAdapter(RapidOcrManifest(asset,asset,asset),python_executable='//server/python.exe')
