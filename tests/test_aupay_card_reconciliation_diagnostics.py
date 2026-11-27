"""Offline faults at the reservation boundary; no network or production authority."""
import json
import sys
from urllib.error import HTTPError

import pytest

from app import aupay_card_reconciliation_entry as entry
from app import aupay_card_reconciliation_apply as repair
from app.aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics, exception_summary
from app.aupay_card_window_preview import Stop
from app.drive_run_state import StateError, validate
from test_aupay_card_reconciliation_apply import case, C0


def http_error(status=503):
    return HTTPError("https://SECRET.invalid/private", status, "SECRET response body", {"Authorization":"SECRET"}, None)


@pytest.mark.parametrize("fault,stage,parent_confirmed", [
    ("native_ack_loss", "native_write", False),
    ("native_transport_readback", "native_transport_readback", False),
    ("native_independent_readback", "native_independent_readback", False),
    ("parent_guard", "parent_guard", False),
    ("parent_prewrite_read", "parent_prewrite_read", False),
    ("parent_prewrite_changed", "parent_prewrite_read", False),
    ("parent_write_rejected", "parent_write", False),
    ("parent_ack_loss", "parent_write", True),
    ("parent_transport_readback", "parent_transport_readback", True),
    ("parent_independent_readback", "parent_independent_readback", True),
])
def test_precise_boundary_unknown_is_never_permission_to_replay(tmp_path, fault, stage, parent_confirmed):
    p=case(tmp_path);trace=ReconciliationDiagnostics();p.args["diagnostics"]=trace
    if fault=="native_ack_loss":p.nt.mode="unknown"
    elif fault=="native_transport_readback":
        original=p.nt.read
        def failed():
            if p.nt.writes:
                try:raise http_error()
                except HTTPError:raise StateError("state_drive_read_failed_503") from None
            return original()
        p.nt.read=failed
    elif fault=="native_independent_readback":
        p.args["native_read"]=lambda:b"SECRET mismatch" if p.nt.writes else p.nt.read()
    elif fault=="parent_guard":
        def failed():
            if p.nt.writes:raise Stop("repair_competing_run")
        p.args["guard"]=failed
    elif fault=="parent_prewrite_read":
        original=p.pt.read
        def failed():
            if p.nt.writes:raise http_error()
            return original()
        p.pt.read=failed
    elif fault=="parent_write_rejected":
        def failed(_payload):raise http_error(403)
        p.pt.write=failed
    elif fault=="parent_prewrite_changed":
        original=p.pt.read
        p.pt.read=lambda:b"SECRET drift" if p.nt.writes else original()
    elif fault=="parent_ack_loss":p.pt.mode="unknown"
    elif fault=="parent_transport_readback":
        original=p.pt.read
        p.pt.read=lambda:b"SECRET mismatch" if p.pt.writes else original()
    elif fault=="parent_independent_readback":
        p.args["parent_read"]=lambda:b"SECRET mismatch" if p.pt.writes else p.pt.read()
    with pytest.raises(Exception) as caught:repair.execute_reconciliation(**p.args)
    report=trace.report(caught.value)
    assert report["last_stage"]==stage
    assert report["effects"]["financial"]=="not_reached"
    assert report["effects"]["checkpoint"]=="not_started"
    assert "SECRET" not in json.dumps(report)
    assert p.db.calls==0 and len(p.nt.writes)==1
    assert bool(p.pt.writes)==parent_confirmed
    native=validate(p.nt.payload,p.cb)
    assert native["generation"]==122 and native["phase"]=="pending"
    assert repair._rows(native,"recurring.sqlite3","recurring_checkpoint")[0]["successful_window_end"]==C0
    assert [r["state"] for r in repair._rows(native,"capabilities.sqlite3","batch_capabilities")]==["issued"]
    assert not repair._rows(native,"journal.sqlite3","batch_events")
    summaries=[json.loads(r["summary_json"]) for r in repair._rows(native,"recurring.sqlite3","recurring_runs")]
    assert any(r.get("status")=="UNKNOWN_NO_REPLAY" and r["replay_allowed"] is False for r in summaries)
    assert any(r.get("status")=="REPAIR_RESERVED" and r["replay_allowed"] is False for r in summaries)
    if fault=="native_transport_readback":
        assert report["http_status"]==503 and report["error_code"]=="state_drive_read_failed_503"
    if fault=="parent_write_rejected":assert report["http_status"]==403
    if fault=="parent_prewrite_read":assert report["effects"]["parent_reservation"]=="not_started"
    if fault=="parent_prewrite_changed":assert report["error_code"]=="ledger_changed_since_read"
    # Reporting the error cannot release a pending state or invoke any writer.
    assert p.db.calls==0 and validate(p.nt.payload,p.cb)["phase"]=="pending"


