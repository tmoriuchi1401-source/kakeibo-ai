"""Validation-only Sheets request worker. No live adapters or posting capability.

Reuses #196's pure review/validator and #199's independent read/projection
checks. Ports MUST enforce private ACLs, atomic create/CAS and independent
read-back. Editable Sheets and the trigger account are not owner approval.
"""
from copy import deepcopy
from hashlib import sha256
import re
from uuid import UUID

from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_item_correction_review import review, registration_plan
from .receipt_sheet_draft import parse_draft

PURPOSE = "receipt-sheet-validation-only-v1"
STATES = {"受付済み", "検証中", "修正が必要", "保留", "検証済み・未記帳", "結果不明・照合待ち"}
FINAL = {"修正が必要", "保留", "検証済み・未記帳"}
UNKNOWN = "結果不明・照合待ち"


def request_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value, version=4)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise StateError("receipt_request_uuid_invalid") from None
    return value


def strong_etag(value):
    return isinstance(value, str) and bool(re.fullmatch(r'"[^"\r\n]+"', value))


class IndependentReceiptReader:
    """#199 read contract, detached from OIDC, Flask and Firestore.

    private_cases is a protected immutable anchor, never a hidden Sheet.
    A draft carries case/source identity inside its cell to catch column sorts.
    No parser interprets editable source classification as trusted evidence.
    """
    def __init__(self, *, metadata, source_bytes, sheet_values, private_cases,
                 spreadsheet_id, private_id, source_folder, owner_email, service_email,
                 parse_imports, parse_rules):
        self.metadata, self.source_bytes = metadata, source_bytes
        self.sheet_values, self.private_cases = sheet_values, private_cases
        self.sid, self.pid, self.folder = spreadsheet_id, private_id, source_folder
        self.owner, self.service = owner_email, service_email
        self.parse_imports, self.parse_rules = parse_imports, parse_rules

    def checked_metadata(self, fid):
        m = deepcopy(self.metadata(fid))
        grants = {(p.get("type"), p.get("role"), p.get("emailAddress"))
                  for p in m.get("permissions", []) if not p.get("deleted")}
        if (m.get("id") != fid or m.get("trashed") is not False
                or [o.get("emailAddress") for o in m.get("owners", [])] != [self.owner]
                or grants != {("user", "owner", self.owner), ("user", "writer", self.service)}
                or not isinstance(m.get("version"), str) or not m["version"]):
            raise StateError("receipt_reader_acl_or_identity_changed")
        return m

    def __call__(self, case_id):
        try:
            before = {fid: self.checked_metadata(fid) for fid in (self.sid, self.pid)}
            if (before[self.sid]["mimeType"] != "application/vnd.google-apps.spreadsheet"
                    or before[self.pid]["mimeType"] != "application/json"):
                raise ValueError()
            catalog, tag = self.private_cases()
            catalog = deepcopy(catalog)
            if not strong_etag(tag):
                raise ValueError()
            anchor = catalog["cases"][case_id]
            source = anchor["source"]
            sid = source["source_id"]
            original = self.checked_metadata(sid)
            if (original["version"] != source["version"] or original["mimeType"] != source["mime_type"]
                    or original.get("parents") != [self.folder]):
                raise ValueError()
            sheets = deepcopy(self.sheet_values())
            if sheets["sheet"] != "確認":
                raise ValueError()
            rows = [r for r in sheets["cases"] if r["case_id"] == case_id]
            txs = [t for t in self.parse_imports(sheets["imports"]) if t.import_id == case_id]
            if len(rows) != 1 or len(txs) != 1 or rows[0]["source"] != source:
                raise ValueError()
            draft = rows[0]["draft"]
            if isinstance(draft, str):
                parsed = parse_draft(draft)
                if parsed["context_digest"] != digest(anchor):
                    raise ValueError()
                draft = {k: parsed[k] for k in ("case_id", "source", "operations")}
            if (set(draft) != {"case_id", "source", "operations"}
                    or draft["case_id"] != case_id or draft["source"] != source):
                raise ValueError()
            selected = [r for _, r in sheets["candidates"] if len(r) > 1 and r[1] == case_id]
            if (digest(selected) != anchor["candidate_digest"]
                    or digest(txs[0].row) != anchor["import_digest"]
                    or digest([sheets["categories"], sheets["rules"]]) != anchor["policy_digest"]):
                raise ValueError()
            raw = self.source_bytes(sid)
            if type(raw) is not bytes or sha256(raw).hexdigest() != source["sha256"]:
                raise ValueError()
            if (self.checked_metadata(sid) != original or self.sheet_values() != sheets
                    or self.private_cases() != (catalog, tag)
                    or any(self.checked_metadata(fid) != m for fid, m in before.items())):
                raise ValueError()
            output = review(txs[0], sheets["candidates"], source, draft["operations"],
                            sheets["categories"], total_confirmation=anchor["total_confirmation"],
                            approved_product_rules=self.parse_rules(sheets["rules"]))
            captured = dict(deepcopy(draft), context_digest=digest(anchor))
            captured["digest"] = digest(captured)
            output["request_snapshot"] = captured
            return output
        except StateError:
            raise
        except Exception:
            # Do not leak raw API failures, source bytes or user input to Actions.
            raise StateError("receipt_reader_changed_or_unavailable") from None


