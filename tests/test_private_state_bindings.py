import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.drive_run_state import StateError
from app.private_state_bindings import VARIABLES, decode_environment, unwrap, wrap


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("ascii")


def test_ciphertext_environment_roundtrip_without_plaintext_export(key, monkeypatch, capsys):
    values = {name: "private-synthetic-" + str(i) for i, name in enumerate(VARIABLES)}
    encrypted = {name: wrap(name, value, key) for name, value in values.items()}
    original = dict(encrypted)
    monkeypatch.setattr("app.settings.service_account_source", lambda: (None, {"private_key": key}))
    target = "amazon-order:0123456789abcdef"
    decoded, decoded_target = decode_environment(encrypted, canary_target=wrap("AMAZON_TARGET", target, key))
    assert decoded == values and decoded_target == target and encrypted == original
    assert all(value not in json.dumps(encrypted) for value in values.values())
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", ["plaintext", "corrupt", "wrong_label", "wrong_key", "missing"])
def test_invalid_ciphertext_fails_closed_without_sensitive_error(key, failure):
    value = "private-synthetic-file-id"
    ciphertext = wrap(VARIABLES[0], value, key)
    name, selected_key = VARIABLES[0], key
    if failure == "plaintext": ciphertext = value
    if failure == "corrupt": ciphertext = ciphertext[:-5] + "AAAAA"
    if failure == "wrong_label": name = VARIABLES[1]
    if failure == "wrong_key": selected_key = "invalid-private-key"
    if failure == "missing": ciphertext = ""
    with pytest.raises(StateError, match="^private_binding_decode_failed$"):
        unwrap(name, ciphertext, selected_key)


def test_missing_binding_stops_before_returning_partial_environment(key, monkeypatch):
    monkeypatch.setattr("app.settings.service_account_source", lambda: (None, {"private_key": key}))
    with pytest.raises(StateError, match="^private_binding_decode_failed$"):
        decode_environment({VARIABLES[0]: wrap(VARIABLES[0], "synthetic-file-id", key)})


def test_invalid_canary_reference_cannot_be_wrapped(key):
    with pytest.raises(StateError, match="private_binding_value_invalid"):
        wrap("AMAZON_TARGET", "order-with-private-data", key)


def test_projection_folder_uses_its_own_optional_encrypted_binding(key):
    name="KAKEIBO_PROJECTION_FOLDER_ID"
    encrypted=wrap(name,"synthetic-folder-id",key)
    assert unwrap(name,encrypted,key)=="synthetic-folder-id"
    assert name not in VARIABLES  # Existing installations keep their required set.
