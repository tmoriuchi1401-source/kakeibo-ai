from datetime import datetime, timedelta, timezone
import base64
import json

import pytest

from app.aupay_card_production import (
    ProtectedAuditKeyProvider,
    SqliteLeaseManager,
    target_binding_reference,
)
from app.aupay_card_writer import PersistentAuditKey, TargetBinding, TargetSnapshot
from app.bank_status_repair import (
    ProtectedBankStatusRepairApprovalProvider,
    SealedBankStatusCellTransport,
    SqliteBankStatusRepairState,
    create_bank_status_repair_preview,
    execute_bank_status_repair_once,
    issue_bank_status_repair_capability,
    repair_reference,
)
from app.sheets import HEADERS


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
IDENTITY = "bankpdf:docomo-smtb:docomo-smtb-primary:" + "a" * 24
KEY = PersistentAuditKey("bank-status-repair-test", b"s" * 32)


def row(status="bank_expense"):
    return (
        IDENTITY, "2026-09-13 10:00:00", "ドコモSMTB銀行PDF", IDENTITY,
        "2026-08-01", "synthetic asset formation", -1000, "銀行口座",
        status, "", "f" * 64, "synthetic",
    )


class Inspector:
    def __init__(self, binding, header=None):
        self.binding = binding
        self.header = tuple(header or HEADERS["取込データ"])

    def inspect(self, worksheet):
        return TargetSnapshot(self.binding.expected_spreadsheet_id, worksheet, self.header)


class Request:
    def __init__(self, callback=None, fail=False):
        self.callback = callback
        self.fail = fail

    def execute(self):
        if self.callback:
            self.callback()
        if self.fail:
            raise TimeoutError("response lost")
        return {"updatedCells": 1}


class Values:
    def __init__(self, reader, *, fail=False, mutate_on_failure=False):
        self.reader = reader
        self.fail = fail
        self.mutate_on_failure = mutate_on_failure
        self.calls = []

    def update(self, **kwargs):
        self.calls.append(kwargs)
        def mutate():
            if not self.fail or self.mutate_on_failure:
                number, current = self.reader.rows[0]
                changed = list(current)
                changed[8] = kwargs["body"]["values"][0][0]
                self.reader.rows = [(number, tuple(changed))]
        return Request(mutate, self.fail)


class Service:
    def __init__(self, values):
        self.api = values
    def spreadsheets(self):
        return self
    def values(self):
        return self.api


class DB:
    def __init__(self, values):
        self.svc = Service(values)


class Reader:
    def __init__(self, rows):
        self.rows = rows
    def __call__(self):
        return self.rows


def components(tmp_path, *, fail=False, mutate_on_failure=False, header=None):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir(); state.mkdir()
    key_path = state / "audit-key.json"
    key_path.write_text(json.dumps({
        "key_id": KEY.key_id,
        "key_b64": base64.b64encode(KEY.secret).decode("ascii"),
    }), encoding="utf-8")
    key_provider = ProtectedAuditKeyProvider(key_path, repo_root=repo)
    binding = TargetBinding(expected_spreadsheet_id="sheet-id")
    preview = create_bank_status_repair_preview(
        run_id="819b7f41-e2c7-4d8e-89cb-bab0e6603f19",
        identity=IDENTITY,
        target_ref=target_binding_reference(binding, KEY),
        before_row=row(), expected_old_value="bank_expense",
        new_value="bank_asset_formation_expense",
    )
    approval_path = state / "approval.json"
    approval_path.write_text(json.dumps({
        "approval_reference": "phase22-exact-canary-status-repair",
        "repair_ref": repair_reference(preview, KEY),
        "target_ref": preview.target_ref,
        "expires_at": (NOW + timedelta(seconds=120)).isoformat(),
    }), encoding="utf-8")
    approval = ProtectedBankStatusRepairApprovalProvider(approval_path, repo_root=repo)
    store = SqliteBankStatusRepairState(state / "repair.sqlite3", repo_root=repo)
    leases = SqliteLeaseManager(state / "leases.sqlite3", repo_root=repo, clock=lambda: NOW)
    capability = issue_bank_status_repair_capability(
        preview, key_provider=key_provider, approval_provider=approval,
        store=store, now=NOW,
    )
    reader = Reader([(87, preview.before_row)])
    values = Values(reader, fail=fail, mutate_on_failure=mutate_on_failure)
    db = DB(values)
    transport = SealedBankStatusCellTransport(
        db, capability=capability, store=store, binding=binding,
        inspector=Inspector(binding, header), key_provider=key_provider,
        clock=lambda: NOW,
    )
    return preview, key_provider, store, leases, capability, reader, values, transport


def run(parts):
    preview, key_provider, store, leases, capability, reader, _, transport = parts
    return execute_bank_status_repair_once(
        preview, capability=capability, store=store, leases=leases,
        reader=reader, transport=transport, key_provider=key_provider,
        owner_id="phase22-test", clock=lambda: NOW,
    )


def test_exact_status_cell_compare_and_set_is_one_shot(tmp_path):
    parts = components(tmp_path)
    result = run(parts)
    preview, _, store, _, capability, reader, values, transport = parts
    assert result.status == "repair_complete"
    assert result.requested_mutations == result.confirmed_changed_cells == 1
    assert reader.rows == [(87, preview.after_row)]
    assert len(values.calls) == 1
    assert values.calls[0]["range"] == "取込データ!I87"
    assert values.calls[0]["body"] == {"values": [["bank_asset_formation_expense"]]}
    assert store.state(capability.capability_id) == "sealed"
    assert [event[0] for event in store.history(preview.run_id, result.attempt_id)] == [
        "pre_read", "write_attempted", "write_result", "post_read", "final",
    ]
    with pytest.raises(RuntimeError, match="capability_reused"):
        transport.write_once(preview, attempt_id=result.attempt_id, row_number=87)
    assert len(values.calls) == 1


@pytest.mark.parametrize("rows", [[], [(87, row()), (88, row())]])
def test_zero_or_multiple_identity_matches_stop_before_write(tmp_path, rows):
    parts = components(tmp_path)
    parts[5].rows = rows
    with pytest.raises(RuntimeError, match="fresh_pre_read_mismatch"):
        run(parts)
    assert parts[6].calls == []
    assert parts[2].state(parts[4].capability_id) == "sealed"


def test_header_mismatch_stops_without_mutation(tmp_path):
    parts = components(tmp_path, header=["wrong"])
    result = run(parts)
    assert result.status == "repair_not_applied"
    assert result.confirmed_changed_cells == 0
    assert len(parts[6].calls) == 0


def test_timeout_after_server_mutation_recovers_without_retry(tmp_path):
    parts = components(tmp_path, fail=True, mutate_on_failure=True)
    result = run(parts)
    assert result.status == "success_recovered"
    assert result.exact
    assert len(parts[6].calls) == 1


def test_timeout_before_mutation_stops_without_retry(tmp_path):
    parts = components(tmp_path, fail=True, mutate_on_failure=False)
    result = run(parts)
    assert result.status == "repair_not_applied"
    assert not result.exact
    assert len(parts[6].calls) == 1


def test_preview_rejects_any_non_status_change(tmp_path):
    preview = components(tmp_path)[0]
    altered = list(preview.after_row)
    altered[6] = -2000
    with pytest.raises(RuntimeError, match="exactly_status"):
        type(preview)(
            **{**preview.__dict__, "after_row": tuple(altered), "after_digest": "0" * 64}
        ).validate()
