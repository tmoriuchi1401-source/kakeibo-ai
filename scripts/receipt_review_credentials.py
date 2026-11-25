"""User-bound Windows DPAPI storage; no plaintext refresh token on disk."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def protect(raw, *, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('windows_dpapi_required')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; never use machine-wide protection.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('owner_dpapi_unavailable')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def save_owner(path, value):
    encrypted = protect(json.dumps(value).encode('utf-8'))
    envelope = {'format': 'receipt-owner-dpapi-v1', 'ciphertext': base64.b64encode(encrypted).decode('ascii')}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(envelope, out)
        out.flush()
        os.fsync(out.fileno())


def load_owner(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if set(value) != {'format', 'ciphertext'} or value['format'] != 'receipt-owner-dpapi-v1':
        raise ValueError('protected_owner_credential_required')
    return json.loads(protect(base64.b64decode(value['ciphertext'], validate=True), decrypt=True))
