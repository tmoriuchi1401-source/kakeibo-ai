"""One image or PDF page per receipt unit. No production callers or amount output."""
from io import BytesIO

from . import medical_layout_local as local
from .medical_layout_shadow import PageFrame, observe_layout
from .medical_numeric_shadow import observe_numeric_fragments
from .medical_payment_evidence import collect_payment_evidence, resolve_payment_evidence
from .medical_receipt_privacy import (
    classify_receipt_text, _payment_labels_on_line, _structured_label_matches,
    _structured_line_key,
)


def _failed(ordinal,stage):
    return {'ordinal':ordinal,'unit_status':stage,'classification':'sensitive_unknown',
            'resolver_status':'needs_review','candidate_count':0,'diagnostics':('observation_incomplete',),
            'counters':{},'external_ai_allowed':False}


def summarize_unit_observation(text,tokens,frame):
    """Private pure observer: caller must pass exactly one receipt image's OCR.

    Page ordinals are validated before evidence collection. This function accepts
    no expected amount, cross-document pairing or pooled multi-pass evidence.
    """
    if type(text) is not str or type(tokens) is not tuple or frame.page!=1 or any(t.page!=1 for t in tokens):
        raise ValueError()
    complete=bool(text.strip()) and bool(tokens)
    layout=observe_layout(tokens,(frame,),expected_pages=1,observation_complete=complete)
    numeric=observe_numeric_fragments(tokens,(frame,),expected_pages=1,observation_complete=complete)
    complete=complete and 'observation_incomplete' not in layout.issues and 'observation_incomplete' not in numeric.issues
    evidence=collect_payment_evidence(text,tokens,observation_complete=complete)
    resolution=resolve_payment_evidence(evidence)
    classification=classify_receipt_text(text).classification if complete else 'sensitive_unknown'
    grouped={}
    for t in tokens:
        grouped.setdefault(_structured_line_key(t),[]).append(t)
    strong=sum(label[1]=='strong' for line in text.splitlines() for label in _payment_labels_on_line(line))
    strong+=sum(label.strength=='strong' for row in grouped.values()
                for label in _structured_label_matches(tuple(sorted(row,key=lambda t:t.x))))
    relevant=[r for r in evidence.regions if r.payment_relevant]
    counters={**numeric.aggregate(),
        'strong_label_views':strong,
        'payment_region_views':sum(r.scope=='payment_region' for r in evidence.regions),
        'possible_payment_views':sum(r.scope=='possible_payment_region' for r in evidence.regions),
        'excluded_views':sum(r.scope=='excluded' for r in evidence.regions),
        'negative_context_regions':sum(r.negative_context for r in layout.regions),
        'competing_payment_region_views':sum(len(r.observations)>1 for r in relevant),
        'low_confidence_payment_views':sum(any(o.state=='low_confidence_numeric' for o in r.observations) for r in relevant),
        'malformed_payment_views':sum(any(o.state=='malformed_numeric' for o in r.observations) for r in relevant),
        'unresolved_payment_views':sum(bool(r.diagnostic_codes) for r in relevant),
        'local_hypotheses':len(layout.hypotheses),
        'payment_proposal_count':sum(len(r.candidates) for r in relevant),
        'legacy_candidate_count':evidence.legacy_resolution.candidate_count}
    return {'unit_status':'observed' if complete else 'observation_incomplete',
            'classification':classification,
            'resolver_status':resolution.status if classification=='medical' else 'needs_review',
            'reason':resolution.reason_code if classification=='medical' else 'not_medical_or_incomplete',
            'candidate_count':resolution.candidate_count if classification=='medical' else 0,
            'diagnostics':resolution.diagnostic_codes,'counters':counters,'external_ai_allowed':False}


def _evaluate_image(image):
    # Same image path for PNG and rendered PDF pages. Source ordinal stays outside
    # OCR/evidence: every isolated unit has its own local page coordinate 1.
    text=local.extraction._run_image_ocr(image)
    tokens=local.extraction._run_image_ocr_tokens(image,page=1)
    return summarize_unit_observation(text,tokens,PageFrame(1,*image.size))


def evaluate_receipt_units(content: bytes,mime_type: str):
    """Opt-in local shadow only. No whole-PDF OCR, candidate merge or money sum.

    The input contract is one receipt per image/page; multi-receipt segmentation
    is not implemented. Failures remain explicit units and make complete false.
    Successful siblings are retained, never used to repair a failed unit.
    """
    units=[]
    result={'input_status':'rejected','unit_count':0,'complete':False,'units':(), 'external_ai_allowed':False}
    try:
        if type(content) is not bytes or not content or len(content)>local._MAX_BYTES or type(mime_type) is not str:
            return result
        mime=mime_type.strip().lower()
        if mime in {'image/png','image/jpeg','image/jpg'}:
            from PIL import Image
            with Image.open(BytesIO(content)) as image:
                expected='PNG' if mime=='image/png' else 'JPEG'
                if image.format!=expected or getattr(image,'n_frames',1)!=1 or image.getexif().get(274,1)!=1:
                    return result
                local._check_size(*image.size); image.load()
                try:
                    unit={'ordinal':1,**_evaluate_image(image)}
                except Exception:
                    unit=_failed(1,'ocr_failed')
                units.append(unit)
        elif mime=='application/pdf':
            from pypdf import PdfReader
            import pypdfium2 as pdfium
            # Validate without extracting or joining embedded text.
            with local.extraction._suppress_pypdf_output():
                reader=PdfReader(BytesIO(content))
                if reader.is_encrypted:
                    return result
                count=len(reader.pages)
            if not 1<=count<=local._MAX_PAGES:
                return result
            document=pdfium.PdfDocument(content)
            try:
                if len(document)!=count:
                    return result
                for index in range(count):
                    page=bitmap=image=None
                    unit=_failed(index+1,'render_failed')
                    try:
                        page=document[index]
                        bitmap=page.render(scale=local._bounded_render_scale(*page.get_size()))
                        image=bitmap.to_pil()
                        local._check_size(*image.size)
                        if max(image.size)>local._MAX_RENDER_EDGE:
                            raise ValueError()
                        try:
                            unit={'ordinal':index+1,**_evaluate_image(image)}
                        except Exception:
                            unit=_failed(index+1,'ocr_failed')
                    except Exception:
                        pass
                    finally:
                        local.extraction._close_if_possible(image)
                        local.extraction._close_if_possible(bitmap)
                        local.extraction._close_if_possible(page)
                    units.append(unit)
            finally:
                local.extraction._close_if_possible(document)
        else:
            return result
        return {'input_status':'accepted','unit_count':len(units),
                'complete':bool(units) and all(u['unit_status']=='observed' for u in units),
                'units':tuple(units),'external_ai_allowed':False}
    except Exception:
        # Initialization/validation failure cannot look like a complete subset.
        return result
