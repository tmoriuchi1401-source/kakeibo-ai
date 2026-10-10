import json

import pytest

from app.production_run import DEPENDENCIES, execute_serial, require_success, run_durable_source
from app.drive_run_state import StateError


def runners(calls, fail=None):
    def run(name):
        calls.append(name)
        if name == fail:
            raise RuntimeError("synthetic-secret-and-private-document-name")
        return {"written": 1, "amount": 12345, "filename": "synthetic-private-name"}
    return {name: lambda name=name: run(name) for name in DEPENDENCIES}


def test_normal_serial_order_and_safe_count_only_summary():
    calls = []
    result = execute_serial(runners(calls))
    assert calls == list(DEPENDENCIES)
    assert result["success"]
    assert all(value["counts"] == {"written": 1} for value in result["sources"].values())
    assert "synthetic-private-name" not in json.dumps(result)
    assert "12345" not in json.dumps(result)


def test_bank_hold_summary_allows_only_safe_refs_statuses_reasons():
    safe = {"file_ref": "a" * 24, "status": "withheld", "reasons": ["transaction_review"]}
    report = execute_serial({"bank": lambda: {"files_withheld": 4, "parse_failed": 1,
        "outside_write_window": 4, "collision": 0, "unresolved_income": 3,
        "files_processed": 0, "file_statuses": [safe,
            {**safe, "filename": "PRIVATE"}, {**safe, "reasons": ["PRIVATE"]},
            {**safe, "file_ref": "PRIVATE"}, {**safe, "status": []}]}},
        selected_sources=frozenset({"bank"}))
    bank = report["sources"]["bank"]
    assert bank["file_statuses"] == [safe]
    assert bank["counts"]["files_withheld"] == 4
    assert bank["counts"]["unresolved_income"] == 3
    assert "PRIVATE" not in json.dumps(report)


def test_independent_sources_continue_but_dependent_accounting_stops():
    calls = []
    result = execute_serial(runners(calls, fail="amazon"))
    assert calls == ["amazon", "receipts", "aupay_balance", "paypay"]
    assert result["sources"]["aupay_card"]["status"] == "skipped"
    assert result["sources"]["auto_expense"]["status"] == "skipped"
    assert result["success"] is False
    assert "private" not in json.dumps(result)


def test_postprocessing_failure_keeps_run_failed_and_allows_independent_refresh():
    calls = []
    result = execute_serial(runners(calls, fail="review_refresh"))
    assert calls == list(DEPENDENCIES)
    assert result["sources"]["expenses_refresh"]["status"] == "success"
    assert result["success"] is False


def test_scheduled_source_selection_keeps_failures_inside_selected_pipeline():
    calls = []
    selected = frozenset({"paypay"})
    result = execute_serial(runners(calls, fail="paypay"), selected_sources=selected)
    assert calls == ["paypay"]
    assert set(result["sources"]) == selected
    assert not result["success"]


def test_scheduled_source_selection_runs_only_requested_source():
    calls = []
    result = execute_serial(runners(calls), selected_sources=frozenset({"bank"}))
    assert calls == ["bank"]
    assert result["success"]


def test_bank_catch_up_is_reported_and_blocks_accounting():
    calls = []
    sources = runners(calls)
    sources["bank"] = lambda: {"catch_up_pending": 1, "written": 0}
    report = execute_serial(sources)
    assert report["sources"]["bank"]["status"] == "partial"
    assert report["sources"]["bank"]["counts"]["catch_up_pending"] == 1
    assert report["sources"]["review_apply"]["status"] == "skipped"
    assert not report["success"]

    history = {name: {"phase": "ready", "counts": {}} for name in DEPENDENCIES}
    history["bank"]["counts"]["catch_up_pending"] = 1
    core = execute_serial(runners([]), history=history,
                          selected_sources=frozenset(DEPENDENCIES) - {"receipts", "paypay", "bank"},
                          require_omitted_ready=True)
    assert core["sources"]["review_apply"]["status"] == "skipped"
    assert not core["success"]


def test_core_accounting_waits_for_omitted_drive_source_to_be_ready():
    calls = []
    selected = frozenset(DEPENDENCIES) - {"receipts", "paypay", "bank"}
    history = {name: {"phase": "ready"} for name in DEPENDENCIES}
    history["receipts"]["phase"] = "pending"
    result = execute_serial(runners(calls), history=history, selected_sources=selected,
                            require_omitted_ready=True)
    assert calls == ["amazon", "aupay_balance"]
    assert result["sources"]["aupay_card"]["status"] == "skipped"
    assert result["sources"]["review_apply"]["status"] == "skipped"
    assert not result["success"]


@pytest.mark.parametrize("result", [{"failure": 1}, {"errors": 1}, {"failed_files": 2}, {"status": "failed"}, {"errors": "0"}, [], {}, {"status": "unexpected"}])
def test_reported_errors_are_not_mistaken_for_success(result):
    with pytest.raises(StateError):
        require_success(result)


def test_source_failure_never_commits_checkpoint():
    calls = []
    class Store:
        def restore(self, directory): calls.append("restore")
        def begin(self, attempt): calls.append("begin")
        def commit(self, directory): calls.append("commit")
    with pytest.raises(StateError):
        run_durable_source(Store(), "synthetic", lambda _path: {"failure": 1}, apply=True)
    assert calls == ["restore", "begin"]


def test_preview_never_writes_durable_state():
    calls = []
    class Store:
        def restore(self, directory): calls.append("restore")
        def begin(self, attempt): raise AssertionError("preview write")
        def commit(self, directory): raise AssertionError("preview write")
    assert run_durable_source(Store(), "synthetic", lambda _path: {"written": 0}, apply=False) == {"written": 0}
    assert calls == ["restore"]


@pytest.mark.parametrize('message,expected',[
    ('confirmation_sheet_header_mismatch','confirmation_sheet_header_mismatch'),
    ('confirmation_unknown_ui_identity','confirmation_unknown_ui_identity'),
    ('private document or API response','source_execution_failed'),
    ('confirmation_unknown_ui_identity private data','source_execution_failed'),
])
def test_only_exact_allowlisted_state_codes_reach_summary(message,expected):
    calls=[];sources=runners(calls)
    def fail():raise StateError(message)
    sources['receipts']=fail
    report=execute_serial(sources)
    assert report['sources']['receipts']['error']==expected
    assert 'private' not in json.dumps(report)


def test_pdf_stage_diagnostics_cross_boundary_without_exception_content():
    from app.production_run import SourceFailure
    calls=[];sources=runners(calls)
    def fail():
        raise SourceFailure('pdf_intake_drive_unavailable','pdf_page_intake',{'written':0},
                            failure_class='StateError',failure_site='pdf_intake_registry.py:153')
    sources['receipts']=fail
    value=execute_serial(sources)['sources']['receipts']
    assert value['stage']=='pdf_page_intake' and value['failure_class']=='StateError'
    assert value['failure_site']=='pdf_intake_registry.py:153'
    rejected=SourceFailure('private token','private token',{},failure_class='private token',failure_site='https://secret:123')
    assert rejected.report()=={'failure':1,'error':'source_execution_failed'}
