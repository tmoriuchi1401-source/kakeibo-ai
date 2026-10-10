"""Sequential fresh-page evaluation. Only source/authority GETs and general AI.

No accounting, Medical, Drive mutation or durable-authority mutation dependency.
Read-only manifests are diagnostics; posting needs a separately bound durable
journal and the existing writer/read-back, never this diagnostic's statuses.
"""
from contextlib import closing,contextmanager
from hashlib import sha256
from io import BytesIO
import math
import pypdfium2 as pdfium
from PIL import Image,ImageOps
from .drive_run_state import StateError
from .page_receipt_model import build_receipt_units,ordered,segmentation_issues
from .receipt_plan.authority import eligible,validate_grant,binding_fields
from .receipt_pdf_units import MAX_SOURCE_BYTES,_render_png,MAX_PAGE_PIXELS
from .pdf_bounded_rendering import (WorkBudget,render_scale,RenderHold,STANDARD_PAGE_PIXELS,
    MAX_TOTAL_OCR_PIXEL_WORK,MAX_LIVE_PIXELS)
from .receipt_text_extraction import _extract_receipt_text
from .receipt_privacy_gate import ReceiptPrivacyBlocked,evaluate_receipt_privacy
from .page_receipt_ai import fresh_render_proof

class PageReceiptBudget(WorkBudget):
    @contextmanager
    def page(self,pixels):
        # Reserve additional repeated exact-page gates and per-receipt local
        # crops. Non-overlapping crop areas total at most one page. No crop AI.
        extra=32*pixels
        if self.ocr_pixel_work+extra+14*pixels>MAX_TOTAL_OCR_PIXEL_WORK:
            raise RenderHold('total_work_budget_exceeded')
        self.ocr_pixel_work+=extra
        with super().page(pixels):yield

def unit_observations(payload,reading):
    texts={};gates={}
    with Image.open(BytesIO(payload)) as image:
        for index,located in enumerate(ordered(reading),1):
            box=located.bbox
            region=(math.floor(box.left*image.width),math.floor(box.top*image.height),
                    math.ceil(box.right*image.width),math.ceil(box.bottom*image.height))
            with image.crop(region) as crop:
                output=BytesIO();crop.save(output,format='PNG');png=output.getvalue()
                extracted=_extract_receipt_text(png,'image/png')
                if extracted.status!='extracted' or not extracted.observation_complete:
                    raise ReceiptPrivacyBlocked()
                texts[index]=extracted.text or ''
                gates[index]=evaluate_receipt_privacy(png,'image/png')
                if gates[index].classification in {'medical','payroll'}:
                    raise ReceiptPrivacyBlocked()
            png=extracted=None
    return texts,gates

