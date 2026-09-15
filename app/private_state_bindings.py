"""Protect fixed IDs in non-secret Actions Variables using the existing SA key.

GitHub logs workflow env before running Python. Only ciphertext crosses that
boundary. No new authentication/key, Google call, or GITHUB_ENV plaintext export.
"""
from __future__ import annotations

import base64
import json
import re

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .drive_run_state import StateError


PREFIX = "rsa-oaep-sha256-v1:"
VARIABLES = ("KAKEIBO_STATE_FOLDER_ID", "AMAZON_STATE_FILE_ID", "AUPAY_CARD_STATE_FILE_ID",
             "BANK_STATE_FILE_ID", "KAKEIBO_RUN_LEDGER_FILE_ID")


def _padding(name):
    if name not in (*VARIABLES, "AMAZON_TARGET", "RECEIPT_REIMPORT_FILE_ID"):
        raise StateError("private_binding_name_invalid")
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(),
                        label=("kakeibo-private-binding-v1:" + name).encode("ascii"))


def _valid(name, value):
    pattern = r"amazon-order:[0-9a-f]{16}" if name == "AMAZON_TARGET" else r"[A-Za-z0-9_-]{10,150}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise StateError("private_binding_value_invalid")


def wrap(name: str, value: str, private_key_pem: str) -> str:
    """Operator-side, in memory, with the public half of the current SA key."""
    _valid(name, value)
    key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    return PREFIX + base64.b64encode(key.public_key().encrypt(value.encode("ascii"), _padding(name))).decode("ascii")


def unwrap(name: str, value: str, private_key_pem: str) -> str:
    try:
        if not value.startswith(PREFIX):
            raise StateError("private_binding_ciphertext_required")
        key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
        raw = base64.b64decode(value[len(PREFIX):], validate=True)
        plain = key.decrypt(raw, _padding(name)).decode("ascii")
        _valid(name, plain)
        return plain
    except Exception:
        raise StateError("private_binding_decode_failed") from None


def decode_environment(env: dict, *, canary_target: str = "") -> tuple[dict, str]:
    # Use precisely the existing service-account credential selection.
    from .settings import service_account_source
    from pathlib import Path
    try:
        path, info = service_account_source()
        info = info or json.loads(Path(path).read_bytes())
        key = info["private_key"]
        result = dict(env)
        for name in VARIABLES:
            result[name] = unwrap(name, env.get(name, ""), key)
        target = unwrap("AMAZON_TARGET", canary_target, key) if canary_target else ""
        return result, target
    except Exception:
        raise StateError("private_binding_decode_failed") from None
