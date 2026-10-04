"""Explicit operator canary only. No writer, handoff, archive, or state save API.

Human page-kind is routing evidence. Restrictive automatic provenance remains
sticky, and grouping confirmation never grants external-AI/posting permission.
"""
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import re
import unicodedata

from PIL import Image
import pypdfium2 as pdfium

from .drive_run_state import StateError
from .pdf_general_grouping import scope_current
from .pdf_page_kind import current_answer
from .pdf_grouping_authority import validate
from .receipt_pdf_grouping import ConfirmedDocumentUnit
from .receipt_pdf_units import MAX_SOURCE_BYTES, MAX_PAGE_PIXELS, _digest, _render_png, _checked_gate
from .pdf_bounded_rendering import WorkBudget, render_scale, RenderHold
from .receipt_privacy_gate import ReceiptPrivacyBlocked
from .receipt_text_extraction import _extract_receipt_text
from .medical_receipt_privacy import classify_receipt_text
from .receipt_validation import apply_receipt_policy


def selected_unit(value, source_id, number, expected):
    """value must come from a validated durable store load, never a UI/cache."""
    if value.get('schema')=='pdf-grouping-authority-v2':
        from .pdf_grouping_authority_v2 import validate as validate_v2, units, page_identity
        validate_v2(value,expected['binding'])
        record=value['records'].get(_digest(source_id))
        if not record or record['status']!='grouping_confirmed':raise StateError('readonly_authority_missing')
        p=record['proposal'];a=record['confirmation']
        if (any(p.get(k)!=expected[k] for k in ('source_file_id','source_content_hash','page_count','proposal_digest'))
                or record['revision']!=expected['grouping_revision'] or a['confirmation_digest']!=expected['confirmation_digest']):
            raise StateError('readonly_authority_stale')
        candidates=[u for u in units(value) if u.source_file_id==source_id and u.page_numbers==(number,)]
        if type(number) is not int or len(candidates)!=1:raise StateError('readonly_single_page_unit_required')
        unit=candidates[0];page=p['pages'][number-1]
        if page['human_classification']!='normal':raise StateError('readonly_kind_not_normal')
        if (unit.unit_id!=expected['unit_ids'].get(number) or page['page_identity']!=page_identity(unit.source_content_hash,number,p['page_count'])
                or unit.member_page_identities!=(page['page_identity'],)):
            raise StateError('readonly_page_identity_changed')
        return unit,page,p
    validate(value,expected['binding'])
    record=value['records'].get(_digest(source_id))
    if not record or record['status']!='grouping_confirmed' or not record['confirmation']:
        raise StateError('readonly_authority_missing')
    p=record['proposal'];a=record['confirmation']
    if (any(p.get(k)!=expected[k] for k in ('source_file_id','source_content_hash','page_count','proposal_digest'))
            or record['revision']!=expected['grouping_revision']
            or a['confirmation_digest']!=expected['confirmation_digest'] or not scope_current(value,p)):
        raise StateError('readonly_authority_stale')
    groups=[g for g in p['groups'] if g['page_numbers']==[number]]
    if type(number) is not int or number not in p.get('grouping_page_numbers',[]) or len(groups)!=1:
        raise StateError('readonly_single_page_unit_required')
    page=p['pages'][number-1];answer=current_answer(value,page)
    if not answer or answer['human_classification']!='normal':
        raise StateError('readonly_kind_not_normal')
    g=groups[0]
    unit=ConfirmedDocumentUnit(source_id,p['source_content_hash'],(number,),tuple(g['member_page_hashes']),
                               tuple(g['page_classifications']),record['revision'],p['proposal_digest'])
    if expected['unit_ids'].get(number)!=unit.unit_id:
        raise StateError('readonly_unit_changed')
    return unit,page,p


def receipt_checks(result,categories,gate,readings,text):
    return apply_receipt_policy(result,categories,gate=gate,readings=readings,text=text)


