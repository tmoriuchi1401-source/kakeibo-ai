"""Pinned local OCR. Runtime accepts pixels only and never downloads models."""
from functools import lru_cache
from hashlib import sha256
import os
from pathlib import Path

MODELS={
    'Det':('PP-OCRv6_det_small.onnx','090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f'),
    'Rec':('PP-OCRv6_rec_small.onnx','6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884'),
    'Cls':('ch_ppocr_mobile_v2.0_cls_mobile.onnx','e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c'),
}
VERSION='rapidocr-3.9.2-ppocrv6-small'

def enabled():return os.environ.get('KAKEIBO_LOCAL_OCR_ENABLED')=='true'

def model_directory():
    value=os.environ.get('KAKEIBO_OCR_MODEL_DIR','')
    if not value or not Path(value).is_absolute():raise ValueError('local_ocr_model_directory_required')
    return Path(value)

def parameters(directory):
    return {'Global.model_root_dir':str(directory),'Global.log_level':'error',
            'EngineConfig.onnxruntime.intra_op_num_threads':4,
            'EngineConfig.onnxruntime.inter_op_num_threads':1}

def verify_models(directory):
    for name,digest in MODELS.values():
        path=directory/name
        if not path.is_file() or sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('local_ocr_model_not_verified')

@lru_cache(maxsize=1)
def _engine(directory):
    verify_models(Path(directory))
    from rapidocr import RapidOCR
    params=parameters(directory)
    params.update({kind+'.model_path':str(Path(directory)/name) for kind,(name,_) in MODELS.items()})
    return RapidOCR(params=params)

def read_tokens(image):
    import numpy as np
    from .medical_anonymization import AnonymizationHold
    try:
        if image.width*image.height>20_000_000:raise ValueError
        out=_engine(str(model_directory()))(np.asarray(image.convert('RGB'))[:,:,::-1])
        if out.boxes is None or out.txts is None or out.scores is None:return []
        if not len(out.boxes)==len(out.txts)==len(out.scores)<=2000:raise ValueError
        result=[]
        for i,(box,text,score) in enumerate(zip(out.boxes,out.txts,out.scores)):
            if not str(text).strip():continue
            points=np.asarray(box)
            if points.shape!=(4,2) or not np.isfinite(points).all() or not 0<=float(score)<=1:raise ValueError
            l,t=np.floor(points.min(axis=0)).astype(int);r,b=np.ceil(points.max(axis=0)).astype(int)
            if not 0<=l<r<=image.width or not 0<=t<b<=image.height:raise ValueError
            result.append(dict(text=str(text),box=(int(l),int(t),int(r),int(b)),
                quad=tuple(tuple(float(v) for v in p) for p in points),confidence=float(score)*100,line=(1,i,1)))
        return result
    except Exception:
        raise AnonymizationHold('local_ocr_unavailable') from None

def main():
    import sys
    if sys.argv[1:]!=['prepare']:raise SystemExit(2)
    # Only public weights are fetched during setup, before opening any receipt.
    from rapidocr import RapidOCR
    directory=model_directory();RapidOCR(params=parameters(directory));verify_models(directory)
    from .medical_locality_models import prepare
    prepare()
    print('Local OCR models verified')

if __name__=='__main__':main()
