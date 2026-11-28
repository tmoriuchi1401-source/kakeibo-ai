"""Read-only proof of the two independently reconciled, fixed card failures.

Hashes bind existing private evidence without publishing transaction identities.
This module cannot save a ledger, advance a checkpoint, or invoke a writer.
"""
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import json
import sqlite3
import re

from .aupay_card_window_preview import require

FAILED_RUN = 41560668947
FAILED_JOB = 125670234114
FAILED_SHA = "6284b63206813e3f731ea77bd6228b235336dd84"
RECOVERED_CHECKPOINT = "2026-11-03T10:35:38+09:00"
RECONCILED_AT = "2026-11-03T04:14:00.641320+00:00"
EVIDENCE_SHA256 = "1fcbcbfa043abb9bf7c2a14ea30851bd46dd1f40604baf64b199a763de60226a"
NATIVE_SHA256 = "10f59540ada795baa478783991ebdf7c24ebc83121a41000029568d5c32889b9"
RESULT_SHA256 = "0371d17ce24f49df27e432f5efbce9f4a63e29f46cc0b104a98ec828cf539788"
MANIFEST_SHA256 = "54b830424aa3032d7874fb3c3507de0cf66a463fe61f4438a3322436bc338f0a"
JOURNAL_SHA256 = "2a3c48e0d17500b16f75b5451204785db49b61c64b64043b32f2ae306c36e5cf"
CAPABILITY_SHA256 = "e2198cb76eb328323baa358458248d2c839deee0e8f5e634c8310c45505bfbcd"
PARENT_CARD_SHA256 = "a3e7eb487d494b62176511b537ef416b517ab4255e9322243c89ea8b6929989e"

# The later run stopped before persistence; it is not the older financial
# completion above. Exact hashes bind the independent before/after GET audit.
NO_WRITE_RUN = 41729101706
NO_WRITE_JOB = 126241487787
NO_WRITE_SHA = "22b0a374dcd7c6b344e092b414c096c067130431"
NO_WRITE_CREATED = "2026-11-03T22:14:53Z"
NO_WRITE_CHECKPOINT = "2026-11-03T14:45:43+09:00"
NO_WRITE_NATIVE_SHA256 = "2ee8d898168d5db2ad59ed006944b2a8306316990c1c3df7ab5a2a62d5a44892"
NO_WRITE_PARENT_SHA256 = "cdee51239e3fadf3a34dec4c4a76e4db64c65dd827d1127bfe3557f1c09c9c14"
NO_WRITE_RESULT_SHA256 = "258c1adcfe85f2e799f4b63eca53534fc5787fec7ae67c91d455011db5894c4c"
NO_WRITE_FINANCIAL_SHA256 = "4388e4c1abb0de1c14ef14adb7900d9c121c368db95676a249f893ca88c36d7a"
NO_WRITE_MONEY_SHA256 = "58d87b362afc69e041de2c31e34e753c0959fb91146dc4518c1e58969c8224b2"
NO_WRITE_MONEY_KEYS = ("money", "money-reviews", "money-migration", "money-notices", "journal")

# Independently audited before reservation, not a successful Apply or a retry.
# The retained exact revisions are native134 and parent1426 -> bank1427 ->1428.
# The direct exception remains unknown; a queued bank guard is only a candidate.
CATCHUP_NO_WRITE_RUN = 46103398237
CATCHUP_NO_WRITE_CHECKPOINT = "2026-11-16T21:00:33+09:00"
CATCHUP_NO_WRITE_EXECUTION = dict(id=CATCHUP_NO_WRITE_RUN,
    head_sha="a92abe6aa4a44d1fb1ceadc52ce2c8caa82b905b", head_branch="main", run_attempt=1,
    status="completed", conclusion="failure", event="workflow_dispatch",
    path=".github/workflows/aupay-card-incremental-apply.yml", created_at="2026-11-28T03:33:07Z")
CATCHUP_NO_WRITE_JOB = 140850673239
CATCHUP_NO_WRITE_NATIVE_SHA256 = "9d8b6aafde553f45d1e659315c45774c255d726f3203c43d219a16507c30b621"
CATCHUP_NO_WRITE_SOURCES_SHA256 = "283473eb6cac7a0661b3a38ca020860981e88e4ac074ecac7656664692d9c15f"
CATCHUP_NO_WRITE_FINANCIAL_SHA256 = "07ad4831d3b5067b782a94cc91bd5061f69defbd2c35fd09da5036b5c6efbecf"
CATCHUP_NO_WRITE_DOCUMENTS_SHA256 = "f57a5c50ecac90bfa0258348f88b3eb16d3936c1b5d1349e30d32853fb7f3c30"
CATCHUP_NO_WRITE_ANCHOR_SHA256 = "8ab018e21bf319cad55409f5b06e2f33fbd96502fbcfff201a04182506e8bead"
CATCHUP_NO_WRITE_AUDIT_SHA256 = "534b81bc4fc9b84f6ceca6040a5bfc252b69d02b7c816dfdbd30c71ae7cbee71"
CATCHUP_BANK_EXECUTION = dict(CATCHUP_NO_WRITE_EXECUTION, id=46103439725,
    conclusion="success", event="schedule", path=".github/workflows/kakeibo-production.yml",
    created_at="2026-11-28T03:33:36Z")
