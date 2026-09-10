"""Repo-external signed persistence for explicit source reconciliation decisions."""
from __future__ import annotations
import hashlib, json, os
from pathlib import Path
from dataclasses import dataclass
from .payroll_source_reconciliation import PayrollSourceReconciliationDecision, serialize_payroll_source_reconciliation_decision, deserialize_payroll_source_reconciliation_decision

@dataclass(frozen=True)
class ReconciliationJournalPreview:
    target_path: Path
    status: str
    record_count: int
    payload: bytes
    content_sha256: str

def _outside(path, root):
    p=Path(path).resolve(); r=Path(root).resolve()
    try: p.relative_to(r)
    except ValueError: return p
    raise ValueError("reconciliation_journal_must_be_outside_repository")

def load_reconciliation_key(path, *, repository_root):
    p=_outside(path, repository_root)
    try: key=bytes.fromhex(p.read_text(encoding="ascii").strip())
    except Exception as exc: raise ValueError("reconciliation_hmac_key_unavailable") from exc
    if len(key)!=32: raise ValueError("reconciliation_hmac_key_invalid")
    return key

def serialize_reconciliation_journal(records, *, local_key):
    records=tuple(records)
    if len(records)!=1: raise ValueError("reconciliation_journal_record_count_invalid")
    raw=json.loads(serialize_payroll_source_reconciliation_decision(records[0]))
    body={"journal_version":"payroll-source-reconciliation-journal-v1","records":[raw]}
    import hmac, hashlib
    body["journal_signature"]=hmac.new(local_key, json.dumps(body,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii"), hashlib.sha256).hexdigest()
    return (json.dumps(body,sort_keys=True,separators=(",",":"),ensure_ascii=True)+"\n").encode("ascii")

def load_reconciliation_journal(path, *, local_key):
    payload=Path(path).read_bytes(); obj=json.loads(payload.decode("ascii"))
    sig=obj.pop("journal_signature", None)
    import hmac, hashlib
    expected=hmac.new(local_key,json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii"),hashlib.sha256).hexdigest()
    if not sig or not hmac.compare_digest(sig,expected): raise ValueError("reconciliation_journal_signature_invalid")
    if obj.get("journal_version")!="payroll-source-reconciliation-journal-v1" or len(obj.get("records",[]))!=1: raise ValueError("reconciliation_journal_shape_invalid")
    return (deserialize_payroll_source_reconciliation_decision(json.dumps(obj["records"][0]),local_key=local_key),)

def preview_reconciliation_journal_write(records, target_path, *, repository_root, local_key):
    records=tuple(records)
    target=_outside(target_path,repository_root)
    if target.suffix.lower()!=".json" or target.is_symlink(): raise ValueError("reconciliation_journal_target_invalid")
    payload=serialize_reconciliation_journal(records,local_key=local_key)
    status="already_present" if target.exists() and target.read_bytes()==payload else ("conflict" if target.exists() else "ready")
    return ReconciliationJournalPreview(target,status,len(tuple(records)),payload,hashlib.sha256(payload).hexdigest())

def write_reconciliation_journal(preview, *, confirmed=False):
    if not confirmed or preview.status!="ready": return False
    preview.target_path.parent.mkdir(parents=True,exist_ok=True)
    with preview.target_path.open("xb") as f: f.write(preview.payload); f.flush(); os.fsync(f.fileno())
    try: preview.target_path.chmod(0o600)
    except OSError: pass
    return True