@pytest.mark.parametrize("error,kind,code,status", [
    (Stop("repair_acl_changed"), "guard_rejected", "repair_acl_changed", None),
    (StateError("ledger_readback_failed"), "state_error", "ledger_readback_failed", None),
    (TimeoutError("SECRET URL token row"), "timeout", "repair_failed_closed", None),
    (RuntimeError("private_but_safe_looking_code"), "unexpected_exception", "repair_failed_closed", None),
    (ValueError("SECRET"), "validation_error", "repair_failed_closed", None),
    (http_error(429), "http_error", "repair_failed_closed", 429),
    (KeyboardInterrupt("SECRET"), "interrupted", "repair_failed_closed", None),
    (SystemExit("SECRET"), "system_exit", "repair_failed_closed", None),
])
def test_exception_output_is_closed_vocabulary_only(error,kind,code,status):
    assert exception_summary(error)==dict(error_code=code,exception_class=kind,http_status=status)
    assert "SECRET" not in json.dumps(exception_summary(error))


def test_no_exception_string_or_class_name_or_context_cycle_is_printed():
    class SECRET_Error(Exception):
        def __str__(self):raise AssertionError("must never stringify")
        def __repr__(self):raise AssertionError("must never repr")
    error=SECRET_Error("SECRET");error.__context__=error
    report=exception_summary(error)
    assert report==dict(error_code="repair_failed_closed",exception_class="unexpected_exception",http_status=None)
    trace=ReconciliationDiagnostics()
    with pytest.raises(ValueError):trace.mark("SECRET")
    assert trace.report(error)["last_stage"]=="entry_start"


@pytest.mark.parametrize("status", [403,429,503])
def test_suppressed_google_http_cause_exposes_only_status(status):
    from httplib2 import Response
    from googleapiclient.errors import HttpError
    from app.aupay_card_window_evidence import EvidenceError
    try:
        try:raise HttpError(Response(dict(status=str(status))),b'{"error":{"message":"SECRET"}}',uri="https://SECRET.invalid")
        except HttpError:raise EvidenceError("evidence_read_failed_no_retry") from None
    except EvidenceError as error:
        result=exception_summary(error)
    assert result==dict(error_code="evidence_read_failed_no_retry",exception_class="evidence_error",http_status=status)
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("error", [http_error(503), KeyboardInterrupt("SECRET"), SystemExit("SECRET")])
def test_actual_entry_main_keeps_unknown_no_replay_and_suppresses_payload(monkeypatch,capsys,error):
    monkeypatch.setattr(sys,"argv",["entry"])
    monkeypatch.setattr(entry.subprocess,"check_output",lambda *a,**k:"a"*40)
    monkeypatch.setattr(entry,"GitHub",lambda env:object())
    def failed(env,head,github,diagnostics):
        diagnostics.mark("parent_guard", "native_reservation", "readback_confirmed")
        print("SECRET stdout")
        print("SECRET stderr",file=sys.stderr)
        raise error
    monkeypatch.setattr(entry,"run",failed)
    assert entry.main()==1
    output=capsys.readouterr()
    assert not output.err and "SECRET" not in output.out
    report=json.loads(output.out)
    assert report["outcome"]=="UNKNOWN_NO_REPLAY" and report["automatic_retry"] is False and report["rollback"] is False
    assert report["diagnostics"]["last_stage"]=="parent_guard"
    assert report["diagnostics"]["effects"]["financial"]=="not_reached"


def test_confirmed_success_is_reported_only_after_all_existing_proofs(tmp_path):
    p=case(tmp_path);trace=ReconciliationDiagnostics();p.args["diagnostics"]=trace
    assert repair.execute_reconciliation(**p.args)["success"] is True
    result=trace.report()
    assert result["last_stage"]=="complete"
    assert result["effects"]["financial"]=="exact_readback_confirmed"
    assert all(v=="readback_confirmed" for k,v in result["effects"].items() if k!="financial")
    assert result["exception_class"]=="none" and result["http_status"] is None