RESERVATION_PROTOCOL = "card-reservation-no-write-v1"
RESERVATION_PREFIX = "OPERATOR-CARD-RESERVATION:"
RESERVATION_MAX_BYTES = 32768
CATCHUP_CONTENT_ROOT = "868e55572d7ea46b48a0d7b20ce81401147b7d95fd7513a3860dcf08d983fd6a"


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def verify_failed_run(github):
    """This is a reconciled financial completion, never a no-write exemption."""
    run = github.get(f"actions/runs/{FAILED_RUN}")
    require(run["id"] == FAILED_RUN and run["head_sha"] == FAILED_SHA
            and run["run_attempt"] == 1 and run["status"] == "completed"
            and run["conclusion"] == "failure" and run["event"] == "workflow_dispatch"
            and run["path"] == ".github/workflows/aupay-card-incremental-apply.yml"
            and run["created_at"] == "2026-11-03T01:35:03Z", "reconciled_run_identity_unproven")
    jobs = github.get(f"actions/runs/{FAILED_RUN}/jobs?per_page=100")
    require(jobs["total_count"] == len(jobs["jobs"]) == 1, "reconciled_jobs_unproven")
    job = jobs["jobs"][0]
    require(job["id"] == FAILED_JOB and job["name"] == "apply"
            and job["status"] == "completed" and job["conclusion"] == "failure",
            "reconciled_job_identity_unproven")
    expected = {
        5: ("Live main and run guard with job-start validated and flags", "success"),
        6: ("Install reviewed hash-pinned wheels before Google credentials", "success"),
        7: ("One checkpoint Apply with protected reviews and exact readback", "failure"),
    }
    for number, (name, conclusion) in expected.items():
        steps = [s for s in job["steps"] if s["number"] == number]
        require(len(steps) == 1 and steps[0]["name"] == name
                and steps[0]["status"] == "completed" and steps[0]["conclusion"] == conclusion,
                "reconciled_step_identity_unproven")


def verify_no_write_run(github, run_id=NO_WRITE_RUN):
    """Only the two reviewed exact attempts; no caller-supplied exemption."""
    require(run_id in {NO_WRITE_RUN, CATCHUP_NO_WRITE_RUN}, "no_write_run_unproven")
    expected = (CATCHUP_NO_WRITE_EXECUTION if run_id == CATCHUP_NO_WRITE_RUN else
        dict(CATCHUP_NO_WRITE_EXECUTION, id=NO_WRITE_RUN, head_sha=NO_WRITE_SHA, created_at=NO_WRITE_CREATED))
    job_id = CATCHUP_NO_WRITE_JOB if run_id == CATCHUP_NO_WRITE_RUN else NO_WRITE_JOB
    run = github.get(f"actions/runs/{run_id}")
    require(all(run.get(k) == v for k, v in expected.items()), "no_write_run_unproven")
    if run_id == CATCHUP_NO_WRITE_RUN:
        bank = github.get(f"actions/runs/{CATCHUP_BANK_EXECUTION['id']}")
        require(all(bank.get(k) == v for k, v in CATCHUP_BANK_EXECUTION.items()), "no_write_bank_unproven")
    jobs = github.get(f"actions/runs/{run_id}/jobs?per_page=100")
    require(jobs["total_count"] == len(jobs["jobs"]) == 1, "no_write_jobs_unproven")
    job = jobs["jobs"][0]
    require(job["id"] == job_id and job["run_id"] == run_id
            and job["name"] == "apply" and job["status"] == "completed"
            and job["conclusion"] == "failure", "no_write_job_unproven")
    for number, name, conclusion in (
        (5, "Live main and run guard with job-start validated and flags", "success"),
        (6, "Install reviewed hash-pinned wheels before Google credentials", "success"),
        (7, "One checkpoint Apply with protected reviews and exact readback", "failure"),
    ):
        steps = [s for s in job["steps"] if s["number"] == number]
        require(len(steps) == 1 and steps[0]["name"] == name and steps[0]["status"] == "completed"
                and steps[0]["conclusion"] == conclusion, "no_write_step_unproven")


def financial_evidence(tables):
    """Existing reader's complete financial columns, independent of UI/sort."""
    result = {}
    for title, width in (("支出明細", 13), ("取込データ", 12), ("収入明細", 10), ("レシート", 9)):
        rows = tables[(title, "FORMULA")]
        require(bool(rows), "no_write_financial_missing")
        indexed = {}
        for row in rows[1:]:
            row = list(row[:width])
            # Saved range GETs represent internal blanks as null; the Sheets
            # values API returns empty strings and omits trailing blanks.
            # Retain the audited null/padding form and its original fixed hash.
            while row and row[-1] in ("", None):
                row.pop()
            row = [None if value == "" else value for value in row]
            row += [""] * (width - len(row))
            if not any(v not in ("", None) for v in row):
                continue
            require(isinstance(row[0], str) and row[0] and row[0] not in indexed,
                    "no_write_financial_identity")
            indexed[row[0]] = row
        result[title] = [rows[0][:width], indexed]
    return _hash(result)


