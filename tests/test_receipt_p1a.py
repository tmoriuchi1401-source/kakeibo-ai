"""PR-A mandatory 23 groups; fake HTTP/credentials, never live dispatch."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
import logging
from pathlib import Path
import subprocess
from urllib.parse import unquote

import pytest
import yaml

from app.drive_run_state import StateError
from app.models import ReceiptItem, ReceiptResult
from app.page_receipt_model import digest
from app.receipt_review_snapshot import candidate_rows
from app.receipt_sheets_authority import HEADERS, UNKNOWN, SheetAuthority, canonical, check_row, display_record, execute, result_formula
from app.receipt_sheets_context import expected_context
from app.receipt_sheets_worker import CONFIG, GooglePort, make_binding, run, verify_delivery_history
from scripts.build_receipt_sheets_bundle import bundle_files, profile_for

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/receipt_p1a_case.json").read_text(encoding="utf-8"))
RID = FIXTURE["request_id"]
OTHER_RID = "d49021de-853e-4ec4-9253-2731021d3ced"
SHA = FIXTURE["worker_sha"]


@pytest.fixture(autouse=True)
def financial_and_network_calls_are_zero(monkeypatch):
    """A forbidden call fails even if a broad UNKNOWN handler swallows it."""
    import socket
    from app.sheets import SheetsDB
    calls = []
    def deny(*args, **kwargs):
        calls.append("forbidden")
        raise AssertionError("forbidden financial/network call")
    monkeypatch.setattr(SheetsDB, "__init__", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    yield calls
    assert calls == []


def case():
    sid, raw = FIXTURE["source_id"], FIXTURE["raw"].encode()
    cid = "receipt:" + sid
    source = dict(source_id=sid, sha256=sha256(raw).hexdigest(),
                  mime_type="image/png", classification="normal", transaction_kind="purchase")
    categories = [["食費", "食品"], ["日用品", "消耗品"]]
    result = ReceiptResult(merchant="合成P1A商店", date="2026-11-20", total=1500,
        items=[ReceiptItem(name=n, amount=a, major_category="食費", minor_category="食品")
               for n, a in [("合成商品A", 600), ("合成商品B", 762)]])
    imported = [cid, "2026-11-20T00:00:00+09:00", "receipt", sid, "2026-11-20",
                "合成P1A商店", 1500, "", "要確認", "", "c"*64, "明細合計1362≠レシート合計1500"]
    candidates = candidate_rows(cid, "c"*64, result)
    anchor = dict(source=source, candidate_digest=digest(candidates), import_digest=digest(imported),
                  policy_digest=digest([categories, []]),
                  total_confirmation=dict(source=source, receipt_total=1500,
                                          actor_id=digest("synthetic statement, not identity proof")))
    machine = [cid, sid, "", source["sha256"], candidates[0][0], candidates[1][0],
               "new:p1a_slot3", RID, "P1A-OFFLINE", "b"*64]
    entry = dict(anchor=anchor, import_row=imported, candidate_rows=candidates, categories=categories,
                 rules=[], decoder=dict(machine=machine, kind_label="一般（完全合成）"))
    expected = expected_context(cid, entry)
    row = [cid, entry["decoder"]["kind_label"], "synthetic-original-link", 1500, 1500, 0,
           "編集中・未登録", "保持する合成メモ", "register:"+RID]
    for i, old in enumerate(expected["original_items"]):
        row.extend([old["name"], 1, 600 if i == 0 else 900, *old["category"],
                    "商品", "指定なし", "採用", i == 1])
    row.extend(["", "", "", "食費", "食品", "商品", "指定なし", "採用", False])
    row.extend(machine)
    c = make_binding(dict(cases={cid:entry}), spreadsheet_id=FIXTURE["spreadsheet_id"],
        folder_id=FIXTURE["folder_id"], owner_email="owner@synthetic.invalid",
        service_email="worker@synthetic.invalid", ref="review/receipt-sheets-p1a-20261120")
    c["enabled"] = True
    return dict(source=source, raw=raw, cid=cid, row=row, entry=entry, expected=expected), c


def registration(tmp_path, **changes):
    w, c = case()
    folder = tmp_path / "bundle"
    folder.mkdir(exist_ok=True)
    files = bundle_files(binding=dict(c, enabled=False), script_id=FIXTURE["script_id"], source_commit=SHA)
    for name, raw in files.items():
        (folder / name).write_bytes(raw)
    payload = dict(config=c, ui=[w["row"]], table=[HEADERS], request_id=RID,
                   script_id=FIXTURE["script_id"], bundle_dir=str(folder))
    payload.update(changes)
    completed = subprocess.run(["node", "apps-script/receipt-validation/Sheets.test.cjs"],
        input=json.dumps(payload, ensure_ascii=True), encoding="utf-8",
        capture_output=True, check=True, cwd=ROOT)
    return w, c, json.loads(completed.stdout)


def env_for(c, **changes):
    return dict({CONFIG:canonical(c), "RECEIPT_VALIDATION_ENABLED":"true",
        "GITHUB_REPOSITORY":c["repository"], "GITHUB_EVENT_NAME":"workflow_dispatch",
        "GITHUB_RUN_ATTEMPT":"1", "GITHUB_REF":"refs/heads/"+c["ref"],
        "RECEIPT_VALIDATION_REF":c["ref"], "RECEIPT_VALIDATION_SHA":SHA, "GITHUB_SHA":SHA,
        "RECEIPT_REQUEST_ID":RID, "GITHUB_RUN_ID":"100",
        "GOOGLE_SERVICE_ACCOUNT_JSON":json.dumps(dict(client_email=c["service_email"]))}, **changes)


def history(**changes):
    item = dict(display_title="Category request "+RID, id=100, run_attempt=1,
                head_sha=SHA, event="workflow_dispatch")
    item.update(changes)
    return dict(total_count=1, workflow_runs=[item])


class Port:
    def __init__(self, rows):
        self.rows, self.writes, self.fault, self.calls = deepcopy(rows), [], None, []
        self.bad_source = None
        self.media_reads = 0
    def read(self):
        if self.writes and self.fault == ("readback", len(self.writes)):
            raise TimeoutError("DUMMY_PRIVATE_CANARY")
        return deepcopy(self.rows)
    def update(self, number, tail):
        self.writes.append((number, deepcopy(tail)))
        stage = len(self.writes)
        if self.fault == ("before", stage): raise TimeoutError("DUMMY_PRIVATE_CANARY")
        self.rows[number-1][7:] = deepcopy(tail)
        if self.fault == ("after", stage): raise TimeoutError("DUMMY_PRIVATE_CANARY")


class Response:
    status_code = 200
    def __init__(self, value=None, content=b""):
        self.value, self.content = value, content
    def json(self): return deepcopy(self.value)


class Session:
    """Requests fake checks every destination, method and payload."""
    def __init__(self, w, c, port):
        self.w, self.c, self.port = w, c, port
    def get(self, url, **kw):
        assert kw["timeout"] == 20 and kw["allow_redirects"] is False
        self.port.calls.append(("GET", url))
        if url.startswith("https://sheets.googleapis.com/v4/spreadsheets/"+self.c["spreadsheet_id"]+"/values/"):
            assert unquote(url).endswith("'_receipt_requests'!A1:J1001")
            return Response(dict(values=self.port.read()))
        assert url == "https://www.googleapis.com/drive/v3/files/"+self.w["source"]["source_id"]
        if kw["params"] == {"alt":"media"}:
            self.port.media_reads += 1
            changed = self.port.bad_source == "bytes" or (self.port.bad_source == "bytes_after" and self.port.media_reads == 2)
            return Response(content=b"changed" if changed else self.w["raw"])
        assert kw["params"] == {"fields":"id,trashed,mimeType,owners(emailAddress)"}
        m = dict(id=self.w["source"]["source_id"], trashed=False, mimeType="image/png",
                 owners=[dict(emailAddress=self.c["owner_email"])])
        if self.port.bad_source == "owner": m["owners"] = []
        if self.port.bad_source == "mime": m["mimeType"] = "application/pdf"
        if self.port.bad_source == "id": m["id"] = "different-synthetic-source"
        return Response(m)
    def put(self, url, **kw):
        assert url == ("https://sheets.googleapis.com/v4/spreadsheets/"+self.c["spreadsheet_id"]+
                       "/values/%27_receipt_requests%27%21H2%3AJ2")
        assert kw["params"] == {"valueInputOption":"RAW"}
        assert kw["timeout"] == 20 and kw["allow_redirects"] is False
        self.port.calls.append(("PUT", url))
        self.port.update(2, kw["json"]["values"][0])
        return Response({})


def worker(w, c, port, *, changes=None, delivery=None):
    connections = []
    def delivery_check(env, _c): verify_delivery_history(delivery or history(), env)
    @contextmanager
    def connection(binding, info):
        connections.append(True)
        assert info["client_email"] == binding["service_email"]
        yield GooglePort(Session(w, binding, port), binding)
    outcome = run(env_for(c, **(changes or {})), connection=connection, delivery_check=delivery_check)
    return outcome, connections


def shown(w, port):
    return display_record(RID, port.rows, receipt_id=w["cid"], source=w["source"])


def test_T01_normal_one_request_to_verified_unposted(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.receipt_sheets_authority")
    w, c, js = registration(tmp_path, setup=True)
    assert js["writes"] == js["gateGets"] == len(js["dispatches"]) == 1
    assert js["dispatches"] == [dict(ref=c["ref"], inputs=dict(request_id=RID))]
    assert js["ui"] == [w["row"]] and not js["stopped"]
    assert js["enabled"] is False and js["token_copy"] is False
    assert js["unrelated_token_unchanged"] and js["triggerMutations"] == 0
    assert js["audit"]["STATE"] == "DISABLED" and js["audit"]["PAT_PRESENT"] == "FAIL"
    p = Port(js["rows"])
    assert worker(w, c, p)[0] == (0, "検証済み・未記帳")
    assert len(p.writes) == 2 and shown(w, p) == "検証済み・未記帳"
    assert p.rows[1][:7] == js["rows"][1][:7]
    result = json.loads(p.rows[1][8])
    assert result["posting_authority"] is False and result["financial_write_count"] == 0
    assert [r.message.split("state=")[1] for r in caplog.records] == ["受付済み", "検証中", "検証済み・未記帳"]
    assert "保持する合成メモ" not in js["rows"][1][5]


@pytest.mark.parametrize("gate", ["true", "false", "unknown"])
def test_T02_local_off_has_no_io_or_uuid_consumption(tmp_path, gate):
    _, c = case()
    _, _, js = registration(tmp_path, config=dict(c, enabled=False), gate_value=gate)
    assert js["calls"] == js["propertyWrites"] == js["dispatches"] == []
    assert js["writes"] == js["gateGets"] == 0 and len(js["rows"]) == 1


@pytest.mark.parametrize("value", ["false", "", "TRUE", "1", "unknown"])
def test_T03_github_off_rejects_before_first_persistent_change(tmp_path, value):
    w, _, js = registration(tmp_path, gate_value=value, repeat=True)
    assert js["writes"] == 0 and not js["dispatches"] and not js["stopped"]
    assert js["propertyWrites"] == [] and js["ui"] == [w["row"]]
    assert js["gateGets"] == 2  # two fake events, one GET each
    error = "receipt_sheets_acceptance_off" if value == "false" else "receipt_sheets_acceptance_not_confirmed"
    assert js["errors"] == [error]*2


@pytest.mark.parametrize("change", [
    dict(gate_status=s) for s in [301, 401, 403, 404, 500]
] + [dict(fault="gate_throw"), dict(gate_raw="{"), dict(gate_raw="null"),
     dict(gate_name="wrong"), dict(gate_raw='{"name":"RECEIPT_VALIDATION_ENABLED"}')])
def test_T04_gate_unknown_fails_closed_without_retry(tmp_path, change):
    _, _, js = registration(tmp_path, **change)
    assert js["gateGets"] == 1 and js["writes"] == 0
    assert js["dispatches"] == js["propertyWrites"] == []
    assert js["errors"] == ["receipt_sheets_acceptance_not_confirmed"]


@pytest.mark.parametrize("change", [
    dict(GITHUB_SHA="b"*40), dict(RECEIPT_VALIDATION_SHA="b"*40),
    dict(RECEIPT_VALIDATION_REF="review/receipt-sheets-other"), dict(GITHUB_REF="refs/heads/main"),
    dict(RECEIPT_VALIDATION_ENABLED="false"), dict(GITHUB_EVENT_NAME="push"),
    dict(GITHUB_REPOSITORY="unapproved/repo"), dict(ACTIONS_STEP_DEBUG="true")])
def test_T05_ref_sha_and_activation_mismatch_before_connection(tmp_path, change):
    w, c, js = registration(tmp_path)
    p = Port(js["rows"])
    result, connections = worker(w, c, p, changes=change)
    assert result == (3, UNKNOWN) and connections == p.writes == p.calls == []
    assert p.rows == js["rows"]


def test_T07_definite_off_does_not_consume_or_automatically_rearm(tmp_path):
    w, _, off = registration(tmp_path, gate_value="false")
    assert off["ui"][0][8] == "register:"+RID
    assert off["writes"] == 0 and not off["stopped"] and off["propertyWrites"] == []
    _, _, audit = registration(tmp_path, action="audit")
    assert audit["gateGets"] == 0 and audit["writes"] == 0 and not audit["dispatches"]
    assert audit["audit"]["AUDIT"] == "PASS"
    _, _, on = registration(tmp_path)  # offline explicitly prepared ready -> register event
    assert on["writes"] == len(on["dispatches"]) == 1
    assert on["ui"] == [w["row"]]


@pytest.mark.parametrize("change", [
    dict(event=dict(oldValue="TRUE")), dict(request_id=OTHER_RID),
    dict(multi_cell=True), dict(range_tab="別タブ"), dict(range_sheet_id="different-synthetic-sheet"),
    dict(owner_override="foreign@synthetic.invalid"), dict(script_id="different-synthetic-script")])
def test_T08_wrong_event_or_target_cannot_accept(tmp_path, change):
    _, _, js = registration(tmp_path, **change)
    assert js["writes"] == js["gateGets"] == 0 and js["dispatches"] == js["propertyWrites"] == []


def test_T09_existing_uuid_row_is_rejected_and_old_runs_are_not_new(tmp_path):
    _, _, first = registration(tmp_path)
    _, _, second = registration(tmp_path, table=first["rows"])
    assert second["writes"] == second["gateGets"] == 0 and second["dispatches"] == []
    assert second["rows"] == first["rows"] and "receipt_sheets_replay_rejected" in second["errors"]
    past = history(status="completed", conclusion="skipped")
    with pytest.raises(StateError):
        verify_delivery_history(past, dict(env_for(case()[1]), GITHUB_RUN_ID="101"))


def test_T10_double_event_has_one_row_one_dispatch(tmp_path):
    _, _, js = registration(tmp_path, repeat=True)
    assert js["writes"] == len(js["dispatches"]) == js["gateGets"] == 1
    assert len(js["rows"]) == 2 and js["repeated"] is None
    assert js["errors"] == ["receipt_sheets_replay_rejected"]


@pytest.mark.parametrize("damage", ["id", "bytes", "bytes_after", "owner", "mime"])
def test_T12_original_identity_change_never_writes_success(tmp_path, damage):
    w, c, js = registration(tmp_path)
    p = Port(js["rows"]); p.bad_source = damage
    assert worker(w, c, p)[0] == (3, UNKNOWN)
    assert len(p.writes) == 1 and p.rows[1][7:9] == ["検証中", ""]
    assert shown(w, p) != "検証済み・未記帳"


@pytest.mark.parametrize("damage", ["missing_secret", "disabled_binding", "wrong_binding", "wrong_sa"])
def test_T13_bad_secrets_or_binding_stop_before_google_token(monkeypatch, tmp_path, damage):
    w, c, js = registration(tmp_path)
    env = env_for(c)
    if damage == "missing_secret": env.pop("GOOGLE_SERVICE_ACCOUNT_JSON")
    if damage == "disabled_binding": env[CONFIG] = canonical(dict(c, enabled=False))
    if damage == "wrong_binding":
        bad = deepcopy(c); next(iter(bad["cases"].values()))["expected_json"] = "{}"; env[CONFIG] = canonical(bad)
    if damage == "wrong_sa": env["GOOGLE_SERVICE_ACCOUNT_JSON"] = '{"client_email":"foreign@synthetic.invalid"}'
    import google.oauth2._client
    tokens = []
    def deny(*a, **kw): tokens.append(True); raise AssertionError("token forbidden")
    monkeypatch.setattr(google.oauth2._client, "jwt_grant", deny)
    assert run(env, delivery_check=lambda *_: None) == (3, UNKNOWN)
    assert tokens == []


@pytest.mark.parametrize("damage", ["amount", "unconfirmed", "quantity", "category"])
def test_T14_amount_and_category_validation_without_financial_write(tmp_path, damage):
    w, _ = case(); row = deepcopy(w["row"])
    if damage == "amount": row[20], row[4], row[5] = 762, 1362, 138
    if damage == "unconfirmed": row[26] = False
    if damage == "quantity": row[19] = -1
    if damage == "category": row[21:23] = ["架空", "分類"]
    w, c, js = registration(tmp_path, ui=[row])
    if damage != "amount":
        assert js["writes"] == 0 and not js["dispatches"]
    else:
        p = Port(js["rows"])
        assert worker(w, c, p)[0] == (2, "修正が必要")
        result = json.loads(p.rows[1][8])
        assert result["issues"] and result["financial_write_count"] == 0


@pytest.mark.parametrize("fault", ["latch_before", "latch_after", "insert_before", "insert_after", "readback", "readback_throw"])
def test_T16_uncertain_save_never_retries_or_dispatches(tmp_path, fault):
    _, _, js = registration(tmp_path, fault=fault, repeat=True)
    assert js["writes"] <= 1 and js["dispatches"] == []
    assert len(js["rows"]) in [1, 2] and js["repeated"] is None
    assert js["stopped"] or fault == "latch_before"
    assert js["output"] is None or js["output"]["state"] == UNKNOWN


@pytest.mark.parametrize("fault", ["dispatch_before", "dispatch_after", "dispatch_status"])
def test_T17_lost_dispatch_response_has_one_attempt_and_keeps_evidence(tmp_path, fault):
    _, _, js = registration(tmp_path, fault=fault, repeat=True)
    assert js["writes"] == len(js["dispatches"]) == 1 and len(js["rows"]) == 2
    assert js["output"]["state"] == UNKNOWN and js["repeated"] is None and js["stopped"]
    assert js["runs"] == (0 if fault == "dispatch_before" else 1)


@pytest.mark.parametrize("fault", ["before", "after", "readback"])
def test_T18_claim_loss_is_unknown_and_not_reclaimed(tmp_path, fault):
    w, c, js = registration(tmp_path)
    p = Port(js["rows"]); p.fault = (fault, 1)
    assert worker(w, c, p)[0] == (3, UNKNOWN) and len(p.writes) == 1
    assert p.rows[1][7] in ["受付済み", "検証中"]


@pytest.mark.parametrize("fault", ["before", "after", "readback"])
def test_T19_result_loss_is_unknown_independent_read_does_not_retry(tmp_path, fault):
    w, c, js = registration(tmp_path)
    p = Port(js["rows"]); p.fault = (fault, 2)
    assert worker(w, c, p)[0] == (3, UNKNOWN) and len(p.writes) == 2
    before = deepcopy(p.rows); p.fault = None
    if fault != "before": assert shown(w, p) == "検証済み・未記帳"
    else: assert shown(w, p) != "検証済み・未記帳"
    assert p.rows == before and len(p.writes) == 2


def test_T20_exception_does_not_force_terminal_or_allow_replay(tmp_path):
    w, c, js = registration(tmp_path); p = Port(js["rows"])
    def crash(_): raise RuntimeError("DUMMY_PRIVATE_CANARY")
    assert execute(RID, authority=SheetAuthority(p), context_check=crash, source_check=crash)["state"] == UNKNOWN
    assert p.rows[1][7:9] == ["検証中", ""] and len(p.writes) == 1
    assert worker(w, c, p)[0] == (3, UNKNOWN) and len(p.writes) == 1


@pytest.mark.parametrize("damage", ["attempt", "another_run", "incomplete", "missing", "wrong_sha"])
def test_T21_rerun_or_history_unknown_stops_before_private_io(tmp_path, damage):
    w, c, js = registration(tmp_path); p = Port(js["rows"])
    h, changes = history(), {}
    if damage == "attempt": changes["GITHUB_RUN_ATTEMPT"] = "2"
    if damage == "another_run":
        h["total_count"] = 2; h["workflow_runs"].append(dict(h["workflow_runs"][0], id=101))
    if damage == "incomplete": h["total_count"] = 101
    if damage == "missing": h = dict(total_count=0, workflow_runs=[])
    if damage == "wrong_sha": h["workflow_runs"][0]["head_sha"] = "b"*40
    assert worker(w, c, p, changes=changes, delivery=h) == ((3, UNKNOWN), [])
    assert p.writes == p.calls == []


@pytest.mark.parametrize("damage", ["header", "columns", "digest", "canonical", "duplicate", "financial_result"])
def test_T22_corrupt_table_or_result_is_not_repaired(tmp_path, damage):
    w, c, js = registration(tmp_path); p = Port(js["rows"])
    if damage == "header": p.rows[0][0] = "wrong"
    if damage == "columns": p.rows[1].pop()
    if damage == "digest": p.rows[1][6] = "b"*64
    if damage == "canonical": p.rows[1][5] += " "
    if damage == "duplicate": p.rows.append(deepcopy(p.rows[1]))
    if damage == "financial_result":
        assert worker(w, c, p)[0][0] == 0
        result = json.loads(p.rows[1][8]); result["financial_write_count"] = 1
        p.rows[1][8] = canonical(result, ensure_ascii=False); p.writes = []
    before = deepcopy(p.rows)
    assert worker(w, c, p)[0] == (3, UNKNOWN)
    assert p.writes == [] and p.rows == before and shown(w, p) == UNKNOWN


def test_T23_existing_workflows_and_main_job_unchanged():
    text = (ROOT / ".github/workflows/category-sheet-request.yml").read_text(encoding="utf-8")
    legacy = text.split("  category-request:", 1)[1].split("\n  receipt-sheets-validation:", 1)[0].strip()
    assert sha256(legacy.encode()).hexdigest() == FIXTURE["main_category_sha256"]
    for name, expected in FIXTURE["unchanged_workflows"].items():
        assert sha256((ROOT/name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() == expected
    flow = yaml.load(text, Loader=yaml.BaseLoader)
    assert flow["concurrency"]["cancel-in-progress"] == "false" and flow["concurrency"]["queue"] == "max"
    assert "|| 'kakeibo-production'" in flow["concurrency"]["group"]
    assert "'receipt-synthetic-validation'" in flow["concurrency"]["group"]
    job = flow["jobs"]["receipt-sheets-validation"]
    for gate in ["github.repository ==", "github.event_name ==", "github.ref ==", "github.sha ==",
                 "vars.RECEIPT_VALIDATION_ENABLED == 'true'", "github.run_attempt == 1",
                 "startsWith(vars.RECEIPT_VALIDATION_REF, 'review/receipt-sheets-')"]:
        assert gate in job["if"]
    assert "env" not in job
    steps = job["steps"]
    assert steps[0]["with"] == {"ref":"$"+"{{ github.sha }}", "persist-credentials":"false"}
    for step in steps[:-1]:
        assert "secrets." not in json.dumps(step)
    worker_env = steps[-1]["env"]
    assert worker_env["GOOGLE_SERVICE_ACCOUNT_JSON"] == "$"+"{{ secrets.RECEIPT_P1_SERVICE_ACCOUNT_JSON }}"
    assert worker_env["RECEIPT_VALIDATION_CONFIG_JSON"] == "$"+"{{ secrets.RECEIPT_P1_CONFIG_JSON }}"
    assert not any(word in json.dumps(job) for word in ["BANK_", "AMAZON_", "PAYPAY_", "RECEIPT_P1_BINDING_SHA256"])
    assert "test \"$(git rev-parse HEAD)\" = \"$GITHUB_SHA\"" in text


@pytest.mark.parametrize("fault", [None, "stop_save", "stop_pat", "stop_busy", "unlock", "stop_before_lock"])
def test_T24_stop_is_independent_and_preserves_rows_ui_and_latches(tmp_path, fault):
    _, _, first = registration(tmp_path)
    properties = {"RECEIPT_SHEETS_STOPPED":"outcome_unconfirmed"} if fault != "stop_before_lock" else {}
    _, _, stopped = registration(tmp_path, action="stop" if fault != "stop_before_lock" else None,
                                  fault=fault, table=first["rows"], properties=properties)
    assert stopped["writes"] == stopped["gateGets"] == 0 and not stopped["dispatches"]
    assert stopped["rows"] == first["rows"] and stopped["ui"] == first["ui"]
    assert stopped["triggerMutations"] == 0 and stopped["unrelated_token_unchanged"]
    if fault not in ["stop_save", "stop_busy"]: assert stopped["enabled"] is False
    if fault in [None, "unlock", "stop_before_lock"]: assert stopped["token_copy"] is False
    if properties: assert stopped["stopped"]
    if fault is None:
        assert stopped["audit"]["STATE_MATCH"] == stopped["audit"]["BINDING"] == "PASS"
        assert stopped["audit"]["PAT_PRESENT"] == "FAIL" and stopped["audit"]["AUDIT"] != "PASS"


def test_T25_transport_financial_deny_fixed_snapshot_and_closed_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="app.receipt_sheets_authority")
    w, c, js = registration(tmp_path)
    frozen = deepcopy(js["rows"]); w["row"][20] = 1
    p = Port(frozen)
    assert worker(w, c, p)[0] == (0, "検証済み・未記帳")
    assert check_row(p.rows[1]) == check_row(frozen[1])
    assert "DUMMY" not in caplog.text and "合成商品" not in caplog.text and "@" not in caplog.text
    assert "RECEIPT_GITHUB_TOKEN" not in json.dumps(js["logs"])
    g = GooglePort(Session(w, c, p), c)
    count = len(p.writes)
    for number, tail in [(1, ["", "", ""]), (1001, ["", "", ""]), (2, [""])]:
        with pytest.raises(StateError): g.update(number, tail)
    assert len(p.writes) == count
    assert all("'_receipt_requests'!" in unquote(url) for method, url in p.calls if method == "PUT")
    formula = result_formula(2)
    assert "n=1" in formula and "financial_write_count" in formula and "UNIQUE" not in formula


@pytest.mark.parametrize("damage", ["missing_profile", "old_script", "old_uuid", "duplicate_function", "duplicate_pure", "audit"])
def test_T26_old_assets_and_function_identity_are_not_overwritten(tmp_path, damage):
    if damage in ["missing_profile", "audit"]:
        _, _, js = registration(tmp_path, missing_profile=damage=="missing_profile", action="audit")
        assert js["propertyWrites"] == js["calls"] == []
        if damage == "audit":
            assert js["audit"]["AUDIT"] == "PASS" and js["audit"]["STATE"] == "ENABLED"
            assert set(js["audit"].values()) <= {"PASS", "ENABLED"}
        else: assert js["audit"]["AUDIT"] == "FAIL"
        return
    w, c = case()
    if damage == "old_script":
        with pytest.raises(ValueError, match="previous trial"):
            profile_for(dict(c, enabled=False), "1IsGyeAI83NWVOVvJhL11zdl9BFCBWM3iaxxlP47_nhKrD-Ev4AgCD1i2", SHA)
    elif damage == "old_uuid":
        entry = deepcopy(w["entry"]); entry["decoder"]["machine"][7] = "c4e61a92-7fe2-4e38-84cd-651c4eef314f"
        c = make_binding(dict(cases={w["cid"]:entry}), spreadsheet_id=c["spreadsheet_id"],
            folder_id=c["folder_id"], owner_email=c["owner_email"], service_email=c["service_email"], ref=c["ref"])
        with pytest.raises(ValueError, match="previous trial"): profile_for(c, FIXTURE["script_id"], SHA)
    else:
        import shutil
        for folder in ["receipt-validation", "receipt-sheets-authority"]:
            shutil.copytree(ROOT / "apps-script" / folder, tmp_path / "apps-script" / folder)
        name = "receipt-validation/Code.gs" if damage == "duplicate_pure" else "receipt-sheets-authority/P1Audit.gs"
        with (tmp_path / "apps-script" / name).open("a") as file:
            file.write("\nfunction receiptTrim_() {}\n")
        with pytest.raises(ValueError, match="duplicate"):
            bundle_files(root=tmp_path, binding=dict(c, enabled=False), script_id=FIXTURE["script_id"], source_commit=SHA)


@pytest.mark.parametrize("setup,code", [("missing", "existing_trigger_required"),
    ("duplicate", "existing_trigger_required"), ("latch", "start_not_ready"),
    ("old_binding", "existing_binding_conflict")])
def test_T01_existing_lifecycle_guards_are_retained(tmp_path, setup, code):
    _, _, js = registration(tmp_path, setup=setup)
    assert js["errors"] == ["receipt_sheets_"+code]
    assert js["writes"] == js["gateGets"] == js["triggerMutations"] == 0 and not js["dispatches"]
    assert js["enabled"] is False and js["unrelated_token_unchanged"]


@pytest.mark.parametrize("index,value", [(0, "foreign-case"), (36, "foreign-case"),
    (37, "different-source"), (39, "b"*64), (45, "c"*64)])
def test_T08_case_source_and_machine_identity_before_save(tmp_path, index, value):
    w, _ = case(); row = deepcopy(w["row"]); row[index] = value
    _, _, js = registration(tmp_path, ui=[row])
    assert js["writes"] == js["gateGets"] == 0 and not js["dispatches"]
    assert js["propertyWrites"] == []


@pytest.mark.parametrize("damage", ["header", "digest", "snapshot", "result"])
def test_T22_receiver_rejects_corruption_before_gate_or_mutation(tmp_path, damage):
    _, _, first = registration(tmp_path); table = deepcopy(first["rows"])
    if damage == "header": table[0][0] = "wrong"
    if damage == "digest": table[1][6] = "b"*64
    if damage == "snapshot": table[1][5] += " "
    if damage == "result": table[1][8] = "{}"
    _, _, js = registration(tmp_path, table=table)
    assert js["writes"] == js["gateGets"] == 0 and js["rows"] == table and not js["dispatches"]


def test_T26_bundle_is_deterministic_local_only_and_requires_empty_destination(tmp_path):
    from scripts.build_receipt_sheets_bundle import build
    _, c = case(); binding = dict(c, enabled=False)
    proof = build(tmp_path/"first", binding=binding, script_id=FIXTURE["script_id"], source_commit=SHA)
    second = build(tmp_path/"second", binding=binding, script_id=FIXTURE["script_id"], source_commit=SHA)
    assert proof == second and proof["deployed"] is False and proof["authorized"] is False
    assert set(proof["files"]) == {"Code.gs", "Sheets.gs", "Connection.gs", "Profile.gs", "P1Audit.gs", "appsscript.json"}
    assert all("gmail" not in s and "drive.readonly" not in s for s in proof["oauth_scopes"])
    with pytest.raises(ValueError, match="empty independent"):
        build(tmp_path/"first", binding=binding, script_id=FIXTURE["script_id"], source_commit=SHA)
