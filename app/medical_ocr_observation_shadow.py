"""Private engine-neutral observations for ONE receipt image; never proposals.

Raw fields are transient and repr-hidden. Do not serialize these DTOs to logs.
An OCR text-region polygon is not a word/cell/amount bounding box.
"""
from dataclasses import dataclass, field
import hashlib
import math
import unicodedata

from .medical_payment_evidence import (
    NumericObservation, PaymentRegionEvidence, PaymentEvidence,
    _NUMERIC_RUN, _scope, _numeric_diagnostics,
)
from .medical_receipt_privacy import (
    _structured_amount, _compact_ocr_token, _payment_labels_on_line,
    resolve_medical_payment_candidates,
)

MAX_IMAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class ReceiptImage:
    unit_id: str = field(repr=False)
    page: int
    image_bytes: bytes = field(repr=False)

    def __post_init__(self):
        if (type(self.image_bytes) is not bytes or not 0 < len(self.image_bytes) <= MAX_IMAGE_BYTES
                or type(self.page) is not int or self.page < 1
                or type(self.unit_id) is not str or not 0 < len(self.unit_id) <= 128):
            raise ValueError('invalid receipt image envelope')


@dataclass(frozen=True)
class TextRegion:
    ordinal: int
    text: str = field(repr=False)
    polygon: tuple[tuple[float, float], ...] = field(repr=False)
    confidence: float | None
    detection_confidence: float | None
    issues: tuple[str, ...] = ()
    granularity: str = 'text_region'

    @property
    def bbox(self):
        if not self.polygon or 'invalid_geometry' in self.issues:
            return None
        xs, ys = zip(*self.polygon)
        return (min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys))


@dataclass(frozen=True)
class OcrObservation:
    unit_id: str = field(repr=False)
    page: int
    image_sha256: str = field(repr=False)
    engine: str
    model_sha256: tuple[str, ...] = field(repr=False)
    width: int
    height: int
    regions: tuple[TextRegion, ...] = field(repr=False)
    issues: tuple[str, ...] = ()

    @property
    def complete(self):
        return bool(self.regions) and not self.issues and not any(
            set(r.issues) & {'recognition_missing', 'invalid_geometry', 'invalid_confidence', 'blank_text'}
            for r in self.regions)


def failed_observation(image, reason='observation_incomplete'):
    return OcrObservation(image.unit_id, image.page, hashlib.sha256(image.image_bytes).hexdigest(),
                          'rapidocr-shadow', (), 0, 0, (), (reason,))


def make_observation(image, engine, hashes, width, height, raw_regions, issues=()):
    """Validate private IPC while retaining individually malformed observations."""
    if (type(width) is not int or type(height) is not int or min(width, height) <= 0
            or width*height > 20_000_000 or max(width, height) > 16384
            or type(raw_regions) is not list or len(raw_regions) > 4096):
        raise ValueError('invalid observation frame')
    regions=[]
    for ordinal, raw in enumerate(raw_regions):
        problems=[]
        text=raw.get('text')
        if text is None:
            text=''; problems.append('recognition_missing')
        if type(text) is not str or len(text)>100_000:
            raise ValueError('invalid observation text')
        if not text.strip(): problems.append('blank_text')
        confidence=raw.get('confidence')
        if (type(confidence) not in (float,int) or not math.isfinite(confidence)
                or not 0 <= confidence <= 1):
            confidence=None; problems.append('invalid_confidence')
        elif confidence < .7:
            # Descriptive uncalibrated band, never a confirmation threshold.
            problems.append('low_confidence')
        polygon=tuple(tuple(p) for p in raw.get('polygon', ()))
        valid=(len(polygon)==4 and all(len(p)==2 and all(type(c) in (int,float)
            and math.isfinite(c) for c in p) for p in polygon))
        if valid:
            xs,ys=zip(*polygon)
            valid=(min(xs)>=0 and min(ys)>=0 and max(xs)<=width and max(ys)<=height
                   and min(xs)<max(xs) and min(ys)<max(ys))
            crosses=[]
            for i in range(4):
                a,b,c=polygon[i],polygon[(i+1)%4],polygon[(i+2)%4]
                crosses.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
            valid=valid and (all(v>0 for v in crosses) or all(v<0 for v in crosses))
        if not valid: problems.append('invalid_geometry')
        detection=raw.get('detection_confidence')
        if type(detection) not in (float,int) or not math.isfinite(detection) or not 0<=detection<=1:
            detection=None
        regions.append(TextRegion(ordinal,text,polygon,confidence,detection,tuple(problems)))
    return OcrObservation(image.unit_id,image.page,hashlib.sha256(image.image_bytes).hexdigest(),
        engine,tuple(hashes),width,height,tuple(regions),tuple(issues))