def verify_no_write_state(native, parent, expected_checkpoint, *, financial, money, receipt_env=None):
    """No state saves: first resume pins all audited data; daily reuse retains proof."""
    from .aupay_card_window_preview import checkpoint

    require(datetime.fromisoformat(expected_checkpoint) >= datetime.fromisoformat(NO_WRITE_CHECKPOINT),
            "no_write_checkpoint_regression")
    checkpoint(native, expected_checkpoint)  # Also rejects leases/unsealed/UNKNOWN journals.
    verify_reconciled_state(native, parent, expected_checkpoint)  # Older proof is never replaced.
    runs = _rows(native, "recurring.sqlite3", "recurring_runs")
    baseline = [json.loads(r["summary_json"]) for r in runs
                if _hash(json.loads(r["summary_json"])) == NO_WRITE_RESULT_SHA256]
    require(len(baseline) == 1 and baseline[0]["source_window_end"] == NO_WRITE_CHECKPOINT
            and baseline[0]["status"] == "noop" and baseline[0]["written"] == 0
            and baseline[0]["write_requests"] == 0 and baseline[0]["failure"] == 0,
            "no_write_baseline_unproven")
    if expected_checkpoint == NO_WRITE_CHECKPOINT:
        require(_hash(native) == NO_WRITE_NATIVE_SHA256
                and _hash(parent["sources"]["aupay_card"]) == NO_WRITE_PARENT_SHA256,
                "no_write_state_changed")
        require(financial_evidence(financial()) == NO_WRITE_FINANCIAL_SHA256, "no_write_financial_changed")
        require(_hash({k: money.read(k) for k in NO_WRITE_MONEY_KEYS}) == NO_WRITE_MONEY_SHA256,
                "no_write_money_changed")
    else:
        # A later daily run must have actually advanced beyond the failed job,
        # not just supplied some other older checkpoint to skip the first proof.
        require(datetime.fromisoformat(expected_checkpoint) > datetime.fromisoformat(NO_WRITE_CREATED.replace("Z", "+00:00")),
                "no_write_followup_unproven")
    if datetime.fromisoformat(expected_checkpoint) >= datetime.fromisoformat(CATCHUP_NO_WRITE_CHECKPOINT):
        verify_catchup_no_write_state(native, parent, expected_checkpoint, financial=financial, money=money, receipt_env=receipt_env)


def verify_catchup_no_write_state(native, parent, expected_checkpoint, *, financial=None, money=None, receipt_env=None):
    """Reuse the fixed audit without saving an archive or changing past results.

    At C2, freeze the audited card, non-bank sources and complete financial and
    protected document snapshots. Later coverage must prove the first new run
    independently; business checkpoint dates are never execution timestamps.
    """
    from .aupay_card_incremental_coverage import successful_coverage_proofs
    cp = datetime.fromisoformat(expected_checkpoint)
    require(cp >= datetime.fromisoformat(CATCHUP_NO_WRITE_CHECKPOINT), "no_write_checkpoint_regression")
    coverage = successful_coverage_proofs(native, parent, expected_checkpoint)
    runs = [json.loads(r["summary_json"]) for r in _rows(native, "recurring.sqlite3", "recurring_runs")]
    require(sum(_hash(r) == CATCHUP_NO_WRITE_ANCHOR_SHA256 for r in runs) == 1,
            "no_write_coverage_anchor_changed")
    if expected_checkpoint == CATCHUP_NO_WRITE_CHECKPOINT:
        receipts = reservation_receipts(native, parent, expected_checkpoint, env=receipt_env)
        bridged = bool(receipts) and reservation_content_root(native) == CATCHUP_CONTENT_ROOT
        require((_hash(native) == CATCHUP_NO_WRITE_NATIVE_SHA256 or bridged)
                and _hash({k:v for k,v in parent["sources"].items() if k != "bank"}) == CATCHUP_NO_WRITE_SOURCES_SHA256,
                "no_write_state_changed")
        bank = parent["sources"]["bank"]
        from .aupay_card_bank_premises import ZERO_COUNTS
        require(bank["phase"] == "ready" and not bank["attempt"] and not bank["error"]
                and {"written", "write_requests", "failure"} <= set(bank["counts"])
                and all(type(bank["counts"].get(k, 0)) is int and bank["counts"].get(k, 0) == 0 for k in ZERO_COUNTS),
                "no_write_bank_unproven")
        # History guard checks the protected anchor before assembling readers.
        # Full preflight below additionally requires independent finance/docs.
        if financial is not None:
            require(_hash({t+"|"+v:rows for (t,v),rows in financial().items()}) == CATCHUP_NO_WRITE_FINANCIAL_SHA256,
                    "no_write_financial_changed")
            require(money is not None and _hash({k:money.read(k) for k in NO_WRITE_MONEY_KEYS}) == CATCHUP_NO_WRITE_DOCUMENTS_SHA256,
                    "no_write_money_changed")
    else:
        created = datetime.fromisoformat(CATCHUP_NO_WRITE_EXECUTION["created_at"].replace("Z", "+00:00"))
        require(any(r.get("checkpoint_before") == CATCHUP_NO_WRITE_CHECKPOINT
                    and r.get("github_execution", {}).get("id") in coverage
                    and datetime.fromisoformat(r["github_execution"]["created_at"].replace("Z", "+00:00")) > created
                    for r in runs), "no_write_followup_unproven")


