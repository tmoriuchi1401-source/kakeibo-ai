"""Pinned public Tesseract weights; downloads occur only during CLI setup."""
from hashlib import sha256
from pathlib import Path

REVISION = 'e12c65a915945e4c28e237a9b52bc4a8f39a0cec'
MODELS = {
    'jpn': '36bdf9ac823f5911e624c30d0553e890b8abc7c31a65b3ef14da943658c40b79',
    'eng': '8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba',
}


def directory():
    from .receipt_local_ocr import model_directory
    return model_directory() / 'tesseract-best'


def verified_directory():
    path = directory()
    for language, expected in MODELS.items():
        model = path / (language + '.traineddata')
        if not model.is_file() or sha256(model.read_bytes()).hexdigest() != expected:
            raise ValueError('medical_locality_model_unverified')
    return path


def prepare():
    import urllib.request
    path = directory(); path.mkdir(parents=True, exist_ok=True)
    for language, expected in MODELS.items():
        target = path / (language + '.traineddata')
        if target.is_file() and sha256(target.read_bytes()).hexdigest() == expected:
            continue
        url = f'https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/{REVISION}/{language}.traineddata'
        with urllib.request.urlopen(url, timeout=60) as response:
            payload = response.read(32_000_001)
        if len(payload) > 32_000_000 or sha256(payload).hexdigest() != expected:
            raise ValueError('medical_locality_model_unverified')
        target.write_bytes(payload)
    verified_directory()
