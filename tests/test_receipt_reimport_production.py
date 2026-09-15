from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.drive_run_state import StateError
from app import production_flow
from app.receipt_reimport_production import ReimportStore, encoded, digest, run_reimport, error_code
from app.receipt_pipeline import ReceiptPipeline
from app.models import ReceiptResult, ReceiptItem
from app.receipt_privacy_gate import ReceiptPrivacyGateResult


class MemoryTransport:
    def __init__(self, payload):
        self.payload=payload; self.writes=0; self.fail_before=False; self.fail_after=False
    def read(self): return self.payload
    def write(self, payload):
        self.writes+=1
        if self.fail_before: raise OSError("synthetic write failure")
        self.payload=payload
        if self.fail_after: raise OSError("synthetic lost response")


def fixtures():
    from hashlib import sha256
    manifest={"spreadsheet_id":"synthetic-sheet", "folder_id":"synthetic-folder",
              "destination":"https://generativelanguage.googleapis.com", "model":"existing-model",
              "sources":[{"source_id":f"synthetic-{i}","version":"1","sha256":sha256(b"fixture").hexdigest(),
                          "mime_type":"image/png","classification":"normal","pages":1} for i in range(10)]}
    value={"schema":"receipt-reimport-v1","manifest":manifest,
           "records":{s["source_id"]:{"phase":"new"} for s in manifest["sources"]}}
    transport=MemoryTransport(encoded(value))
    store=ReimportStore(transport,digest(manifest),"synthetic-sheet")
    parsed=ReceiptResult(date="2026-01-01",merchant="Synthetic shop",total=100,
        items=[ReceiptItem(name="Synthetic item",amount=100,major_category="食費",minor_category="食品")])
    db=Mock(); db.get.return_value=[]; db.categories.return_value=[("食費","食品")]
    pipeline=Mock(); pipeline.reanalyze_bytes.return_value={"status":"analyzed","parsed":parsed.model_dump(),"issues":[]}
    kwargs=dict(pipeline=pipeline,db=db,load_source=Mock(return_value=b"fixture"),verify_source=Mock(),apply=True,
                operation="reanalyze",limit=1,run_id="synthetic-run",api_key_present=True,model="existing-model")
    return store,transport,kwargs


def test_one_real_analysis_is_reused_without_resend_on_restart():
    store,t,kw=fixtures()
    first=run_reimport(store,**kw)
    assert first["analyzed"]==1 and first["remaining"]==9
    assert first["written"]==0
    kw["operation"]="replay";kw["api_key_present"]=False
    fresh=ReimportStore(t,store.expected_digest,store.spreadsheet_id)
    second=run_reimport(fresh,**kw)
    assert second["reused"]==1 and second["analyzed"]==0 and second["written"]==0
    assert kw["pipeline"].reanalyze_bytes.call_count==1
    assert kw["load_source"].call_count==1
    kw["verify_source"].assert_called_once()


def test_preview_does_not_download_analyze_or_save():
    s,t,k=fixtures(); k.update(apply=False,api_key_present=False)
    assert run_reimport(s,**k)["analyzed"]==0
    k["pipeline"].reanalyze_bytes.assert_not_called(); k["load_source"].assert_not_called()
    assert t.writes==0


def test_missing_injected_secret_is_specific_and_no_request_intent():
    s,t,k=fixtures(); k["api_key_present"]=False
    with pytest.raises(StateError,match="gemini_secret_not_injected"): run_reimport(s,**k)
    assert t.writes==0


def test_changed_bytes_cannot_be_sent():
    s,t,k=fixtures(); k["load_source"].return_value=b"changed"
    with pytest.raises(StateError,match="receipt_source_content_changed"): run_reimport(s,**k)
    k["pipeline"].reanalyze_bytes.assert_not_called(); assert t.writes==0


def test_failed_intent_save_cannot_send():
    s,t,k=fixtures(); t.fail_before=True
    with pytest.raises(StateError,match="receipt_result_write_unknown"): run_reimport(s,**k)
    k["pipeline"].reanalyze_bytes.assert_not_called(); assert t.writes==1


def test_unknown_save_response_readbacks_without_second_write():
    s,t,k=fixtures(); t.fail_after=True
    assert run_reimport(s,**k)["analyzed"]==1
    assert t.writes==2  # Exactly one intent and one result, never retry either.


def test_failed_ai_request_is_persisted_and_not_retried():
    s,t,k=fixtures(); k["pipeline"].reanalyze_bytes.side_effect=TimeoutError("private response")
    with pytest.raises(StateError,match="gemini_transport_unknown"): run_reimport(s,**k)
    with pytest.raises(StateError,match="receipt_analysis_reconciliation_required"): run_reimport(s,**k)
    assert k["pipeline"].reanalyze_bytes.call_count==1


def test_saved_analysis_is_not_used_after_source_version_changes():
    s,t,k=fixtures(); run_reimport(s,**k)
    k.update(operation="replay",api_key_present=False)
    k["verify_source"].side_effect=StateError("receipt_source_version_changed")
    with pytest.raises(StateError,match="receipt_source_version_changed"):run_reimport(s,**k)
    assert k["pipeline"].reanalyze_bytes.call_count==1


def test_manifest_cannot_replace_approved_sources_or_target():
    s,t,k=fixtures()
    value=deepcopy(s.value);value["manifest"]["sources"][0]["source_id"]="other-source"
    t.payload=encoded(value)
    with pytest.raises(StateError,match="receipt_manifest_or_result_invalid"):
        ReimportStore(t,s.expected_digest,s.spreadsheet_id)