def _rows(native, file, table):
    # Existing envelope validation runs first. Only SELECT on disposable memory.
    raw = base64.b64decode(native["files"][file]["data"], validate=True)
    with closing(sqlite3.connect(":memory:")) as con:
        con.deserialize(raw)
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM " + table)]


def reservation_content(native):
    """Compact logical roots; only authenticated receipt rows may be appended."""
    from .drive_run_state import CARD_FILES
    result = {}
    for file, tables in CARD_FILES.items():
        with closing(sqlite3.connect(':memory:')) as con:
            con.deserialize(base64.b64decode(native['files'][file]['data'],validate=True))
            con.execute('PRAGMA trusted_schema=OFF');con.execute('PRAGMA query_only=ON')
            schema_sha256=_hash(con.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall())
        result[file] = {}
        for table in sorted(tables):
            rows = _rows(native, file, table)
            if table == "recurring_runs":
                rows = [r for r in rows if not r['run_id'].startswith(RESERVATION_PREFIX)]
            result[file][table] = dict(rows=len(rows), sha256=_hash(rows),schema_sha256=schema_sha256)
    return result


def reservation_content_root(native):
    return _hash({f:{t:v['sha256'] for t,v in ts.items()} for f,ts in reservation_content(native).items()})


def reservation_public_key(env):
    import os
    from cryptography.hazmat.primitives import serialization
    env = os.environ if env is None else env
    require(bool(env.get('GOOGLE_SERVICE_ACCOUNT_JSON')), 'reservation_receipt_key_required')
    return serialization.load_pem_private_key(json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])['private_key'].encode(), password=None).public_key()


def validate_reservation_record(record):
    """Dynamic execution premises, restricted to this existing card purpose."""
    keys = {'protocol','bindings','checkpoint','checkpoint_row','anchor_summary_sha256',
        'failed_execution','failed_job_id','native_before','native_pending','parent_before','parent_pending',
        'tables','financial_sha256','documents_sha256','acl_sha256','source_contract_sha256',
        'window_end','money_ids','previous_receipt_sha256','evidence_mode','protected_heads'}
    require(set(record)==keys and record['protocol']==RESERVATION_PROTOCOL, 'reservation_record_invalid')
    require(set(record['bindings'])=={'spreadsheet_id','folder_id','card_id','parent_id','projection_id'}, 'reservation_record_invalid')
    cp,end=map(datetime.fromisoformat,(record['checkpoint'],record['window_end']))
    require(cp.tzinfo is not None and end.tzinfo is not None and 0<(end-cp).total_seconds()<=12*86400,
            'reservation_record_invalid')
    failed=record['failed_execution']
    require(set(failed)=={'id','head_sha','head_branch','run_attempt','status','conclusion','event','path','created_at'}
        and type(failed['id']) is int and failed['run_attempt']==1 and failed['head_branch']=='main'
        and failed['status']=='completed' and failed['conclusion']=='failure'
        and failed['event'] in {'schedule','workflow_dispatch'}
        and failed['path']=='.github/workflows/aupay-card-incremental-apply.yml', 'reservation_failure_unproven')
    for name in ('native','parent'):
        first,pending=record[name+'_before'],record[name+'_pending']
        require(set(first)=={'sha256','generation','revision'} and set(pending)=={'sha256','generation','revision','attempt'}
            and type(first['generation']) is int and pending['generation']==first['generation']+1
            and bool(re.fullmatch('[0-9a-f]{32}',pending['attempt'])), 'reservation_record_invalid')
        for node in (first,pending):
            require(set(node['revision'])=={'id','modifiedTime','size','md5Checksum'}
                and bool(node['revision']['id']) and bool(re.fullmatch('[0-9a-f]{64}',node['sha256'])), 'reservation_record_invalid')
    require(record['evidence_mode'] in {'legacy_c2','diagnostic'}
        and type(record['failed_job_id']) is int and type(record['money_ids']) is list
        and len(record['money_ids'])<=100 and len(set(record['money_ids']))==len(record['money_ids']), 'reservation_record_invalid')
    for key in ('financial_sha256','documents_sha256','acl_sha256','source_contract_sha256','anchor_summary_sha256'):
        require(bool(re.fullmatch('[0-9a-f]{64}',record[key])), 'reservation_record_invalid')
    require(not record['previous_receipt_sha256'] or bool(re.fullmatch('[0-9a-f]{64}',record['previous_receipt_sha256'])), 'reservation_record_invalid')
    from .drive_run_state import CARD_FILES
    require(set(record['tables'])==set(CARD_FILES) and set(record['protected_heads'])==set(NO_WRITE_MONEY_KEYS)
        and all(isinstance(v,str) and v for v in record['bindings'].values())
        and re.fullmatch('[0-9a-f]{40}',failed['head_sha']) and record['failed_job_id']>0,
        'reservation_record_invalid')
    require(datetime.fromisoformat(failed['created_at'].replace('Z','+00:00')).tzinfo is not None,
        'reservation_record_invalid')
    for file,tables in CARD_FILES.items():
        require(set(record['tables'][file])==set(tables),'reservation_record_invalid')
        for descriptor in record['tables'][file].values():
            require(set(descriptor)=={'rows','sha256','schema_sha256'} and type(descriptor['rows']) is int
                and descriptor['rows']>=0 and re.fullmatch('[0-9a-f]{64}',descriptor['sha256'])
                and re.fullmatch('[0-9a-f]{64}',descriptor['schema_sha256']),
                'reservation_record_invalid')
    for value in record['protected_heads'].values():
        require(set(value)=={'file_id','revision'} and bool(value['file_id'])
            and set(value['revision'])=={'id','modifiedTime','size','md5Checksum'},'reservation_record_invalid')


