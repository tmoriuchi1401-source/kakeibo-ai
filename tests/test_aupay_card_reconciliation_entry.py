"""Owner/SHA/dispatch fence and existing encrypted capsule trust boundary."""
import base64
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zlib

import pytest
import yaml

from app import aupay_card_reconciliation_entry as entry
from test_aupay_card_reconciliation_apply import NOW,case
from app.aupay_card_reconciliation_apply import execute_reconciliation


def environment():
    return dict(entry.FLAGS,GITHUB_ACTIONS="true",RUNNER_ENVIRONMENT="github-hosted",RUNNER_OS="Linux",
        GITHUB_REPOSITORY=entry.REPO,GITHUB_EVENT_NAME="workflow_dispatch",GITHUB_REF="refs/heads/main",
        GITHUB_ACTOR=entry.REPO.split("/")[0],GITHUB_TRIGGERING_ACTOR=entry.REPO.split("/")[0],
        GITHUB_RUN_ATTEMPT="1",CONFIRM=entry.CONFIRM,EXPECTED_SHA="a"*40,GITHUB_SHA="a"*40,
        GITHUB_WORKFLOW_SHA="a"*40,KAKEIBO_VALIDATED_MAIN_SHA="a"*40,GITHUB_RUN_ID="41",
        GITHUB_WORKFLOW_REF=f"{entry.REPO}/.github/workflows/{entry.WORKFLOW}@refs/heads/main",
        KAKEIBO_CARD_SCHEDULE_ENABLED="false",KAKEIBO_CORE_SCHEDULE_ENABLED="false",
        KAKEIBO_RECEIPT_SCHEDULE_ENABLED="false",KAKEIBO_PAYPAY_SCHEDULE_ENABLED="false")


class GitHub:
    def __init__(self):self.previous=False;self.competing=False
    def get(self,path):
        current=dict(id=41,head_sha="a"*40,run_attempt=1,status="in_progress",event="workflow_dispatch",
                     path=".github/workflows/"+entry.WORKFLOW)
        if path=="git/ref/heads/main":return {"object":{"sha":"a"*40}}
        if path=="actions/runs/41":return current
        if path.startswith("actions/workflows/"):
            runs=[current]+([dict(id=40,status="completed",conclusion="failure")] if self.previous else [])
            return dict(total_count=len(runs),workflow_runs=runs)
        return dict(total_count=int(self.competing),workflow_runs=[dict(id=2)] if self.competing else [])


def test_one_dispatch_fence_consumes_even_pre_google_failure():
    github=GitHub();entry.boundary(environment(),"a"*40,github)
    github.previous=True
    with pytest.raises(RuntimeError,match="consumed_no_retry"):
        entry.boundary(environment(),"a"*40,github)


@pytest.mark.parametrize("changes",[dict(GITHUB_RUN_ATTEMPT="2"),dict(GITHUB_REF="refs/heads/draft"),
    dict(EXPECTED_SHA="b"*40),dict(KAKEIBO_VALIDATED_MAIN_SHA="b"*40),dict(RUNNER_ENVIRONMENT="self-hosted"),
    dict(GITHUB_EVENT_NAME="schedule"),dict(CONFIRM="APPLY"),dict(RUNNER_DEBUG="1"),
    dict(GEMINI_API_KEY="forbidden"),dict(KAKEIBO_LEGACY_DISABLED="false")])
def test_wrong_release_or_authority_denied_before_google(changes):
    with pytest.raises(RuntimeError):entry.boundary(environment()|changes,"a"*40,GitHub())


def test_competing_writer_is_denied():
    github=GitHub();github.competing=True
    with pytest.raises(RuntimeError,match="competing_run"):
        entry.boundary(environment(),"a"*40,github)


