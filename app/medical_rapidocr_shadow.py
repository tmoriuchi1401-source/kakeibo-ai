"""Opt-in local shadow adapter. No RapidOCR import in the production process."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import subprocess
import struct

from .medical_ocr_observation_shadow import ReceiptImage, failed_observation, make_observation

SUPPORTED_RAPIDOCR = '3.9.2'
SUPPORTED_ORT = '1.29.0'
SUPPORTED_ASSETS = {
    'det':'090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f',
    'cls':'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c',
    'rec':'6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884',
}


@dataclass(frozen=True)
class ModelAsset:
    path: str = field(repr=False)
    sha256: str

    def __post_init__(self):
        if (type(self.path) is not str or not Path(self.path).is_absolute()
                or not re.fullmatch('[0-9a-f]{64}', self.sha256)):
            raise ValueError('invalid local model manifest')


@dataclass(frozen=True)
class RapidOcrManifest:
    detector: ModelAsset
    classifier: ModelAsset
    recognizer: ModelAsset
    rapidocr_version: str = SUPPORTED_RAPIDOCR
    onnxruntime_version: str = SUPPORTED_ORT

    def wire(self):
        return {'rapidocr_version':self.rapidocr_version,'onnxruntime_version':self.onnxruntime_version,
            'models':{role:{'path':asset.path,'sha256':asset.sha256} for role,asset in
                      zip(('det','cls','rec'),(self.detector,self.classifier,self.recognizer))}}


class RapidOcrShadowAdapter:
    """Trusted deployment config; one short-lived worker per immutable image.

    Worker stdout is private IPC, never a log/CLI result. No raw output or exception
    enters this object's repr or failure responses. Executable must be provisioned
    locally with the audited optional dependency set; nothing is installed here.
    """
    def __init__(self, manifest: RapidOcrManifest, *, python_executable: str, timeout_seconds=90):
        if (not Path(python_executable).is_absolute() or python_executable.startswith(('\\\\','//'))
                or not 0 < timeout_seconds <= 300):
            raise ValueError('invalid local worker configuration')
        self.manifest=manifest
        self.executable=python_executable
        self.timeout=timeout_seconds

    def observe(self, image: ReceiptImage):
        if type(image) is not ReceiptImage:
            raise TypeError('one immutable ReceiptImage required')
        try:
            manifest=self.manifest.wire()
            header=json.dumps(manifest).encode('utf-8')
            if len(header)>65536: return failed_observation(image)
            payload=struct.pack('>I',len(header))+header+image.image_bytes
            root=str(Path(__file__).resolve().parents[1])
            bootstrap=f'import sys,runpy;sys.path.insert(0,{root!r});runpy.run_module("app.medical_rapidocr_worker",run_name="__main__")'
            result=subprocess.run([self.executable,'-I','-B','-c',bootstrap],input=payload,
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=self.timeout,
                check=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode or len(result.stdout)>8*1024*1024:
                return failed_observation(image)
            raw=json.loads(result.stdout)
            if raw.get('status')!='observed':
                code=raw.get('reason')
                return failed_observation(image,code if code in {
                    'model_verification_failed','engine_version_unsupported','invalid_image',
                    'observation_incomplete','offline_violation'} else 'observation_incomplete')
            return make_observation(image,'rapidocr/'+self.manifest.rapidocr_version,
                tuple(manifest['models'][role]['sha256'] for role in ('det','cls','rec')),
                raw['width'],raw['height'],raw['regions'],
                ('observation_incomplete',) if not raw.get('complete') else ())
        except Exception:
            return failed_observation(image)
