"""Explicit, bounded operator canary around the existing ReceiptPipeline writer.

Grouping never authorizes accounting. A separate durable operator intent freezes
the exact rows before append. No AI, Medical, mover, UI or archive API exists here.
Pending/unknown delivery requires reconciliation, never an automatic second write.
"""
from copy import deepcopy
from datetime import date
import json
from hashlib import sha256
from io import BytesIO
import re
from types import SimpleNamespace
from PIL import Image

from .drive_run_state import StateError
from .receipt_pipeline import ReceiptPipeline
from .receipt_confirmation import same_row
from .receipt_validation import validate_receipt_result, POLICY_VERSION
from .receipt_reimport import _date, _money
from .receipt_reimport_production import encoded, digest
from .sheets import HEADERS
from .utils import now_jst_string

ALLOWED_PAGES = (2,3,5,6,7,8,9,11,12,13)
TABLES = ('レシート','支出明細','取込データ')
SCHEMA = 'pdf-receipt-write-canary-v1'


class CanaryStore:
    """Reuse the established v2 If-Match transport, with a separate binding."""
    def __init__(self, transport, manifest):
        self.transport,self.manifest=transport,manifest
        self.payload,self.tag=transport.read_versioned()
        self.value=json.loads(self.payload)
        self._validate(self.value)
        self.replayed_pages=set()

    def _validate(self,value):
        if (set(value)!={'schema','manifest','records'} or value['schema']!=SCHEMA
                or value['manifest']!=self.manifest or not isinstance(value['records'],dict)):
            raise StateError('canary_state_invalid')
        for key,r in value['records'].items():
            if (key!=r['unit_id'] or r['page_number'] not in ALLOWED_PAGES
                    or r['phase'] not in {'pending','applied'} or r['policy_version']!=POLICY_VERSION
                    or digest(r['plan'])!=r['plan_digest']
                    or any(title not in TABLES for title,_ in r['plan'])):
                raise StateError('canary_state_invalid')
            titles=[title for title,_ in r['plan']]
            if (len(titles)<3 or titles[0]!='レシート' or titles[-1]!='取込データ'
                    or any(t!='支出明細' for t in titles[1:-1])
                    or self.manifest['unit_ids'].get(str(r['page_number']))!=key):
                raise StateError('canary_state_invalid')
            receipt=r['plan'][0][1];imported=r['plan'][-1][1];expenses=[x[1] for x in r['plan'][1:-1]]
            rid='R-'+key;iid='receipt:'+key
            if (len(receipt)!=9 or len(imported)!=12 or receipt[0]!=rid or imported[0]!=iid
                    or imported[3]!=key or receipt[6]!='解析済' or imported[8]!='解析済'
                    or any(len(x)!=13 or x[0]!=f'{rid}-{n:02d}' or x[9:11]!=[rid,iid]
                           or x[12]!='active' for n,x in enumerate(expenses,1))
                    or receipt[3]<=0 or receipt[3]!=imported[6]
                    or sum(x[4] for x in expenses)!=receipt[3]):
                raise StateError('canary_state_invalid')
            e=r.get('duplicate_comparison')
            if e is not None:
                fields={'schema','decision','receipt_id','import_id','source_file_id','source_content_hash',
                    'rows_digest','candidate_digest','payload_sha256','old_date','new_date',
                    'old_item_count','new_item_count','date_original_verified','merchant_original_verified'}
                if (not isinstance(e,dict) or set(e)!=fields or e['schema']!='receipt-distinct-originals-v1'
                        or e['decision']!='distinct_transactions' or e['receipt_id']!='R-'+e['source_file_id']
                        or e['import_id']!='receipt:'+e['source_file_id'] or e['source_file_id']==key
                        or e['candidate_digest']!=r['parsed_digest'] or e['payload_sha256']!=r['payload_sha256']
                        or any(not isinstance(e[k],str) or not re.fullmatch('[0-9a-f]{64}',e[k])
                            for k in ('source_content_hash','rows_digest','candidate_digest','payload_sha256'))
                        or not _date(e['old_date']) or not _date(e['new_date']) or e['old_date']==e['new_date']
                        or e['new_date']!=receipt[1] or e['new_item_count']!=len(expenses)
                        or type(e['old_item_count']) is not int or e['old_item_count']<1
                        or e['date_original_verified'] is not True or e['merchant_original_verified'] is not True):
                    raise StateError('canary_comparison_state_invalid')

    def save(self,value):
        self._validate(value)
        proposed=encoded(value)
        self.transport.replace_versioned(self.payload,self.tag,proposed)
        payload,tag=self.transport.read_versioned()
        if payload!=proposed:raise StateError('canary_state_readback_mismatch')
        self.value,self.payload,self.tag=deepcopy(value),payload,tag

    def unchanged(self):
        if self.transport.read_versioned()!=(self.payload,self.tag):
            raise StateError('canary_intent_changed')


