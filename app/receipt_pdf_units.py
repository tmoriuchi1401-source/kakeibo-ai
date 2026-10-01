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
from pathlib import Path
import tempfile
from typing import Protocol

from .medical_receipt_privacy import classify_receipt_text
from .receipt_privacy_gate import ReceiptPrivacyGateResult, evaluate_receipt_privacy
from .receipt_text_extraction import _extract_receipt_text, _suppress_pypdf_output

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

    def metadata(self) -> dict:
        return {key: value for key, value in vars(self).items() if key != '_payload'}


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


def _render_png(page) -> bytes:
    """Encode a new image from RGB samples, carrying no source metadata."""
    from PIL import Image
    with closing(page.render(scale=RENDER_SCALE)) as bitmap:
        with bitmap.to_pil() as image, image.convert('RGB') as rgb:
            with Image.frombytes('RGB', rgb.size, rgb.tobytes()) as clean:
                output = BytesIO()
                clean.save(output, format='PNG')
                return output.getvalue()


def _checked_gate(payload: bytes, known: str | None):
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
    result = evaluate_receipt_privacy(payload, 'image/png', known_source_classification=known)
    if type(result) is not ReceiptPrivacyGateResult:
        raise ValueError('invalid_page_gate')
    return ReceiptPrivacyGateResult.model_validate(
        {name: getattr(result, name) for name in ReceiptPrivacyGateResult.model_fields}, strict=True), complete


def observe_pdf(content: bytes, source_file_id: str, *,
                known_source_classification: str | None = None,
                known_page_classifications: dict[int, str] | None = None) -> PdfObservations:
    """Finish every local observation before returning any sendable page.

    OCR is required even for text PDFs: embedded text cannot reveal all visible
    image content. Embedded text can only restrict the rendered image's gate.
    Resource limits reject the whole document before any external AI call.
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
            # Bound the complete document before allocating rendered pixels.
            pixels = 0
            for index in range(count):
                with closing(document[index]) as page:
                    width, height = page.get_size()
                    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                        return rejected('pdf_resource_limit_exceeded')
                    page_pixels = math.ceil(width * RENDER_SCALE) * math.ceil(height * RENDER_SCALE)
                    if page_pixels > MAX_PAGE_PIXELS:
                        return rejected('pdf_resource_limit_exceeded')
                    pixels += page_pixels
            if pixels > MAX_DOCUMENT_PIXELS:
                return rejected('pdf_resource_limit_exceeded')
            pages = []
            payload_size = 0
            for index in range(count):
                payload = None
                page_hash = _digest([source_hash, index + 1, 'unobserved'])
                status, kind, reason = 'pdf_render_failed', 'sensitive_unknown', 'observation_incomplete'
                try:
                    with closing(document[index]) as page:
                        payload = _render_png(page)
                    page_hash = sha256(payload).hexdigest()
                    payload_size += len(payload)
                    if payload_size > MAX_PAYLOAD_BYTES:
                        return rejected('pdf_resource_limit_exceeded')
                    status = 'extraction_failed'
                    with _suppress_pypdf_output():
                        embedded = reader.pages[index].extract_text() or ''
                    embedded_kind = classify_receipt_text(embedded).classification
                    restriction = known_source_classification or (known_page_classifications or {}).get(index + 1)
                    if embedded.strip() and embedded_kind in {'medical', 'payroll'}:
                        restriction = embedded_kind
                    gate, complete = _checked_gate(payload, restriction)
                    status, kind, reason = gate.extraction_status, gate.classification, gate.reason_code
                    # Preserve sensitive embedded evidence even if OCR misses it.
                    if status != 'extracted' or not complete:
                        kind, reason = 'sensitive_unknown', 'observation_incomplete'
                    elif embedded.strip() and embedded_kind != 'normal':
                        kind, reason = embedded_kind, 'embedded_sensitive_or_incomplete'
                except Exception:
                    # Never expose parser/OCR errors or classify a failed subset.
                    kind = 'sensitive_unknown'
                pages.append(PageObservation(source_file_id, source_hash, index + 1,
                    page_hash, status, kind, reason, payload if kind == 'normal' else None))
            return PdfObservations(source_file_id, source_hash, tuple(pages))
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