def test_remaining_batch_does_not_resend_completed_first_receipt():
    s,t,k=fixtures();run_reimport(s,**k)
    k["limit"]=3
    result=run_reimport(s,**k)
    assert result["analyzed"]==3 and result["reused"]==1 and result["remaining"]==6
    assert k["pipeline"].reanalyze_bytes.call_count==4


def test_linux_privacy_difference_is_held_and_not_promoted():
    s,t,k=fixtures(); k["pipeline"].reanalyze_bytes.return_value={"status":"privacy_blocked","classification":"sensitive_unknown"}
    assert run_reimport(s,**k)["privacy_held"]==1
    assert s.value["records"]["synthetic-0"]["phase"]=="held"


@pytest.mark.parametrize("code,expected",[(401,"gemini_auth_rejected"),(403,"gemini_auth_rejected"),
    (429,"gemini_quota_rejected"),(400,"gemini_api_or_model_rejected"),(404,"gemini_api_or_model_rejected")])
def test_safe_error_categories(code,expected):
    assert error_code(SimpleNamespace(code=code))==expected


@pytest.mark.parametrize("apply,operation,key",[(False,"reanalyze",False),(True,"replay",False),(True,"reanalyze",True)])
def test_parent_propagates_existing_key_only_for_approved_analysis(monkeypatch,apply,operation,key):
    called=Mock(return_value=SimpleNamespace(returncode=0,stdout='{"found":10,"failure":0}'))
    monkeypatch.setattr(production_flow.subprocess,"run",called)
    production_flow.invoke("receipt_reimport",apply=apply,env={"GEMINI_API_KEY":"synthetic-secret",
        "GOOGLE_GMAIL_TOKEN_JSON":"synthetic-unused-gmail", "BANK_AUDIT_KEY_JSON":"synthetic-unused-bank",
        "RECEIPT_REIMPORT_OPERATION":operation})
    assert ("GEMINI_API_KEY" in called.call_args.kwargs["env"]) is key
    assert "GOOGLE_GMAIL_TOKEN_JSON" not in called.call_args.kwargs["env"]
    assert "BANK_AUDIT_KEY_JSON" not in called.call_args.kwargs["env"]


@pytest.mark.parametrize("bank,target",[(True,""),(False,"amazon-order:"+"a"*16)])
def test_fixed_receipt_scope_cannot_run_other_sources(bank,target):
    args=SimpleNamespace(scope="receipt_reimport",bank_apply=bank,amazon_target=target,
                         receipt_store="wrapped-test",receipt_manifest="a"*64)
    with pytest.raises(StateError,match="receipt_scope_other_source_forbidden"):
        production_flow.validate_scope(args)


def test_child_error_code_survives_parent_without_api_body(monkeypatch):
    called=Mock(return_value=SimpleNamespace(returncode=1,stdout='{"failure":1,"error":"gemini_quota_rejected"}'))
    monkeypatch.setattr(production_flow.subprocess,"run",called)
    with pytest.raises(StateError,match="^gemini_quota_rejected$"):
        production_flow.invoke("receipt_reimport",apply=True,env={})


def test_pipeline_reanalysis_bypasses_marker_only_and_reuses_ai(monkeypatch):
    from app import receipt_pipeline as module
    s,t,k=fixtures(); db=k["db"]; db.import_ids.return_value={"receipt:synthetic-0"}
    ai=Mock(); ai.client._api_client._http_options.base_url="https://generativelanguage.googleapis.com/"
    ai.analyze_receipt.return_value=ReceiptResult.model_validate(k["pipeline"].reanalyze_bytes.return_value["parsed"])
    gate=ReceiptPrivacyGateResult(classification="normal",extraction_status="extracted",extraction_method="image_ocr",
                                 text_present=True,status="ready_for_gemini",reason_code="normal_receipt_evidence")
    monkeypatch.setattr(module,"evaluate_receipt_privacy",lambda *a,**kw:gate)
    pipeline=ReceiptPipeline(db,ai)
    assert pipeline.process_bytes(b"fixture","image/png","synthetic-0")["reason"]=="already_imported"
    assert pipeline.reanalyze_bytes(b"fixture","image/png","synthetic-0",destination="https://generativelanguage.googleapis.com")["status"]=="analyzed"
    ai.analyze_receipt.assert_called_once(); db.append.assert_not_called()


@pytest.mark.parametrize("classification,reason",[("medical","no_candidate"),("sensitive_unknown","insufficient_evidence"),("payroll","payroll_strong_signal")])
def test_shared_pipeline_never_sends_non_normal(monkeypatch,classification,reason):
    from app import receipt_pipeline as module
    kwargs=dict(classification=classification,extraction_status="extracted",extraction_method="image_ocr",
                text_present=True,status="needs_review" if classification=="medical" else "blocked",reason_code=reason)
    if classification=="medical":kwargs["category"]="医療費"
    gate=ReceiptPrivacyGateResult(**kwargs)
    monkeypatch.setattr(module,"evaluate_receipt_privacy",lambda *a,**kw:gate)
    ai=Mock(); db=Mock()
    result=ReceiptPipeline(db,ai).reanalyze_bytes(b"fixture","image/png","synthetic-0",destination="https://generativelanguage.googleapis.com")
    assert result["status"]=="privacy_blocked"
    ai.analyze_receipt.assert_not_called(); db.append.assert_not_called()