def empty_journal():
    return dict(schema=PURPOSE, generation=0, requests={})


def validate_journal(value):
    try:
        if (set(value) != {"schema", "generation", "requests"} or value["schema"] != PURPOSE
                or type(value["generation"]) is not int or value["generation"] < 0
                or not isinstance(value["requests"], dict) or len(value["requests"]) > 1000):
            raise ValueError()
        for rid, r in value["requests"].items():
            request_uuid(rid)
            if (set(r) != {"request_id", "purpose", "snapshot", "state", "result"}
                    or r["request_id"] != rid or r["purpose"] != PURPOSE or r["state"] not in STATES):
                raise ValueError()
            s = deepcopy(r["snapshot"])
            claimed = s.pop("digest")
            if (set(s) != {"case_id", "source", "operations", "context_digest"}
                    or claimed != digest(s) or not isinstance(s["case_id"], str) or not s["case_id"]
                    or not isinstance(s["operations"], list) or len(s["operations"]) > 100
                    or not re.fullmatch("[0-9a-f]{64}", s["context_digest"])
                    or not isinstance(s["source"], dict) or not s["source"].get("source_id")):
                raise ValueError()
            if r["state"] in FINAL:
                result = r["result"]
                if (set(result) != {"state", "issues", "snapshot_digest", "posting_authority", "financial_write_count"}
                        or result["state"] != r["state"] or result["posting_authority"] is not False
                        or type(result["financial_write_count"]) is not int or result["financial_write_count"] != 0
                        or result["snapshot_digest"] != claimed or not isinstance(result["issues"], list)):
                    raise ValueError()
            elif r["result"] is not None:
                raise ValueError()
    except Exception:
        raise StateError("receipt_validation_journal_invalid") from None
    return value


class ValidationJournal:
    """Dedicated non-TTL CAS store; no authority scope and no automatic retry.

    One atomic document serializes UUID and case/source duplicate claims.
    Missing state is never initialized. ACL preflight is required on every IO.
    The port is intentionally not connected to Drive or production state.
    """
    def __init__(self, transport, *, preflight):
        self.transport, self.preflight = transport, preflight

    def load(self):
        self.preflight()
        value, tag = self.transport.read_versioned()
        if not strong_etag(tag):
            raise StateError("receipt_validation_strong_cas_required")
        return deepcopy(validate_journal(value)), tag

    def save(self, value, tag):
        validate_journal(value)
        self.preflight()
        # The implementation must use an atomic server-side If-Match.
        self.transport.replace_versioned(deepcopy(value), tag)
        actual, new_tag = self.load()
        if actual != value or tag == new_tag:
            raise StateError("receipt_validation_readback_unknown")

    def accept(self, request):
        request_uuid(request["request_id"])
        if set(request) != {"request_id", "purpose", "snapshot"} or request["purpose"] != PURPOSE:
            raise StateError("receipt_validation_purpose_invalid")
        value, tag = self.load()
        rid, snapshot = request["request_id"], request["snapshot"]
        if rid in value["requests"]:
            raise StateError("receipt_validation_replay_rejected")
        for old in value["requests"].values():
            s = old["snapshot"]
            same_case = s["case_id"] == snapshot["case_id"]
            same_source = s["source"]["source_id"] == snapshot["source"]["source_id"]
            if (same_source and not same_case or (same_source or same_case)
                    and (old["state"] not in FINAL or s == snapshot)):
                raise StateError("receipt_validation_duplicate_or_unresolved")
        value["requests"][rid] = dict(deepcopy(request), state="受付済み", result=None)
        value["generation"] += 1
        self.save(value, tag)

    def transition(self, rid, state, result=None):
        value, tag = self.load()
        old = value["requests"][rid]
        allowed = {"受付済み": {"検証中", UNKNOWN}, "検証中": FINAL | {UNKNOWN}}
        if state not in allowed.get(old["state"], set()):
            raise StateError("receipt_validation_transition_forbidden")
        old.update(state=state, result=deepcopy(result))
        value["generation"] += 1
        self.save(value, tag)

    def query(self, rid, *, run_completed=False):
        # Caller may set run_completed ONLY from independent authoritative run
        # observation, never elapsed time. This is a projection, not a repair.
        record = deepcopy(self.load()[0]["requests"][request_uuid(rid)])
        if run_completed and record["state"] in {"受付済み", "検証中"}:
            record["state"] = UNKNOWN
        return record