class ReadonlyPageReceipts:
    def __init__(self,current_page,load_source,load_grant,analyzer,categories,*,budget=None,completion_drafts=False):
        self.current_page,self.load_source,self.load_grant=current_page,load_source,load_grant
        self.analyzer,self.categories=analyzer,categories
        self.budget=budget or PageReceiptBudget()
        self.completion_drafts=completion_drafts

    def run(self,source_id,number,*,previous=None):
        report={'page_number':number,'status':'analysis_failed','units':[],
            'accounting_allowed':False,'medical_handoff_allowed':False,'archive_allowed':False}
        payload=raw=None;stage='authority'
        try:
            page=self.current_page(source_id,number)
            report.update(source_content_hash=page.source.source_content_hash,
                stable_page_identity=page.stable_page_identity,automatic_classification=page.automatic_classification)
            # Medical is excluded before source download, document open or render.
            if page.automatic_classification in {'medical','payroll'} or page.human_page_kind in {'medical','payroll'}:
                report['status']='medical_manual_pending' if 'medical' in {page.automatic_classification,page.human_page_kind} else 'payroll_held'
                return report
            if page.automatic_classification!='normal':
                if not eligible(page):report['status']='privacy_blocked';return report
                try:validate_grant(self.load_grant(page),page)
                except StateError:
                    report['status']='human_general_authority_required';return report
            raw=self.load_source(source_id)
            if type(raw) is not bytes or len(raw)>MAX_SOURCE_BYTES or sha256(raw).hexdigest()!=page.source.source_content_hash:
                raise StateError('page_source_changed')
            stage='render'
            if page.source.source_kind=='pdf':
                with closing(pdfium.PdfDocument(raw)) as document:
                    if len(document)!=page.source.page_count:raise StateError('page_structure_changed')
                    with closing(document[number-1]) as pdfpage:
                        scale,pixels=render_scale(*pdfpage.get_size(),STANDARD_PAGE_PIXELS)
                        with self.budget.page(pixels):
                            payload=_render_png(pdfpage,scale,STANDARD_PAGE_PIXELS)
                            report['effective_render_scale']=scale
                            proof=fresh_render_proof(page,raw,number,len(document),payload)
                            return self._analyze(page,payload,report,previous,proof)
            with Image.open(BytesIO(raw)) as original:
                if getattr(original,'n_frames',1)!=1 or original.width*original.height>min(MAX_PAGE_PIXELS,MAX_LIVE_PIXELS//8):
                    raise RenderHold('image_resource_limit_exceeded')
                with self.budget.page(original.width*original.height):
                    with ImageOps.exif_transpose(original) as oriented, oriented.convert('RGB') as rgb:
                        clean=Image.new('RGB',rgb.size);clean.paste(rgb)
                        try:
                            output=BytesIO();clean.save(output,format='PNG');payload=output.getvalue()
                        finally:clean.close()
                    proof=fresh_render_proof(page,raw,1,1,payload)
                    return self._analyze(page,payload,report,previous,proof)
        except ReceiptPrivacyBlocked:report.update(status='privacy_blocked',reason='exact_payload_or_crop_gate')
        except StateError:report.update(status='authority_held',reason='source_or_authority_not_current')
        except RenderHold as error:report.update(status='privacy_blocked',reason=str(error))
        except Exception:report.update(status='analysis_failed',reason='render_failed' if stage=='render' else 'response_or_validation_failed')
        finally:payload=raw=None
        return report

    def _analyze(self,page,payload,report,previous,render_proof):
        fingerprint=sha256(payload).hexdigest()
        before=self.analyzer.calls
        try:
            readings,proof=self.analyzer.analyze(page,payload,self.categories,
                expected_payload_sha256=fingerprint,render_proof=render_proof)
            if sha256(payload).hexdigest()!=fingerprint:raise StateError('page_payload_changed')
            report.update(payload_sha256=fingerprint,payload_mime='image/png',payload_pages=[page.page_number],privacy=proof)
            report['reading_diagnostic']=[[{'receipt_index':i,'receipt_count':len(reading.receipts),
                'bbox':located.bbox.model_dump(),'item_boxes':[b.model_dump() for b in located.item_boxes],
                'date':located.receipt.date,'total':located.receipt.total,
                'item_count':len(located.receipt.items),'item_sum':sum(x.amount for x in located.receipt.items),
                'transaction_kind':located.receipt.transaction_kind}
                for i,located in enumerate(ordered(reading),1)] for reading in readings]
            if segmentation_issues(*readings):
                report.update(build_receipt_units(page,*readings,self.categories,previous=previous));return report
            texts,gates=unit_observations(payload,readings[1])
            result=build_receipt_units(page,*readings,self.categories,previous=previous,unit_texts=texts,unit_gates=gates)
            report.update(result)
            if result['status']=='receipts_observed':
                report['status']='would_need_review' if any(u['validation_issues'] for u in result['units']) else 'would_import'
            # Recheck protected authority after analysis too, before retaining a
            # reusable manifest. A hold/edit while the model ran invalidates it.
            latest=self.current_page(page.source.source_file_id,page.page_number)
            if (binding_fields(latest)!=binding_fields(page) or latest.human_page_kind!=page.human_page_kind
                    or sha256(self.load_source(page.source.source_file_id)).hexdigest()!=page.source.source_content_hash):
                raise StateError('page_authority_stale')
            if proof['basis']=='human_general_receipt':validate_grant(self.load_grant(page),page)
            if self.completion_drafts:
                from .general_receipt_completion import drafts_for_page
                manifest={k:report[k] for k in ('page_key','segmentation_digest')}
                manifest.update(source=page.source.model_dump(),page_number=page.page_number,
                    stable_page_identity=page.stable_page_identity,
                    units=[{k:u[k] for k in ('receipt_unit_id','receipt_index','parent_page_identity','bbox','accounting_status')}
                           for u in report['units']])
                report['manifest']=manifest
                report['completion_drafts']=drafts_for_page(page,manifest,*readings,categories=self.categories,
                    unit_texts=texts,unit_gates=gates)
                from .receipt_item_review import prepare
                from .receipt_item_confirmation import annotated
                report['item_records']=[]
                for draft,one,two in zip(report['completion_drafts'],ordered(readings[0]),ordered(readings[1])):
                    record=prepare(draft,self.categories)
                    try:record=annotated(record,[one.receipt,two.receipt],self.categories)
                    except StateError:pass # Count/total instability remains a hard review gate.
                    report['item_records'].append(record)
            return report
        except StateError:raise
        except ReceiptPrivacyBlocked:raise
        except Exception:
            report.update(status='analysis_failed',reason='response_or_validation_failed',units=[])
            return report
        finally:
            report['gemini_calls']=self.analyzer.calls-before
            report['analysis_diagnostic']=self.analyzer.last_diagnostic
