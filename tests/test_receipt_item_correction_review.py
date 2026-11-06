"""Synthetic review and event harness. No live auth/Sheets transport is tested.

Only this test harness can write: it uses the existing ReceiptConfirmation
planner/writer over a fake DB. It is not a deployable backend or permission.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.category_rules import CategoryRule
from app.drive_run_state import StateError
from app.models import ReceiptItem, ReceiptResult
from app.page_receipt_model import digest
from app.receipt_confirmation import ReceiptConfirmation
from app.receipt_item_correction_plan import preview_item_corrections
from app.receipt_item_correction_review import review, registration_plan, STATES
from app.receipt_reimport_production import target_snapshot
from test_receipt_confirmation import DB, Store
from test_receipt_item_correction_plan import scenario, edit, add, CATEGORIES, SOURCE


OWNER = "d" * 64
SOURCE_RECORD = dict(source_id=SOURCE, sha256="e" * 64, version="7",
                     mime_type="image/png", classification="normal", transaction_kind="purchase")


def confirmed(source=SOURCE_RECORD, total=1368):
    return dict(source=deepcopy(source), receipt_total=total, actor_id=OWNER)


def case(amounts=(500, 730), total=1368, **kw):
    tx, rows = scenario(amounts, total, categories=[CATEGORIES[0]], **kw)
    return tx, rows


class MemoryRegistration:
    """Test-only event runner: private request journal + existing real writer.

    actor/session/CSRF/expiry stand for an authenticated host's independently
    verified context. These fake checks are NOT Google OAuth evidence.
    no_go=False is synthetic permission ONLY and is never persisted to config.
    """
    def __init__(self, tx, rows):
        self.tx, self.rows = tx, rows
        self.source = deepcopy(SOURCE_RECORD)
        self.confirmation = confirmed(total=tx.amount)
        self.operations = []
        self.no_go = True
        self.journal = {}
        self.claimed = False
        self.states = [STATES[0]]
        self.rules = []
        self.db, self.store = DB(), Store()
        self.db.categories = lambda: CATEGORIES
        self.db.rows["取込データ"] = [deepcopy(tx.row)]
        self.db.rows["レシート"] = [["R-" + SOURCE, tx.date, tx.merchant, tx.amount,
                                      "", "", "要確認", "synthetic", tx.note]]
        self.initial = deepcopy(self.db.rows)
        self.expected_source = deepcopy(self.source)
        self.expected_candidates = digest(rows)
        self.service = ReceiptConfirmation(self.store, self.db, self.verify)

    def verify(self, source, folder):
        if source != self.source or source != self.expected_source or digest(self.rows) != self.expected_candidates:
            raise StateError("synthetic_source_or_candidate_changed")

    def display(self):
        value = review(self.tx, self.rows, self.source, self.operations, CATEGORIES,
                       total_confirmation=self.confirmation, approved_product_rules=self.rules)
        value["state"] = self.states[-1]
        return value

    def register(self, request_id, submitted, *, actor=OWNER, session=True, csrf=True, expires=101, now=100):
        if actor != OWNER or not session or not csrf or now >= expires:
            raise StateError("synthetic_authenticated_owner_request_required")
        if request_id in self.journal:
            old = self.journal[request_id]
            if old["snapshot"] != submitted:
                raise StateError("synthetic_replay_changed")
            return old["state"]  # no retry, even after NO-GO changes
        if self.claimed:
            raise StateError("synthetic_case_already_claimed_reconcile_only")
        self.states += [STATES[1], STATES[2]]
        self.verify(self.source, "synthetic")
        fresh = self.display()
        plan = registration_plan(submitted, fresh)
        self.journal[request_id] = dict(snapshot=deepcopy(submitted), state=plan["state"])
        if plan["state"] == STATES[3]:
            self.states.append(STATES[3])
            return STATES[3]
        parsed = ReceiptResult(merchant=self.tx.merchant, date=self.tx.date, total=plan["receipt_total"],
                               items=[ReceiptItem(name=i["name"], amount=i["amount"], quantity=i["quantity"],
                                      major_category=i["category"][0], minor_category=i["category"][1])
                                      for i in plan["items"]])
        before = target_snapshot(self.service.tables(), SOURCE)
        if before["expense_rows"] or digest(self.db.rows) != digest(self.initial):
            raise StateError("synthetic_ledger_changed_or_duplicate")
        item = dict(kind="normal", source=deepcopy(self.source), folder_id="synthetic", before=before)
        # Reuse the existing duplicate checks and exact item-row planner.
        item["plan"] = self.service._plan(item, parsed, "")
        if self.no_go:
            self.states.append(STATES[4])
            return STATES[4]
        self.verify(self.source, "synthetic")
        if submitted != self.display()["snapshot"]:
            raise StateError("synthetic_input_changed_before_claim")
        self.claimed = True  # private durable-claim stub; never reset/retry
        item.update(status="pending", inputs=[""] * 8)
        self.store.value["confirmation_items"][request_id] = deepcopy(item)
        try:
            self.service._write_accounting_plan(request_id, item)
            # Independent DB get/read-back, no reliance on append response.
            if not self.service._complete(item["plan"]):
                raise StateError("synthetic_independent_readback_failed")
        except Exception:
            self.journal[request_id]["state"] = STATES[6]
            self.states.append(STATES[6])
            return STATES[6]
        self.journal[request_id]["state"] = STATES[5]
        self.states.append(STATES[5])
        return STATES[5]


@pytest.mark.parametrize("amounts,total,gap", [([500, 730], 1368, 138), ([500, 439], 967, 28), ([500, 28], 500, -28)])
def test_A_B_amount_origins_and_prior_confirmation(amounts, total, gap):
    tx, rows = case(amounts, total)
    view = review(tx, rows, SOURCE_RECORD, [], CATEGORIES, total_confirmation=confirmed(total=total))
    assert view["sheet"] == "確認" and view["case_id"] == tx.import_id
    assert view["difference"] == gap
    assert view["difference_kind"] == ("不足" if gap > 0 else "超過")
    assert not view["total_confirmation_required"]
    assert f"{gap:+,}" in view["calculation"] and view["original_link"].endswith(SOURCE + "/view")
    assert view["financial_write_count"] == 0


@pytest.mark.parametrize("operation", ["edit", "add", "remove", "quantity"])
def test_C_D_single_registration_real_writer_itemwise_readback(operation):
    tx, rows = case([500, 28], 500) if operation == "remove" else case()
    runner = MemoryRegistration(tx, rows)
    if operation == "edit":runner.operations = [edit(rows[1][1][0], name="洗剤", amount=868, category=CATEGORIES[1])]
    elif operation == "add":runner.operations = [add("原本の追加商品", 138)]
    elif operation == "remove":runner.operations = [dict(action="remove", item_id=rows[1][1][0], evidence="原本の重複抽出行を除外")]
    else:runner.operations = [edit(rows[1][1][0], quantity=2, amount=868)]
    snapshot = runner.display()["snapshot"]
    assert runner.db.writes == 0  # editing/display never posts
    runner.no_go = False  # in-memory synthetic grant only
    assert runner.register("request-1", snapshot) == STATES[5]
    expenses = runner.db.get("'支出明細'!A2:M")
    assert sum(r[4] for r in expenses) == tx.amount
    assert len(expenses) == (3 if operation == "add" else 1 if operation == "remove" else 2)
    if operation == "edit":assert {tuple(r[5:7]) for r in expenses} == set(CATEGORIES[:2])
    before = deepcopy(runner.db.rows)
    assert runner.register("request-1", snapshot) == STATES[5]
    with pytest.raises(StateError, match="already_claimed"):
        runner.register("request-2", snapshot)
    assert runner.db.rows == before
    assert runner.display()["state"] == STATES[5]
    assert runner.states == [STATES[0], STATES[1], STATES[2], STATES[5]]


def rule(category=CATEGORIES[0], **kw):
    values = dict(rule_id="synthetic-product-rule", kind="product", source="receipt", account_alias="",
                  billing_name="", merchant="合成商店", product_id="", product_name="牛乳", amount=None,
                  category=category, approved_expense_id="synthetic-expense", approved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                  revision=1, active=True)
    values.update(kw)
    return CategoryRule(**values)


def test_E_product_rule_reuse_conflicts_held_rule_and_store_scope():
    tx, rows = case()
    operations = [edit(rows[1][1][0], name="牛乳", amount=868)]
    result = preview_item_corrections(tx, rows, operations, CATEGORIES, total_confirmed=True,
                                      approved_product_rules=[rule()])
    assert result["status"] == "preview_balanced" and result["items"][1]["category"] == CATEGORIES[0]
    for rules, expected in [([rule(), rule(CATEGORIES[1], rule_id="second")], "category_rule_conflict"),
                             ([rule(approved_at=datetime(2027, 1, 1, tzinfo=timezone.utc))], "approval_not_applicable")]:
        result = preview_item_corrections(tx, rows, operations, CATEGORIES, total_confirmed=True, approved_product_rules=rules)
        assert result["status"] == "needs_review" and any(expected in x for x in result["issues"])
    result = preview_item_corrections(tx, rows, [edit(rows[1][1][0], name="牛乳", amount=868, category=CATEGORIES[1])],
                                      CATEGORIES, total_confirmed=True, approved_product_rules=[rule()])
    assert any("candidate_rule_conflict" in x for x in result["issues"])
    result = preview_item_corrections(tx, rows, operations, CATEGORIES, total_confirmed=True,
                                      approved_product_rules=[rule(CATEGORIES[1], kind="store_total", product_name="")])
    assert result["items"][1]["category"] == ("その他", "未分類")


@pytest.mark.parametrize("name,kind,amount,category,target,okay", [
    ("外税", "tax", 138, CATEGORIES[0], True, True),
    ("内税", "tax", 138, CATEGORIES[0], True, False),
    ("外税", "tax", 138, CATEGORIES[1], False, False),
    ("商品値引き", "discount", -28, CATEGORIES[0], True, True),
])
def test_F_printed_adjustments_no_fabrication_or_included_tax_double_count(name, kind, amount, category, target, okay):
    tx, rows = case([500, 730], 1230 + amount)
    op = add(name, amount)
    op.update(kind=kind, category=category)
    if target:op["target_item_id"] = rows[0][1][0]
    runner = MemoryRegistration(tx, rows)
    runner.operations = [op]
    runner.no_go = False
    state = runner.register("request", runner.display()["snapshot"])
    assert state == (STATES[5] if okay else STATES[3])
    if not okay:assert runner.db.writes == 0


@pytest.mark.parametrize("change", ["source_id", "sha256", "version", "classification", "candidate", "operations", "actor", "session", "csrf", "expired", "ledger"])
def test_G_stale_identity_snapshot_owner_auth_and_ledger_zero_writes(change):
    tx, rows = case()
    runner = MemoryRegistration(tx, rows)
    runner.operations = [edit(rows[1][1][0], amount=868)]
    submitted = runner.display()["snapshot"]
    runner.no_go = False
    kwargs = {}
    if change in {"source_id", "sha256", "version", "classification"}:runner.source[change] = "changed"
    elif change == "candidate":runner.rows[0][1][4] = "候補改変"
    elif change == "operations":runner.operations = [edit(rows[1][1][0], amount=869)]
    elif change == "actor":kwargs["actor"] = "f" * 64
    elif change in {"session", "csrf"}:kwargs[change] = False
    elif change == "expired":kwargs["now"] = 101
    elif change == "ledger":runner.db.rows["支出明細"].append(["existing", tx.date, tx.merchant, "既存支出", tx.amount])
    with pytest.raises((StateError, ValueError)):
        runner.register("request", submitted, **kwargs)
    assert runner.db.writes == 0


def test_H_NO_GO_and_flag_change_never_posts_old_request():
    tx, rows = case()
    runner = MemoryRegistration(tx, rows)
    runner.operations = [edit(rows[1][1][0], amount=868)]
    submitted = runner.display()["snapshot"]
    assert runner.register("request", submitted) == STATES[4]
    assert runner.db.writes == 0 and runner.display()["state"] == STATES[4]
    runner.no_go = False
    assert runner.register("request", submitted) == STATES[4]
    assert runner.db.writes == 0


@pytest.mark.parametrize("lost", ["before", "partial", "after"])
def test_I_unknown_readback_no_automatic_retry_or_second_request(lost):
    tx, rows = case()
    runner = MemoryRegistration(tx, rows)
    runner.operations = [edit(rows[1][1][0], amount=868)]
    runner.no_go = False
    original = runner.db.append_raw
    def fail(title, values):
        if title == ("取込データ" if lost == "after" else "支出明細"):
            if lost == "after":original(title, values)
            if lost != "partial" or not runner.db.rows["支出明細"]:raise TimeoutError("synthetic response loss")
        original(title, values)
    runner.db.append_raw = fail
    if lost == "partial":
        def partial(title, values):
            if title == "支出明細" and runner.db.rows[title]:raise TimeoutError("synthetic partial write")
            original(title, values)
        runner.db.append_raw = partial
    submitted = runner.display()["snapshot"]
    expected = STATES[5] if lost == "after" else STATES[6]
    assert runner.register("request", submitted) == expected
    before = deepcopy(runner.db.rows)
    assert runner.register("request", submitted) == expected
    with pytest.raises(StateError):runner.register("new-request", submitted)
    assert runner.db.rows == before and runner.display()["state"] == expected


@pytest.mark.parametrize("field", ["version", "sha256"])
def test_confirmation_never_carries_to_another_original_version(field):
    tx, rows = case()
    source = deepcopy(SOURCE_RECORD)
    source[field] = "8" if field == "version" else "c" * 64
    value = review(tx, rows, source, [], CATEGORIES, total_confirmation=confirmed())
    assert value["total_confirmation_required"]


@pytest.mark.parametrize("value", [0, -1, "NaN", "Infinity", "", True])
def test_bad_saved_quantities_never_become_a_balanced_preview(value):
    tx, rows = case()
    rows[0][1][5] = value
    with pytest.raises(ValueError):
        preview_item_corrections(tx, rows, [edit(rows[1][1][0], amount=868)], CATEGORIES, total_confirmed=True)


def test_quantity_requires_line_amount_confirmation_and_snapshot_includes_removed_source():
    tx, rows = case([500, 28], 500)
    op = dict(action="remove", item_id=rows[1][1][0], evidence="原本の重複行を除外")
    a = preview_item_corrections(tx, rows, [op], CATEGORIES, total_confirmed=True)
    rows[1][1][4] = "除外された行の候補改変"
    b = preview_item_corrections(tx, rows, [op], CATEGORIES, total_confirmed=True)
    assert a["snapshot_digest"] != b["snapshot_digest"]
    tx, rows = case()
    result = preview_item_corrections(tx, rows, [edit(rows[1][1][0], quantity=2)], CATEGORIES, total_confirmed=True)
    assert any("quantity_line_amount" in x for x in result["issues"])


def test_semicolon_note_matches_and_unknown_cause_is_held_without_writes():
    tx, rows = case()
    tx = replace(tx, note="メモ; " + tx.note)
    value = preview_item_corrections(tx, rows, [], CATEGORIES, total_confirmed=True)
    assert value["status"] == "needs_review" and value["difference"] == 138
    assert value["financial_write_count"] == 0


def test_row_coordinates_are_not_identity_and_policy_changes_invalidate_submission():
    tx, rows = case()
    operations = [edit(rows[1][1][0], amount=868)]
    first = review(tx, rows, SOURCE_RECORD, operations, CATEGORIES, total_confirmation=confirmed())
    relocated = [(n + 20, row) for n, row in rows]
    second = review(tx, relocated, SOURCE_RECORD, operations, CATEGORIES, total_confirmation=confirmed())
    assert first["snapshot"] == second["snapshot"]
    changed = review(tx, rows, SOURCE_RECORD, operations, CATEGORIES, total_confirmation=confirmed(),
                     approved_product_rules=[rule(product_name="無関係の商品")])
    with pytest.raises(ValueError, match="snapshot_stale"):
        registration_plan(first["snapshot"], changed)
    assert "500 + 730 = 1230円" == first["original_item_calculation"]


@pytest.mark.parametrize("change", ["duplicate", "mixed_case", "wrong_hash", "invalid_target", "fabricated"])
def test_malformed_or_unbound_items_never_reach_writer(change):
    tx, rows = case()
    runner = MemoryRegistration(tx, rows)
    runner.operations = [edit(rows[1][1][0], amount=868)]
    if change == "duplicate":runner.rows.append(deepcopy(runner.rows[-1]))
    elif change == "mixed_case":runner.operations = [edit("receipt:another-case:001", amount=868)]
    elif change == "wrong_hash":runner.rows[0][1][2] = "b" * 64
    elif change == "invalid_target":runner.operations[0]["changes"]["target_item_id"] = {}
    elif change == "fabricated":runner.operations = [add("差額補正", 138)]
    with pytest.raises(ValueError):runner.display()
    assert runner.db.writes == 0