class ReadonlyPdfReceipts:
    """Load-only authority and bytes-only source dependencies; sequential caller.

    analyze(payload,categories) reuses GeminiAI.analyze_receipt and returns its
    result and bounded readings for diagnostic stability checks. No durable
    Gemini flag is changed by this explicitly authorized read-only invocation.
    """
    def __init__(self,load_authority,load_source,analyze,categories,expected,*,model):
        self.load_authority=load_authority;self.load_source=load_source;self.analyze=analyze
        self.categories=categories;self.expected=expected;self.model=model;self.budget=WorkBudget()

    def run(self,number):
        report={'page_number':number,'model':self.model,'status':'analysis_failed','validation_issues':[],
                'accounting_allowed':False,'medical_handoff_allowed':False,'archive_allowed':False}
        payload=content=extracted=None
        stage='authority'
        try:
            unit,page,proposal=selected_unit(self.load_authority(),self.expected['source_file_id'],number,self.expected)
            stable='page_identity' in page
            report.update(unit_id=unit.unit_id,source_content_hash=unit.source_content_hash,
                          human_classification='normal',effective_classification=unit.classification)
            if stable:report.update(page_identity=page['page_identity'],observation_render_hash=page['observation_render_hash'])
            else:report['page_hash']=page['page_hash']
            content=self.load_source(unit.source_file_id)
            if not isinstance(content,bytes) or len(content)>MAX_SOURCE_BYTES or sha256(content).hexdigest()!=unit.source_content_hash:
                raise StateError('readonly_source_changed')
            stage='render'
            with closing(pdfium.PdfDocument(content)) as document:
                if len(document)!=proposal['page_count']:raise StateError('readonly_source_changed')
                if unit.classification!='normal':
                    report.update(status='privacy_blocked',reason='sticky_automatic_privacy',gemini_calls=0)
                    return report
                with closing(document[number-1]) as pdfpage:
                    scale=(page['observation_metadata'] if stable else page)['effective_render_scale']
                    maximum,pixels=render_scale(*pdfpage.get_size(),MAX_PAGE_PIXELS)
                    if stable:
                        scale,pixels=render_scale(*pdfpage.get_size(),MAX_PAGE_PIXELS,preferred=scale or maximum)
                    elif not scale or scale>maximum:raise StateError('readonly_render_identity_changed')
                    with self.budget.page(pixels):
                        payload=_render_png(pdfpage,scale,MAX_PAGE_PIXELS)
                        payload_hash=sha256(payload).hexdigest()
                        if not stable and payload_hash!=page['page_hash']:raise StateError('readonly_page_changed')
                        with Image.open(BytesIO(payload)) as image:
                            if image.format!='PNG' or image.mode!='RGB' or image.info or getattr(image,'n_frames',1)!=1:
                                raise StateError('readonly_payload_invalid')
                        report.update(payload_sha256=sha256(payload).hexdigest(),payload_mime='image/png',payload_pages=[number],
                                      effective_render_scale=scale)
                        stage='privacy'
                        extracted=_extract_receipt_text(payload,'image/png')
                        if extracted.status!='extracted' or not extracted.observation_complete:
                            report.update(status='privacy_blocked',reason='observation_incomplete',gemini_calls=0)
                            return report
                        first=classify_receipt_text('\n'.join([extracted.text or '',
                            ' '.join(t.text for t in extracted.structured_tokens)]))
                        known=(first.classification if first.reason_code in {
                            'medical_strong_signal','medical_multiple_signals','payroll_strong_signal',
                            'payroll_multiple_signals','conflicting_sensitive_evidence','sensitive_signal_insufficient'} else 'normal')
                        gate,complete=_checked_gate(payload,known)
                        report['privacy']={'classification':gate.classification,'gemini_allowed':gate.gemini_allowed,
                            'extraction_status':gate.extraction_status,'complete':complete,'reason':gate.reason_code}
                        if not complete or gate.classification!='normal' or not gate.gemini_allowed:
                            report.update(status='privacy_blocked',reason='exact_payload_gate',gemini_calls=0)
                            return report
                        # Recheck durable authority + whole-source identity just
                        # before Gemini (a concurrent hold/edit cannot authorize).
                        stage='authority'
                        latest,_,_=selected_unit(self.load_authority(),unit.source_file_id,number,self.expected)
                        if latest!=unit or sha256(self.load_source(unit.source_file_id)).hexdigest()!=unit.source_content_hash:
                            raise StateError('readonly_authority_stale')
                        stage='analysis'
                        if sha256(payload).hexdigest()!=payload_hash:raise StateError('readonly_payload_changed')
                        result,readings=(self.analyze(payload,self.categories,expected_payload_sha256=payload_hash)
                                        if stable else self.analyze(payload,self.categories))
                        issues,checks=receipt_checks(result,self.categories,gate,readings,extracted.text or '')
                        report.update(status='would_need_review' if issues else 'would_import',
                            date=result.date,merchant=result.merchant,total=result.total,item_count=len(result.items),
                            parsed=result.model_dump(),validation_issues=issues,checks=checks)
                        return report
        except ReceiptPrivacyBlocked:
            report.update(status='privacy_blocked',reason='adapter_exact_payload_gate',gemini_calls=0)
        except StateError as error:
            report.update(status='authority_held',reason=str(error))
        except RenderHold as error:
            report.update(status='privacy_blocked',reason=str(error),gemini_calls=0)
        except Exception as error:
            # Do not persist raw exception bodies, image data or OCR text.
            report.update(status=('privacy_blocked' if stage in {'render','privacy'} else
                                  'authority_held' if stage=='authority' else 'analysis_failed'),
                          reason={'render':'render_failed','privacy':'ocr_or_gate_failed',
                                  'authority':'readonly_authority_unavailable','analysis':'readonly_analysis_failed'}[stage])
            if stage=='analysis':
                from .gemini_errors import gemini_api_status, is_gemini_api_error
                status=gemini_api_status(error)
                if status is not None:
                    report['gemini_api_status']=status
                    report['analysis_failure_kind']='gemini_http_error'
                elif is_gemini_api_error(error):
                    report['analysis_failure_kind']='gemini_transport_or_response_error'
                elif isinstance(error,(TypeError,AttributeError)):
                    report['analysis_failure_kind']='adapter_contract_error'
                else:
                    report['analysis_failure_kind']='result_or_validation_error'
                from pydantic import ValidationError
                import traceback
                from pathlib import PurePath
                report['analysis_failure_class']=next((label for cls,label in (
                    (ValidationError,'schema_validation'),(TypeError,'type'),(AttributeError,'attribute'),
                    (ValueError,'value'),(RuntimeError,'runtime')) if isinstance(error,cls)),'other')
                # Only source-code filenames/line numbers, never traceback text,
                # exception messages, locals, absolute paths or document values.
                frames=traceback.extract_tb(error.__traceback__)
                report['analysis_failure_sites']=[{'file':PurePath(f.filename).name,'line':f.lineno}
                    for f in frames[-3:] if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py',PurePath(f.filename).name)]
        finally:
            payload=content=extracted=None
        return report
