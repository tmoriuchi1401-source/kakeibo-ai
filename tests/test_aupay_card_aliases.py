"""Window-independent ownership using the real planner, writer and Drive codec."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from app.aupay_card_aliases import confirmed_alias_updates, merge_registry
from app.aupay_card_apply_plan import build_canonical_apply_plan
from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import DurableState
from app.transaction_plan import reconcile_transactions
from test_aupay_card_first_window_apply import fixture
from card_first_window_fakes import Gmail


def collected(raws):
    from app.aupay_mail_pipeline import AuPayCardMailPipeline
    return AuPayCardMailPipeline._collect(Gmail(raws), "synthetic", 500,
        money_records=[], money_notices=[], sleeper=lambda _: None, request_interval=0)


def test_within_window_then_noncanonical_only_after_checkpoint_and_fresh_restore(tmp_path):
    p = fixture(tmp_path)
    before = deepcopy(p.db.rows)
    assert p.prepared["plan"].noncanonical_resend_count == 39
    from app.aupay_card_first_window_apply import execute_fixed
    execute_fixed(**p.execution)
    assert p.db.rows[:6] == before
    # Restore solely from the remote payload; no original local runner state.
    fresh_dir = tmp_path / "fresh"
    DurableState(p.durable.transport, p.durable.binding).restore(fresh_dir)
    fresh = SqliteRecurringRunState(fresh_dir / "recurring.sqlite3", repo_root=tmp_path / "repo")
    registry = fresh.alias_registry()
    assert len(registry) == 39 and sum(len(e["aliases"]) for e in registry) == 78
    raws = p.prepared["transactions"]
    raw_by_id = {r["import_id"]: r for r in raws}
    for entry in registry:
        # Both original and non-owner alias work alone, independent of the
        # old window and of which source sorts first in this new window.
        for identity in entry["aliases"]:
            result = reconcile_transactions([raw_by_id[identity]], p.db.rows, alias_registry=registry)
            plan = build_canonical_apply_plan(p.prepared["collection"], result)
            assert not plan.candidates and plan.duplicate_existing_identity_count == 1
            assert result["transactions"][0].existing_source_identities == (entry["owner"],)
    assert fresh.successful_window_end() == p.state.successful_window_end()
    assert p.db.write_calls == 1


def registry_fixture():
    from test_transaction_plan import row, mail_id
    from app.canonical_import import materialize_import_row
    from app.transaction_plan import normalize_card_transaction
    from test_aupay_card_recurring import NOW
    raws = [row(mail_id("a")), row(mail_id("b"))]
    rec = reconcile_transactions(raws, [], alias_registry=())
    physical = [materialize_import_row(normalize_card_transaction(raws[0]),
                imported_at=NOW, status="auto_expense")]
    registry = confirmed_alias_updates(raws, rec, physical, (), {physical[0][0]})
    return raws, physical, registry


def test_twelve_owner_twenty_four_source_counterexample_and_two_import_delta():
    from test_transaction_plan import row, mail_id
    from app.canonical_import import materialize_import_row
    from app.transaction_plan import normalize_card_transaction, build_write_plan
    from test_aupay_card_recurring import NOW
    raws = []
    for i in range(12):
        amount = (4386, 120)[i-10] if i >= 10 else 1000+i
        for char in "ab":
            raws.append(row(mail_id(char, i+1), merchant=f"合成店舗{i}", amount=amount,
                            memo=f"メール明細No.{i+1:03d}"))
    rec = reconcile_transactions(raws, [], alias_registry=())
    rows = [materialize_import_row(item.canonical, imported_at=NOW,
            status="transfer_aupay_charge" if item.canonical.amount_yen == 120 else "auto_expense")
            for item in rec["transactions"]]
    # Fix the real counterexample's structure without publishing real source IDs.
    registry = confirmed_alias_updates(raws, rec, rows, (), {r[0] for r in rows})
    assert len(registry) == 12 and sum(len(e["aliases"]) for e in registry) == 24
    alias = next(r for r in raws if r["import_id"] not in {x[0] for x in rows})
    assert build_write_plan([alias], rows)["summary"]["insert"] == 1
    after = reconcile_transactions([alias], rows, alias_registry=registry)
    assert after["summary"]["existing_identity_duplicates"] == 1
    assert reconcile_transactions(raws, rows, alias_registry=registry)["summary"]["existing_identity_duplicates"] == 12
    before = [r for r in rows if r[6] not in {4386, 120}]
    assert len(before) == 10 and len(rows) - len(before) == 2
    assert sum(r[6] for r in rows if r not in before) == 4506


def test_late_unknown_alias_holds_but_independent_same_date_amount_remains_new():
    from test_transaction_plan import row, mail_id
    raws, rows, registry = registry_fixture()
    late = dict(raws[0], import_id=mail_id("c"))
    with pytest.raises(RuntimeError, match="unresolved_no_checkpoint"):
        reconcile_transactions([late], rows, alias_registry=registry)
    # Different merchant or detail number/memo is not dropped merely
    # for sharing date and amount. Identical unprovable content stays on hold.
    independent = row(mail_id("d", 2), memo="メール明細No.002")
    result = reconcile_transactions([independent], rows, alias_registry=registry)
    assert result["transactions"][0].state == "new"
    assert not result["transactions"][0].existing_source_identities


@pytest.mark.parametrize("mode", ["source", "owner", "missing", "duplicate", "second_owner", "reassignment"])
def test_corrupt_alias_or_owner_never_grants_duplicate_authority(mode):
    raws, rows, registry = registry_fixture()
    alias = deepcopy(raws[1])
    if mode == "source": alias["source_hash"] = "0" * 64
    if mode == "owner": rows[0][8] = "changed-status"
    if mode == "missing": rows.clear()
    if mode == "duplicate": rows.append(deepcopy(rows[0]))
    if mode == "second_owner": rows.append([alias["import_id"]] + rows[0][1:])
    if mode == "reassignment":
        second = deepcopy(registry[0]); second["owner_row_sha256"] = "0" * 64
        registry.append(second)
    with pytest.raises(RuntimeError):
        reconcile_transactions([alias], rows, alias_registry=registry)


@pytest.mark.parametrize("mode", ["unknown", "drop", "conflict", "expense"])
def test_partial_or_unknown_never_publishes_aliases_or_restores_for_resend(tmp_path, mode):
    from app.aupay_card_first_window_apply import execute_fixed
    p = fixture(tmp_path)
    old_cp = p.state.successful_window_end()
    p.db.mode = mode
    with pytest.raises(RuntimeError): execute_fixed(**p.execution)
    assert p.state.successful_window_end() == old_cp and not p.state.alias_registry()
    with pytest.raises(RuntimeError, match="reconciliation_required"):
        DurableState(p.durable.transport, p.durable.binding).restore(tmp_path / "fresh")
    assert p.db.write_calls == 1


def test_append_only_registry_rejects_mutation_and_failed_summaries_cannot_seed(tmp_path):
    from test_aupay_card_recurring import components, NOW
    p = components(tmp_path)
    raws, rows, registry = registry_fixture()
    state = p[2]
    state.record(dict(run_id="failed", status="failed", source_window_start=NOW.isoformat(),
        source_window_end=NOW.isoformat(), card_aliases=registry), advance_checkpoint=False)
    assert not state.alias_registry()
    with sqlite3.connect(state.path) as con:
        with pytest.raises(sqlite3.IntegrityError, match="append_only_run"):
            con.execute("UPDATE recurring_runs SET status='complete'")
