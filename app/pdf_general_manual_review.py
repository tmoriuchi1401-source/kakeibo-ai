"""Shared PDF form -> established complete-manual service, no new writer.

The durable owner request remains in _PDF確認受付 and the existing Drive Unit
journal. A one-request adapter presents that verified snapshot to the existing
manual-entry parser/writer; no extra queue append can have ambiguous delivery.
No OCR/AI/Medical parser, source mover or hidden-cell authority exists here.
The hosted dispatcher must enforce its reviewed-code/main/mutex boundary.
"""
from copy import deepcopy
import json

from .drive_run_state import StateError
from .pdf_production_authority import DrivePdfAuthority
from .pdf_unit_processing import DriveUnitProcessingStore,digest
from .pdf_page_kind import current_answer
from .receipt_pdf_units import _digest
from . import manual_entry
from .pdf_general_manual import confirm


class _OneOwnerRequest:
    """Reuse manual_entry.execute without writing a second physical queue.

    Only its exact expense append reaches the real DB through the existing
    service's writer fence. Queue state is projection; Drive intent is durable.
    """
    def __init__(self,db,request_id,payload):
        self.db=db
        self.row=[request_id,'dispatching',json.dumps(payload,sort_keys=True,separators=(',',':')),'','','']

    def get_raw(self,rng):
        if rng=="'_手入力受付'!A1:F":return [manual_entry.HEADER[:],deepcopy(self.row)]
        return self.db.get_raw(rng)

    def set_raw_range(self,rng,rows):
        if rng!="'_手入力受付'!A2" or len(rows)!=1 or rows[0][0]!=self.row[0] or rows[0][2]!=self.row[2]:
            raise StateError('pdf_manual_request_projection_forbidden')
        self.row=deepcopy(rows[0])

    def categories(self):return self.db.categories()
    def expense_records(self):return self.db.expense_records()
    def append_raw(self,title,rows):
        if title!='支出明細':raise StateError('pdf_manual_accounting_target_forbidden')
        return self.db.append_raw(title,rows)


class GeneralManualReview:
    def __init__(self,authority,completion,db):
        if not isinstance(authority,DrivePdfAuthority) or not isinstance(completion,DriveUnitProcessingStore):
            raise StateError('pdf_manual_durable_dependencies_required')
        self.authority,self.completion,self.db=authority,completion,db

    def current(self,identity):
        try:
            current=self.authority.current(identity['source_file_id'])
            numbers=identity['page_numbers']
            if (current.status!='grouping_confirmed' or not isinstance(numbers,list) or len(numbers)!=1
                    or type(numbers[0]) is not int or not 1<=numbers[0]<=current.page_count):raise ValueError()
            spec=next(s for s in current.units if s['page_numbers']==numbers)
            if (spec['automatic_classifications']!=['sensitive_unknown']
                    or spec['human_classifications']!=['normal'] or not all(spec['page_kind_digests'])):
                raise ValueError()
            value=self.authority.legacy.load();p=value['records'][_digest(current.source_file_id)]['proposal']
            number=numbers[0];page=p['pages'][number-1];answer=current_answer(value,page)
            if (p['source_content_hash']!=current.source_content_hash or p['page_count']!=current.page_count
                    or not answer or answer['human_classification']!='normal'):
                raise ValueError()
            key='pdf-general-manual-view-v1:'+_digest([current.source_file_id,current.source_content_hash,
                number,page['page_hash'],answer['confirmation_digest'],p['proposal_digest'],p['grouping_version']])
            expected={'schema':'pdf-page-review-v1','kind':'general_manual','source_file_id':current.source_file_id,
                'source_content_hash':current.source_content_hash,'page_numbers':numbers,
                'page_hashes':[page['page_hash']],'kind_digests':[answer['confirmation_digest']],
                'review_id':key,'proposal_digest':p['proposal_digest'],'grouping_version':p['grouping_version']}
            if identity!=expected or self.authority.verify(spec) is not True:raise ValueError()
            state=self.completion.load()
            if any(h['classification'] in {'medical','payroll'} and
                    h['unit']['source_file_id']==current.source_file_id and
                    h['unit']['source_content_hash']==current.source_content_hash and h['page_number']==number
                    for h in state['privacy_holds'].values()):
                raise StateError('pdf_manual_strong_sensitive_evidence')
            return spec
        except StateError:raise
        except Exception:raise StateError('pdf_manual_ui_identity_changed') from None

    def __call__(self,identity,request_id,submitted,read_owner):
        if not manual_entry.UUID.fullmatch(request_id) or not callable(read_owner):
            raise StateError('pdf_manual_owner_request_required')
        spec=self.current(identity)
        try:
            if (set(submitted)!={'date','amount','major','minor','merchant','payment','note'}
                    or (submitted['major'],submitted['minor']) not in set(self.db.categories())):
                raise ValueError()
            manual_entry._payload(json.dumps(submitted),set(self.db.categories()))
        except Exception:raise StateError('pdf_manual_owner_input_invalid') from None
        payload={**deepcopy(submitted),'pdf_unit':digest(spec)}
        canonical_request=request_id
        previous=self.completion.load()['records'].get(spec['unit_id'])
        if previous:
            # A second button press can mint another UI UUID. Same owner input
            # must still reconcile the first immutable accounting intent.
            if (previous['unit']!=spec or previous['route']!='general_manual'
                    or previous['input_digest']!=digest(payload)):
                raise StateError('pdf_manual_existing_intent_changed')
            try:
                row=previous['planned_rows']['支出明細'][0]
                canonical_request=row[10].removeprefix('manual:')
                if (not row[10].startswith('manual:') or not manual_entry.UUID.fullmatch(canonical_request)
                        or row[0]!=manual_entry.expense_id(canonical_request)
                        or previous['writer_reference']!=digest(['existing-general-manual-v1',canonical_request])):
                    raise ValueError()
            except Exception:raise StateError('pdf_manual_existing_intent_changed') from None
        def verify_current(candidate):
            return candidate==spec and self.current(identity)==spec
        def owner(uid,provided):
            return uid==canonical_request and provided==payload and read_owner()==submitted
        # The callback re-reads the shared captured UUID + visible input and
        # its original link. Cells never supply the durable Unit proof above.
        if not owner(canonical_request,payload):raise StateError('pdf_manual_owner_snapshot_changed')
        request_db=_OneOwnerRequest(self.db,canonical_request,payload)
        result=confirm(request_db,self.completion,spec,canonical_request,
            verify_current=verify_current,verify_owner_request=owner)
        if result['status']!='manual_imported':raise StateError('pdf_manual_accounting_readback_required')
        return '一般手入力済み'
