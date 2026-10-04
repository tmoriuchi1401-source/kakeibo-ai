"""Frozen plans around the existing receipt writer; no new ledger writer.

An unknown/pending delivery is read-back only. Never resume a partial append,
overwrite a conflicting row, or recreate an intent from accounting markers.
"""
from copy import deepcopy
from datetime import date
from hashlib import sha256
from types import SimpleNamespace

from .drive_run_state import StateError
from .pdf_unit_processing import DriveUnitProcessingStore, digest, TABLES
from .pdf_unit_payload import PayloadHold
from .receipt_confirmation import same_row
from .receipt_pipeline import ReceiptPipeline
from .receipt_reimport import _date, _money
from .receipt_validation import POLICY_VERSION, validate_receipt_result
from .sheets import HEADERS
from .utils import now_jst_string

WIDTHS={'レシート':'I','支出明細':'M','取込データ':'L'}


def table_rows(db,title):
    return db.get_raw(f"'{title}'!A2:{WIDTHS[title]}")


def readback(db,plan,*,complete=True):
    for title,rows in plan.items():
        existing=table_rows(db,title)
        expected_ids={r[0] for r in rows}
        units={r[0][2:] if title=='レシート' else r[3] if title=='取込データ'
            else r[9][2:] for r in rows}
        for actual in existing:
            if not actual:continue
            related=(title=='レシート' and actual[0] in {'R-'+u for u in units}
                or title=='取込データ' and len(actual)>3 and actual[3] in units
                or title=='支出明細' and (len(actual)>10 and (
                    actual[9] in {'R-'+u for u in units} or actual[10] in {'receipt:'+u for u in units})
                    or any(str(actual[0]).startswith('R-'+u+'-') for u in units)))
            if related and actual[0] not in expected_ids:
                raise StateError('pdf_receipt_existing_content_conflict')
        for row in rows:
            found=[r for r in existing if r and r[0]==row[0]]
            if len(found)>1 or found and not same_row(title,found[0],row):
                raise StateError('pdf_receipt_existing_content_conflict')
            if complete and not found:raise StateError('pdf_receipt_readback_missing')
    return True


def _headers(db):
    for title,width in WIDTHS.items():
        if db.get_raw(f"'{title}'!A1:{width}1")!=[HEADERS[title]]:
            raise StateError('pdf_receipt_header_mismatch')


def require_new_identity(db,spec):
    rid='R-'+spec['unit_id'];iid='receipt:'+spec['unit_id']
    # Legacy parent receipts/other Unit revisions cannot establish replay.
    parent_ids={'R-'+spec['source_file_id'],'receipt:'+spec['source_file_id']}
    for title in TABLES:
        for r in table_rows(db,title):
            if r and (r[0] in {rid,iid}|parent_ids or
                    title=='支出明細' and (str(r[0]).startswith(rid+'-') or
                        len(r)>10 and (r[9]==rid or r[10]==iid))):
                raise StateError('pdf_receipt_existing_identity_without_intent')


def _duplicates(db,spec,result):
    require_new_identity(db,spec)
    for title,amount_index in [('レシート',3),('支出明細',4)]:
        for r in table_rows(db,title):
            if len(r)<=amount_index:continue
            if title=='支出明細' and len(r)>12 and r[12] in {'excluded','superseded'}:continue
            day=_date(r[1])
            if day and _money(r[amount_index])==result.total and abs(
                    (date.fromisoformat(day)-date.fromisoformat(result.date)).days)<=7:
                # Candidate only; this never declares two transactions identical.
                raise StateError('pdf_receipt_duplicate_candidate')


class PlanningDB:
    def __init__(self,categories):self.cats=deepcopy(categories);self.plan={}
    def categories(self):return deepcopy(self.cats)
    def import_ids(self):return set()
    def receipt_ids(self):return set()
    def expense_index(self):return {}
    def ensure_expense_status_column(self):pass
    def append(self,title,rows):
        if title not in TABLES:raise StateError('pdf_receipt_write_forbidden')
        self.plan.setdefault(title,[]).extend(deepcopy(rows))


