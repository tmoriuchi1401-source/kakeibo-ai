import hashlib
import json

import pytest

from app.drive_run_state import StateBinding, StateError
from app.production_ledger import ProductionLedger, initial_ledger
from app.production_flow import assemble
from app.production_run import DEPENDENCIES, execute_serial


class Drive:
    def __init__(self, payload): self.payload, self.writes, self.fail_at = payload, 0, 0
    def read(self): return self.payload
    def write(self, value):
        self.writes += 1
        if self.writes == self.fail_at:
            raise StateError("state_drive_write_unknown")
        self.payload = value


@pytest.mark.parametrize('fail', [False, True])
def test_receipts_scope_only_runs_and_changes_receipts_ledger(setup, monkeypatch, tmp_path, fail):
    from copy import deepcopy
    _, drive, ledger = setup
    before = deepcopy(ledger.value['sources'])
    calls = []
    def source(name, **kwargs):
        calls.append(name)
        if fail:
            raise StateError('source_command_timed_out')
        return {'written': 2, 'failure': 0}
    monkeypatch.setattr('app.production_flow.invoke', source)
    result = execute_serial(assemble({}, tmp_path, apply=True, bank_apply=False, ledger=ledger),
                            history=before, receipts_only=True)
    assert calls == ['receipts']
    assert set(result['sources']) == {'receipts'}
    assert result['success'] is (not fail)
    assert ledger.value['sources']['receipts']['phase'] == ('pending' if fail else 'ready')
    assert all(ledger.value['sources'][k] == v for k, v in before.items() if k != 'receipts')
    if fail:
        execute_serial(assemble({}, tmp_path, apply=True, bank_apply=False, ledger=ledger), receipts_only=True)
        assert calls == ['receipts']  # Unknown writes require reconciliation, never an automatic retry.


@pytest.fixture
def setup():
    binding = StateBinding("production_run", "sheet", "private-folder", "ledger-file")
    drive = Drive(initial_ledger(binding))
    return binding, drive, ProductionLedger(drive, binding)


def test_non_checkpoint_source_unknown_write_blocks_automatic_restart(setup, monkeypatch, tmp_path):
    binding, drive, ledger = setup
    calls = []
    def source(source, **kwargs):
        calls.append(source)
        if source == "paypay":
            raise RuntimeError("synthetic append outcome unknown")
        return {"written": 0}
    monkeypatch.setattr("app.production_flow.invoke", source)
    runners = assemble({}, tmp_path, apply=True, bank_apply=False, ledger=ledger)
    with pytest.raises(RuntimeError):
        runners["paypay"]()
    runners["aupay_balance"]()  # Independent source can still complete.
    resumed = ProductionLedger(drive, binding)
    with pytest.raises(StateError, match="reconciliation_required"):
        assemble({}, tmp_path, apply=True, bank_apply=False, ledger=resumed)["paypay"]()
    assert calls == ["paypay", "aupay_balance"]
    assert resumed.value["sources"]["paypay"]["phase"] == "pending"
    assert resumed.value["sources"]["aupay_balance"]["last_success"]


def test_new_writes_require_durable_intent_before_invoking_source(setup, monkeypatch, tmp_path):
    _, drive, ledger = setup
    drive.fail_at = 1
    monkeypatch.setattr("app.production_flow.invoke", lambda *args, **kwargs: pytest.fail("source write before intent"))
    with pytest.raises(StateError):
        assemble({}, tmp_path, apply=True, bank_apply=False, ledger=ledger)["receipts"]()
    assert drive.writes == 1


def test_last_success_and_safe_counts_survive_failed_later_run(setup):
    _, drive, ledger = setup
    ledger.begin("receipts", "a" * 32)
    ledger.complete("receipts", {"written": 2, "amount": 1200, "filename": "synthetic-private-name"}, 1.5)
    stamp = ledger.value["sources"]["receipts"]["last_success"]
    ledger.begin("receipts", "b" * 32)
    ledger.fail("receipts")
    assert ledger.value["sources"]["receipts"]["last_success"] == stamp
    assert ledger.value["sources"]["receipts"]["counts"] == {"written": 2}
    assert b"synthetic-private-name" not in drive.payload
    assert b"1200" not in drive.payload


def test_save_failure_after_write_keeps_pending_and_requires_exact_operator_release(setup):
    binding, drive, ledger = setup
    ledger.begin("review_apply", "c" * 32)
    drive.fail_at = 2
    with pytest.raises(StateError):
        ledger.complete("review_apply", {"written": 1}, 2.0)
    recovered = ProductionLedger(drive, binding)
    with pytest.raises(StateError):
        recovered.begin("review_apply", "d" * 32)
    with pytest.raises(StateError):
        recovered.release_after_reconciliation("review_apply", observed_digest="wrong", evidence_reference="a" * 64)
    recovered.release_after_reconciliation("review_apply", observed_digest=hashlib.sha256(drive.payload).hexdigest(), evidence_reference="a" * 64)
    recovered.begin("review_apply", "d" * 32)


def test_corrupt_missing_or_wrong_binding_ledger_is_not_initialized(setup):
    binding, _, _ = setup
    for payload in (b"", b"{}", b"broken", initial_ledger(StateBinding("production_run", "wrong", "folder", "file"))):
        drive = Drive(payload)
        with pytest.raises(StateError):
            ProductionLedger(drive, binding)
        assert drive.writes == 0


def test_preview_preserves_historical_success_timestamps():
    stamp = "2026-09-14T06:00:00+09:00"
    history = {source: {"last_success": stamp} for source in DEPENDENCIES}
    report = execute_serial({source: lambda: {"written": 0} for source in DEPENDENCIES}, history=history, preview=True)
    assert all(item["last_success"] == stamp for item in report["sources"].values())
