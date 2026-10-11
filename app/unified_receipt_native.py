"""Thin connections to native validators/writers; no live client construction.

The host's inspection_reader must read original bytes/version, exact Unit/page
registry, native authenticated grants and both route declarations afresh. It
returns NativeInspection, never a Sheet/browser/audit-manifest permission.
This module cannot construct that authority, migrate legacy input or move files.
"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass

from .drive_run_state import StateError
from .unified_receipt_review import digest
from .unified_receipt_eligibility import current_snapshot, identity_scope
from .unified_receipt_write import NativeInspection

_executing = ContextVar('unified_native_receipt_target',default=None)


def confirmation_operation_identity(item):
    source=item['source']
    # Both entry points use the existing receipt ledger namespace. The review
    # purpose/request ID cannot create another financial operation for it.
    return digest(['receipt-confirmation-operation-v1','R-'+source['source_id'],
                   source['sha256'],source['version']])


def pdf_operation_identity(page, unit_id):
    from .pdf_intake_authority import page_key
    return digest(['pdf-intake-receipt-operation-v1',page_key(page),unit_id])


class NativePort:
    def __init__(self, inspection_reader):
        if not callable(inspection_reader):raise StateError('unified_native_reader_required')
        self.reader=inspection_reader

    def inspect(self, case, action):
        result=self.reader(case.case_id,action)
        if not isinstance(result,NativeInspection) or result.case!=case:
            raise StateError('unified_native_original_or_snapshot_changed')
        return deepcopy(result)

    def recovery_binding(self, request):
        result=self.reader(request.case_id,request.requested_action)
        if not isinstance(result,NativeInspection):raise StateError('unified_native_reader_required')
        c=result.case;s=identity_scope(c)
        fixed={k:s[k] for k in ('source_file_id','source_content_hash','source_version','stable_page_identity',
                               'receipt_unit_id','receipt_index','review_purpose')}
        fixed.update(case_id=c.case_id,current_snapshot_digest=digest(current_snapshot(c)),
                     authority_revision=result.authority_revision,operation_identity=result.operation_identity)
        if any(getattr(request,k)!=v for k,v in fixed.items()):
            raise StateError('unified_native_recovery_scope_changed')
        return result

class ReceiptConfirmationPort(NativePort):
    """General image, manual medical, or the native existing-expense link.

    Only a mapped current review can be used. Native inputs already accepted by
    the existing UI/validator must match; no copying values between cases. Host
    must install this same gate in every active legacy service instance before
    exposing the unified route. Without that wiring inspection denies writes.
    """
    writer_name='receipt_confirmation'

    def __init__(self, service, review_id, inspection_reader):
        super().__init__(inspection_reader)
        self.service,self.review_id=service,review_id
        self.bridge=self.case_reader=self.payload_reader=None

    def bind_shared_gate(self, bridge, case_reader, payload_reader):
        self.bridge,self.case_reader,self.payload_reader=bridge,case_reader,payload_reader
        self.service.request_gate=self

    def in_native_execution(self, service):
        return _executing.get()==(id(service),self.review_id)

    def submit_legacy(self, service, key, *, dry_run=False):
        if service is not self.service or key!=self.review_id or self.bridge is None:
            raise StateError('unified_legacy_gate_target_missing')
        case=self.case_reader();payload=self.payload_reader()
        if dry_run:
            return int(bool(self.bridge.prepare(case,payload['requested_action'],payload['request_id'])['operation_targets']))
        result=self.bridge.submit(case,payload,entry='legacy')
        return int(result['state']=='complete' and not result.get('replay'))

    def _current(self, case, action):
        item=self.service.items.get(self.review_id)
        if (not item or item['kind'] not in {'normal','medical'} or case.identity.get('review_id')!=self.review_id
                or item.get('decision_origin')=='automatic' or item.get('require_reconfirm')):
            raise StateError('unified_confirmation_mapping_required')
        src=item['source'];i=case.identity
        if (src['source_id']!=i['source_file_id'] or src['sha256']!=i['source_content_hash']
                or src['version']!=i['source_version']):raise StateError('unified_confirmation_source_changed')
        metadata=self.service.verify_source(src,item['folder_id'])
        if not isinstance(metadata,dict) or metadata.get('version')!=src['version']:
            raise StateError('unified_confirmation_exact_version_required')
        live=self.service.ui_rows().get(self.review_id)
        if not live or live[1][7:15]!=item['inputs']:
            raise StateError('unified_confirmation_unsent_input_changed')
        markers=('date','merchant','amount','category','payment','action','duplicate_target','memo')
        if [case.value(k) for k in markers]!=item['inputs']:
            raise StateError('unified_confirmation_snapshot_changed')
        native_action=item['inputs'][5]
        if action=='link_existing':
            if native_action!='既存支出と重複（紐付け）' or not item['inputs'][6]:
                raise StateError('unified_confirmation_link_required')
        elif native_action not in ({'候補明細で確定','重複候補と別の支出として確定'} if item['kind']=='normal'
                                  else {'医療費を確定','重複候補と別の支出として確定'}):
            raise StateError('unified_confirmation_action_invalid')
        return item

    def inspect(self, case, action):
        result=super().inspect(case,action)
        item=self._current(case,action)
        if item['status']!='waiting' or self.service.request_gate is not self or self.bridge is None:
            raise StateError('unified_confirmation_shared_gate_required')
        from .receipt_pipeline import validate_receipt_result
        parsed=self.service._parsed(item)
        if validate_receipt_result(parsed,self.service.db.categories())[0] is not True:
            raise StateError('unified_confirmation_native_validation_held')
        self.service._plan(item,parsed,item['inputs'][6] if action=='link_existing' else '',
                           item['inputs'][5]=='重複候補と別の支出として確定')
        if result.operation_identity!=confirmation_operation_identity(item):
            raise StateError('unified_confirmation_operation_identity_changed')
        return result

    def validate_draft(self, case, draft):
        item=deepcopy(self._current(case,'link_existing' if case.value('duplicate_target') else 'post'))
        if draft.identity!=case.identity or draft.legacy_id!=case.legacy_id:
            raise StateError('unified_draft_identity_changed')
        markers=('date','merchant','amount','category','payment','action','duplicate_target','memo')
        if {f[0] for f in draft.fields}!=set(markers):raise StateError('unified_confirmation_draft_fields_invalid')
        item['inputs']=[draft.value(k) for k in markers]
        from .receipt_pipeline import validate_receipt_result
        parsed=self.service._parsed(item)
        if not validate_receipt_result(parsed,self.service.db.categories())[0]:
            raise StateError('unified_confirmation_native_validation_held')
        plan=self.service._plan(item,parsed,item['inputs'][6],item['inputs'][5]=='重複候補と別の支出として確定')
        return dict(status='validated_not_authorized',plan_digest=digest(plan),accounting_writes=0)

    def execute(self, request):
        observed=self.recovery_binding(request)
        self._current(observed.case,request.requested_action)
        token=_executing.set((id(self.service),self.review_id))
        try:
            written=self.service.apply_confirmations(keys=[self.review_id])
        finally:_executing.reset(token)
        if written!=1 or self.service.items[self.review_id]['status']!='applied':
            raise StateError('unified_confirmation_native_held')
        return dict(accounting_writes=written)

    def readback(self, request):
        observed=self.recovery_binding(request)
        item=self._current(observed.case,request.requested_action)
        if item['status'] not in {'pending','applied'} or not item.get('plan'):
            return False
        from .receipt_reimport_production import digest as native_digest
        if item.get('confirmation_hash')!=native_digest(item['inputs']):return False
        # Existing _complete checks every ID/row, including the linked import
        # instead of inventing a new expense. No apply/append on recovery.
        return self.service._complete(item['plan']) is True


class PDFManualUnitPort(NativePort):
    """Registered PDF Unit snapshot -> native authenticated manual posting.

    Native PageIntake validates the owner proof, independent posting authority,
    exact Unit snapshot, privacy, duplicates, CAS intent and scoped materializer.
    Auto posting is never substituted for the unified explicit action.
    """
    writer_name='pdf_intake'

    def __init__(self, runner, page_key, unit_id, snapshot_reader, inspection_reader):
        super().__init__(inspection_reader)
        self.runner,self.key,self.uid,self.snapshot_reader=runner,page_key,unit_id,snapshot_reader

    def _unit(self, case):
        record=self.runner.store.load()['pages'][self.key];page=record['page'];unit=record['units'][self.uid]
        i=case.identity
        if (i['receipt_unit_id']!=self.uid or page['source']['source_file_id']!=i['source_file_id']
                or page['source']['source_content_hash']!=i['source_content_hash']
                or page['stable_page_identity']!=i['stable_page_identity']
                or unit['candidate']['record']['legacy']['identity']['receipt_index']!=i['receipt_index']):
            raise StateError('unified_pdf_unit_binding_changed')
        return page,unit

    def inspect(self, case, action):
        result=super().inspect(case,action);page,unit=self._unit(case)
        if action!='post' or unit['intent'] is not None or unit['posting_authority'] is None:
            raise StateError('unified_pdf_posting_authority_required')
        snapshot=self.snapshot_reader()
        if unit['candidate'].get('posting',{}).get('snapshot')!=snapshot:
            raise StateError('unified_pdf_native_snapshot_stale')
        checked=self.runner.post_manual(self.key,self.uid,snapshot,apply=False)
        if checked.get('status')!='ready_to_write' or checked.get('write')!=0:
            raise StateError('unified_pdf_native_validation_held')
        if result.operation_identity!=pdf_operation_identity(page,self.uid):
            raise StateError('unified_pdf_operation_identity_changed')
        return result

    def execute(self, request):
        observed=self.recovery_binding(request);self._unit(observed.case)
        result=self.runner.post_manual(self.key,self.uid,self.snapshot_reader(),apply=True)
        if result.get('status')!='imported' or result.get('write_outcome')=='unknown':
            raise StateError('unified_pdf_readback_required')
        return result

    def readback(self, request):
        from .pdf_intake_runner import exact_readback
        observed=self.recovery_binding(request);_,unit=self._unit(observed.case)
        intent=unit['intent']
        if not intent or intent['posting_binding'] is None:return False
        if intent['posting_binding']['snapshot_digest']!=digest(self.snapshot_reader()):
            raise StateError('unified_pdf_recovery_snapshot_changed')
        # Unlike recover(apply=True), this cannot update any authority or retry.
        before=None if intent['status']=='complete' else intent['before_ledger_digest']
        return exact_readback(self.runner.db,intent['plan'],before)=='complete'


@dataclass(frozen=True)
class GroupedPlan:
    """Trusted existing grouped validator output, never browser JSON."""
    spec: dict
    input_digest: str
    writer_reference: str
    parsed: dict
    ledger_source_id: str
    original_url: str
    prepared_at: str
    categories: list
    expected: dict
    before_ledger_digest: str


class GroupedUnitPort(NativePort):
    """Confirmed multi-page Unit -> existing reserve + scoped materializer.

    The existing grouping/privacy/owner validator supplies GroupedPlan afresh.
    The existing DriveUnitProcessingStore is injected from its pinned host.
    No source analysis, new writer, medical candidate or archive call exists.
    """
    writer_name='pdf_intake'

    def __init__(self, processing_store, db, plan_reader, verify_current, inspection_reader):
        super().__init__(inspection_reader)
        self.store,self.db,self.plan_reader,self.verify_current=processing_store,db,plan_reader,verify_current

    def _plan(self, case):
        plan=self.plan_reader(case.case_id)
        if not isinstance(plan,GroupedPlan):raise StateError('unified_grouped_native_plan_required')
        s=plan.spec;i=case.identity
        if (case.kind=='medical' or s['unit_id']!=i['receipt_unit_id']
                or s['source_file_id']!=i['source_file_id'] or s['source_content_hash']!=i['source_content_hash']
                or s['page_numbers']!=case.pages or s['member_page_identities']!=i.get('page_identities')
                or self.verify_current(deepcopy(s)) is not True):
            raise StateError('unified_grouped_binding_changed')
        # Also recompute the exact native materializer's plan before claiming.
        from .receipt_plan.planning import PlanningDB
        from .receipt_pipeline import ReceiptPipeline
        from .models import ReceiptResult
        preview=PlanningDB(plan.categories)
        ReceiptPipeline(preview,None,clock=lambda:plan.prepared_at)._materialize_result(
            ReceiptResult.model_validate(plan.parsed),plan.ledger_source_id,plan.original_url,[])
        operations=[dict(table=t,rows=[r for title,r in preview.plan if title==t])
                    for t in ('レシート','支出明細','取込データ')]
        if operations!=plan.expected['operations'] or digest(operations)!=plan.expected['digest']:
            raise StateError('unified_grouped_materializer_plan_changed')
        return plan

    def inspect(self, case, action):
        result=super().inspect(case,action);plan=self._plan(case)
        if action!='post' or plan.spec['unit_id'] in self.store.load()['records']:
            raise StateError('unified_grouped_already_claimed')
        # Canonical identity of the native Unit processing reservation.
        target=digest(['pdf-unit-processing',plan.spec['unit_id']])
        if result.operation_identity!=target:raise StateError('unified_grouped_operation_identity_changed')
        from .pdf_intake_runner import ledger_snapshot,exact_readback
        if digest(ledger_snapshot(self.db))!=plan.before_ledger_digest or exact_readback(self.db,plan.expected)!='not_written':
            raise StateError('unified_grouped_ledger_changed')
        return result

    def execute(self, request):
        from .pdf_intake_runner import ScopedWriter,exact_readback
        from .receipt_pipeline import ReceiptPipeline
        from .models import ReceiptResult
        observed=self.recovery_binding(request);plan=self._plan(observed.case)
        rows={o['table']:deepcopy(o['rows']) for o in plan.expected['operations']}
        intent,created=self.store.reserve(plan.spec,'receipt',plan.input_digest,rows,plan.writer_reference,
                                          verify_current=self.verify_current)
        if not created:raise StateError('unified_grouped_readback_only')
        self.store.verify_pending(plan.spec['unit_id'],intent['intent_digest'])
        fresh=self._plan(observed.case)
        if fresh!=plan:raise StateError('unified_grouped_changed_after_claim')
        writer=ScopedWriter(self.db,plan.categories,plan.expected,plan.before_ledger_digest)
        ReceiptPipeline(writer,None,clock=lambda:plan.prepared_at)._materialize_result(
            ReceiptResult.model_validate(plan.parsed),plan.ledger_source_id,plan.original_url,[])
        if exact_readback(self.db,plan.expected,plan.before_ledger_digest)!='complete':
            raise StateError('unified_grouped_exact_readback_required')
        self.store.complete(plan.spec['unit_id'],intent['intent_digest'],verify_current=self.verify_current,
            verify_readback=lambda record:exact_readback(self.db,plan.expected,plan.before_ledger_digest)=='complete')
        return dict(status='imported')

    def readback(self, request):
        from .pdf_intake_runner import exact_readback
        observed=self.recovery_binding(request);plan=self._plan(observed.case)
        record=self.store.load()['records'].get(plan.spec['unit_id'])
        if not record or record['unit']!=plan.spec:return False
        rows={o['table']:o['rows'] for o in plan.expected['operations']}
        if record['planned_rows']!=rows or record['input_digest']!=plan.input_digest or record['writer_reference']!=plan.writer_reference:
            raise StateError('unified_grouped_recovery_intent_changed')
        return exact_readback(self.db,plan.expected,plan.before_ledger_digest)=='complete'


class ExplicitUnitPostingPort(NativePort):
    """Existing independently approved Unit writer, including its native CAS.

    Retains its current p4/p10 scope; never broadens that canary authority to a
    different page/source. New registered PDFs use PDFManualUnitPort instead.
    """
    writer_name='receipt_unit_posting'

    def validate_request(self, request):
        if request.request_id!=self.rid:raise StateError('unified_native_request_id_changed')

    def __init__(self, readers, store, writer, projection, request_id, context_id,
                 attempt_id, inspection_reader, *, clock):
        super().__init__(inspection_reader)
        self.readers,self.store,self.writer,self.projection=readers,store,writer,projection
        self.rid,self.context,self.attempt,self.clock=request_id,context_id,attempt_id,clock

    def _plan(self, case):
        from .receipt_plan import unit_posting as native
        plan=native.fresh_plan(self.readers,self.rid);value,_=self.store.read();native.validate_state(value)
        native.bound(value['grant'],plan,self.rid,self.readers.owner_actor_id,self.context)
        native.plan_receipt(self.readers,plan,self.rid,self.context)
        ident=plan['snapshot']['identity']
        for k in ('source_file_id','source_content_hash','stable_page_identity','receipt_unit_id','receipt_index'):
            if case.identity[k]!=ident[k]:raise StateError('unified_explicit_unit_scope_changed')
        return native,plan,value

    def inspect(self, case, action):
        result=super().inspect(case,action);native,plan,value=self._plan(case)
        if action!='post' or value['state']!='unused' or not value['grant']['approved_at']<=self.clock()<value['grant']['expires_at']:
            raise StateError('unified_explicit_unit_authority_held')
        target=digest(['receipt-unit-posting',self.context,self.rid])  # native permanent event ID
        if result.operation_identity!=target:raise StateError('unified_explicit_unit_identity_changed')
        return result

    def execute(self, request):
        from .receipt_plan.unit_posting import execute
        self.recovery_binding(request)
        if request.request_id!=self.rid:raise StateError('unified_native_request_id_changed')
        return execute(self.readers,self.store,self.writer,self.projection,self.rid,self.context,
                       attempt_id=self.attempt,now=self.clock(),operation='apply')

    def readback(self, request):
        from .receipt_plan.posting import outcome
        observed=self.recovery_binding(request);_,plan,value=self._plan(observed.case)
        status,_=outcome(self.readers,plan,value['grant'])
        return status=='written'


class ExistingAuthorityPort(NativePort):
    """Metadata-only native service in the existing authenticated host.

    Services are injected from the pinned/deployed authority container; missing
    services are unavailable, never replaced with local bools or new grants.
    Native state/CAS and native readback stay inside that existing service.
    """
    def __init__(self, action, service, native_request_reader, native_readback, inspection_reader):
        super().__init__(inspection_reader)
        if action not in {'link_existing','confirm_grouping','confirm_page_kind','confirm_privacy'}:
            raise StateError('unified_authority_route_invalid')
        self.action,self.service,self.request_reader,self.verify=action,service,native_request_reader,native_readback

    def execute(self, request):
        self.recovery_binding(request)
        native=self.request_reader(request.request_id)  # trusted server record
        if self.action=='link_existing':
            return self.service.confirm(request.request_id,selected_decision=native['selected_decision'],
                                        method='POST',explicit_post=True)
        if self.action=='confirm_grouping':return self.service.review(native['request'],intent_digest=native['intent_digest'])
        if self.action=='confirm_page_kind':
            return self.service.confirm(native['expected'],native['selections'],request_id=request.request_id,
                                        intent_digest=native['intent_digest'])
        # Existing authenticated page gate: kind alone does not authorize AI.
        return self.service.confirm(request.request_id,**native['confirmation_arguments'])

    def readback(self, request):
        self.recovery_binding(request)
        return self.verify(request) is True
