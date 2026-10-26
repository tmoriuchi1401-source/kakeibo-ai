"""Opt-in encrypted diagnostic delivery; no credentials, Google calls or writers."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import re

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .drive_run_state import StateError, _external_directory

RECIPIENT_ENV = "KAKEIBO_AUTHORITY_RECIPIENT"
MAX_BYTES = 65536
LABEL = b"kakeibo-authority-report-v1"
ARTIFACT_PATH = Path("kakeibo-authority-delivery") / "authority-report.enc.json"


def _public(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise ValueError()
    key = serialization.load_der_public_key(base64.b64decode(value, validate=True))
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 3072:
        raise ValueError()
    return key


def validate_delivery(env, mode, scope):
    if not env.get(RECIPIENT_ENV):
        return
    try:
        owner = env.get("GITHUB_REPOSITORY_OWNER")
        if (mode != "preview" or scope != "drive_bank" or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or not owner or env.get("GITHUB_ACTOR") != owner
                or env.get("GITHUB_TRIGGERING_ACTOR", owner) != owner):
            raise ValueError()
        _public(env[RECIPIENT_ENV])
    except Exception:
        raise StateError("authority_delivery_requires_bank_preview_and_owner_key") from None


def _der(key):
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _metadata(recipient, run_id, attempt, head):
    if not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError()
    return dict(version=1, recipient=recipient, run_id=run_id, run_attempt=attempt, head_sha=head)


def _aad(metadata):
    return json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("ascii")


def _padding():
    return padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=LABEL)


def _write(path, data):
    path = Path(path)
    _external_directory(path.parent)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
    return path


def seal_report(payload: bytes, env: dict, directory: Path):
    """Write only ciphertext into the exact directory uploaded by the workflow."""
    try:
        validate_delivery(env, "preview", "drive_bank")
        key = _public(env[RECIPIENT_ENV])
        if not payload or len(payload) > MAX_BYTES:
            raise ValueError()
        metadata = _metadata(hashlib.sha256(_der(key)).hexdigest(), env["GITHUB_RUN_ID"],
                             env["GITHUB_RUN_ATTEMPT"], env["GITHUB_SHA"])
        secret, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        encoded = lambda value: base64.b64encode(value).decode("ascii")
        envelope = dict(metadata, wrapped_key=encoded(key.encrypt(secret, _padding())),
                        nonce=encoded(nonce), ciphertext=encoded(AESGCM(secret).encrypt(nonce, payload, _aad(metadata))))
        root = _external_directory(Path(directory))
        (root / ARTIFACT_PATH.parent).mkdir(mode=0o700, exist_ok=False)
        return _write(root / ARTIFACT_PATH, json.dumps(envelope, sort_keys=True).encode("ascii"))
    except Exception:
        raise StateError("authority_report_encryption_failed") from None


def generate_recipient(private_path, public_path, password):
    """Owner-side only: password and encrypted private key never go to Actions."""
    if len(password) < 16:
        raise ValueError()
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password))
    _write(private_path, private)
    _write(public_path, base64.b64encode(_der(key.public_key())))


def recover_report(envelope_path, private_path, output_path, password, *, run_id, attempt, head):
    """Decrypt only the expected run/head to a new external local file."""
    try:
        envelope_path, private_path = Path(envelope_path), Path(private_path)
        if envelope_path.stat().st_size > 2 * MAX_BYTES + 8192:
            raise ValueError()
        _external_directory(private_path.parent)
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=password)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 3072:
            raise ValueError()
        expected = _metadata(hashlib.sha256(_der(key.public_key())).hexdigest(), run_id, attempt, head)
        envelope = json.loads(envelope_path.read_bytes())
        if set(envelope) != set(expected) | {"wrapped_key", "nonce", "ciphertext"} or any(envelope[k] != v for k,v in expected.items()):
            raise ValueError()
        decoded = lambda name: base64.b64decode(envelope[name], validate=True)
        secret = key.decrypt(decoded("wrapped_key"), _padding())
        payload = AESGCM(secret).decrypt(decoded("nonce"), decoded("ciphertext"), _aad(expected))
        if not payload or len(payload) > MAX_BYTES or not isinstance(json.loads(payload), dict):
            raise ValueError()
        return _write(output_path, payload)
    except Exception:
        raise StateError("authority_report_recovery_failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    generate = commands.add_parser("keygen")
    generate.add_argument("--private-key", required=True)
    generate.add_argument("--public-key", required=True)
    recover = commands.add_parser("recover")
    for name in ("envelope", "private-key", "output", "expected-run-id", "expected-attempt", "expected-sha"):
        recover.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        password = getpass.getpass("Local private-key passphrase: ").encode("utf-8")
        if args.operation == "keygen":
            if password != getpass.getpass("Repeat passphrase: ").encode("utf-8"):
                raise ValueError()
            generate_recipient(args.private_key, args.public_key, password)
        else:
            recover_report(args.envelope, args.private_key, args.output, password,
                run_id=args.expected_run_id, attempt=args.expected_attempt, head=args.expected_sha)
        print(json.dumps({"success":True}))
    except Exception:
        print(json.dumps({"success":False,"error":"authority_report_local_operation_failed"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