@dataclass(frozen=True)
class ShadowProjection:
    source: OcrObservation = field(repr=False)
    evidence: PaymentEvidence = field(repr=False)
    # Character offsets refer to NFKC text in the original REGION, not sub-boxes.
    numeric_spans: tuple[tuple[tuple[int, int], ...], ...] = field(repr=False)

    def summary(self):
        regions=self.evidence.regions
        relevant=[r for r in regions if r.payment_relevant]
        diagnostics=set(self.source.issues) | {d for r in relevant for d in r.diagnostic_codes}
        if not self.source.complete: diagnostics.add('observation_incomplete')
        malformed=sum(any(_structured_amount(unicodedata.normalize('NFKC',region.text)[a:b].strip()) is None
            for a,b in spans) for region,spans in zip(self.source.regions,self.numeric_spans))
        return {'status':'needs_review','candidate_count':0,
            'observation_complete':self.source.complete,'regions':len(regions),
            'numeric_observations':sum(len(r.observations) for r in regions),
            'payment_regions':sum(r.scope=='payment_region' for r in regions),
            'possible_payment_regions':sum(r.scope=='possible_payment_region' for r in regions),
            'negative_regions':sum(r.scope=='excluded' for r in regions),
            'competing_payment_regions':sum(len(r.observations)>1 for r in relevant),
            'low_confidence_regions':sum('low_confidence' in r.issues for r in self.source.regions),
            'malformed_regions':malformed,
            'diagnostics':sorted(diagnostics)}


def project_observation(observation: OcrObservation) -> ShadowProjection:
    """One-way, no-candidate projection into existing safety evidence concepts.

    No synthetic Tesseract tokens, confidence rescaling or independent-vote claims.
    Source retains polygons, raw scores, granularity and page/unit provenance.
    """
    regions=[]; spans=[]
    for source in observation.regions:
        text=unicodedata.normalize('NFKC',source.text)
        labels=_payment_labels_on_line(text)
        scope=_scope(_compact_ocr_token(text), bool(labels))
        runs=tuple(_NUMERIC_RUN.finditer(text))
        spans.append(tuple((m.start(),m.end()) for m in runs))
        unresolved=source.confidence is None or source.confidence<.7
        malformed=any(_structured_amount(m.group().strip()) is None for m in runs)
        numbers=tuple(NumericObservation(
            'low_confidence_numeric' if unresolved else
            'malformed_numeric' if _structured_amount(m.group().strip()) is None else 'valid_numeric',
            ordinal, None) for ordinal,m in enumerate(runs))
        reasons=set(_numeric_diagnostics(numbers)) if scope in ('payment_region','possible_payment_region') else set()
        if malformed: reasons.add('ambiguous_numeric_observations')
        if set(source.issues)&{'invalid_geometry','invalid_confidence','recognition_missing','blank_text'}:
            reasons.add('observation_incomplete')
        if scope in ('payment_region','possible_payment_region'):
            reasons.add('structural_relationship_unresolved')
        regions.append(PaymentRegionEvidence('structured',source.ordinal,scope,
            page=observation.page, observations=numbers,candidates=(),diagnostic_codes=tuple(sorted(reasons))))
    evidence=PaymentEvidence(tuple(regions),bool(observation.regions),observation.complete,
                             resolve_medical_payment_candidates(()))
    return ShadowProjection(observation,evidence,tuple(spans))
