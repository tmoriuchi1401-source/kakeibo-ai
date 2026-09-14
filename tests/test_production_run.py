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
