"""Private one-image IPC worker, not a receipt CLI. Never run with terminal IO.

    Upstream-specific interception is pinned to RapidOCR 3.9.2 / ORT 1.29.0.
    Native telemetry is disabled before sessions. Network-capable Python entry
    points are denied for the entire worker lifetime. Production deployment must
    additionally enforce OS-level outbound denial for native dependencies.
"""
import hashlib
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import socket
import struct
import sys
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from .medical_ocr_observation_shadow import MAX_IMAGE_BYTES
from .medical_rapidocr_shadow import SUPPORTED_RAPIDOCR, SUPPORTED_ORT, SUPPORTED_ASSETS


class WorkerFailure(Exception):
    pass


@contextmanager
def offline_guard():
    """Worker-local guards, never monkeypatch the production application's IO."""
    attempted=[]
    def deny(*args,**kwargs):
        attempted.append(True)
        raise WorkerFailure('offline_violation')
    with ExitStack() as stack:
        for key in ('connect','connect_ex','sendto'):
            stack.enter_context(patch.object(socket.socket,key,deny))
        for key in ('create_connection','getaddrinfo'):
            stack.enter_context(patch.object(socket,key,deny))
        yield stack,deny,attempted


def verified_models(manifest):
    """Hash and execute the SAME immutable bytes; no model path reopen race."""
    result={}
    try:
        if set(manifest['models'])!={'det','cls','rec'}:
            raise ValueError()
        for role,asset in manifest['models'].items():
            path=Path(asset['path'])
            if (not path.is_absolute() or str(path).startswith(('\\\\','//'))
                    or not 0<path.stat().st_size<=128*1024*1024):
                raise ValueError()
            content=path.read_bytes()
            if len(content)>128*1024*1024 or hashlib.sha256(content).hexdigest()!=asset['sha256']:
                raise ValueError()
            result[role]=content
    except Exception:
        raise WorkerFailure('model_verification_failed') from None
    return result


def validate_runtime(manifest, version):
    if (manifest.get('rapidocr_version')!=SUPPORTED_RAPIDOCR
            or manifest.get('onnxruntime_version')!=SUPPORTED_ORT
            or version('rapidocr')!=SUPPORTED_RAPIDOCR or version('onnxruntime')!=SUPPORTED_ORT):
        raise WorkerFailure('engine_version_unsupported')
    if {role:asset.get('sha256') for role,asset in manifest.get('models',{}).items()}!=SUPPORTED_ASSETS:
        raise WorkerFailure('model_verification_failed')


def capture_regions(det, rec, mapped_boxes):
    """Capture BEFORE empty/low-score output filtering; never truncate zip()."""
    texts=rec.txts if rec.txts is not None else ()
    scores=rec.scores if rec.scores is not None else ()
    detection=det.scores if det.scores is not None else ()
    count=max(len(mapped_boxes),len(texts),len(scores),len(detection))
    if count>4096: raise WorkerFailure('observation_incomplete')
    regions=[]
    def finite(value):
        number=float(value)
        return number if math.isfinite(number) else None
    for i in range(count):
        regions.append({'text':str(texts[i]) if i<len(texts) else None,
            'confidence':finite(scores[i]) if i<len(scores) else None,
            'detection_confidence':finite(detection[i]) if i<len(detection) else None,
            'polygon':[[finite(c) for c in point] for point in mapped_boxes[i]] if i<len(mapped_boxes) else []})
    return regions, count>0 and len(mapped_boxes)==len(texts)==len(scores)==len(detection)


def run_one(manifest, image_bytes):
    os.environ['ORT_DISABLE_TELEMETRY']='1'
    logging.disable(logging.CRITICAL)
    with offline_guard() as (stack,deny,attempted):
        from importlib.metadata import version
        validate_runtime(manifest,version)
        assets=verified_models(manifest)
        import onnxruntime as ort
        ort.disable_telemetry_events()
        ort.set_default_logger_severity(4)
        import requests
        from rapidocr import RapidOCR
        from rapidocr.utils.download_file import DownloadFile
        from rapidocr.inference_engine.onnxruntime.main import OrtInferSession
        from rapidocr.main import map_boxes_to_original
        from PIL import Image
        stack.enter_context(patch.object(DownloadFile,'run',deny))
        stack.enter_context(patch.object(requests.sessions.Session,'request',deny))
        options=ort.SessionOptions()
        options.log_severity_level=4
        options.intra_op_num_threads=4
        options.inter_op_num_threads=1
        options.enable_cpu_mem_arena=False
        sessions={role:ort.InferenceSession(content,sess_options=options,providers=['CPUExecutionProvider'])
                  for role,content in assets.items()}
        if any(s.get_providers()!=['CPUExecutionProvider'] for s in sessions.values()):
            raise WorkerFailure('observation_incomplete')
        if 'character' not in sessions['rec'].get_modelmeta().custom_metadata_map:
            raise WorkerFailure('model_verification_failed')
        # Private worker-only hook: no OmegaConf object serialization, URL, path
        # fallback or model auto-download. Upstream wrapper runs verified sessions.
        def init_session(self,cfg):
            self.session=sessions[cfg.task_type.value]
        with patch.object(OrtInferSession,'__init__',init_session):
            engine=RapidOCR(params={'Global.log_level':'critical'})
        captured={}
        original=engine.build_final_output
        def capture(img,det,cls,rec,crops,ops):
            boxes=map_boxes_to_original(det.boxes.copy(),ops,*img.shape[:2]) if det.boxes is not None else ()
            captured['regions'],captured['complete']=capture_regions(det,rec,boxes)
            return original(img,det,cls,rec,crops,ops)
        engine.build_final_output=capture
        try:
            if type(image_bytes) is not bytes or not 0<len(image_bytes)<=MAX_IMAGE_BYTES:
                raise ValueError()
            image=Image.open(BytesIO(image_bytes))
            if (image.format not in ('PNG','JPEG') or getattr(image,'n_frames',1)!=1
                    or max(image.size)>16384 or image.width*image.height>20_000_000):
                image.close(); raise ValueError()
            image.load()
        except Exception:
            raise WorkerFailure('invalid_image') from None
        with image:
            engine(image)
            if attempted: raise WorkerFailure('offline_violation')
            return {'status':'observed','width':image.width,'height':image.height,
                'regions':captured.get('regions',[]),'complete':captured.get('complete',False)}


def main():
    if sys.stdin.isatty() or sys.stdout.isatty():
        return 2
    # Preserve only a private IPC descriptor; suppress Python AND native stdout.
    response_fd=os.dup(sys.stdout.fileno())
    with open(os.devnull,'w') as sink:
        os.dup2(sink.fileno(),sys.stdout.fileno())
        os.dup2(sink.fileno(),sys.stderr.fileno())
        try:
            header_size=struct.unpack('>I',sys.stdin.buffer.read(4))[0]
            if not 0<header_size<=65536: raise ValueError()
            manifest=json.loads(sys.stdin.buffer.read(header_size))
            content=sys.stdin.buffer.read(MAX_IMAGE_BYTES+1)
            result=run_one(manifest,content)
        except WorkerFailure as error:
            result={'status':'failed','reason':str(error)}
        except BaseException:
            result={'status':'failed','reason':'observation_incomplete'}
        with os.fdopen(response_fd,'wb') as output:
            output.write(json.dumps(result,allow_nan=False).encode('utf-8'))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
