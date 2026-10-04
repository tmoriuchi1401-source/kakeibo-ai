"""Opt-in confirmed general PDF Unit intake; no runtime activation or mover.

Observe every page first. UI/local-cache values never authorize a Unit. A Drive
completion intent fences the existing receipt writer and all ambiguous replay.
The caller must already hold the existing production mutex; this module does
not create schedules, acquire Secrets, provision state, or call Medical.
"""
from hashlib import sha256
from dataclasses import replace

from .drive_run_state import StateError
from .pdf_bounded_rendering import WorkBudget,RenderHold
from .pdf_production_authority import DrivePdfAuthority
from .pdf_unit_analysis import PdfUnitAnalyzer
from .pdf_unit_processing import DriveUnitProcessingStore
from .pdf_unit_payload import PayloadHold,rendered_unit
from .pdf_receipt_materialization import materialize,reconcile,require_new_identity
from .receipt_pdf_units import observe_pdf
from .receipt_privacy_gate import ReceiptPrivacyBlocked


class PdfUnitIntake:
    def __init__(self,authority,completion,db,analyzer):
        if (not isinstance(authority,DrivePdfAuthority) or
                not isinstance(completion,DriveUnitProcessingStore) or
                not isinstance(analyzer,PdfUnitAnalyzer)):
            raise StateError('pdf_intake_durable_dependencies_required')
        self.authority,self.completion,self.db,self.analyzer=authority,completion,db,analyzer

    def process(self,content,source_id,*,known_source_classification=None):
        report={'document_type':'pdf_page_units','status':'grouping_required',
            'units':[],'all_units_terminal':False,'archive_allowed':False}
        current=self.authority.current(source_id)
        if sha256(content).hexdigest()!=current.source_content_hash:
            raise StateError('pdf_production_source_changed')
        budget=WorkBudget()
        restrictions={n:k for n,k in enumerate(current.automatic_classifications,1) if k!='normal'}
        restrictions.update(self.completion.restrictions(source_id,current.source_content_hash))
        observed=observe_pdf(content,source_id,known_source_classification=known_source_classification,
            known_page_classifications=restrictions,work_budget=budget)
        if len(observed.pages)!=current.page_count:
            raise StateError('pdf_production_page_structure_changed')
        # Even the single-page observation PNG is discarded before fresh render.
        observed=replace(observed,pages=tuple(replace(p,_payload=None) for p in observed.pages))
        # Complete local observation before resolving any AI or writer.
        medical_numbers={n for s in current.medical_pages for n in s['page_numbers']}
        def verify(spec):
            budget.checkpoint()
            result=self.authority.verify(spec)
            budget.checkpoint()
            return result
        halted=False
        for spec in current.units:
            numbers=spec['page_numbers'];record={'page_numbers':numbers,'unit_id':spec['unit_id'],
                'status':'privacy_pending'}
            if halted:
                record['status']='authority_held';report['units'].append(record);continue
            if set(numbers)&medical_numbers or 'medical' in spec['human_classifications']:
                record['status']='medical_pending';report['units'].append(record);continue
            if set(spec['automatic_classifications']+spec['human_classifications'])!={'normal'}:
                report['units'].append(record);continue
            failed=[p for p in observed.pages if p.page_number in numbers and
                (p.classification!='normal' or not p.observation_complete)]
            if failed:
                for p in failed:self.completion.block(spec,p.classification if p.classification!='normal'
                    else 'sensitive_unknown','local_observation_incomplete',verify_current=self.authority.verify)
                report['units'].append(record);continue
            try:
                replay=reconcile(self.db,self.completion,spec,verify_current=verify)
                if replay:record.update(replay)
                else:
                    require_new_identity(self.db,spec)
                    with rendered_unit(content,spec,observed,budget) as (png,payload_hash):
                        result=self.analyzer.analyze(png,payload_hash,self.db.categories(),spec,
                            verify_current=verify)
                        record.update(materialize(self.db,self.completion,spec,png,payload_hash,result,
                            verify_current=verify))
                        del result
            except (PayloadHold,ReceiptPrivacyBlocked,RenderHold) as exc:
                classification=getattr(exc,'classification','sensitive_unknown')
                reason=getattr(exc,'reason','exact_payload_privacy_blocked')
                if isinstance(exc,RenderHold):reason='total_work_budget_exceeded'
                self.completion.block(spec,classification,reason,verify_current=self.authority.verify)
            except StateError as exc:
                # Fixed reason codes only. No raw SDK/OCR/accounting exceptions.
                code=str(exc)
                known={'pdf_receipt_validation_failed','pdf_receipt_duplicate_candidate',
                    'pdf_receipt_unstable_reread','pdf_receipt_existing_identity_without_intent'}
                record['status']='needs_review' if code in known else 'authority_held'
                record['reason_code']=code if code in known else 'pdf_unit_reconciliation_required'
                if code not in known:halted=True
            except Exception:
                record['status']='analysis_or_write_held';record['reason_code']='pdf_unit_operation_failed'
                halted=True
            report['units'].append(record)
        covered={n for s in current.units for n in s['page_numbers']}
        for p in observed.pages:
            if p.page_number not in covered:
                report['units'].append({'page_numbers':[p.page_number],
                    'status':'medical_pending' if p.page_number in medical_numbers else 'grouping_required'})
        report['status']='partial_completion' if any(r['status']=='imported' for r in report['units']) else 'grouping_required'
        # Medical/manual/skip terminal references are reconciled by the separate
        # parent planner. This intake cannot grant archive authority itself.
        report['all_units_terminal']=len(covered)==current.page_count and all(
            r['status']=='imported' for r in report['units'])
        if report['all_units_terminal']:report['status']='all_units_terminal'
        report['work']=budget.metadata()
        return report