def execute_request(request_id, *, requests, reader, journal):
    """Actions input is ONLY a UUID; ports read all private data independently.

    requests supplies an immutable captured request, not workflow payload data.
    Once acceptance is attempted, any ambiguous response returns UNKNOWN with
    no retry, recovery write or automatic replay. Query is a separate read.
    """
    request_uuid(request_id)
    accepted = False
    try:
        request = deepcopy(requests(request_id))
        if request["request_id"] != request_id:
            raise StateError("receipt_validation_request_mismatch")
        journal.accept(request)
        accepted = True
        journal.transition(request_id, "検証中")
        try:
            fresh = reader(request["snapshot"]["case_id"])
            if fresh["request_snapshot"] != request["snapshot"]:
                raise StateError("receipt_validation_captured_input_changed")
            plan = registration_plan(fresh["snapshot"], fresh)
            again = reader(request["snapshot"]["case_id"])
            if (again["snapshot"] != fresh["snapshot"]
                    or again["request_snapshot"] != request["snapshot"]):
                raise StateError("receipt_validation_concurrent_edit")
            result = {k: deepcopy(plan[k]) for k in
                      ("state", "issues", "snapshot_digest", "posting_authority", "financial_write_count")}
            result["snapshot_digest"] = request["snapshot"]["digest"]
        except (StateError, ValueError):
            result = dict(state="保留", issues=["receipt_validation_source_or_input_changed"],
                          snapshot_digest=request["snapshot"]["digest"],
                          posting_authority=False, financial_write_count=0)
        journal.transition(request_id, result["state"], result)
        return journal.query(request_id)
    except StateError as exc:
        if not accepted and str(exc) in {"receipt_validation_replay_rejected", "receipt_validation_duplicate_or_unresolved"}:
            raise
    except Exception:
        pass
    return dict(request_id=request_id, state=UNKNOWN, posting_authority=False, financial_write_count=0)


def project_result(rows, record):
    """#199 case/source/UUID-bound projection; never mutates an input or link.

    A live writer must apply ONLY this result field with atomic row identity
    checking. Sheets values.batchUpdate cannot supply that CAS; do not use it.
    """
    validate_journal(dict(schema=PURPOSE, generation=0, requests={record["request_id"]: record}))
    snapshot = record["snapshot"]
    found = [i for i, r in enumerate(rows) if r["case_id"] == snapshot["case_id"]]
    if len(found) != 1:
        raise StateError("receipt_result_case_missing_or_duplicate")
    i = found[0]
    row = rows[i]
    if row["source"] != snapshot["source"] or row["submission"] != {
            "request_id": record["request_id"], "snapshot_digest": snapshot["digest"]}:
        raise StateError("receipt_result_projection_stale")
    output = deepcopy(rows)
    output[i]["result"] = dict(request_id=record["request_id"], state=record["state"],
        snapshot_digest=snapshot["digest"],
        draft_notice="登録時の明細と一致" if row["draft"]["operations"] == snapshot["operations"] else "未登録の変更あり",
        posting_authority=False, financial_write_count=0)
    return output