class AppendOnlyDB:
    def __init__(self,db,plan,categories,before_write):
        self.db,self.plan,self.cats,self.before_write=db,plan,categories,before_write
        self.appended=0
    def categories(self):return deepcopy(self.cats)
    def import_ids(self):return {r[0] for r in table_rows(self.db,'取込データ') if r}
    def receipt_ids(self):return {r[0] for r in table_rows(self.db,'レシート') if r}
    def expense_index(self):return {r[0]:n for n,r in enumerate(table_rows(self.db,'支出明細'),2) if r}
    def ensure_expense_status_column(self):_headers(self.db)
    def append(self,title,rows):
        if not rows:return
        if title not in self.plan or rows!=self.plan[title]:
            raise StateError('pdf_receipt_write_forbidden')
        self.before_write();readback(self.db,self.plan,complete=False)
        if any(r and r[0] in {x[0] for x in rows} for r in table_rows(self.db,title)):
            raise StateError('pdf_receipt_concurrent_accounting_change')
        try:self.db.append_raw(title,rows)
        except Exception:
            # Lost acknowledgement may be accepted only after exact read-back.
            # An absent/partial row stops here; never send this append again.
            readback(self.db,{title:rows})
        readback(self.db,{title:rows});self.appended+=len(rows)


def reconcile(db,store,spec,*,verify_current):
    """Existing applied/pending intent -> exact read-back, no render/AI/append."""
    if not isinstance(store,DriveUnitProcessingStore):
        raise StateError('pdf_receipt_durable_intent_required')
    record=store.load()['records'].get(spec['unit_id'])
    if not record:return None
    if record['unit']!=spec or record['route']!='receipt':
        raise StateError('pdf_receipt_intent_changed')
    done=store.complete(spec['unit_id'],record['intent_digest'],verify_current=verify_current,
        verify_readback=lambda r:readback(db,r['planned_rows']))
    return {'status':'imported','replayed':True,'appended':0,'readback':True,
            'intent_digest':done['intent_digest']}


def materialize(db,store,spec,payload,payload_sha256,result,*,verify_current,clock=now_jst_string):
    """Call only after a fresh exact-payload gate and normal authority checks."""
    if not isinstance(store,DriveUnitProcessingStore):
        raise StateError('pdf_receipt_durable_intent_required')
    if set(spec['automatic_classifications']+spec['human_classifications'])!={'normal'}:
        raise StateError('pdf_receipt_normal_authority_required')
    if not isinstance(payload,bytes) or sha256(payload).hexdigest()!=payload_sha256:
        raise StateError('pdf_receipt_payload_changed')
    old=reconcile(db,store,spec,verify_current=verify_current)
    if old:return old
    _headers(db);categories=deepcopy(db.categories())
    if result.transaction_kind!='purchase' or not validate_receipt_result(result,categories)[0]:
        raise StateError('pdf_receipt_validation_failed')
    timestamp=clock();cached=SimpleNamespace(analyze_receipt=lambda *_,**__:result.model_copy(deep=True))
    url=f"https://drive.google.com/file/d/{spec['source_file_id']}/view#page={spec['page_numbers'][0]}"
    planning=PlanningDB(categories)
    outcome=ReceiptPipeline(planning,cached,clock=lambda:timestamp)._process_image_bytes(
        payload,'image/png',spec['unit_id'],url,known_source_classification='normal',observe_medical=False)
    if outcome['status']=='privacy_blocked':
        raise PayloadHold(outcome['classification'],'planning_payload_privacy_blocked')
    if outcome['status']!='imported' or set(planning.plan)!=set(TABLES):
        # Planning has no review/snapshot capability and cannot persist a review.
        raise StateError('pdf_receipt_validation_failed')
    _duplicates(db,spec,result)
    input_digest=digest([POLICY_VERSION,result.model_dump()])
    record,created=store.reserve(spec,'receipt',input_digest,planning.plan,
        digest(['receipt-pipeline',POLICY_VERSION,planning.plan]),verify_current=verify_current)
    if not created:
        return reconcile(db,store,spec,verify_current=verify_current)
    def barrier():
        store.verify_pending(spec['unit_id'],record['intent_digest'])
        if verify_current(deepcopy(spec)) is not True:
            raise StateError('pdf_receipt_source_or_authority_changed')
        if db.categories()!=categories:raise StateError('pdf_receipt_categories_changed')
        if sha256(payload).hexdigest()!=payload_sha256:raise StateError('pdf_receipt_payload_changed')
    barrier()
    fenced=AppendOnlyDB(db,planning.plan,categories,barrier)
    outcome=ReceiptPipeline(fenced,cached,clock=lambda:timestamp)._process_image_bytes(
        payload,'image/png',spec['unit_id'],url,known_source_classification='normal',observe_medical=False)
    if outcome['status']=='privacy_blocked':
        raise PayloadHold(outcome['classification'],'writer_payload_privacy_blocked')
    if outcome['status']!='imported':raise StateError('pdf_receipt_write_incomplete')
    barrier();done=store.complete(spec['unit_id'],record['intent_digest'],verify_current=verify_current,
        verify_readback=lambda r:readback(db,r['planned_rows']))
    return {'status':'imported','replayed':False,'appended':fenced.appended,'readback':True,
            'intent_digest':done['intent_digest']}
