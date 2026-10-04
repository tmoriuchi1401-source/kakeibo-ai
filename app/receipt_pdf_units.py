"""Local PDF observations and v1 single-page receipt units.

Only fresh RGB pixels can become a payload. Original PDF objects, metadata,
attachments and extracted text never enter that payload or the local manifest.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
import math
import os
import re
from pathlib import Path
import tempfile
from typing import Protocol

from .medical_receipt_privacy import classify_receipt_text
from .receipt_privacy_gate import ReceiptPrivacyGateResult, evaluate_receipt_privacy
from .receipt_text_extraction import _extract_receipt_text, _suppress_pypdf_output
from . import pdf_bounded_rendering as bounded

MAX_PDF_PAGES = 50
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_PAGE_PIXELS = 12_000_000
MAX_DOCUMENT_PIXELS = 100_000_000
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
RENDER_SCALE = 3
TERMINAL_STATES = frozenset({'imported', 'confirmed', 'intentionally_skipped'})


def _digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def is_pdf(content: bytes, mime_type: str) -> bool:
    return mime_type.strip().lower() == 'application/pdf' or b'%PDF-' in content[:1024]


@dataclass(frozen=True)
class PageObservation:
    source_file_id: str
    source_content_hash: str
    page_number: int
    page_hash: str
    extraction_status: str
    classification: str
    reason_code: str
    _payload: bytes | None = field(default=None, repr=False, compare=False)
    effective_render_scale: float | None = None
    observation_complete: bool = False
    render_attempts: int = 0
    grouping_hints: dict | None = None

    def metadata(self) -> dict:
        value = {key: value for key, value in vars(self).items() if key != '_payload'}
        if self.effective_render_scale is None:  # Legacy metadata remains exact.
            for key in ('effective_render_scale', 'observation_complete', 'render_attempts', 'grouping_hints'):
                value.pop(key)
        return value


@dataclass(frozen=True)
class DocumentUnit:
    observation: PageObservation

    @property
    def page_range(self) -> tuple[int, int]:
        n = self.observation.page_number
        return n, n

    @property
    def unit_hash(self) -> str:
        # Anchor content identity to immutable source bytes and page range.
        # PNG compression or renderer upgrades must not create new ledger IDs.
        return _digest(['pdf-page-content-v1', self.observation.source_content_hash, self.page_range])

    @property
    def source_id(self) -> str:
        p = self.observation
        return 'pdf-unit-v1:' + _digest([p.source_file_id, p.source_content_hash,
                                       self.page_range, self.unit_hash])

    def metadata(self) -> dict:
        return {**self.observation.metadata(), 'page_range': list(self.page_range),
                'unit_hash': self.unit_hash, 'unit_id': self.source_id,
                'import_id': 'receipt:' + self.source_id}


class PageGrouping(Protocol):
    """Future candidate boundary; proposed groups are not accounting authority.

    Only a single-page document currently gets an automatic unit. Multi-page
    groups require a separate human confirmation boundary in the next phase.
    """
    def units(self, pages: tuple[PageObservation, ...]) -> tuple[DocumentUnit, ...]: ...


class SinglePageGrouping:
    def units(self, pages: tuple[PageObservation, ...]) -> tuple[DocumentUnit, ...]:
        return tuple(DocumentUnit(page) for page in pages)


@dataclass(frozen=True)
class PdfObservations:
    source_file_id: str
    source_content_hash: str
    pages: tuple[PageObservation, ...]
    status: str = 'observed'
    work: dict = field(default_factory=dict, compare=False)
    _page_renderer: object = field(default=None, repr=False, compare=False)

    def page_payload(self, number):
        """Future selected-member regeneration; never retains multi-page PNGs."""
        page = self.pages[number - 1]
        if page.classification != 'normal':
            raise ValueError('pdf_payload_privacy_blocked')
        if page._payload is not None:  # Existing one-page intake / explicit mocks.
            return page._payload
        if self._page_renderer is None:
            raise ValueError('pdf_payload_unavailable')
        return self._page_renderer(page)


def _render_png(page, scale=RENDER_SCALE, pixel_budget=MAX_PAGE_PIXELS) -> bytes:
    """Encode a new image from RGB samples, carrying no source metadata."""
    with closing(page.render(scale=scale)) as bitmap:
        if bitmap.width * bitmap.height > pixel_budget or bitmap.width * bitmap.height > MAX_PAGE_PIXELS:
            raise bounded.RenderHold('render_pixel_budget_exceeded')
        with bitmap.to_pil() as image, image.convert('RGB') as rgb:
            # convert creates fresh RGB samples. Drop inherited image metadata
            # before encoding; avoid another full raster and tobytes copy.
            rgb.info.clear()
            output = BytesIO()
            rgb.save(output, format='PNG')
            return output.getvalue()


def _checked_gate(payload: bytes, known: str | None, *, observation=None):
    # Kind observation completeness is distinct from Medical's payment-region
    # diagnostics. Keep that Medical policy and its amount decisions untouched.
    extracted = _extract_receipt_text(payload, 'image/png')
    complete = extracted.status == 'extracted' and extracted.observation_complete
    first_decision = classify_receipt_text('\n'.join(
        [extracted.text or '', ' '.join(t.text for t in extracted.structured_tokens)]))
    if first_decision.reason_code in {
        'medical_strong_signal', 'medical_multiple_signals',
        'payroll_strong_signal', 'payroll_multiple_signals',
        'conflicting_sensitive_evidence', 'sensitive_signal_insufficient',
    }:
        # No subsequent reading may erase sensitive evidence from this pass.
        known = first_decision.classification
    if observation is not None:
        from dataclasses import asdict
        from .receipt_pdf_grouping import evidence_from_text
        observation['sensitive'] = (first_decision.classification if first_decision.reason_code in {
            'medical_strong_signal', 'medical_multiple_signals', 'payroll_strong_signal',
            'payroll_multiple_signals', 'conflicting_sensitive_evidence', 'sensitive_signal_insufficient'} else None)
        observation['hints'] = asdict(evidence_from_text(extracted.text or '')) if complete else None
        observation['failed'] = extracted.status != 'extracted'
    result = evaluate_receipt_privacy(payload, 'image/png', known_source_classification=known)
    if type(result) is not ReceiptPrivacyGateResult:
        raise ValueError('invalid_page_gate')
    return ReceiptPrivacyGateResult.model_validate(
        {name: getattr(result, name) for name in ReceiptPrivacyGateResult.model_fields}, strict=True), complete


def _sticky(kinds):
    kinds = set(k for k in kinds if k and k != 'normal')
    if 'sensitive_unknown' in kinds or {'medical', 'payroll'} <= kinds:
        return 'sensitive_unknown'
    return next(iter(kinds), None)


def valid_render_metadata(page):
    legacy = {'source_file_id', 'source_content_hash', 'page_number', 'page_hash',
              'extraction_status', 'classification', 'reason_code'}
    if set(page) == legacy:
        return True
    if set(page) != legacy | {'effective_render_scale', 'observation_complete', 'render_attempts', 'grouping_hints'}:
        return False
    scale, complete, attempts, hints = (page[k] for k in
        ('effective_render_scale', 'observation_complete', 'render_attempts', 'grouping_hints'))
    if (type(scale) not in (int, float) or not math.isfinite(scale) or
            not (scale == 0 or bounded.MIN_SCALE <= scale <= bounded.MAX_SCALE) or
            type(complete) is not bool or type(attempts) is not int or not 0 <= attempts <= bounded.MAX_PAGE_ATTEMPTS or
            (complete and (not attempts or not scale or page['extraction_status'] != 'extracted')) or
            (page['classification'] == 'normal' and not complete)):
        return False
    if hints is None:
        return True
    return (page['classification'] == 'normal' and complete and type(hints) is dict and
        set(hints) == {'issuer', 'date', 'receipt', 'printed_page', 'printed_count', 'has_total', 'continuation'} and
        all(type(hints[k]) is str and (not hints[k] or re.fullmatch(r'[0-9a-f]{64}', hints[k]))
            for k in ('issuer', 'date', 'receipt')) and
        all(type(hints[k]) is int and 0 <= hints[k] <= 99 for k in ('printed_page', 'printed_count')) and
        all(type(hints[k]) is bool for k in ('has_total', 'continuation')))


def _observe_page(page, embedded, source_id, source_hash, number, known, budget, *, retain=False):
    payload = retained = None
    page_hash = _digest([source_hash, number, 'unobserved'])
    scale, attempts, complete = 0.0, 0, False
    status, kind, reason = 'pdf_render_failed', 'sensitive_unknown', 'render_failed'
    embedded_kind = classify_receipt_text(embedded).classification if embedded.strip() else None
    sticky = _sticky((known, embedded_kind))
    hints = None
    try:
        width, height = page.get_size()
        for attempt in range(bounded.MAX_PAGE_ATTEMPTS):
            cap = min(MAX_PAGE_PIXELS, bounded.STANDARD_PAGE_PIXELS if attempt == 0 else bounded.REREAD_PAGE_PIXELS)
            try:
                scale, pixels = bounded.render_scale(width, height, cap)
            except bounded.RenderHold as error:
                if attempt == 0 and str(error) == 'page_too_large_at_minimum_scale':
                    # The standard resolution may be smaller than the minimum
                    # scale permits. Only the bounded second pass may fit it.
                    continue
                raise
            with budget.page(pixels):
                attempts += 1
                status, reason = 'pdf_render_failed', 'render_failed'
                payload = _render_png(page, scale, cap)
                if len(payload) > MAX_PAYLOAD_BYTES:
                    raise bounded.RenderHold('page_payload_budget_exceeded')
                page_hash = sha256(payload).hexdigest()
                status, reason = 'extraction_failed', 'ocr_failed'
                info = {}
                gate, complete = _checked_gate(payload, sticky, observation=info)
                sticky = _sticky((sticky, info.get('sensitive'),
                    'sensitive_unknown' if info.get('failed') else None,
                    gate.classification if gate.classification in {'medical', 'payroll'} else None,
                    'sensitive_unknown' if gate.reason_code in {
                        'conflicting_sensitive_evidence', 'sensitive_signal_insufficient'} else None))
                status = gate.extraction_status
                complete = complete and status == 'extracted'
                if status != 'extracted':
                    sticky = _sticky((sticky, 'sensitive_unknown'))
                kind = (sticky or gate.classification) if complete and status == 'extracted' else 'sensitive_unknown'
                reason = ('ocr_failed' if status != 'extracted' else
                          'privacy_unresolved' if kind == 'sensitive_unknown' and complete else
                          'sticky_sensitive_evidence' if sticky and complete else
                          gate.reason_code if complete else 'observation_incomplete')
                hints = info.get('hints') if kind == 'normal' and complete else None
                continuation_gap = (hints and (hints['continuation'] or hints['printed_count'] > 1)
                    and not all(hints[k] for k in ('issuer', 'date', 'receipt')))
                retry = not complete or kind == 'sensitive_unknown' or continuation_gap
                if retain and kind == 'normal' and complete and (not retry or attempt == bounded.MAX_PAGE_ATTEMPTS - 1):
                    retained = payload
                else:
                    retained = None
                payload = None  # Before releasing reservation / next render.
            if not retry:
                break
    except bounded.RenderHold as error:
        status, kind, reason, complete = 'resource_budget_exceeded', 'sensitive_unknown', str(error), False
        hints = retained = None
    except Exception:
        kind, complete = 'sensitive_unknown', False
        hints = retained = None
    finally:
        payload = None
    return PageObservation(source_id, source_hash, number, page_hash, status, kind, reason,
                           retained, scale, complete, attempts, hints)


def observe_pdf(content: bytes, source_file_id: str, *,
                known_source_classification: str | None = None,
                known_page_classifications: dict[int, str] | None = None,
                work_budget=None) -> PdfObservations:
    """Finish every local observation before returning any sendable page.

    OCR is required even for text PDFs: embedded text cannot reveal all visible
    image content. Embedded text can only restrict the rendered image's gate.
    Per-page failures remain restricted observations; no multi-page observation
    grants external AI or accounting authority.
    """
    source_hash = sha256(content).hexdigest()
    def rejected(status):
        return PdfObservations(source_file_id, source_hash, (), status)
    if not content or len(content) > MAX_SOURCE_BYTES:
        return rejected('pdf_content_limit_or_empty')
    import pypdfium2 as pdfium
    from pypdf import PdfReader
    try:
        with _suppress_pypdf_output():
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted:
                return rejected('pdf_encrypted')
            count = len(reader.pages)
        if not 0 < count <= MAX_PDF_PAGES:
            return rejected('pdf_page_limit_exceeded')
        with closing(pdfium.PdfDocument(content)) as document:
            if len(document) != count:
                return rejected('pdf_page_count_mismatch')
        pages = []
        if work_budget is not None and not isinstance(work_budget,bounded.WorkBudget):
            raise ValueError('invalid_work_budget')
        budget = work_budget if work_budget is not None else bounded.WorkBudget()
        for index in range(count):
            failure_status, failure_reason = 'extraction_failed', 'embedded_extraction_failed'
            try:
                with _suppress_pypdf_output():
                    embedded = reader.pages[index].extract_text() or ''
                # A fresh native document also releases decoded-image caches
                # between pages; only source bytes and metadata survive.
                failure_status, failure_reason = 'pdf_render_failed', 'render_failed'
                with closing(pdfium.PdfDocument(content)) as document, closing(document[index]) as page:
                    restriction = _sticky((known_source_classification,
                        (known_page_classifications or {}).get(index + 1)))
                    observed = _observe_page(page, embedded, source_file_id, source_hash,
                        index + 1, restriction, budget, retain=count == 1)
            except Exception:
                observed = PageObservation(source_file_id, source_hash, index + 1,
                    _digest([source_hash, index + 1, 'unobserved']), failure_status,
                    'sensitive_unknown', failure_reason, None, 0.0)
            pages.append(observed)
        def regenerate(observation):
            # Source bytes remain local and bounded, but no image is captured
            # by this callable. Only an explicitly requested member renders.
            with closing(pdfium.PdfDocument(content)) as fresh:
                with closing(fresh[observation.page_number - 1]) as page:
                    width, height = page.get_size()
                    scale, pixels = bounded.render_scale(width, height, MAX_PAGE_PIXELS,
                        preferred=observation.effective_render_scale)
                    if scale != observation.effective_render_scale:
                        raise ValueError('pdf_payload_scale_changed')
                    with bounded.WorkBudget().page(pixels):
                        payload = _render_png(page, scale, MAX_PAGE_PIXELS)
                        if sha256(payload).hexdigest() != observation.page_hash:
                            raise ValueError('pdf_payload_identity_changed')
                        return payload
        return PdfObservations(source_file_id, source_hash, tuple(pages),
                               work=budget.metadata(), _page_renderer=regenerate)
    except Exception:
        return rejected('pdf_observation_failed')


def document_result(observations: PdfObservations) -> dict:
    # Page observations express privacy, not transaction boundaries. Do not
    # manufacture accounting units for a document that still needs grouping.
    page_count = len(observations.pages)
    units = SinglePageGrouping().units(observations.pages) if page_count == 1 else ()
    records = []
    for unit in units:
        kind = unit.observation.classification
        state = 'ready' if kind == 'normal' else 'medical_pending' if kind == 'medical' else 'privacy_pending'
        records.append({**unit.metadata(), 'status': state})
    return {'document_type': 'pdf_page_units', 'source_file_id': observations.source_file_id,
            'source_content_hash': observations.source_content_hash,
            'observation_status': observations.status,
            'page_count': page_count, 'pages': [page.metadata() for page in observations.pages],
            'status': 'grouping_required' if page_count > 1 else 'needs_review',
            'units': records, 'all_units_terminal': False, 'archive_allowed': False}


def update_document_status(result: dict) -> None:
    if result.get('page_count', 0) > 1 or result.get('status') == 'grouping_required':
        result.update(status='grouping_required', all_units_terminal=False, archive_allowed=False)
        return
    states = [unit['status'] for unit in result['units']]
    complete = bool(states) and all(state in TERMINAL_STATES for state in states)
    result['all_units_terminal'] = complete
    result['status'] = ('completed' if complete else 'partially_processed'
                        if any(state in TERMINAL_STATES for state in states) else 'needs_review')
    # v1 never enables Drive archival, including fully resolved PDFs.
    result['archive_allowed'] = False


class PdfUnitManifestStore:
    """Data-minimised local snapshot; ledger markers remain the dedupe authority."""
    def __init__(self, directory: str | Path = '.private/pdf-document-units'):
        self.directory = Path(directory)

    def restrictions(self, source_file_id: str, source_content_hash: str) -> dict[int, str]:
        """Previous sensitive provenance can restrict, never authorize, a replay."""
        path = self.directory / (_digest([source_file_id, source_content_hash]) + '.json')
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if (value['source_file_id'] != source_file_id
                    or value['source_content_hash'] != source_content_hash):
                raise ValueError()
            restrictions = {}
            # Retain restrictions from both local observation and later image
            # gates. Legacy PR #91 manifests only have the units collection.
            for records in (value.get('pages', []), value['units']):
                seen = set()
                for unit in records:
                    n, kind = unit['page_number'], unit['classification']
                    if (type(n) is not int or not 1 <= n <= MAX_PDF_PAGES or n in seen
                            or kind not in {'normal', 'medical', 'payroll', 'sensitive_unknown'}):
                        raise ValueError()
                    seen.add(n)
                    if kind != 'normal':
                        previous = restrictions.get(n, kind)
                        restrictions[n] = kind if previous == kind else 'sensitive_unknown'
            return restrictions
        except Exception:
            raise ValueError('pdf_unit_manifest_invalid') from None

    def save(self, result: dict) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / (_digest([result['source_file_id'], result['source_content_hash']]) + '.json')
        fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target
