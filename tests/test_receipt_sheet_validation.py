"""Entirely synthetic, offline Sheets -> UUID request -> Python -> projection."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
from threading import Barrier, RLock

import pytest

from app.category_rules import CategoryRule
from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app.reconciliation import parse_import_rows
from app.receipt_sheet_draft import parse_draft
from app.receipt_sheet_cells import receipt_name, receipt_quantity
from app.receipt_sheet_validation import (JOURNAL_SCHEMA, PURPOSE, PREPARED, UNKNOWN, IndependentReceiptReader,
    ValidationJournal, empty_journal, execute_request, execute_accepted_request,
    project_result, validate_journal, result_cache_entry, cached_result)
from test_receipt_item_correction_plan import scenario, edit, add, SOURCE, CATEGORIES

RID = "11111111-1111-4111-8111-111111111111"
RID2 = "22222222-2222-4222-8222-222222222222"
RID3 = "33333333-3333-4333-8333-333333333333"  # Offline fixture only.
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def zero_external_writes(monkeypatch):
    calls = []
    def deny(*args, **kwargs):
        calls.append(True)
        raise AssertionError("external IO forbidden")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    # Existing financial writers may not even be constructed by this path.
    from app.sheets import SheetsDB
    monkeypatch.setattr(SheetsDB, "__init__", deny)
    yield
    assert calls == []


class CAS:
    def __init__(self):
        self.value, self.generation = empty_journal(), 0
        self.lock, self.writes = RLock(), 0
        self.attempts = 0
        self.fault = None
        self.weak = False

    def read_versioned(self):
        with self.lock:
            return deepcopy(self.value), 'W/"0"' if self.weak else f'"{self.generation}"'

    def replace_versioned(self, value, tag):
        with self.lock:
            self.attempts += 1
            if self.fault == "before":
                raise TimeoutError()
            if tag != f'"{self.generation}"' or self.fault == "conflict":
                raise StateError("synthetic_cas_conflict")
            self.value, self.generation = deepcopy(value), self.generation + 1
            self.writes += 1
            if self.fault == "after":
                raise TimeoutError()


class World:
    def __init__(self, amounts=(500, 730), total=1368, categories=None, names=None):
        self.tx, candidates = scenario(amounts, total, categories=categories or [CATEGORIES[0]], names=names)
        self.source = dict(source_id=SOURCE, sha256=sha256(b"synthetic original only").hexdigest(),
                           version="7", mime_type="image/png", classification="normal", transaction_kind="purchase")
        self.case_id = self.tx.import_id
        self.sheet = dict(sheet="確認", cases=[dict(case_id=self.case_id, source=deepcopy(self.source),
            draft=dict(case_id=self.case_id, source=deepcopy(self.source), operations=[]),
            note="保持するメモ", original_link="synthetic-original-link", result=None)],
            imports=[deepcopy(self.tx.row)], candidates=candidates, categories=deepcopy(CATEGORIES), rules=[])
        self.anchor = dict(source=deepcopy(self.source), candidate_digest=digest([r for _, r in candidates]),
            import_digest=digest(self.tx.row), policy_digest=digest([self.sheet["categories"], []]),
            total_confirmation=dict(source=deepcopy(self.source), receipt_total=total, actor_id="d" * 64))
        self.catalog = dict(cases={self.case_id: self.anchor})
        self.metadata = {fid:dict(id=fid, trashed=False, version="7", owners=[dict(emailAddress="owner@synthetic.invalid")],
            permissions=[dict(type="user", role="owner", emailAddress="owner@synthetic.invalid"),
                         dict(type="user", role="writer", emailAddress="service@synthetic.invalid")],
            mimeType=mime, parents=["synthetic-folder"]) for fid, mime in
            [(SOURCE, "image/png"), ("synthetic-sheet", "application/vnd.google-apps.spreadsheet"),
             ("synthetic-private", "application/json")]}
        self.raw = b"synthetic original only"
        self.cas = CAS()
        self.preflights = 0
        def preflight():
            self.preflights += 1
        self.journal = ValidationJournal(self.cas, preflight=preflight)
        self.requests = {}
        self.reader = IndependentReceiptReader(metadata=lambda fid: deepcopy(self.metadata[fid]),
            source_bytes=lambda fid: self.raw, sheet_values=lambda: deepcopy(self.sheet),
            private_cases=lambda: (deepcopy(self.catalog), '"anchor-1"'), spreadsheet_id="synthetic-sheet",
            private_id="synthetic-private", source_folder="synthetic-folder", owner_email="owner@synthetic.invalid",
            service_email="service@synthetic.invalid", parse_imports=parse_import_rows, parse_rules=lambda rows: rows)

    @property
    def row(self):
        return next(r for r in self.sheet["cases"] if r["case_id"] == self.case_id)

    def corrected(self):
        self.row["draft"]["operations"] = [edit(self.sheet["candidates"][1][1][0], amount=self.tx.amount - 500)]
        return self

    def capture(self, rid=RID):
        snapshot = self.reader(self.case_id)["request_snapshot"]
        self.requests[rid] = dict(request_id=rid, purpose=PURPOSE, snapshot=deepcopy(snapshot))
        self.row["submission"] = dict(request_id=rid, snapshot_digest=snapshot["digest"])
        return deepcopy(self.requests[rid])

    def run(self, rid=RID):
        return execute_request(rid, requests=lambda key: deepcopy(self.requests[key]), reader=self.reader, journal=self.journal)


@pytest.mark.parametrize("amounts,total,gap", [((500,730),1368,138), ((500,439),967,28)])
def test_mismatch_two_examples_then_corrected_amount_and_projection(amounts, total, gap):
    w = World(amounts, total)
    assert w.reader(w.case_id)["difference"] == gap
    w.capture()
    assert w.run()["state"] == "修正が必要"
    w.corrected().capture(RID2)
    result = w.run(RID2)
    assert result["state"] == "検証済み・未記帳"
    before = deepcopy(w.sheet["cases"])
    projected = project_result(w.sheet["cases"], result)
    assert projected[0]["result"]["financial_write_count"] == 0
    assert projected[0]["result"]["posting_authority"] is False
    assert w.sheet["cases"] == before
    for key in ("draft", "note", "original_link"):
        assert projected[0][key] == before[0][key]
    assert w.preflights > 0


@pytest.mark.parametrize("action", ["add", "remove", "quantity", "rename", "category"])
def test_product_operations(action):
    w = World((500,28),500) if action == "remove" else World()
    key = w.sheet["candidates"][1][1][0]
    ops = {"add":[add("原本の商品",138)],
           "remove":[dict(action="remove",item_id=key,evidence="原本で重複を確認")],
           "quantity":[edit(key,quantity=2,amount=868)],
           "rename":[edit(key,name="修正した商品",amount=868,category=CATEGORIES[0])],
           "category":[edit(key,amount=868,category=CATEGORIES[1])]}
    w.row["draft"]["operations"] = ops[action]
    w.capture()
    assert w.run()["state"] == "検証済み・未記帳"


@pytest.mark.parametrize("operation,expected", [
    (add("差額調整",138), "保留"),
    (dict(add("内税",138),kind="tax",target_item_id=""), "修正が必要"),
    (dict(add("値引き",138),kind="discount"), "保留"),
    (dict(add("外税",138),kind="tax",category=CATEGORIES[1]), "修正が必要"),
])
def test_tax_discount_fabrication_never_accepted(operation, expected):
    w = World()
    w.row["draft"]["operations"] = [operation]
    # Invalid operations are still saved as untrusted input by the capture port;
    # no preview success is required to dispatch a validation request.
    captured = dict(case_id=w.case_id, source=w.source, operations=[operation], context_digest=digest(w.anchor))
    captured["digest"] = digest(captured)
    w.requests[RID] = dict(request_id=RID,purpose=PURPOSE,snapshot=captured)
    assert w.run()["state"] == expected


def test_valid_discount_and_category_conflict_and_quantity_confirmation():
    w = World((500,730),1202)
    w.row["draft"]["operations"] = [dict(add("商品値引き",-28),kind="discount", target_item_id=w.sheet["candidates"][0][1][0])]
    w.capture()
    assert w.run()["state"] == "検証済み・未記帳"
    w = World().corrected()
    w.sheet["rules"] = [CategoryRule(rule_id="synthetic-product-rule",kind="product",source="receipt",account_alias="",
        billing_name="",merchant="合成商店",product_id="",product_name="商品2",amount=None,
        category=CATEGORIES[1], approved_expense_id="synthetic-expense", approved_at=datetime(2026,1,1,tzinfo=timezone.utc),
        revision=1,active=True)]
    # Port representation uses real CategoryRule; policy digest is supplied by
    # fixture via to_row, matching a real raw Sheets parser's responsibility.
    rules = w.sheet["rules"]
    w.sheet["rules"] = [r.to_row() for r in rules]
    w.reader.parse_rules = lambda rows: rules
    w.anchor["policy_digest"] = digest([w.sheet["categories"], w.sheet["rules"]])
    w.capture()
    assert w.run()["state"] == "修正が必要"
    w = World()
    w.row["draft"]["operations"] = [edit(w.sheet["candidates"][1][1][0],quantity=2)]
    w.capture()
    assert w.run()["state"] == "修正が必要"


@pytest.mark.parametrize("change", ["hash", "version", "candidate", "policy", "import", "draft", "acl", "medical", "missing_confirmation"])
def test_stale_source_candidate_policy_and_medical_rejected(change):
    w = World().corrected()
    w.capture()
    if change == "hash": w.raw = b"changed synthetic original"
    elif change == "version": w.metadata[SOURCE]["version"] = "8"
    elif change == "candidate": w.sheet["candidates"][0][1][4] = "changed"
    elif change == "policy": w.sheet["categories"] = []
    elif change == "import": w.sheet["imports"][0][5] = "changed"
    elif change == "draft": w.row["draft"]["operations"] = []
    elif change == "acl": w.metadata[SOURCE]["permissions"].append(dict(type="anyone",role="reader"))
    elif change == "medical": w.anchor["source"]["classification"] = "medical"
    elif change == "missing_confirmation": w.anchor["total_confirmation"] = None
    assert w.run()["state"] == "保留"


def test_unconfirmed_total_is_not_promoted_to_owner_approval():
    w = World().corrected()
    w.anchor["total_confirmation"] = None
    w.capture()
    assert w.run()["state"] == "修正が必要"


def test_sort_and_later_edits_preserve_inputs_projection_by_id():
    w = World().corrected()
    other = dict(case_id="other-case",note="別案件保持",draft={},result=None)
    w.sheet["cases"].insert(0, other)
    w.capture()
    result = w.run()
    w.sheet["cases"].reverse()
    w.row["draft"]["operations"] = []
    projected = project_result(w.sheet["cases"], result)
    assert projected[0]["result"]["draft_notice"] == "未登録の変更あり"
    assert projected[1] == other
    assert w.row["note"] == "保持するメモ"
    w.row["submission"]["request_id"] = RID2
    with pytest.raises(StateError,match="projection_stale"):
        project_result(w.sheet["cases"],result)


@pytest.mark.parametrize("change", ["column_sort", "duplicate_case", "deleted_case", "during_read"])
def test_partial_sort_concurrent_edit_and_duplicate_case_fail_closed(change):
    w = World().corrected()
    w.capture()
    if change == "column_sort": w.row["draft"]["case_id"] = "wrong-case"
    elif change == "duplicate_case": w.sheet["cases"].append(deepcopy(w.row))
    elif change == "deleted_case": w.sheet["cases"] = []
    else:
        calls = []
        def read():
            calls.append(True)
            result = deepcopy(w.sheet)
            if len(calls) % 2 == 0: result["cases"][0]["note"] = "concurrent"
            return result
        w.reader.sheet_values = read
    assert w.run()["state"] == "保留"


def test_replay_second_uuid_same_snapshot_and_write_flags_never_post(monkeypatch):
    w = World().corrected()
    w.capture()
    assert w.run()["state"] == "検証済み・未記帳"
    writes = w.cas.writes
    monkeypatch.setenv("UNIFIED_RECEIPT_WRITE_ENABLED", "true")
    with pytest.raises(StateError,match="replay"):
        w.run()
    w.capture(RID2)
    with pytest.raises(StateError,match="duplicate"):
        w.run(RID2)
    assert w.cas.writes == writes


@pytest.mark.parametrize("fault", ["before", "after", "conflict"])
def test_ambiguous_accept_no_retry(fault):
    w = World().corrected()
    w.capture()
    w.cas.fault = fault
    assert w.run()["state"] == UNKNOWN
    assert w.cas.writes == (1 if fault == "after" else 0)
    if fault == "after":
        w.cas.fault = None
        with pytest.raises(StateError,match="replay"):
            w.run()
        assert w.cas.writes == 1


def test_terminal_response_loss_persists_result_and_read_only_reconciliation():
    w = World().corrected()
    w.capture()
    real = w.cas.replace_versioned
    def fail_final(value, tag):
        if value["generation"] == 3: w.cas.fault = "after"
        real(value,tag)
    w.cas.replace_versioned = fail_final
    assert w.run()["state"] == UNKNOWN
    w.cas.fault = None
    assert w.journal.query(RID)["state"] == "検証済み・未記帳"
    assert w.cas.writes == 3
    with pytest.raises(StateError): w.run()
    assert w.cas.writes == 3


def test_competing_workers_and_weak_cas_and_journal_corruption():
    w = World().corrected()
    request = w.capture()
    def claim():
        try:
            ValidationJournal(w.cas,preflight=lambda:None).accept(request)
            return True
        except StateError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _:claim(), range(2))).count(True) == 1
    assert w.cas.writes == 1
    w.cas.weak = True
    with pytest.raises(StateError,match="strong_cas"):
        w.journal.load()
    w.cas.weak = False
    w.cas.value["requests"][RID]["purpose"] = "ACCOUNTING_POST"
    with pytest.raises(StateError,match="journal_invalid"):
        w.journal.load()


def test_terminal_cas_conflict_unknown_blocks_all_resubmission_and_query_never_writes():
    w = World().corrected()
    w.capture()
    real = w.cas.replace_versioned
    def conflict_final(value, tag):
        if value["generation"] == 3: raise StateError("synthetic_cas_conflict")
        real(value, tag)
    w.cas.replace_versioned = conflict_final
    assert w.run()["state"] == UNKNOWN
    assert w.journal.query(RID)["state"] == "検証中"
    assert w.journal.query(RID, run_completed=True)["state"] == UNKNOWN
    assert w.cas.writes == 2
    w.row["draft"]["operations"] = []
    w.capture(RID2)
    with pytest.raises(StateError,match="duplicate_or_unresolved"):
        w.run(RID2)
    assert w.cas.writes == 2


def test_readback_failure_never_retries_or_emits_false_success():
    w = World().corrected()
    w.capture()
    real = w.cas.read_versioned
    def read():
        value, tag = real()
        if w.cas.writes == 3:
            raise TimeoutError("synthetic readback unavailable")
        return value, tag
    w.cas.read_versioned = read
    assert w.run()["state"] == UNKNOWN
    assert w.cas.writes == 3


def test_projection_missing_duplicate_or_changed_source_refused():
    w = World().corrected()
    w.capture()
    result = w.run()
    for rows in ([], w.sheet["cases"] * 2):
        with pytest.raises(StateError,match="case_missing_or_duplicate"):
            project_result(rows,result)
    w.row["source"]["version"] = "8"
    with pytest.raises(StateError,match="projection_stale"):
        project_result(w.sheet["cases"],result)


def test_initial_medical_source_never_enters_general_validator():
    w = World()
    w.anchor["source"]["classification"] = "medical"
    w.row["source"]["classification"] = "medical"
    w.row["draft"]["source"]["classification"] = "medical"
    with pytest.raises(StateError):
        w.reader(w.case_id)
    assert w.cas.writes == 0


def draft_text(w, row):
    s = w.source
    return (f"案件｜{w.case_id}\n原本｜{SOURCE}｜{s['sha256']}｜7｜image/png\n基準｜{digest(w.anchor)}\n"
            + "｜".join(row))


@pytest.mark.parametrize("row", [
    ["変更","ID","商品🍎","2.0","868","","食費","食品","","原本で確認済み"],
    ["追加","new:item0001","商品🍎","0.125","138","product","食費","食品","","原本で確認済み"],
    ["除外","ID","","","","","","","","重複行を確認済み"],
])
def test_javascript_capture_to_python_worker_hash_and_snapshot_match(row):
    w = World()
    row[1] = w.sheet["candidates"][1][1][0] if row[0] != "追加" else row[1]
    text = draft_text(w, row)
    payload = dict(draft=text,case_id=w.case_id,source=w.source)
    completed = subprocess.run(["node",str(ROOT / "apps-script/receipt-validation/Code.test.cjs"),"--capture"],
        input=json.dumps(payload,ensure_ascii=True),capture_output=True,text=True,encoding="utf-8",check=True)
    request = json.loads(completed.stdout)
    assert request["snapshot"] == parse_draft(text)
    w.row["draft"] = text
    w.requests[RID] = request
    assert w.run()["state"] == ("検証済み・未記帳" if row[0] in {"変更","追加"} else "修正が必要")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "0", "true", "1e2"])
def test_draft_invalid_quantities(bad):
    w = World()
    text = draft_text(w,["変更","ID","",bad,"868","","","","","原本で確認済み"])
    with pytest.raises(ValueError): parse_draft(text)


@pytest.mark.parametrize("rid", ["x", "11111111-1111-1111-8111-111111111111", "$(secret)"])
def test_uuid_input_cannot_be_interpreted_as_code(rid):
    w = World()
    with pytest.raises(StateError,match="uuid_invalid"):
        w.run(rid)
    assert w.cas.writes == 0


def test_no_cloud_or_financial_entrypoints_in_new_module():
    import ast
    tree = ast.parse((ROOT / "app/receipt_sheet_validation.py").read_text(encoding="utf-8"))
    modules = [n.module or "" for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any(any(word in m for word in ("auth", "firestore", "flask", "confirmation", "sheets", "pipeline")) for m in modules)
    w = World().corrected()
    w.capture()
    w.run()
    value = w.journal.query(RID)
    value["result"]["financial_write_count"] = 1
    with pytest.raises(StateError): validate_journal(dict(schema=PURPOSE,generation=0,requests={RID:value}))


def prepared_world():
    w = World().corrected()
    _, tag = w.journal.load()  # Capture the private version BEFORE input.
    request = dict(request_id=RID, purpose=PURPOSE, snapshot=w.reader(w.case_id)["request_snapshot"])
    before = deepcopy(w.sheet)
    w.journal.prepare(request, expected_tag=tag)
    assert w.sheet == before  # private save does not require ANY Sheets write
    return w, request


def test_private_only_accept_and_worker_survive_absent_sheet_uuid_and_display_failure():
    w, request = prepared_world()
    receipt = w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    assert receipt["state"] == "受付済み" and "submission" not in w.row
    result = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    assert result["state"] == "検証済み・未記帳"
    writes = w.cas.writes
    with pytest.raises(StateError, match="display_pending"):
        cached_result([], result)  # Sheets publication failed, private result survives
    assert w.journal.query(RID) == result and w.cas.writes == writes
    entry = result_cache_entry(result)
    assert cached_result([entry, deepcopy(entry)], result) == entry


def test_private_prepared_ticket_is_not_a_registration_or_worker_permission():
    w, request = prepared_world()
    with pytest.raises(StateError, match="replay"):
        execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    with pytest.raises(StateError, match="display_pending"):
        result_cache_entry(w.journal.query(RID))
    assert w.cas.writes == 1


def test_unknown_is_transient_only_and_never_an_appendable_cache_entry():
    w, request = prepared_world()
    w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    transient = dict(w.journal.query(RID), state=UNKNOWN)
    before = deepcopy(w.sheet["cases"])
    assert project_result(before, transient)[0]["result"]["state"] == UNKNOWN
    with pytest.raises(StateError, match="display_pending"):
        result_cache_entry(transient)
    with pytest.raises(StateError, match="display_pending"):
        cached_result([], transient)
    assert w.sheet["cases"] == before
    assert w.journal.query(RID)["state"] == "受付済み"


def test_terminal_query_after_transient_unknown_has_no_same_uuid_cache_conflict():
    w, request = prepared_world()
    w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    cache = []
    transient = dict(w.journal.query(RID), state=UNKNOWN)
    with pytest.raises(StateError):
        cache.append(result_cache_entry(transient))
    assert cache == []
    terminal = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    cache.append(result_cache_entry(terminal))
    writes = w.cas.writes
    for _ in range(2):
        fresh = w.journal.query(RID)
        assert fresh["request_id"] == transient["request_id"]
        assert cached_result(cache, fresh)["state"] == "検証済み・未記帳"
    assert w.cas.writes == writes
    assert terminal["result"]["financial_write_count"] == 0


def test_same_uuid_worker_reexecution_refused_after_private_only_accept():
    w, request = prepared_world()
    w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    result = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    writes = w.cas.writes
    for action in (lambda: w.journal.accept_prepared(RID, request["snapshot"]["digest"]),
                   lambda: execute_accepted_request(RID, reader=w.reader, journal=w.journal)):
        with pytest.raises(StateError, match="replay"):
            action()
    assert w.cas.writes == writes and result["result"]["financial_write_count"] == 0


def test_competing_prepared_uuids_for_one_case_only_one_private_acceptance():
    w, first = prepared_world()
    _, tag = w.journal.load()
    second = dict(deepcopy(first), request_id=RID2)
    second["snapshot"]["operations"][0]["changes"]["amount"] = 867
    raw = deepcopy(second["snapshot"])
    del raw["digest"]
    second["snapshot"]["digest"] = digest(raw)
    w.journal.prepare(second, expected_tag=tag)
    def accept(req):
        try:
            w.journal.accept_prepared(req["request_id"], req["snapshot"]["digest"])
            return True
        except StateError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(accept, [first, second])).count(True) == 1
    assert sum(r["state"] == "受付済み" for r in w.cas.value["requests"].values()) == 1


def test_acceptance_response_loss_keeps_private_claim_and_never_reaccepts():
    w, request = prepared_world()
    w.cas.fault = "after"
    with pytest.raises(TimeoutError):
        w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    w.cas.fault = None
    assert w.journal.query(RID)["state"] == "受付済み"
    with pytest.raises(StateError, match="replay"):
        w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    assert w.cas.writes == 2


def test_worker_claim_response_loss_does_not_allow_a_second_worker():
    w, request = prepared_world()
    w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    w.cas.fault = "after"
    assert execute_accepted_request(RID, reader=w.reader, journal=w.journal)["state"] == UNKNOWN
    w.cas.fault = None
    with pytest.raises(StateError, match="replay"):
        execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    assert w.cas.writes == 3


def changed_request(first, *, case_id=None, source_id=None):
    request = dict(deepcopy(first), request_id=RID2)
    snapshot = request["snapshot"]
    if case_id is not None:
        snapshot["case_id"] = case_id
    if source_id is not None:
        snapshot["source"]["source_id"] = source_id
    snapshot["operations"] = []
    del snapshot["digest"]
    snapshot["digest"] = digest(snapshot)
    return request


def synchronize_loads(w):
    """Force two contenders to read the SAME version before either CAS."""
    generation, read = w.cas.generation, w.cas.read_versioned
    barrier = Barrier(2, timeout=5)
    def synchronized():
        value, tag = read()
        if tag == f'"{generation}"':
            barrier.wait()
        return value, tag
    w.cas.read_versioned = synchronized


@pytest.mark.parametrize("changed", [False, True])
def test_latest_ticket_rejects_old_uuid_and_preserves_immutable_history(changed):
    w, first = prepared_world()
    assert w.cas.value["schema"] == JOURNAL_SCHEMA
    original = w.journal.query(RID)
    _, tag = w.journal.load()
    second = changed_request(first) if changed else dict(deepcopy(first), request_id=RID2)
    w.journal.prepare(second, expected_tag=tag)
    assert w.cas.value["active_prepared"] == {w.case_id: RID2}
    before = deepcopy(w.cas.value)
    # A superseded UUID is rejected even if its old snapshot still matches.
    with pytest.raises(StateError, match="replay"):
        w.journal.accept_prepared(RID, first["snapshot"]["digest"])
    with pytest.raises(StateError, match="replay"):
        w.journal.prepare(first, expected_tag=w.journal.load()[1])
    assert w.cas.value == before and w.cas.writes == 2
    assert w.journal.query(RID) == original
    accepted = w.journal.accept_prepared(RID2, second["snapshot"]["digest"])
    second["snapshot"]["operations"].append({"changed": "caller mutation"})
    accepted["snapshot"]["operations"].append({"changed": "returned mutation"})
    assert w.journal.query(RID2)["snapshot"] == before["requests"][RID2]["snapshot"]
    assert w.cas.value["active_prepared"] == {}


@pytest.mark.parametrize("tag", ['"0"', 'W/"1"', None])
def test_delayed_or_unproven_prepare_never_replaces_latest_ticket(tag):
    w, first = prepared_world()
    before = deepcopy(w.cas.value)
    with pytest.raises(StateError, match="preparation_stale"):
        w.journal.prepare(changed_request(first), expected_tag=tag)
    assert w.cas.value == before and w.cas.attempts == 1


def test_preparation_requires_explicit_pre_capture_etag_and_frozen_snapshot():
    w, first = prepared_world()
    with pytest.raises(TypeError):
        w.journal.prepare(changed_request(first))
    frozen = w.journal.query(RID)["snapshot"]
    first["snapshot"]["operations"].clear()
    assert w.journal.query(RID)["snapshot"] == frozen
    # Current input differs, so passing its independently rebuilt digest stops.
    w.row["draft"]["operations"] = []
    fresh = w.reader(w.case_id)["request_snapshot"]
    with pytest.raises(StateError, match="replay"):
        w.journal.accept_prepared(RID, fresh["digest"])
    assert w.cas.writes == 1
    w.journal.accept_prepared(RID, frozen["digest"])
    result = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    assert result["state"] == "保留" and result["snapshot"] == frozen
    assert result["result"]["financial_write_count"] == 0


@pytest.mark.parametrize("operation", ["prepare", "accept", "accept_alias", "claim"])
def test_forced_same_etag_cas_race_has_one_winner(operation):
    w, first = prepared_world()
    second = changed_request(first, case_id="other-case" if operation == "accept_alias" else None)
    if operation == "accept_alias":
        w.journal.prepare(second, expected_tag=w.journal.load()[1])
    if operation == "claim":
        w.journal.accept_prepared(RID, first["snapshot"]["digest"])
    tag = w.journal.load()[1]
    before_writes = w.cas.writes
    synchronize_loads(w)
    def compete(index):
        try:
            if operation == "prepare":
                request = second if index == 0 else dict(deepcopy(second), request_id=RID3)
                w.journal.prepare(request, expected_tag=tag)
            elif operation == "accept_alias":
                request = first if index == 0 else second
                w.journal.accept_prepared(request["request_id"], request["snapshot"]["digest"])
            elif operation == "accept":
                w.journal.accept_prepared(RID, first["snapshot"]["digest"])
            else:
                w.journal.transition(RID, "検証中")
            return True
        except StateError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(compete, range(2))).count(True) == 1
    assert w.cas.writes == before_writes + 1
    assert w.cas.attempts == before_writes + 2
    assert validate_journal(w.cas.value) == w.cas.value


@pytest.mark.parametrize("state", ["受付済み", "検証中", UNKNOWN])
@pytest.mark.parametrize("identity", ["same", "other_case", "other_source"])
def test_preparation_blocks_related_unresolved_work(state, identity):
    w, first = prepared_world()
    w.journal.accept_prepared(RID, first["snapshot"]["digest"])
    if state != "受付済み":
        w.journal.transition(RID, state)
    _, tag = w.journal.load()
    second = changed_request(first, case_id="other-case" if identity == "other_case" else None,
                             source_id="other-source" if identity == "other_source" else None)
    before = deepcopy(w.cas.value)
    attempts = w.cas.attempts
    with pytest.raises(StateError, match="duplicate_or_unresolved"):
        w.journal.prepare(second, expected_tag=tag)
    assert w.cas.value == before and w.cas.attempts == attempts


def test_final_history_blocks_same_snapshot_and_source_alias_but_allows_new_input():
    w, first = prepared_world()
    w.journal.accept_prepared(RID, first["snapshot"]["digest"])
    execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    _, tag = w.journal.load()
    for second in (dict(deepcopy(first), request_id=RID2), changed_request(first, case_id="other-case")):
        with pytest.raises(StateError, match="duplicate_or_unresolved"):
            w.journal.prepare(second, expected_tag=tag)
    second = changed_request(first)
    w.journal.prepare(second, expected_tag=tag)
    assert w.journal.accept_prepared(RID2, second["snapshot"]["digest"])["state"] == "受付済み"


@pytest.mark.parametrize("operation", ["prepare", "accept"])
@pytest.mark.parametrize("fault", ["before", "after", "conflict", "readback"])
def test_unknown_ticket_io_never_retries_or_repairs(operation, fault):
    w, first = prepared_world()
    tag, writes, attempts = w.journal.load()[1], w.cas.writes, w.cas.attempts
    second = changed_request(first)
    if fault == "readback":
        read = w.cas.read_versioned
        def lost_readback():
            if w.cas.writes > writes:
                raise TimeoutError("synthetic lost readback")
            return read()
        w.cas.read_versioned = lost_readback
    else:
        w.cas.fault = fault
    with pytest.raises((TimeoutError, StateError)):
        if operation == "prepare":
            w.journal.prepare(second, expected_tag=tag)
        else:
            w.journal.accept_prepared(RID, first["snapshot"]["digest"])
    assert w.cas.attempts == attempts + 1
    assert w.cas.writes == writes + (fault in {"after", "readback"})
    assert validate_journal(w.cas.value) == w.cas.value
    if fault == "readback":
        w.cas.read_versioned = read
    w.cas.fault = None
    before = deepcopy(w.cas.value)
    w.journal.query(RID)  # Reconciliation is read-only, never a new attempt.
    assert w.cas.value == before and w.cas.attempts == attempts + 1


def test_three_event_sequence_accepts_at_most_once_without_apps_script_execution():
    w, first = prepared_world()
    # Synthetic port-level sequence only, NOT a live handler/device assertion.
    ready, register = "ready:" + RID, "register:" + RID
    accepted = 0
    for old, new in [(ready, register), (register, ready), (ready, register)]:
        if (old, new) != (ready, register):
            continue
        try:
            w.journal.accept_prepared(RID, first["snapshot"]["digest"])
            accepted += 1
        except StateError:
            pass
    assert accepted == 1 and w.cas.writes == 2
    assert sum(r["state"] == "受付済み" for r in w.cas.value["requests"].values()) == 1
    result = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    assert result["result"]["financial_write_count"] == 0
    assert result["result"]["posting_authority"] is False


@pytest.mark.parametrize("state", [PREPARED, "受付済み", "検証中", UNKNOWN, "検証済み・未記帳"])
def test_legacy_journal_is_read_only_and_never_infers_latest_or_auto_migrates(state):
    w, first = prepared_world()
    if state != PREPARED:
        w.journal.accept_prepared(RID, first["snapshot"]["digest"])
    if state == "検証済み・未記帳":
        execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    elif state in {"検証中", UNKNOWN}:
        w.journal.transition(RID, state)
    w.cas.value["schema"] = PURPOSE
    del w.cas.value["active_prepared"]
    before = deepcopy(w.cas.value)
    attempts = w.cas.attempts
    record = w.journal.query(RID)
    assert record["state"] == state
    if state == "検証済み・未記帳":
        assert result_cache_entry(record)["state"] == state
    _, tag = w.journal.load()
    for action in (lambda: w.journal.prepare(changed_request(first), expected_tag=tag),
                   lambda: w.journal.accept_prepared(RID, first["snapshot"]["digest"]),
                   lambda: w.journal.accept(changed_request(first)),
                   lambda: w.journal.transition(RID, "検証中"),
                   lambda: w.journal.save(before, tag)):
        with pytest.raises(StateError, match="migration_required"):
            action()
    assert w.cas.value == before and w.cas.attempts == attempts


@pytest.mark.parametrize("change", ["missing", "unknown_uuid", "other_case", "consumed", "not_dict", "legacy_extra"])
def test_corrupt_active_index_is_not_repaired(change):
    w, _ = prepared_world()
    if change == "missing": del w.cas.value["active_prepared"]
    elif change == "unknown_uuid": w.cas.value["active_prepared"][w.case_id] = RID2
    elif change == "other_case": w.cas.value["active_prepared"] = {"other-case": RID}
    elif change == "consumed": w.cas.value["requests"][RID]["state"] = "受付済み"
    elif change == "not_dict": w.cas.value["active_prepared"] = []
    else: w.cas.value["schema"] = PURPOSE
    with pytest.raises(StateError, match="journal_invalid"):
        w.journal.load()
    assert w.cas.writes == 1


def test_result_cache_cannot_target_another_case_and_later_edits_survive():
    w, request = prepared_world()
    w.journal.accept_prepared(RID, request["snapshot"]["digest"])
    result = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    entry = result_cache_entry(result)
    wrong = dict(entry, case_id="another-case")
    with pytest.raises(StateError, match="display_pending"):
        cached_result([wrong], result)
    w.row["draft"]["operations"] = []
    w.row["note"] = "登録後のメモを保持"
    before = deepcopy(w.sheet["cases"])
    rendered = project_result(before, result)
    assert w.sheet["cases"] == before
    assert rendered[0]["note"] == "登録後のメモを保持"
    assert rendered[0]["draft"]["operations"] == []
    assert rendered[0]["result"]["draft_notice"] == "未登録の変更あり"


def ordinary_cells(w):
    base = w.reader(w.case_id)["preview"]["original_items"]
    binding = digest([w.case_id, w.source])
    return dict(case_id=w.case_id, source=deepcopy(w.source), items=[dict(
        _item_id=i["item_id"], _binding=binding, name=i["name"], quantity=i["quantity"],
        amount=i["amount"], category=list(i["category"]), kind=i["kind"],
        target="", mode="採用", confirmed=False) for i in base])


def javascript_preparation(w, draft, tag):
    candidate = dict(case_id=w.case_id, source=w.source, draft=draft,
                     original_items=w.reader(w.case_id)["preview"]["original_items"],
                     categories=CATEGORIES, context_digest=digest(w.anchor))
    completed = subprocess.run(["node", str(ROOT / "apps-script/receipt-validation/Code.test.cjs"), "--prepare"],
        input=json.dumps(dict(candidate=candidate, tag=tag), ensure_ascii=True, allow_nan=False),
        capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(completed.stdout)


@pytest.mark.parametrize("form", ["cells", "text", "operations"])
@pytest.mark.parametrize("quantity,name,expected_name", [
    (2.0, " \t商品🍎 ", "商品🍎"), (0.125, "商品🍎", "商品🍎"),
    (0.001, "\u3000日本語\u00a0", "日本語"), (9999999.999, "商品", "商品"),
    (2, "👩‍💻", "👩‍💻"), (2, "e\u0301", "e\u0301"),
])
def test_mvp_preparation_cross_language_snapshot_digest_and_etag(form, quantity, name, expected_name):
    w = World()
    _, tag = w.journal.load()  # Private version precedes both input readers.
    cells = ordinary_cells(w)
    cells["items"][1].update(name=name, quantity=quantity, amount=868, confirmed=True)
    draft = cells if form == "cells" else draft_text(w, ["変更",w.sheet["candidates"][1][1][0],
        name,str(quantity),"868","","","","","原本で確認済み"])
    if form == "operations":
        draft = dict(case_id=w.case_id, source=w.source, operations=[edit(
            w.sheet["candidates"][1][1][0], name=name, quantity=quantity, amount=868, category=list(CATEGORIES[0]))])
    captured = javascript_preparation(w, draft, tag)
    assert captured["error"] is None and captured["writes"] == 1
    assert captured["trace"][:2] == ["etag", "input"]
    assert captured["trace"][-4:] == ["fixture_uuid", "prepare", "cas", "readback"]
    assert captured["trace"].count("etag") == 1
    assert captured["options"] == {"expected_tag": tag}
    w.row["draft"] = draft
    fresh = w.reader(w.case_id)
    assert captured["request"]["snapshot"] == fresh["request_snapshot"]
    assert captured["request"]["snapshot"]["digest"] == fresh["request_snapshot"]["digest"]
    changes = fresh["request_snapshot"]["operations"][0]["changes"]
    assert changes["name"] == expected_name and changes["quantity"] == quantity
    # Exercise the real existing Python CAS with the options emitted by JS.
    w.journal.prepare(captured["request"], **captured["options"])
    w.journal.accept_prepared(RID, fresh["request_snapshot"]["digest"])
    result = execute_accepted_request(RID, reader=w.reader, journal=w.journal)
    assert result["state"] == "検証済み・未記帳"
    assert result["result"]["financial_write_count"] == 0


@pytest.mark.parametrize("form", ["cells", "text", "operations"])
@pytest.mark.parametrize("quantity,name", [
    (0.00001,"商品"), (0.0000001,"商品"), (10**21,"商品"),
    (0,"商品"), (-1,"商品"), (0.1234,"商品"), (10000000,"商品"),
    (2.0000000000000004,"商品"), (True,"商品"),
    (2,"\u0085商品"), (2,"商品\ufeff"), (2,"商品\x00"), (2,"\ud800"),
])
def test_mvp_invalid_quantity_or_name_rejected_by_both_languages(form, quantity, name):
    w = World()
    _, tag = w.journal.load()
    cells = ordinary_cells(w)
    cells["items"][1].update(name=name, quantity=quantity, amount=868, confirmed=True)
    draft = cells if form == "cells" else draft_text(w, ["変更",w.sheet["candidates"][1][1][0],
        name,str(quantity),"868","","","","","原本で確認済み"])
    if form == "operations":
        draft = dict(case_id=w.case_id, source=w.source, operations=[edit(
            w.sheet["candidates"][1][1][0], name=name, quantity=quantity, amount=868)])
    captured = javascript_preparation(w, draft, tag)
    assert captured["error"] and captured["writes"] == 0 and captured["rule"] is None
    assert "fixture_uuid" not in captured["trace"]
    w.row["draft"] = draft
    with pytest.raises(StateError):
        w.reader(w.case_id)
    assert w.cas.attempts == 0


def test_receipt_normalization_does_not_round_fold_or_accept_nonfinite_numbers():
    for quantity in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="quantity_invalid"):
            receipt_quantity(quantity)
    assert receipt_quantity(2.0) == 2 and type(receipt_quantity(2.0)) is int
    assert receipt_name(" \t商品🍎\r\n") == "商品🍎"
    assert receipt_name("é") != receipt_name("e\u0301")
    assert receipt_name("商品 A") != receipt_name("商品A")
    with pytest.raises(ValueError, match="name_invalid"):
        receipt_name("  ")


def test_legacy_empty_edit_name_still_means_retain_original():
    w = World()
    _, tag = w.journal.load()
    draft = draft_text(w, ["変更",w.sheet["candidates"][1][1][0],"  ","2","868","","","","","原本で確認済み"])
    captured = javascript_preparation(w, draft, tag)
    w.row["draft"] = draft
    assert captured["error"] is None
    assert captured["request"]["snapshot"] == w.reader(w.case_id)["request_snapshot"]
    assert "name" not in captured["request"]["snapshot"]["operations"][0]["changes"]


def test_ordinary_product_cells_need_no_typed_ids_hashes_or_delimiters():
    w = World()
    cells = ordinary_cells(w)
    cells["items"][1].update(name="原本の商品名", quantity=2, amount=868, confirmed=True)
    cells["items"].append(dict(_item_id="new:emptyslot", _binding=digest([w.case_id,w.source]),
        name="", quantity="", amount="", category=list(CATEGORIES[0]), kind="product",
        target="", mode="採用", confirmed=False))
    w.row["draft"] = cells
    fresh = w.reader(w.case_id)
    assert fresh["preview"]["item_total"] == 1368
    assert fresh["preview"]["status"] == "preview_balanced"
    assert fresh["request_snapshot"]["operations"][0]["changes"]["quantity"] == 2
    candidate = dict(case_id=w.case_id, source=w.source, draft=cells,
                     original_items=fresh["preview"]["original_items"], categories=CATEGORIES,
                     context_digest=digest(w.anchor))
    completed = subprocess.run(["node", str(ROOT / "apps-script/receipt-validation/Code.test.cjs"), "--capture"],
        input=json.dumps(candidate, ensure_ascii=True), capture_output=True, text=True, encoding="utf-8", check=True)
    assert json.loads(completed.stdout)["snapshot"] == fresh["request_snapshot"]
    cells["items"][1]["_binding"] = digest("moved from another case")
    with pytest.raises(StateError):
        w.reader(w.case_id)


@pytest.mark.parametrize("kind", ["product", "tax"])
def test_ordinary_addition_and_exclusion_or_tax_target_use_automatic_ids(kind):
    w = World((500,28),638) if kind == "product" else World()
    cells = ordinary_cells(w)
    if kind == "product":
        cells["items"][1].update(mode="除外", confirmed=True)
    cells["items"].append(dict(_item_id="new:autoslot1", _binding=digest([w.case_id,w.source]),
        name="新しい商品" if kind == "product" else "外税", quantity=1, amount=138,
        category=list(CATEGORIES[0]), kind=kind, target=1 if kind == "tax" else "",
        mode="採用", confirmed=True))
    w.row["draft"] = cells
    fresh = w.reader(w.case_id)
    assert fresh["preview"]["status"] == "preview_balanced"
