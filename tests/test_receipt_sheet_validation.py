"""Entirely synthetic, offline Sheets -> UUID request -> Python -> projection."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import socket
import subprocess
from threading import RLock

import pytest

from app.category_rules import CategoryRule
from app.drive_run_state import StateError
from app.page_receipt_model import digest
from app.reconciliation import parse_import_rows
from app.receipt_sheet_draft import parse_draft
from app.receipt_sheet_validation import (PURPOSE, UNKNOWN, IndependentReceiptReader,
    ValidationJournal, empty_journal, execute_request, project_result, validate_journal)
from test_receipt_item_correction_plan import scenario, edit, add, SOURCE, CATEGORIES

RID = "11111111-1111-4111-8111-111111111111"
RID2 = "22222222-2222-4222-8222-222222222222"
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
        self.fault = None
        self.weak = False

    def read_versioned(self):
        with self.lock:
            return deepcopy(self.value), 'W/"0"' if self.weak else f'"{self.generation}"'

    def replace_versioned(self, value, tag):
        with self.lock:
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