def table_rows(db,title):
    width={'レシート':'I','支出明細':'M','取込データ':'L'}[title]
    return db.get_raw(f"'{title}'!A2:{width}")


def verify_plan(db,plan,*,complete):
    tables={title:table_rows(db,title) for title in TABLES}
    for title,row in plan:
        matches=[r for r in tables[title] if r and r[0]==row[0]]
        if len(matches)>1 or (matches and not same_row(title,matches[0],row)):
            raise StateError('canary_existing_content_conflict')
        if complete and not matches:raise StateError('canary_readback_missing')


def check_duplicates(db,source_id,result,*,verified_distinct=frozenset()):
    """Same identity is handled by exact replay; other matching totals are held."""
    rid='R-'+source_id;iid='receipt:'+source_id
    if any(r and r[0] in {rid,iid} for title in ('レシート','取込データ') for r in table_rows(db,title)):
        raise StateError('canary_existing_identity_without_intent')
    for row in table_rows(db,'レシート'):
        if len(row)<4:continue
        if row[0] in verified_distinct:continue
        day=_date(row[1])
        if day and _money(row[3])==result.total and abs((date.fromisoformat(day)-date.fromisoformat(result.date)).days)<=7:
            raise StateError('canary_possible_duplicate')
    for row in table_rows(db,'支出明細'):
        if len(row)<5:continue
        if len(row)>12 and row[12] in {'superseded','excluded'}:continue
        if len(row)>9 and row[9] in verified_distinct:continue
        day=_date(row[1])
        if day and _money(row[4])==result.total and abs((date.fromisoformat(day)-date.fromisoformat(result.date)).days)<=7:
            raise StateError('canary_possible_duplicate')


class PlanningDB:
    """Exercise the real writer without persistence; refuse any other capability."""
    def __init__(self,categories):self.cats=categories;self.plan=[]
    def categories(self):return self.cats
    def import_ids(self):return set()
    def receipt_ids(self):return set()
    def expense_index(self):return {}
    def ensure_expense_status_column(self):pass
    def append(self,title,rows):
        if title not in TABLES:raise StateError('canary_write_forbidden')
        self.plan.extend((title,deepcopy(row)) for row in rows)


class AppendOnlyDB:
    """Fence exact planned new rows; never update/delete or repair other rows."""
    def __init__(self,db,plan,categories,before_write):
        self.db,self.plan,self.cats,self.before_write=db,plan,categories,before_write
        self.appended=0
    def categories(self):return self.cats
    def import_ids(self):return {r[0] for r in table_rows(self.db,'取込データ') if r}
    def receipt_ids(self):return {r[0] for r in table_rows(self.db,'レシート') if r}
    def expense_index(self):return {r[0]:n for n,r in enumerate(table_rows(self.db,'支出明細'),2) if r}
    def ensure_expense_status_column(self):
        if self.db.get_raw("'支出明細'!M1:M1")!=[['計上状態']]:
            raise StateError('canary_header_mismatch')
    def append(self,title,rows):
        if not rows:return
        expected=[row for sheet,row in self.plan if sheet==title]
        if title not in TABLES or rows!=expected:raise StateError('canary_write_forbidden')
        self.before_write();verify_plan(self.db,self.plan,complete=False)
        if any(r and r[0] in {x[0] for x in rows} for r in table_rows(self.db,title)):
            raise StateError('canary_concurrent_accounting_change')
        # Existing literal writer prevents formula interpretation of OCR text.
        # The narrowly fenced live caller suppresses unrelated UI restoration.
        try:self.db.append_raw(title,rows)
        except Exception:
            # Acknowledge only exact read-back; never re-send an ambiguous append.
            verify_plan(self.db,[(title,row) for row in rows],complete=True)
        verify_plan(self.db,[(title,row) for row in rows],complete=True)
        self.appended+=len(rows)


