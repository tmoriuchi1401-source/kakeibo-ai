"""Sheets-authoritative validation, under exclusive writer/structural controls.

No CAS, journal, recovery, dispatch, financial writer or editable UI read here.
The receiver only appends; exactly one Actions worker updates existing rows.
Owners must not structurally edit the management tab while enabled.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from uuid import UUID

from .category_rules import parse_rules
from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_item_correction_review import review, registration_plan
from .receipt_sheet_context import expected_context
from .reconciliation import parse_import_rows

TAB = "_receipt_requests"
HEADERS = ["request_id", "receipt_id", "source_id", "source_version", "source_sha256",
           "snapshot_json", "snapshot_digest", "status", "validation_result", "updated_at"]
FINAL = {"検証済み・未記帳", "修正が必要", "保留"}
UNKNOWN = "結果不明・照合待ち"
STATES = {"受付済み", "検証中"} | FINAL
MAX_ROWS = 1000


def fail(code):
    raise StateError("receipt_sheets_" + code)


def canonical(value, *, ensure_ascii=True):
    return json.dumps(value, ensure_ascii=ensure_ascii, sort_keys=True, separators=(",", ":"), allow_nan=False)


def parse(text, *, ensure_ascii=True):
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                fail("duplicate_json_key")
            obj[k] = v
        return obj
    value = json.loads(text, object_pairs_hook=unique,
                       parse_constant=lambda _: fail("invalid_json"))
    if canonical(value, ensure_ascii=ensure_ascii) != text:
        fail("noncanonical_json")
    return value


def request_id(value):
    if type(value) is not str or str(UUID(value, version=4)) != value:
        fail("request_id_invalid")
    return value


def checked_snapshot(snapshot):
    if (set(snapshot) != {"case_id", "source", "context_json", "operations"}
            or type(snapshot["context_json"]) is not str
            or type(snapshot["operations"]) is not list or len(snapshot["operations"]) > 100):
        fail("snapshot_invalid")
    entry = parse(snapshot["context_json"])
    expected = expected_context(snapshot["case_id"], entry)
    if snapshot["source"] != expected["source"]:
        fail("source_mismatch")
    return entry


def record_row(rid, snapshot, *, now):
    request_id(rid)
    checked_snapshot(snapshot)
    s = snapshot["source"]
    row = [rid, snapshot["case_id"], s["source_id"], s["version"], s["sha256"],
           canonical(snapshot), digest(snapshot), "受付済み", "", now]
    check_row(row)
    return row


def check_row(row):
    if (type(row) is not list or len(row) != len(HEADERS)
            or any(type(x) is not str for x in row) or any(len(x) > 40000 for x in row)):
        fail("row_invalid")
    request_id(row[0])
    snapshot = parse(row[5])
    checked_snapshot(snapshot)
    s = snapshot["source"]
    if (row[1:5] != [snapshot["case_id"], s["source_id"], s["version"], s["sha256"]]
            or row[6] != digest(snapshot) or row[7] not in STATES
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", row[9])):
        fail("row_binding_invalid")
    if row[7] in FINAL:
        result = parse(row[8], ensure_ascii=False)
        if (set(result) != {"request_id", "snapshot_digest", "state", "issues", "posting_authority", "financial_write_count"}
                or result["request_id"] != row[0] or result["snapshot_digest"] != row[6]
                or result["state"] != row[7] or type(result["issues"]) is not list
                or result["posting_authority"] is not False
                or type(result["financial_write_count"]) is not int or result["financial_write_count"] != 0):
            fail("result_invalid")
    elif row[8] != "":
        fail("nonterminal_result")
    return snapshot


class SheetAuthority:
    """Compare-before-write + read-back, NOT CAS. No repair or retry.

    port.read returns header + all occupied rows, port.update writes H:J only.
    All writers/structural edits must be excluded by the activation gate;
    double reads cannot prevent an uncooperative concurrent Sheet editor.
    """
    def __init__(self, port, *, clock=None):
        self.port = port
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    def find(self, rid):
        request_id(rid)
        rows = self.port.read()
        if not rows or rows[0] != HEADERS or len(rows) > MAX_ROWS:
            fail("table_invalid")
        seen, matches = set(), []
        for number, row in enumerate(rows[1:], 2):
            # Gaps, duplicates and unrelated corruption stop the whole path.
            check_row(row)
            if row[0] in seen:
                fail("request_duplicate")
            seen.add(row[0])
            if row[0] == rid:
                matches.append((number, row))
        if len(matches) != 1:
            fail("request_missing")
        return deepcopy(matches[0])

    def transition(self, before, state, result=None):
        number, row = before
        allowed = {"受付済み": {"検証中"}, "検証中": FINAL}
        if state not in allowed.get(row[7], set()):
            fail("replay_rejected")
        if self.find(row[0]) != before:
            fail("changed_before_write")
        after = deepcopy(row)
        after[7:] = [state, "" if result is None else canonical(result, ensure_ascii=False), self.clock()]
        check_row(after)
        self.port.update(number, after[7:])
        if self.find(row[0]) != (number, after):
            fail("write_readback_unknown")
        return number, after


def validate_frozen(snapshot):
    entry = checked_snapshot(snapshot)
    txs = parse_import_rows([entry["import_row"]])
    output = review(txs[0], list(enumerate(entry["candidate_rows"], 2)), snapshot["source"],
                    snapshot["operations"], entry["categories"],
                    total_confirmation=entry["anchor"]["total_confirmation"],
                    approved_product_rules=parse_rules(entry["rules"]))
    return registration_plan(output["snapshot"], output)


def execute(rid, *, authority, source_check, context_check, validate=validate_frozen):
    """Run once. Any uncertain mutation/read returns UNKNOWN with zero resend."""
    request_id(rid)
    initial = authority.find(rid)
    if initial[1][7] != "受付済み":
        fail("replay_rejected")
    try:
        claimed = authority.transition(initial, "検証中")
        snapshot = check_row(claimed[1])
        context_check(snapshot)  # Approved immutable baseline; never UI cells.
        source_check(snapshot["source"])
        plan = validate(deepcopy(snapshot))
        source_check(snapshot["source"])
        # No terminal write on an exception, including changed source/context.
        result = dict(request_id=rid, snapshot_digest=claimed[1][6], state=plan["state"],
                      issues=deepcopy(plan["issues"]), posting_authority=False, financial_write_count=0)
        finished = authority.transition(claimed, plan["state"], result)
        return dict(request_id=rid, state=finished[1][7], financial_write_count=0, posting_authority=False)
    except Exception:
        return dict(request_id=rid, state=UNKNOWN, financial_write_count=0, posting_authority=False)


def display_record(rid, rows, *, receipt_id, source):
    """Read-only independent display check. An unknown worker reply isn't success.

    A later independent read can establish the actual committed terminal result;
    this function never sends, retries, repairs or resumes work.
    """
    class ReadOnly:
        def read(self): return deepcopy(rows)
    try:
        _, row = SheetAuthority(ReadOnly()).find(rid)
        if row[1:5] != [receipt_id, source["source_id"], source["version"], source["sha256"]]:
            fail("display_mismatch")
        return row[7] if row[7] in FINAL else "受付確認中・未記帳／チェックを繰り返さない"
    except Exception:
        return UNKNOWN


def result_formula(row):
    """Install only in existing G cell after approval; no extra UI controls.

    Frozen management cells are a prerequisite. Formula checks exact UUID,
    receipt/source and result bindings, count=1 (never deduplicates rows).
    Missing/duplicate/mismatched records cannot render terminal success.
    """
    if type(row) is not int or row < 2:
        fail("formula_row_invalid")
    tab = f"'{TAB}'!"
    rid = f'REGEXEXTRACT(TO_TEXT($I{row}),"^(?:ready|register):([a-f0-9-]{{36}})$")'
    # JSON result is canonical and contains only fixed state/binding keys plus issues.
    pattern = ('"^\\{""financial_write_count"":0,""issues"":\\[.*\\],'
               '""posting_authority"":false,""request_id"":"""&rid&'
               '""",""snapshot_digest"":"""&dg&""",""state"":"""&st&"""\\}$"')
    return (
        f'=IF(OR($A{row}="",$A{row}<>$AK{row}),"{UNKNOWN}",IFERROR(LET('
        f'rid,{rid},n,COUNTIF({tab}A2:A1000,rid),'
        f'rs,FILTER({tab}A2:J1000,{tab}A2:A1000=rid),'
        'dg,INDEX(rs,1,7),st,INDEX(rs,1,8),res,INDEX(rs,1,9),'
        f'ok,AND(n=1,INDEX(rs,1,2)=$A{row},INDEX(rs,1,3)=$AL{row},'
        f'INDEX(rs,1,4)=TO_TEXT($AM{row}),INDEX(rs,1,5)=$AN{row},'
        'REGEXMATCH(dg,"^[a-f0-9]{64}$")),'
        f'IF(NOT(ok),"{UNKNOWN}",IF(OR(st="受付済み",st="検証中"),'
        '"受付確認中・未記帳／チェックを繰り返さない",'
        f'IF(AND(OR(st="検証済み・未記帳",st="修正が必要",st="保留"),REGEXMATCH(res,{pattern})),'
        f'st&"（登録時の内容）","{UNKNOWN}"))),'
        f'IF(OR($I{row}="",LEFT(TO_TEXT($I{row}),6)="ready:"),"編集中・未登録","{UNKNOWN}")))'
    )