def verify_reservation_receipt(receipt, public_key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    require(set(receipt)=={'protocol','record','operator','parent_ready_sha256','parent_ready_card_sha256','signature'}
        and receipt['protocol']==RESERVATION_PROTOCOL and len(json.dumps(receipt).encode())<=RESERVATION_MAX_BYTES,
        'reservation_receipt_invalid')
    validate_reservation_record(receipt['record'])
    operator=receipt['operator']
    require(set(operator)=={'id','head_sha','head_branch','run_attempt','event','path','created_at'}
        and operator['run_attempt']==1 and operator['head_branch']=='main' and operator['event']=='workflow_dispatch'
        and operator['path']=='.github/workflows/kakeibo-production.yml'
        and operator['id']!=receipt['record']['failed_execution']['id'], 'reservation_receipt_invalid')
    body={k:v for k,v in receipt.items() if k!='signature'}
    try:public_key.verify(base64.b64decode(receipt['signature'],validate=True),_hash_bytes(body),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256())
    except Exception:require(False,'reservation_receipt_signature_invalid')
    return receipt


def _hash_bytes(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()


def reservation_summary(row):
    """No unsigned alias/result fields may be smuggled into a noop receipt."""
    summary=json.loads(row['summary_json'])
    require(set(summary)=={'run_id','status','failure','source_window_start','source_window_end','reservation_reconciliation'},
        'reservation_receipt_invalid')
    receipt=summary['reservation_reconciliation'];cp=receipt['record']['checkpoint']
    require(row['run_id']==summary['run_id']==RESERVATION_PREFIX+str(receipt['operator']['id'])
        and row['status']==summary['status']=='noop' and summary['failure']==0
        and row['window_start']==row['window_end']==summary['source_window_start']==summary['source_window_end']==cp
        and datetime.fromisoformat(row['recorded_at']).tzinfo is not None,'reservation_receipt_invalid')
    return receipt


def reservation_receipts(native, parent, checkpoint_value, *, env=None, public_key=None):
    """Read-only receipt chain, preserving old failure conclusions and roots."""
    rows=[r for r in _rows(native,'recurring.sqlite3','recurring_runs') if r['run_id'].startswith(RESERVATION_PREFIX)]
    rows.sort(key=lambda r:json.loads(r['summary_json'])['reservation_reconciliation']['record']['native_pending']['generation'])
    if not rows:return []
    public_key=public_key or reservation_public_key(env)
    result=[];seen=set();current=reservation_content(native);cp=datetime.fromisoformat(checkpoint_value)
    for row in rows:
        receipt=reservation_summary(row);summary=json.loads(row['summary_json'])
        verify_reservation_receipt(receipt,public_key);r=receipt['record'];rid=r['failed_execution']['id']
        require(row['run_id']==RESERVATION_PREFIX+str(receipt['operator']['id'])
            and summary['run_id']==row['run_id'] and row['status']==summary['status']=='noop'
            and summary['failure']==0 and summary['source_window_start']==summary['source_window_end']==r['checkpoint']
            and rid not in seen and cp>=datetime.fromisoformat(r['checkpoint']), 'reservation_receipt_invalid')
        seen.add(rid)
        prior=r['previous_receipt_sha256']
        require(prior==(_hash(result[-1]) if result else ''), 'reservation_receipt_chain_changed')
        if result and result[-1]['record']['checkpoint']==r['checkpoint']:
            require(r['native_before']['generation']==result[-1]['record']['native_pending']['generation']+1,
                    'reservation_generation_changed')
        for file, tables in r['tables'].items():
            require(file in current and set(tables)==set(current[file]),'reservation_content_changed')
            for table,expected in tables.items():
                require(current[file][table]['schema_sha256']==expected['schema_sha256'],'reservation_content_changed')
                actual=_rows(native,file,table)
                if table=='recurring_runs':actual=[x for x in actual if not x['run_id'].startswith(RESERVATION_PREFIX)]
                if table=='recurring_checkpoint' and cp>datetime.fromisoformat(r['checkpoint']):continue
                require(len(actual)>=expected['rows'] and _hash(actual[:expected['rows']])==expected['sha256'],
                        'reservation_content_changed')
        require(any(_hash(json.loads(v['summary_json']))==r['anchor_summary_sha256']
                    for v in _rows(native,'recurring.sqlite3','recurring_runs')), 'reservation_content_changed')
        require(native['phase']=='ready' and not native['attempt']
            and native['generation']>=r['native_pending']['generation']+1
            and parent['sources']['aupay_card']['phase']=='ready' and not parent['sources']['aupay_card']['attempt'],
            'reservation_release_incomplete')
        if cp==datetime.fromisoformat(r['checkpoint']):
            require(_hash(parent['sources']['aupay_card'])==receipt['parent_ready_card_sha256'], 'reservation_release_incomplete')
        result.append(receipt)
    if checkpoint_value==CATCHUP_NO_WRITE_CHECKPOINT:
        require(result[0]['record']['native_before']['sha256']==CATCHUP_NO_WRITE_NATIVE_SHA256
            and reservation_content_root(native)==CATCHUP_CONTENT_ROOT, 'reservation_content_changed')
    return result


def append_reservation_receipt(native, receipt):
    """One append through existing recurring state, no new DB or checkpoint."""
    import tempfile
    from pathlib import Path
    from copy import deepcopy
    from .aupay_card_recurring import SqliteRecurringRunState
    validate_reservation_record(receipt['record'])
    require(native['source']=='au_pay_card_gmail' and _hash(native)==receipt['record']['native_pending']['sha256'],
            'reservation_snapshot_changed')
    require(len(json.dumps(receipt).encode())<=RESERVATION_MAX_BYTES,'reservation_receipt_invalid')
    r=receipt['record'];result=deepcopy(native['files'])
    with tempfile.TemporaryDirectory(prefix='card-reconciliation-') as temp:
        path=Path(temp)/'recurring.sqlite3';path.write_bytes(base64.b64decode(result['recurring.sqlite3']['data']))
        state=SqliteRecurringRunState(path,repo_root=Path(__file__).resolve().parents[1])
        state.record(dict(run_id=RESERVATION_PREFIX+str(receipt['operator']['id']),status='noop',failure=0,
            source_window_start=r['checkpoint'],source_window_end=r['checkpoint'],reservation_reconciliation=receipt),advance_checkpoint=False)
        raw=path.read_bytes();result['recurring.sqlite3']=dict(data=base64.b64encode(raw).decode(),sha256=hashlib.sha256(raw).hexdigest())
    proposed=dict(native,files=result)
    for file in result:
        if file!='recurring.sqlite3':require(result[file]==native['files'][file],'reservation_content_changed')
    require(reservation_content(proposed)==reservation_content(native),'reservation_content_changed')
    return result


def successful_checkpoint(native):
    """Resolve coverage from the protected native object, memory/GET only."""
    from .aupay_card_window_preview import checkpoint

    rows = _rows(native, "recurring.sqlite3", "recurring_checkpoint")
    require(len(rows) == 1, "checkpoint_invalid")
    return checkpoint(native, rows[0]["successful_window_end"])


def preserved_unknown_proofs(native, parent, expected_checkpoint):
    """Read-only proof for a NEW completed reconciliation, never old success.

    The old exact pending native/card and append-only tables remain evidence.
    This does not reserve, repair, release pending, or change a checkpoint.
    Current readiness and every existing journal/capability guard still apply.
    """
    from .aupay_card_window_preview import checkpoint
    from .aupay_card_aliases import merge_registry
    from .drive_run_state import CARD_FILES, _validate_file
    from .aupay_card_reservation_recovery import (
        validate_revocations, completed_successor, archive_chain, digest, summaries as read_summaries, FAILURE_PATHS,
    )

    checkpoint(native, expected_checkpoint)
    card = parent["sources"]["aupay_card"]
    require(card["phase"] == "ready" and not card["attempt"] and not card["error"],
            "reconciliation_parent_pending")
    runs = _rows(native, "recurring.sqlite3", "recurring_runs")
    summaries = {r["run_id"]: json.loads(r["summary_json"]) for r in runs}
    from .production_run import COUNT_KEYS
    latest=summaries[_rows(native,"recurring.sqlite3","recurring_checkpoint")[0]["run_id"]]
    require(card["counts"]=={k:v for k,v in latest.items() if k in COUNT_KEYS and type(v) is int and v>=0},
            "reconciliation_parent_counts_changed")
    revocations = validate_revocations(native, summaries)
    proofs = {}
    for archive_row in runs:
        archive = summaries[archive_row["run_id"]]
        if archive_row["status"] != "UNKNOWN_NO_REPLAY":
            continue
        require(archive["status"] == "UNKNOWN_NO_REPLAY" and archive["replay_allowed"] is False,
                "reconciliation_unknown_not_preserved")
        old, old_card = archive["native_before"], archive["parent_card_before"]
        if archive.get("archive_schema") in {2,3}:
            old_raw = base64.b64decode(archive["native_before_raw_b64"], validate=True)
            parent_raw = base64.b64decode(archive["parent_before_raw_b64"], validate=True)
            require(hashlib.sha256(old_raw).hexdigest() == archive["native_before_bytes_sha256"]
                    and json.loads(old_raw) == old
                    and hashlib.sha256(parent_raw).hexdigest() == archive["parent_before_bytes_sha256"]
                    and json.loads(parent_raw)["sources"]["aupay_card"] == old_card,
                    "reconciliation_exact_archive_changed")
        if archive.get("archive_schema")==3:
            require(archive["adopted_archive_ids"]==[a["run_id"] for a in archive_chain(read_summaries(old))],
                    "recovery_adopted_archive_changed")
            own=[p for p in revocations.values() if p["archive_id"]==archive["run_id"]]
            require(len(own)==1 and own[0]["unused_proof"]["failed_run_uuid"]==archive["recovery"]["failed_run_uuid"],
                    "recovery_revocation_archive_missing")
        require(_hash(old) == archive["native_sha256"] and _hash(old_card) == archive["parent_card_sha256"]
                and old["binding"] == native["binding"] and old["source"] == native["source"]
                and old["schema"] == native["schema"] and old["phase"] == "pending" and old["attempt"]
                and old_card["phase"] == "pending" and old_card["attempt"]
                and native["generation"] >= old["generation"] + 2,
                "reconciliation_archive_changed")
        for file, item in old["files"].items():
            require(file in CARD_FILES and file in native["files"], "reconciliation_old_file_missing")
            raw = base64.b64decode(item["data"], validate=True)
            require(hashlib.sha256(raw).hexdigest() == item["sha256"], "reconciliation_old_file_changed")
            _validate_file(old["source"], file, raw)
            for table in CARD_FILES[file] - {"recurring_checkpoint"}:
                retained = {_hash(r) for r in _rows(native, file, table)}
                def kept(row):
                    if _hash(row) in retained:return True
                    if table!="batch_capabilities":return False
                    proof=revocations.get(row["capability_id"],{})
                    return proof.get("before")==row and _hash(proof["after"]) in retained
                require(all(kept(r) for r in _rows(old, file, table)),
                        "reconciliation_old_history_changed")
        old_cp = _rows(old, "recurring.sqlite3", "recurring_checkpoint")
        require(len(old_cp) == 1 and old_cp[0]["successful_window_end"] == archive["checkpoint_before"],
                "reconciliation_old_checkpoint_changed")
        terminal,repair = completed_successor(archive,summaries)
        certificate = repair.get("checkpoint_reconciliation", {})
        require(repair.get("status") == "complete" and repair.get("failure") == 0
                and repair.get("run_id") not in {archive["run_id"], old["attempt"], old_card["attempt"]}
                and certificate.get("archive_id") == terminal["run_id"]
                and certificate.get("evidence_sha256") == terminal["evidence_sha256"]
                and len(archive["evidence_sha256"]) == 64
                and repair["source_window_end"] == archive["checkpoint_after"]
                and datetime.fromisoformat(expected_checkpoint) >= datetime.fromisoformat(archive["checkpoint_after"])
                    > datetime.fromisoformat(archive["checkpoint_before"]),
                "reconciliation_new_completion_missing")
        if terminal.get("archive_schema")==3:
            ids=sorted(terminal["adopted_archive_ids"]+[terminal["run_id"]])
            require(certificate.get("archive_chain_ids")==ids and archive["run_id"] in ids
                    and certificate.get("archive_chain_sha256")==digest([summaries[i] for i in ids]),
                    "recovery_archive_chain_changed")
        aliases = merge_registry(repair.get("card_aliases", []))
        require(aliases and _hash(list(aliases)) == certificate.get("aliases_sha256"),
                "reconciliation_alias_proof_missing")
        rid = repair["run_id"]
        manifests = [r for r in _rows(native, "manifests.sqlite3", "batch_manifests") if r["run_id"] == rid]
        caps = [r for r in _rows(native, "capabilities.sqlite3", "batch_capabilities") if r["run_id"] == rid]
        events = sorted((r for r in _rows(native, "journal.sqlite3", "batch_events") if r["run_id"] == rid),
                        key=lambda r: r["seq"])
        require(len(manifests) == len(caps) == 1 and caps[0]["state"] == "sealed"
                and manifests[0]["payload_hash"] == certificate.get("manifest_sha256")
                and hashlib.sha256(manifests[0]["payload"].encode()).hexdigest() == manifests[0]["payload_hash"]
                and _hash(caps[0]) == certificate.get("capability_sha256")
                and _hash(events) == certificate.get("journal_sha256")
                and any((e["stage"], e["state"]) == ("write_result", "acknowledged") for e in events)
                and events[-1]["stage"] == "final" and events[-1]["state"] == "confirmed",
                "reconciliation_new_batch_unproven")
        if terminal.get("archive_schema")==3:
            require(all(datetime.fromisoformat(v["revoked_at"])<=datetime.fromisoformat(caps[0]["issued_at"])
                        for v in revocations.values() if v["archive_id"] in certificate["archive_chain_ids"]),
                    "recovery_revocation_time_invalid")
        manifest = json.loads(manifests[0]["payload"])
        if terminal.get("archive_schema")==3:
            from .aupay_card_reservation_recovery import PROTOCOL
            reservation=summaries.get("RESERVATION:"+rid,{})
            intent=summaries.get("INTENT:"+rid,{})
            require(reservation.get("protocol")==PROTOCOL and reservation.get("record_sha256")==terminal["evidence_sha256"]
                    and reservation.get("github_execution")==repair.get("github_execution")
                    and isinstance(repair.get("github_execution"),dict)
                    and reservation.get("replay_allowed") is False
                    and intent.get("protocol")==PROTOCOL and intent.get("status")=="REPAIR_DISPATCH_INTENT"
                    and intent.get("replay_allowed") is False and intent.get("manifest_sha256")==manifests[0]["payload_hash"],
                    "recovery_dispatch_intent_missing")
        started=datetime.fromisoformat(manifest["created_at"])
        if "execution_started_at" in repair:
            require(datetime.fromisoformat(repair["execution_started_at"])==started
                    and datetime.fromisoformat(repair["execution_completed_at"])>=started,
                    "recovery_execution_time_invalid")
        require(manifest["run_id"] == rid and caps[0]["batch_ref"] == manifest["batch_ref"]
                and manifest["batch_size"] == repair["written"] == repair["new_eligible"]
                and repair["write_requests"] == 1 and manifest["source_window"]["end"] == archive["checkpoint_after"]
                and manifest["source_window"]["start"] == repair["source_window_start"],
                "reconciliation_new_batch_mismatch")
        start = datetime.fromisoformat(repair["source_window_start"])
        end = datetime.fromisoformat(repair["source_window_end"])
        require((datetime.fromisoformat(archive["checkpoint_before"]) - start).total_seconds() == 172800
                and 0 < (end - start).total_seconds() <= 1209600,
                "reconciliation_coverage_unproven")
        prior = archive["github_failure"]
        require(set(prior) == {"id", "head_sha", "head_branch", "run_attempt", "status", "conclusion",
                              "event", "path", "created_at"}
                and len(prior["head_sha"]) == 40 and prior["event"] in {"schedule", "workflow_dispatch"}
                and prior["id"] not in proofs and prior["run_attempt"] == 1
                and prior["status"] == "completed" and prior["conclusion"] == "failure"
                and prior["head_branch"] == "main"
                and prior["path"] in FAILURE_PATHS
                and datetime.fromisoformat(prior["created_at"].replace("Z", "+00:00"))
                    < started,
                "reconciliation_failed_identity_invalid")
        proofs[prior["id"]] = prior
    return proofs


def verify_reconciled_state(native, parent, expected_checkpoint):
    """Before prepare/pending/write, require the exact completed native proof."""
    from .production_run import COUNT_KEYS

    require(datetime.fromisoformat(expected_checkpoint) >= datetime.fromisoformat(RECOVERED_CHECKPOINT),
            "reconciled_checkpoint_regression")
    require(native["phase"] == "ready" and not native["attempt"], "reconciled_native_pending")
    card = parent["sources"]["aupay_card"]
    require(card["phase"] == "ready" and not card["attempt"] and not card["error"],
            "reconciled_parent_pending")
    runs = _rows(native, "recurring.sqlite3", "recurring_runs")
    baseline = [json.loads(r["summary_json"]) for r in runs
                if _hash(json.loads(r["summary_json"])) == RESULT_SHA256]
    require(len(baseline) == 1, "reconciled_result_unproven")
    run_id = baseline[0]["run_id"]
    require(baseline[0]["status"] == "complete"
            and baseline[0]["source_window_end"] == RECOVERED_CHECKPOINT
            and (baseline[0]["written"], baseline[0]["duplicate"], baseline[0]["money_review"],
                 baseline[0]["write_requests"], baseline[0]["failure"]) == (22, 3, 4, 1, 0),
            "reconciled_result_unproven")
    manifests = [r for r in _rows(native, "manifests.sqlite3", "batch_manifests") if r["run_id"] == run_id]
    require(len(manifests) == 1 and manifests[0]["payload_hash"] == MANIFEST_SHA256
            and hashlib.sha256(manifests[0]["payload"].encode()).hexdigest() == MANIFEST_SHA256,
            "reconciled_manifest_unproven")
    caps = [r for r in _rows(native, "capabilities.sqlite3", "batch_capabilities") if r["run_id"] == run_id]
    require(len(caps) == 1 and caps[0]["state"] == "sealed" and _hash(caps[0]) == CAPABILITY_SHA256,
            "reconciled_capability_unproven")
    events = sorted((r for r in _rows(native, "journal.sqlite3", "batch_events")
                     if r["run_id"] == run_id), key=lambda r: r["seq"])
    require(_hash(events) == JOURNAL_SHA256, "reconciled_journal_unproven")
    cp = _rows(native, "recurring.sqlite3", "recurring_checkpoint")
    require(len(cp) == 1 and cp[0]["successful_window_end"] == expected_checkpoint,
            "reconciled_checkpoint_changed")
    current = [json.loads(r["summary_json"]) for r in runs if r["run_id"] == cp[0]["run_id"]]
    require(len(current) == 1 and current[0]["status"] in {"complete", "noop"}
            and current[0]["source_window_end"] == expected_checkpoint
            and type(current[0].get("failure")) is int and current[0]["failure"] == 0,
            "reconciled_latest_result_unproven")
    counts = {k: v for k, v in current[0].items() if k in COUNT_KEYS and type(v) is int and v >= 0}
    require(card["counts"] == counts and datetime.fromisoformat(card["last_success"])
            >= datetime.fromisoformat(RECONCILED_AT), "reconciled_parent_result_changed")
    if expected_checkpoint == RECOVERED_CHECKPOINT:
        require(_hash(native) == NATIVE_SHA256 and _hash(card) == PARENT_CARD_SHA256,
                "reconciled_first_boundary_changed")