def capsule(record,att,key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw=entry.encode(record)
    signature=key.sign(entry.encode(att),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256())
    value=dict(record=base64.b64encode(raw).decode(),attestation=att,signature=base64.b64encode(signature).decode())
    aes=b"k"*32;nonce=b"n"*12
    sealed=dict(key=base64.b64encode(key.public_key().encrypt(aes,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
        algorithm=hashes.SHA256(),label=entry.LABEL))).decode(),nonce=base64.b64encode(nonce).decode(),
        data=base64.b64encode(AESGCM(aes).encrypt(nonce,zlib.compress(entry.encode(value)),entry.LABEL)).decode())
    return base64.b64encode(entry.encode(sealed)).decode()


@pytest.mark.parametrize("change",[None,"not_approved","tree","record","expired","scope"])
def test_signed_capsule_cannot_substitute_owner_approval_or_fixed_record(monkeypatch,change):
    from cryptography.hazmat.primitives.asymmetric import rsa
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    record={"synthetic":"private-fixed-plan"};digest=hashlib.sha256(entry.encode(record)).hexdigest()
    monkeypatch.setattr(entry,"RECORD_SHA256",digest)
    att=dict(record_sha256=digest,code_tree_sha256="b"*40,owner_approved=True,
        scope="fixed_card_import_reconciliation_only",approval=dict(expires_at=(NOW+timedelta(seconds=240)).isoformat()))
    if change=="not_approved":att["owner_approved"]=False
    if change=="tree":att["code_tree_sha256"]="c"*40
    if change=="record":record["synthetic"]="substituted"
    if change=="expired":att["approval"]["expires_at"]=(NOW-timedelta(seconds=1)).isoformat()
    if change=="scope":att["scope"]="receipt_or_other_finance"
    sealed=capsule(record,att,key)
    if change:
        with pytest.raises(RuntimeError):entry.decode_capsule(sealed,key,"b"*40,NOW)
    else:assert entry.decode_capsule(sealed,key,"b"*40,NOW)==(record,att)


@pytest.mark.parametrize("change",["ledger","source","documents","approval"])
def test_changed_preconditions_never_reserve_or_write(tmp_path,change):
    p=case(tmp_path)
    if change=="ledger":p.db.rows[0][6]+=1
    if change=="source":
        def changed():raise RuntimeError("changed original")
        p.args["source_check"]=changed
    if change=="documents":p.documents["review"]["old"]="changed input"
    if change=="approval":
        provider=p.args["approval_provider"]
        value=json.loads(provider.path.read_bytes());value["batch_ref"]="writer-batch-v1:"+"0"*32
        provider.path.write_text(json.dumps(value))
    with pytest.raises((RuntimeError,AssertionError)):execute_reconciliation(**p.args)
    assert not p.nt.writes and not p.pt.writes and p.db.calls==0


def test_workflow_has_shared_lock_no_schedule_only_existing_secrets():
    path=Path(entry.ROOT)/".github/workflows"/entry.WORKFLOW
    workflow=yaml.load(path.read_text(encoding="utf-8"),Loader=yaml.BaseLoader)
    assert set(workflow["on"])=={"workflow_dispatch"}
    assert workflow["concurrency"]==dict(group="kakeibo-production",**{"cancel-in-progress":"false","queue":"max"})
    assert workflow["permissions"]==dict(contents="read",actions="read")
    steps=workflow["jobs"]["repair"]["steps"]
    assert all("secrets." not in json.dumps(step) for step in steps[:-1])
    # Existing bank/Amazon state bindings are GET-only readiness guards.
    assert "BANK_PDF_RECURRING_AUTHORITY_JSON" not in steps[-1]["env"] and "GEMINI_API_KEY" not in steps[-1]["env"]
    assert all(not any(x in json.dumps(s) for x in ("upload-artifact","continue-on-error")) for s in steps)
    result=subprocess.run([sys.executable,"-S","-B","-c","import app.aupay_card_reconciliation_entry"],capture_output=True,text=True)
    assert result.returncode==0 and not result.stdout and not result.stderr
