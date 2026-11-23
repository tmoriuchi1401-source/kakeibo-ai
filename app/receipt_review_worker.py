"""PR-B backend: fixed snapshot -> validation only, with persistent attempt fence.

No financial constructor, source move or AI call. Apps Script performs atomic
compare/transition under the receiver's lock; direct Sheet PUT is not used.
"""
from copy import deepcopy
from hashlib import sha256
import re
import sqlite3

from .page_receipt_model import digest
from .receipt_review_cases import check_case, source_identity, validate_snapshot
from .receipt_sheets_authority import UNKNOWN, parse, request_id


class SourceMismatch(ValueError):
    """Definite identity mismatch, distinct from an unreadable/unknown reply."""


class AttemptFence:
    """Keep this private DB across restarts. Never delete/reset an UNKNOWN UUID.

    Insert commits BEFORE the first claim RPC. It is an additional local fence;
    the Script's permanent request ledger is the cross-process authority.
    """
    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("persistent_attempt_fence_required")
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (request_id TEXT PRIMARY KEY)")

    def consume(self, rid):
        request_id(rid)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO attempts VALUES (?)", (rid,))

    def consume_source(self, sid, hashed):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO attempts VALUES (?)", ("source:"+sid,))
            db.execute("INSERT INTO attempts VALUES (?)", ("hash:"+hashed,))

    def attempted(self, rid):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT 1 FROM attempts WHERE request_id=?",(rid,)).fetchone() is not None


