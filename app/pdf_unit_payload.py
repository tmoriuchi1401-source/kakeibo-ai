"""Fresh rendered members -> one bounded RGB PNG; no image/state persistence."""
from contextlib import closing, contextmanager
from hashlib import sha256
from io import BytesIO
import math

from PIL import Image
import pypdfium2 as pdfium

from .pdf_bounded_rendering import render_scale, RenderHold, MIN_SCALE
from .receipt_pdf_units import _render_png, _checked_gate, MAX_PAGE_PIXELS, MAX_SOURCE_BYTES
from .pdf_page_identity import page_identity


class PayloadHold(ValueError):
    def __init__(self, classification, reason):
        self.classification,self.reason=classification,reason
        super().__init__(reason)


def _normal(png):
    evidence={}
    gate,complete=_checked_gate(png,'normal',observation=evidence)
    if (not complete or gate.classification!='normal' or gate.gemini_allowed is not True
            or gate.extraction_status!='extracted' or gate.status!='ready_for_gemini'):
        kind=evidence.get('sensitive') or gate.classification
        if kind=='normal':kind='sensitive_unknown'
        raise PayloadHold(kind,'exact_payload_privacy_blocked')


@contextmanager
def rendered_unit(content, spec, observations, budget):
    """Keep the work/live budget held across downstream SDK/validation/writer.

    At most one member image is decoded at a time. A grouped Unit has one bounded
    canvas plus one member scratch. Original PDF metadata/attachments are absent
    from the freshly encoded RGB PNG; encoder hash is not page authority.
    """
    delivered=False
    try:
        ns=spec['page_numbers'];count=spec['page_count'];source_hash=spec['source_content_hash']
        if (not isinstance(content,bytes) or len(content)>MAX_SOURCE_BYTES
                or sha256(content).hexdigest()!=source_hash or len(observations.pages)!=count
                or observations.source_content_hash!=source_hash
                or spec['member_page_identities']!=[page_identity(source_hash,n,count) for n in ns]
                or any(observations.pages[n-1].classification!='normal' or
                    not observations.pages[n-1].observation_complete for n in ns)
                or set(spec['automatic_classifications']+spec['human_classifications'])!={'normal'}):
            raise PayloadHold('sensitive_unknown','payload_source_or_page_changed')
        geometry=[]
        with closing(pdfium.PdfDocument(content)) as doc:
            if len(doc)!=count:raise PayloadHold('sensitive_unknown','payload_page_structure_changed')
            for n in ns:
                with closing(doc[n-1]) as page:geometry.append(page.get_size())
        preferred=min(observations.pages[n-1].effective_render_scale for n in ns)
        scale,_=render_scale(max(w for w,h in geometry),sum(h for w,h in geometry),
            MAX_PAGE_PIXELS,preferred=preferred)
        sizes=[(math.ceil(w*scale),math.ceil(h*scale)) for w,h in geometry]
        # Include per-page rounding in the final canvas, not just the combined
        # point geometry. Downward quantization never speculatively over-renders.
        while max(w for w,h in sizes)*sum(h for w,h in sizes)>MAX_PAGE_PIXELS:
            scale=round(scale-0.000001,6)
            if scale<MIN_SCALE:raise RenderHold('page_too_large_at_minimum_scale')
            sizes=[(math.ceil(w*scale),math.ceil(h*scale)) for w,h in geometry]
        width,height=max(w for w,h in sizes),sum(h for w,h in sizes)
        pixels=width*height
        # Charge bounded worst-case local gate/planning/writer rechecks, including
        # member gates for a composite. No per-Unit budget resets inside a PDF.
        # Member/composite gates, analyzer gate + existing Gemini permission,
        # then the planning and real writer gates. Charge every local recheck.
        checks=5
        with budget.page(pixels,render_calls=len(ns),ocr_checks=checks):
            if len(ns)==1:
                with closing(pdfium.PdfDocument(content)) as doc,closing(doc[ns[0]-1]) as page:
                    payload=_render_png(page,scale,MAX_PAGE_PIXELS)
                _normal(payload)
            else:
                with Image.new('RGB',(width,height),'white') as canvas:
                    offset=0
                    for n,size in zip(ns,sizes):
                        # Fresh native document releases renderer caches after
                        # every member. No list of images/PNG bytes is accumulated.
                        with closing(pdfium.PdfDocument(content)) as doc,closing(doc[n-1]) as page:
                            member=_render_png(page,scale,MAX_PAGE_PIXELS)
                        _normal(member)
                        with Image.open(BytesIO(member)) as image:
                            if image.mode!='RGB' or image.info or image.size!=size:
                                raise PayloadHold('sensitive_unknown','render_output_invalid')
                            canvas.paste(image,(0,offset))
                        offset+=size[1];del member
                    canvas.info.clear();output=BytesIO();canvas.save(output,format='PNG')
                    payload=output.getvalue()
                _normal(payload)
            with Image.open(BytesIO(payload)) as image:
                if (image.format!='PNG' or image.mode!='RGB' or image.info or
                        getattr(image,'n_frames',1)!=1 or image.width*image.height>MAX_PAGE_PIXELS):
                    raise PayloadHold('sensitive_unknown','render_output_invalid')
            if len(payload)>MAX_SOURCE_BYTES:raise RenderHold('payload_byte_budget_exceeded')
            delivered=True
            yield payload,sha256(payload).hexdigest()
            del payload
    except (PayloadHold,RenderHold):raise
    except Exception:
        if delivered:raise  # SDK/accounting failures must keep their own recovery path.
        raise PayloadHold('sensitive_unknown','render_or_ocr_failed') from None
    finally:
        if 'payload' in locals():del payload
