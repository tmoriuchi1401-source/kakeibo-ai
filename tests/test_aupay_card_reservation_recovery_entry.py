"""No-network release/capsule tests for the independent recovery entry."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from app import aupay_card_reconciliation_entry as old
from app import aupay_card_reservation_recovery_entry as entry
from app.aupay_card_reservation_recovery import RECOVERY_WORKFLOW
from test_aupay_card_reconciliation_entry import environment,capsule
from test_aupay_card_reconciliation_apply import NOW


def env():
    return environment()|dict(CONFIRM=entry.CONFIRM,GITHUB_WORKFLOW_REF=f"{old.REPO}/.github/workflows/{RECOVERY_WORKFLOW}@refs/heads/main")


class GitHub:
    def __init__(self):self.prior=[];self.competing=False
    def get(self,path):
        current=dict(id=41,head_sha="a"*40,run_attempt=1,status="in_progress",event="workflow_dispatch",
            path=".github/workflows/"+RECOVERY_WORKFLOW)
        if path=="git/ref/heads/main":return dict(object=dict(sha="a"*40))
        if path=="actions/runs/41":return current
        if path.startswith("actions/workflows/"):return dict(total_count=1+len(self.prior),workflow_runs=[current]+self.prior)
        return dict(total_count=int(self.competing),workflow_runs=[dict(id=2)] if self.competing else [])


def test_old_entry_remains_consumed_new_release_is_independent():
    github=GitHub();entry.boundary(env(),"a"*40,github)
    github.prior=[dict(id=40,head_sha="b"*40,status="completed",conclusion="failure",run_attempt=1)]
    entry.boundary(env(),"a"*40,github) # Runtime still requires its signed ancestry.
    github.prior[0]["head_sha"]="a"*40
    with pytest.raises(RuntimeError,match="consumed_no_retry"):entry.boundary(env(),"a"*40,github)


@pytest.mark.parametrize("changes",[dict(GITHUB_RUN_ATTEMPT="2"),dict(CONFIRM=old.CONFIRM),
    dict(GITHUB_WORKFLOW_REF=f"{old.REPO}/.github/workflows/{old.WORKFLOW}@refs/heads/main"),
    dict(KAKEIBO_VALIDATED_MAIN_SHA="b"*40),dict(RUNNER_ENVIRONMENT="self-hosted"),dict(RUNNER_DEBUG="1")])
def test_rerun_old_authority_or_unvalidated_release_denied(changes):
    with pytest.raises(RuntimeError):entry.boundary(env()|changes,"a"*40,GitHub())


def test_runtime_ancestry_requires_every_old_unknown_and_every_prior_dispatch():
    first=dict(id=10,head_sha="d"*40,run_attempt=1,status="completed",conclusion="failure")
    second=dict(id=40,head_sha="b"*40,run_attempt=1,status="completed",conclusion="failure")
    record=dict(old_failure=second,recovery=dict(previous_record=dict(old_failure=first)))
    class History(GitHub):
        def get(self,path):
            if path=="actions/runs/10":return first
            if path=="actions/runs/40":return second
            return super().get(path)
    h=History();h.prior=[second]
    old.verify_failure_ancestry(record,env(),h)
    first["run_attempt"]=2
    # A later API rerun cannot match the immutable signed attempt1 record.
    record["recovery"]["previous_record"]["old_failure"]=dict(first,run_attempt=1)
    with pytest.raises(RuntimeError):old.verify_failure_ancestry(record,env(),h)
    first["run_attempt"]=1;h.prior.append(dict(id=99,head_sha="e"*40,status="completed",conclusion="failure",run_attempt=1))
    with pytest.raises(RuntimeError):old.verify_failure_ancestry(record,env(),h)


def test_recovery_capsule_has_separate_domain_and_scope(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    record=dict(synthetic="bound-new-approval");sha=hashlib.sha256(old.encode(record)).hexdigest()
    att=dict(owner_approved=True,record_sha256=sha,code_tree_sha256="b"*40,scope=entry.SCOPE,
        approval=dict(expires_at=(NOW+timedelta(seconds=240)).isoformat()))
    monkeypatch.setattr(old,"LABEL",entry.LABEL)
    sealed=capsule(record,att,key)
    assert old.decode_capsule(sealed,key,"b"*40,NOW,record_sha256=sha,label=entry.LABEL,scope=entry.SCOPE)==(record,att)
    for changes in (dict(label=b"kakeibo-card-reconciliation-v1"),dict(scope="fixed_card_import_reconciliation_only"),dict(record_sha256="0"*64)):
        kwargs=dict(record_sha256=sha,label=entry.LABEL,scope=entry.SCOPE);kwargs.update(changes)
        with pytest.raises(Exception):old.decode_capsule(sealed,key,"b"*40,NOW,**kwargs)


def test_workflow_and_shared_runtime_keep_the_existing_writer_and_secrets(monkeypatch):
    text=(Path(old.ROOT)/".github/workflows"/RECOVERY_WORKFLOW).read_text(encoding="utf-8")
    w=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(w["on"])=={"workflow_dispatch"}
    assert w["concurrency"]==dict(group="kakeibo-production",**{"cancel-in-progress":"false","queue":"max"})
    assert w["permissions"]==dict(contents="read",actions="read")
    assert "app.aupay_card_reservation_recovery_entry --guard-only" in text
    assert "GEMINI_API_KEY" not in text and "BANK_PDF_RECURRING_AUTHORITY_JSON" not in text
    assert entry.CONFIRM in text and "secrets." not in json.dumps(w["jobs"]["repair"]["steps"][:-1])
    calls=[]
    def shared(*args,**kwargs):calls.append(kwargs);return dict(success=True)
    monkeypatch.setattr(old,"run",shared)
    entry.run({},"a"*40,object())
    assert calls[0]["boundary_checker"] is entry.boundary and calls[0]["record_sha256"]==entry.RECORD_SHA256
    assert calls[0]["label"]==entry.LABEL and calls[0]["scope"]==entry.SCOPE
    result=subprocess.run([sys.executable,"-S","-B","-c","import app.aupay_card_reservation_recovery_entry"],capture_output=True,text=True)
    assert result.returncode==0 and not result.stdout and not result.stderr