def run_canary(store,db,number,fresh_candidate,verify_fresh,*,distinct_originals=None):
    if number not in ALLOWED_PAGES:raise StateError('canary_page_not_allowed')
    for title in TABLES:
        width={'レシート':'I','支出明細':'M','取込データ':'L'}[title]
        if db.get_raw(f"'{title}'!A1:{width}1")!=[HEADERS[title]]:
            raise StateError('canary_header_mismatch')
    # Caller checks durable authority, source SHA, stable identity and exact PNG
    # privacy before returning even a previously analyzed structured candidate.
    unit_id,payload,result,categories,proof=fresh_candidate(number)
    expected={'unit_id':store.manifest['unit_ids'].get(str(number)),
        'page_identity':store.manifest['page_identities'].get(str(number)),
        'page_number':number,'source_content_hash':store.manifest['source_content_hash'],
        'confirmation_digest':store.manifest['confirmation_digest'],
        'grouping_revision':store.manifest['grouping_revision'],
        'human_classification':'normal','effective_classification':'normal'}
    if proof!=expected or unit_id!=expected['unit_id']:
        raise StateError('canary_authority_mismatch')
    from .receipt_pdf_units import MAX_PAGE_PIXELS
    with Image.open(BytesIO(payload)) as image:
        if (image.format!='PNG' or image.mode!='RGB' or image.info or getattr(image,'n_frames',1)!=1
                or image.width*image.height>MAX_PAGE_PIXELS):
            raise StateError('canary_payload_invalid')
    if not validate_receipt_result(result,categories)[0] or result.transaction_kind!='purchase':
        raise StateError('canary_validation_failed')
    old=store.value['records'].get(unit_id)
    if old:
        if old['phase']!='applied':raise StateError('canary_pending_reconciliation_required')
        if old['proof']!=proof or old['parsed_digest']!=digest(result.model_dump()):
            raise StateError('canary_replay_candidate_changed')
        if old.get('duplicate_comparison'):
            if distinct_originals is None:raise StateError('canary_duplicate_comparison_required')
            # The original comparison PNG is a diagnostic fingerprint. Applied
            # replay uses source/Unit/candidate/rows; fresh PNG has already passed
            # its exact gate and may encode differently on a future renderer.
            distinct_originals.verify(old['duplicate_comparison'],result,payload,replay=True)
        verify_fresh(number,unit_id);verify_plan(db,old['plan'],complete=True)
        store.replayed_pages.add(number)
        return {'page_number':number,'status':'replayed','appended':0,'readback':True}
    if number!=11 and (11 not in store.replayed_pages or not any(
            r['page_number']==11 and r['phase']=='applied' for r in store.value['records'].values())):
        raise StateError('canary_p11_required')
    if number==11 and (result.date!='2026-09-26' or result.total!=159 or len(result.items)!=1):
        raise StateError('canary_p11_candidate_changed')
    evidence=distinct_originals.resolve(result,payload) if distinct_originals is not None else None
    permitted=distinct_originals.verify(evidence,result,payload) if evidence else frozenset()
    check_duplicates(db,unit_id,result,verified_distinct=permitted)
    timestamp=now_jst_string()
    cached=lambda *_,**__:result.model_copy(deep=True)
    ai=SimpleNamespace(analyze_receipt=cached)
    planning=PlanningDB(categories)
    pipeline=ReceiptPipeline(planning,ai,clock=lambda:timestamp)
    outcome=pipeline._process_image_bytes(payload,'image/png',unit_id,
        f"https://drive.google.com/file/d/{store.manifest['source_file_id']}/view#page={number}",
        known_source_classification='normal',observe_medical=False)
    if outcome['status']!='imported':raise StateError('canary_validation_failed')
    plan=planning.plan
    verify_fresh(number,unit_id)
    intent=dict(unit_id=unit_id,page_number=number,phase='pending',policy_version=POLICY_VERSION,
        timestamp=timestamp,parsed_digest=digest(result.model_dump()),proof=proof,
        payload_sha256=sha256(payload).hexdigest(),plan=plan,plan_digest=digest(plan))
    if evidence:intent['duplicate_comparison']=evidence
    value=deepcopy(store.value);value['records'][unit_id]=intent;store.save(value)
    def barrier():
        store.unchanged();verify_fresh(number,unit_id)
        if db.categories()!=categories:raise StateError('canary_categories_changed')
        if evidence:distinct_originals.verify(evidence,result,payload)
    fenced=AppendOnlyDB(db,plan,categories,barrier)
    outcome=ReceiptPipeline(fenced,ai,clock=lambda:timestamp)._process_image_bytes(
        payload,'image/png',unit_id,
        f"https://drive.google.com/file/d/{store.manifest['source_file_id']}/view#page={number}",
        known_source_classification='normal',observe_medical=False)
    if outcome['status']!='imported':raise StateError('canary_write_incomplete')
    verify_plan(db,plan,complete=True);barrier()
    value=deepcopy(store.value);value['records'][unit_id]['phase']='applied';store.save(value)
    return {'page_number':number,'status':'imported','appended':fenced.appended,'readback':True,
            'items':len(result.items),'total':result.total,'payment_method':result.payment_method}
