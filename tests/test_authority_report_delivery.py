"""Offline recovery/privacy tests only; production services are never called."""
import base64
import json
from pathlib import Path

import pytest

from app import authority_report_delivery as delivery
from app import production_flow as flow
from app.drive_run_state import StateError

PASSWORD = b"synthetic-local-password-only"
PAYLOAD = b'{"bank":{"policy":{"income_accounts":[["synthetic-bank","private-alias"]],"expected_spreadsheet_id":"private-target"}}}'


@pytest.fixture(scope="module")
def recipient(tmp_path_factory):
    root = tmp_path_factory.mktemp("owner-key")
    private, public = root / "owner.pem", root / "owner-public.txt"
    delivery.generate_recipient(private, public, PASSWORD)
    return private, public.read_text()


def environment(recipient):
    return {delivery.RECIPIENT_ENV:recipient[1], "GITHUB_EVENT_NAME":"workflow_dispatch",
        "GITHUB_REPOSITORY_OWNER":"synthetic-owner", "GITHUB_ACTOR":"synthetic-owner",
        "GITHUB_TRIGGERING_ACTOR":"synthetic-owner", "GITHUB_RUN_ID":"123", "GITHUB_RUN_ATTEMPT":"1", "GITHUB_SHA":"a"*40}


def recover(envelope, private, output, **overrides):
    args = dict(run_id="123", attempt="1", head="a"*40)
    args.update(overrides)
    return delivery.recover_report(envelope, private, output, PASSWORD, **args)


def test_encrypted_artifact_recovers_exact_report_without_credentials_or_plaintext(tmp_path, recipient, capsys):
    envelope = delivery.seal_report(PAYLOAD, environment(recipient), tmp_path)
    assert envelope == tmp_path / delivery.ARTIFACT_PATH
    assert list(envelope.parent.iterdir()) == [envelope]
    assert "ENCRYPTED PRIVATE KEY" in recipient[0].read_text()
    assert "private-alias" not in envelope.read_text() and "private-target" not in envelope.read_text()
    assert base64.b64encode(PAYLOAD).decode() not in envelope.read_text()
    output = tmp_path / "recovered.private.json"
    assert recover(envelope, recipient[0], output) == output
    assert output.read_bytes() == PAYLOAD
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("field,value", [("run_id","124"),("attempt","2"),("head","b"*40)])
def test_recovery_rejects_other_run_attempt_or_sha_without_creating_plaintext(tmp_path, recipient, field, value):
    envelope = delivery.seal_report(PAYLOAD, environment(recipient), tmp_path)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(StateError, match="^authority_report_recovery_failed$"):
        recover(envelope, recipient[0], output, **{field:value})
    assert not output.exists()


@pytest.mark.parametrize("field", ["ciphertext","wrapped_key","nonce"])
def test_recovery_rejects_tampering_without_creating_plaintext(tmp_path, recipient, field):
    envelope = delivery.seal_report(PAYLOAD, environment(recipient), tmp_path)
    value = json.loads(envelope.read_text())
    data = bytearray(base64.b64decode(value[field])); data[0] ^= 1
    value[field] = base64.b64encode(data).decode()
    envelope.write_text(json.dumps(value))
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(StateError, match="^authority_report_recovery_failed$"):
        recover(envelope, recipient[0], output)
    assert not output.exists()


def test_wrong_owner_key_or_passphrase_cannot_recover_and_existing_output_is_preserved(tmp_path, recipient):
    envelope = delivery.seal_report(PAYLOAD, environment(recipient), tmp_path)
    other_private, other_public = tmp_path / "other.pem", tmp_path / "other.txt"
    delivery.generate_recipient(other_private, other_public, PASSWORD)
    output = tmp_path / "existing.private.json"
    output.write_bytes(b"keep-existing")
    for key,password in [(other_private,PASSWORD),(recipient[0],b"wrong-password"),(recipient[0],PASSWORD)]:
        with pytest.raises(StateError, match="^authority_report_recovery_failed$"):
            delivery.recover_report(envelope, key, output, password, run_id="123", attempt="1", head="a"*40)
        assert output.read_bytes() == b"keep-existing"


@pytest.mark.parametrize("mode,scope", [("apply","drive_bank"),("preview","core"),("preview","all")])
def test_recipient_cannot_enable_apply_or_other_sources(recipient, mode, scope):
    with pytest.raises(StateError, match="^authority_delivery_requires_bank_preview_and_owner_key$"):
        delivery.validate_delivery(environment(recipient), mode, scope)


@pytest.mark.parametrize("field", ["GITHUB_ACTOR","GITHUB_TRIGGERING_ACTOR"])
def test_other_actor_cannot_use_arbitrary_recipient_to_extract_details(recipient, field):
    env = environment(recipient); env[field] = "other-actor"
    with pytest.raises(StateError): delivery.validate_delivery(env, "preview", "drive_bank")


def test_encryption_rejects_bad_key_and_oversized_report_without_artifact(tmp_path, recipient):
    env = environment(recipient)
    for payload,key in [(PAYLOAD,"not-a-public-key"),(b"x"*(delivery.MAX_BYTES+1),recipient[1])]:
        env[delivery.RECIPIENT_ENV] = key
        with pytest.raises(StateError, match="^authority_report_encryption_failed$"):
            delivery.seal_report(payload, env, tmp_path)
        assert not (tmp_path / delivery.ARTIFACT_PATH).exists()


def test_existing_diagnostic_can_seal_report_and_preserves_its_public_boolean_contract(tmp_path, recipient):
    from test_production_flow import diagnostic_policy
    path, env = diagnostic_policy(tmp_path)
    env.update(environment(recipient))
    result = flow.preview_authority_summary(path.parent.parent, {"bank":{"status":"success"}}, env)
    assert result["bank"]["policy_valid"]
    assert all(type(value) is bool for value in result["bank"].values())
    output = tmp_path / "recovered.private.json"
    recover(tmp_path / delivery.ARTIFACT_PATH, recipient[0], output)
    original = next(tmp_path.glob("kakeibo-authority-*.private.json"))
    assert output.read_bytes() == original.read_bytes()


def test_workflow_uploads_only_ciphertext_for_owner_bank_preview():
    import yaml
    path = Path(flow.REPO) / ".github/workflows/kakeibo-production.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["production"]
    upload = next(s for s in job["steps"] if s.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"]["path"] == "${{ runner.temp }}/kakeibo-authority-delivery/authority-report.enc.json"
    assert upload["with"]["retention-days"] == "1" and upload["with"]["if-no-files-found"] == "error"
    for guard in ["inputs.mode == 'preview'", "inputs.scope == 'drive_bank'", "github.actor == github.repository_owner", "github.triggering_actor == github.repository_owner", "inputs.authority_recipient != ''"]:
        assert guard in upload["if"]
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job["if"]
    assert job["env"][delivery.RECIPIENT_ENV] == "${{ inputs.authority_recipient }}"
    # The disabled partial-recovery path reads live run/queue status. Neither
    # permission can mutate Actions, repository contents or report artifacts.
    assert workflow["permissions"] == {"contents":"read", "actions":"read"}