def checked_record(record, rid):
    row, entry = record["row"], check_case(record["entry"])
    if (type(row) is not list or len(row) != 10 or not all(type(v) is str for v in row)
            or row[0] != request_id(rid) or entry["request_id"] != rid
            or row[1:5] != [entry["case_id"],entry["source"]["source_id"],entry["source"]["version"],entry["source"]["sha256"]]
            or row[7] not in {"受付済み","検証中","検証済み・未記帳","修正が必要","保留"}
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", row[9])):
        raise ValueError("request_binding_invalid")
    snapshot = parse(row[5])
    if digest(snapshot) != row[6] or snapshot["source"] != entry["source"] or snapshot["case_id"] != entry["case_id"]:
        raise ValueError("request_digest_invalid")
    return deepcopy(row), deepcopy(entry), snapshot


def execute_review(rid, *, port, fence, duplicate_check=None):
    """One attempt, zero retry. Only read-only inspection is allowed after UNKNOWN."""
    request_id(rid)
    try:
        row, entry, snapshot = checked_record(port.read(rid), rid)
        if row[7] != "受付済み":
            raise ValueError("replay_rejected")
        fence.consume(rid)
        claimed = port.claim(rid, row)
        if claimed[:7] != row[:7] or claimed[7:9] != ["検証中",""]:
            raise ValueError("claim_unknown")
        # Recheck the immutable catalog after claim; never read current UI drafts.
        current, current_entry, frozen = checked_record(port.read(rid), rid)
        if current != claimed or current_entry != entry or frozen != snapshot:
            raise ValueError("context_changed")
        try:
            port.source_check(entry["source"])
        except SourceMismatch:
            plan = dict(state="保留",issues=["source_identity_mismatch"],financial_write_count=0,posting_authority=False)
        else:
            try:
                plan = validate_snapshot(snapshot, entry)
            except (ValueError, KeyError, TypeError):
                plan = dict(state="修正が必要",issues=["snapshot_validation_failed"],financial_write_count=0,posting_authority=False)
            if duplicate_check is not None and duplicate_check(entry, snapshot):
                plan = dict(state='保留', issues=['existing_receipt_duplicate'],
                    financial_write_count=0, posting_authority=False)
            # A network failure is UNKNOWN; a definite mismatch is a reasoned hold.
            try:
                port.source_check(entry["source"])
            except SourceMismatch:
                plan = dict(state="保留",issues=["source_identity_mismatch"],financial_write_count=0,posting_authority=False)
        result = dict(request_id=rid,snapshot_digest=row[6],state=plan["state"],issues=plan["issues"],
            posting_authority=False,financial_write_count=0)
        # Totals are frozen in the snapshot and kept distinct in the result.
        for key in ("receipt_total","item_total","difference"):
            if key in plan:
                result[key] = plan[key]
        finished = port.finish(rid, claimed, result)
        actual, actual_entry, actual_snapshot = checked_record(port.read(rid), rid)
        if (finished != actual or actual[:7] != row[:7] or actual_entry != entry or actual_snapshot != snapshot
                or actual[7] != result["state"] or parse(actual[8],ensure_ascii=False) != result):
            raise ValueError("finish_unknown")
        return result
    except Exception:
        return dict(request_id=rid,state=UNKNOWN,posting_authority=False,financial_write_count=0)


class ScriptPort:
    """Single-attempt RPC with an already authorized OWNER OAuth session.

    Execution API does not support service accounts. Credential provisioning,
    API executable deployment and first real-data run require separate approval.
    The caller supplies an OAuth requests session without retry/refresh adapters.
    """
    def __init__(self, session, *, script_id, folder_id, owner_email):
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,150}", script_id) or not re.fullmatch(r"[A-Za-z0-9_-]{10,150}", folder_id):
            raise ValueError("script_binding_invalid")
        self.session, self.script_id, self.folder_id, self.owner_email = session, script_id, folder_id, owner_email

    def rpc(self, action, **data):
        response = self.session.post("https://script.googleapis.com/v1/scripts/"+self.script_id+":run",
            json={"function":"receiptReviewBackend","parameters":[dict(action=action,**data)]},
            timeout=30,allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("review_rpc_unknown")
        body = response.json()
        if body.get("done") is not True or "error" in body or body.get("response",{}).get("@type") != "type.googleapis.com/google.apps.script.v1.ExecutionResponse":
            raise RuntimeError("review_rpc_unknown")
        return body["response"].get("result")

    def lookup_source(self, sid, hashed): return self.rpc("lookup",source_id=sid,sha256=hashed)
    def status(self): return self.rpc("status")
    def pending(self): return self.rpc("pending")
    def intake(self, entry, row): return self.rpc("intake",entry=entry,row=row)
    def read(self, rid): return self.rpc("read",request_id=rid)
    def claim(self, rid, before): return self.rpc("claim",request_id=rid,before=before)
    def finish(self, rid, before, result): return self.rpc("finish",request_id=rid,before=before,result=result)

    def metadata(self, sid):
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", sid):
            raise ValueError("source_id_invalid")
        response = self.session.get("https://www.googleapis.com/drive/v3/files/"+sid,
            params={"fields":"id,version,mimeType,trashed,parents,owners(emailAddress)"}, timeout=20,allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("source_read_unknown")
        m = response.json()
        if (m.get("id") != sid or m.get("trashed") is not False or m.get("parents") != [self.folder_id]
                or [x.get("emailAddress") for x in m.get("owners",[])] != [self.owner_email]):
            raise SourceMismatch("source_identity_mismatch")
        return m

    def source_check(self, source):
        source_identity(source)
        before = self.metadata(source["source_id"])
        if before["version"] != source["version"] or before["mimeType"] != source["mime_type"]:
            raise SourceMismatch("source_identity_mismatch")
        response = self.session.get("https://www.googleapis.com/drive/v3/files/"+source["source_id"],
            params={"alt":"media"},timeout=20,allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("source_read_unknown")
        if sha256(response.content).hexdigest() != source["sha256"] or before != self.metadata(source["source_id"]):
            raise SourceMismatch("source_identity_mismatch")
        if source['mime_type'] == 'application/pdf':
            from .pdf_intake_production import page_count
            if page_count(response.content) != 1:
                raise SourceMismatch('source_pdf_structure_mismatch')
