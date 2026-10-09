"""Runner-side intent capture in the EXISTING hidden PDF受付 queue.

No trigger or onEdit dependency. No actor inference, dispatch or ledger access.
The existing runner/operator must explicitly call this adapter; this change
does not activate polling in a schedule. Live capture is disabled by default.
"""
import json
from .drive_run_state import StateError
from .pdf_review_fields import QUEUE
from .page_receipt_model import digest
from .receipt_item_review import UUID, check_snapshot


def capture(sheet,expected,snapshot,*,request_id,clock,write_enabled=False):
    if not UUID.fullmatch(request_id):raise StateError('item_queue_uuid_invalid')
    fields=check_snapshot(snapshot,expected)
    if fields['action']=='未選択':return {'status':'not_requested','appended':0}
    if fields['action'] not in {'記帳する','保留する'}:raise StateError('item_queue_action_invalid')
    def rows():
        raw=sheet._get(QUEUE,'A2:F1001')
        if any(len(r)>6 for r in raw):raise StateError('item_queue_schema_changed')
        return [list(r)+['']*(6-len(r)) for r in raw]
    previous=rows()
    for row in previous:
        if len(row)<3:continue
        try:old=json.loads(row[2])
        except Exception:
            if row[1] in {'accepted','dispatching'}:raise StateError('item_queue_unknown_pending')
            continue
        if not isinstance(old,dict):continue # Legacy grouping requests stay untouched.
        if row[0]==request_id:
            if old!=snapshot:raise StateError('item_queue_uuid_reused')
            return {'status':'already_captured','request_id':row[0],'appended':0}
        if old.get('token')==expected['token'] and row[1] in {'accepted','dispatching','running','unknown'}:
            if old!=snapshot:raise StateError('item_queue_existing_request_requires_readback')
            return {'status':'already_captured','request_id':row[0],'appended':0}
    if len(previous)>=1000:raise StateError('item_queue_bounded_limit')
    if not write_enabled:return {'status':'would_capture','snapshot_digest':digest(snapshot),'appended':0}
    # Optimistic queue read is rechecked; deployment must provide the existing
    # single-runner lock/concurrency. Never retry an ambiguous append.
    if rows()!=previous:raise StateError('item_queue_changed')
    row=[request_id,'accepted',json.dumps(snapshot,ensure_ascii=False,separators=(',',':')),clock(),'','']
    region=f'A{len(previous)+2}:F{len(previous)+2}'
    try:sheet._write([{'range':f"'{QUEUE}'!{region}",'values':[row]}])
    except Exception:pass
    after=rows()
    if after!=previous+[row]:raise StateError('item_queue_unknown_delivery')
    return {'status':'accepted','request_id':request_id,'snapshot_digest':digest(snapshot),'appended':1}
