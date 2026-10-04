"""Read-only evidence of already-completed manual Units, never a writer.

Completion labels/Spreadsheet status are insufficient. Require current Drive
source/page authority, the immutable completion intent, and exact accounting
rows. Medical stays in its existing backend; only a metadata reference is used.
"""
from copy import deepcopy
import json

from .drive_run_state import DriveStateTransport, StateError
from .pdf_production_authority import DrivePdfAuthority
from .pdf_unit_processing import DriveUnitProcessingStore, digest
from .receipt_confirmation import ReceiptConfirmation, review_id, same_row
from .receipt_reimport_production import ReimportStore, digest as medical_digest
from .receipt_pdf_units import DocumentUnit, PageObservation, _digest
from .pdf_page_kind import current_answer
from . import manual_entry


class ManualTerminalReadback:
    def __init__(self, authority, completion, db, *, medical_store=None):
        if (not isinstance(authority,DrivePdfAuthority)
                or not isinstance(completion,DriveUnitProcessingStore)
                or medical_store is not None and (not isinstance(medical_store,ReimportStore)
                    or not isinstance(medical_store.transport,DriveStateTransport))):
            raise StateError('pdf_manual_terminal_durable_dependencies_required')
        self.authority,self.completion,self.db,self.medical_store=authority,completion,db,medical_store

    def verify(self, spec, intent_digest):
        before=self.completion.load()
        record=before['records'].get(spec['unit_id'])
        if (not record or record['unit']!=spec or record['phase']!='applied'
                or record['intent_digest']!=intent_digest or self.authority.verify(spec) is not True):
            raise StateError('pdf_manual_terminal_intent_or_source_changed')
        if record['route']=='general_manual':
            if any(h['classification'] in {'medical','payroll'}
                    and h['unit']['source_file_id']==spec['source_file_id']
                    and h['unit']['source_content_hash']==spec['source_content_hash']
                    and h['page_number'] in spec['page_numbers'] for h in before['privacy_holds'].values()):
                raise StateError('pdf_manual_terminal_strong_sensitive_hold')
            self._general(spec,record)
        elif record['route']=='medical_manual':self._medical(spec,record)
        else:raise StateError('pdf_manual_terminal_route_forbidden')
        if self.authority.verify(spec) is not True or self.completion.load()!=before:
            raise StateError('pdf_manual_terminal_scope_changed')
        return True

    def _general(self, spec, record):
        row=record['planned_rows']['支出明細'][0]
        request_id=str(row[10]).removeprefix('manual:')
        if (not str(row[10]).startswith('manual:') or not manual_entry.UUID.fullmatch(request_id)
                or row[0]!=manual_entry.expense_id(request_id)
                or row[3]!='手入力' or row[8:10]!=['manual',''] or row[12]!='active'
                or record['writer_reference']!=digest(['existing-general-manual-v1',request_id])):
            raise StateError('pdf_manual_terminal_general_reference_changed')
        # Shared PDF input normalizes date/amount before saving the owner intent.
        payload={'date':row[1],'amount':row[4],'merchant':row[2],'major':row[5],
            'minor':row[6],'payment':row[7],'note':row[11],'pdf_unit':digest(spec)}
        if (record['input_digest']!=digest(payload) or (row[5],row[6]) not in set(self.db.categories())):
            raise StateError('pdf_manual_terminal_general_input_changed')
        parsed=manual_entry._payload(json.dumps(payload),set(self.db.categories()))
        if parsed[:6]!=(row[1],row[4],row[2],row[11],row[5],row[6]) or parsed[6]!=row[7]:
            raise StateError('pdf_manual_terminal_general_input_changed')
        rows=self.db.get_raw("'支出明細'!A2:M")
        matches=[r for r in rows if r and r[0]==row[0]]
        if len(matches)!=1 or not same_row('支出明細',matches[0],row):
            raise StateError('pdf_manual_terminal_accounting_readback_required')

    def _medical(self, spec, record):
        store=self.medical_store
        if store is None:raise StateError('pdf_manual_terminal_medical_backend_required')
        forbidden={self.authority.legacy.transport.binding.file_id,self.completion.transport.binding.file_id}
        if self.authority.migrated is not None:forbidden.add(self.authority.migrated.transport.binding.file_id)
        if store.transport.binding.file_id in forbidden:
            raise StateError('pdf_manual_terminal_medical_store_must_be_separate')
        if store.transport.read()!=store.payload:
            raise StateError('pdf_manual_terminal_medical_state_changed')
        current=self.authority.current(spec['source_file_id'])
        if spec not in current.medical_pages or len(spec['page_numbers'])!=1:
            raise StateError('pdf_manual_terminal_medical_authority_changed')
        value=self.authority.legacy.load()
        proposal=value['records'][_digest(spec['source_file_id'])]['proposal']
        page=proposal['pages'][spec['page_numbers'][0]-1];answer=current_answer(value,page)
        if not answer or answer['human_classification']!='medical':
            raise StateError('pdf_manual_terminal_medical_kind_required')
        # Reuse existing page Unit/review identity; no new Medical identity/HMAC.
        source={'source_id':DocumentUnit(PageObservation(**page)).source_id,
            'version':page['page_hash'],'sha256':page['source_content_hash'],'mime_type':'application/pdf',
            'pdf_page':{'original_file_id':page['source_file_id'],'page_number':page['page_number'],
                'page_hash':page['page_hash'],'page_kind_confirmation_digest':answer['confirmation_digest']}}
        key=review_id('medical',source)
        item=deepcopy(store.value.get('confirmation_items',{}).get(key,{}))
        inputs=item.get('inputs',[])
        if (item.get('source')!=source or item.get('kind')!='medical' or item.get('status')!='applied'
                or item.get('decision_origin')!='human' or not isinstance(inputs,list) or len(inputs)!=8
                or inputs[5] not in {'医療費を確定','既存支出と重複（紐付け）','重複候補と別の支出として確定'}
                or item.get('confirmation_hash')!=medical_digest(inputs)
                or record['input_digest']!=item['confirmation_hash']
                or record['writer_reference']!=digest(['existing-medical-manual-v1',key,item['confirmation_hash']])
                or record['planned_rows']!={} or not item.get('plan')):
            raise StateError('pdf_manual_terminal_medical_confirmation_required')
        existing=ReceiptConfirmation(store,self.db,lambda *_:None)
        # Existing complete manual validator and read-back, never capture/apply.
        try:
            parsed=existing._parsed(item)
            self._medical_plan(source,item,parsed,existing)
        except Exception:raise StateError('pdf_manual_terminal_medical_plan_changed') from None
        from .medical_local_duplicate import verify_saved_targets
        verify_saved_targets(item,existing.tables)
        if not existing._complete(item['plan']):
            raise StateError('pdf_manual_terminal_accounting_readback_required')
        if store.transport.read()!=store.payload:
            raise StateError('pdf_manual_terminal_medical_state_changed')

    @staticmethod
    def _medical_plan(source,item,parsed,existing):
        """A matching arbitrary row is not evidence for this Medical receipt."""
        from .utils import canonical_hash
        from .receipt_reimport import _date, _money
        from datetime import date
        plan=item['plan'];sid=source['source_id'];rid='R-'+sid;iid='receipt:'+sid
        if not isinstance(plan,list) or any(not isinstance(p,list) or len(p)!=2 for p in plan):raise ValueError()
        receipts=[r for t,r in plan if t=='レシート'];imports=[r for t,r in plan if t=='取込データ']
        expenses=[r for t,r in plan if t=='支出明細']
        if (len(receipts)!=1 or len(imports)!=1 or len(plan)!=2+len(expenses)
                or len(receipts[0])!=9 or len(imports[0])!=12):raise ValueError()
        r,i=receipts[0],imports[0]
        if (r[0]!=rid or i[0]!=iid or i[2:4]!=['receipt',sid]
                or r[1:5]!=[parsed.date,parsed.merchant,parsed.total,parsed.payment_method]
                or i[4:8]!=r[1:5] or r[6]!='解析済' or i[10]!=canonical_hash(parsed.model_dump())):
            raise ValueError()
        linked=item['inputs'][5]=='既存支出と重複（紐付け）'
        if linked:
            target=str(item['inputs'][6])
            rows=[x for x in existing.tables()['expense_rows'] if x and x[0]==target]
            if (expenses or not target or i[8:10]!=['matched_receipt',target] or len(rows)!=1
                    or len(rows[0])!=13 or rows[0][12]!='active'
                    or _money(rows[0][4])!=parsed.total or not _date(rows[0][1])
                    or abs((date.fromisoformat(_date(rows[0][1]))-date.fromisoformat(parsed.date)).days)>7):
                raise ValueError()
        else:
            if i[8:10]!=['解析済',''] or len(expenses)!=len(parsed.items):raise ValueError()
            for n,(x,p) in enumerate(zip(expenses,parsed.items),1):
                if (len(x)!=13 or x[:11]!=[f'{rid}-{n:02d}',parsed.date,parsed.merchant,p.name,
                        p.amount,p.major_category,p.minor_category,parsed.payment_method,'receipt',rid,iid]
                        or x[12]!='active'):raise ValueError()
