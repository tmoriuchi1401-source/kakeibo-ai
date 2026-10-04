"""Real bounded rasterization, synthetic OCR, and side-effect-free proposals."""
from dataclasses import asdict, replace
from io import BytesIO
import json
import math
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
from pypdf import PdfWriter
import pytest

from app import pdf_bounded_rendering as bounded, receipt_pdf_units as pdf
from app import receipt_pdf_grouping as grouping
from test_pdf_grouping_authority import context, request
from test_receipt_pdf_units import synthetic_pdf, local_ocr
from test_receipt_pipeline import _normal_gate, _medical_gate, _payroll_gate, _sensitive_gate


SCAN_SIZES = [(1913, 2702), (1043, 3283), (920, 4134), (1434, 1597),
              (1292, 2806), (1137, 2642), (1028, 3248), (1526, 2109),
              (791, 4192), (905, 3089), (1302, 2386), (1399, 2768),
              (680, 4587), (784, 3697)]


def blank_pdf(sizes):
    writer = PdfWriter()
    for width, height in sizes:
        writer.add_blank_page(width=width, height=height)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def gate_passes(monkeypatch, passes):
    """Each observation receives a real validated gate model, never authority."""
    calls = []
    steps = iter(passes)
    def check(payload, known, *, observation):
        calls.append(known)
        result, complete, sensitive, hints = next(steps)
        observation.update(sensitive=sensitive, hints=hints)
        return result, complete
    monkeypatch.setattr(pdf, '_checked_gate', check)
    return calls


@pytest.mark.parametrize('size', [(72, 72), (1434, 1597), (1913, 2702), *SCAN_SIZES])
@pytest.mark.parametrize('cap', [4_000_000, 12_000_000])
def test_adaptive_geometry_includes_integer_rounding(size, cap):
    scale, pixels = bounded.render_scale(*size, cap)
    assert bounded.MIN_SCALE <= scale <= bounded.MAX_SCALE
    assert pixels == math.ceil(size[0] * scale) * math.ceil(size[1] * scale) <= cap
    assert scale <= min(3, math.sqrt(cap / math.prod(size)))
    assert bounded.render_scale(*size, cap, preferred=scale) == (scale, pixels)
    if math.prod(size) * 9 > cap:
        assert scale < 3


@pytest.mark.parametrize('size', [(0, 2), (-1, 1), (float('nan'), 1),
    (1, float('inf')), (True, 1), ('10', 2), (1, 1000), (100001, 5000)])
def test_invalid_geometry_fails_closed_before_rasterization(size):
    with pytest.raises(bounded.RenderHold, match='invalid_page_geometry'):
        bounded.render_scale(*size, 12_000_000)


def test_page_cannot_fit_even_at_minimum_scale():
    with pytest.raises(bounded.RenderHold, match='page_too_large_at_minimum_scale'):
        bounded.render_scale(100_000, 100_000, 12_000_000)


def test_minimum_scale_uses_only_bounded_second_pass_if_standard_cannot_fit(monkeypatch):
    # 7.8M at minimum: it fits the existing 12M cap, but not the 4M standard.
    gate_passes(monkeypatch, [(_normal_gate(), True, None, None)])
    rendered = Mock(return_value=b'bounded synthetic image')
    monkeypatch.setattr(pdf, '_render_png', rendered)
    result = pdf._observe_page(SimpleNamespace(get_size=lambda: (50000, 10000)), '',
        'source', 'a' * 64, 1, None, bounded.WorkBudget())
    assert result.classification == 'normal' and result.render_attempts == 1
    assert rendered.call_args.args[2] == 12_000_000


def test_actual_bitmap_dimensions_are_checked_before_pixel_copy():
    bitmap = SimpleNamespace(width=4000, height=4000, close=Mock(), to_pil=Mock())
    page = SimpleNamespace(render=Mock(return_value=bitmap))
    with pytest.raises(bounded.RenderHold, match='render_pixel_budget_exceeded'):
        pdf._render_png(page, .5, 4_000_000)
    bitmap.to_pil.assert_not_called()
    bitmap.close.assert_called_once()


def test_fourteen_scan_pages_render_sequentially_without_png_retention(monkeypatch, tmp_path):
    # This case uses the offline proposal CLI. Actual Actions authority must
    # continue rejecting local JSON (covered by the durable authority suite).
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    gate_passes(monkeypatch, [(_normal_gate(), True, None, None)] * 14)
    original = pdf._render_png
    live = peak = 0
    def render(page, scale, cap):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            value = original(page, scale, cap)
            with Image.open(BytesIO(value)) as image:
                assert image.width * image.height <= cap <= pdf.MAX_PAGE_PIXELS
            return value
        finally:
            live -= 1
    monkeypatch.setattr(pdf, '_render_png', render)
    # The old 100M cumulative cap must not govern sequential observations.
    monkeypatch.setattr(pdf, 'MAX_DOCUMENT_PIXELS', 1)
    source = blank_pdf(SCAN_SIZES)
    observed = pdf.observe_pdf(source, 'synthetic-scan')
    assert len(observed.pages) == 14 and observed.status == 'observed'
    assert peak == observed.work['peak_live_pages'] == 1
    assert observed.work['peak_live_pixels'] <= bounded.MAX_LIVE_PIXELS
    assert observed.work['render_calls'] == 14
    assert observed.work['render_pixels'] <= 14 * bounded.STANDARD_PAGE_PIXELS
    assert all(p._payload is None and p.observation_complete for p in observed.pages)
    assert all(p.classification == 'normal' and p.effective_render_scale < 3 for p in observed.pages)
    # Proposing neither re-renders nor re-OCRs; it consumes only page metadata.
    monkeypatch.setattr(pdf, '_render_png', Mock(side_effect=AssertionError('No proposal rasterization')))
    monkeypatch.setattr(grouping, '_extract_receipt_text', Mock(side_effect=AssertionError('No proposal OCR')))
    service = grouping.PdfGroupingService(grouping.GroupingStore(tmp_path / 'state'))
    view = service.prepare(observed)
    assert view['status'] == 'grouping_required' and view['confirmation'] is None
    assert [g['page_numbers'] for g in view['proposal']['groups']] == [[n] for n in range(1, 15)]
    assert service.units(observed) == ()


def test_payload_regeneration_renders_only_requested_member(monkeypatch):
    gate_passes(monkeypatch, [(_normal_gate(), True, None, None)] * 3)
    observations = pdf.observe_pdf(blank_pdf([(72, 72)] * 3), 'source')
    renderer = Mock(wraps=pdf._render_png)
    monkeypatch.setattr(pdf, '_render_png', renderer)
    payload = observations.page_payload(2)
    assert payload.startswith(b'\x89PNG') and renderer.call_count == 1
    assert pdf.sha256(payload).hexdigest() == observations.pages[1].page_hash
    assert all(p._payload is None for p in observations.pages)


@pytest.mark.parametrize('passes,kind,attempts,complete', [
    ([(_normal_gate(), True, None, None)], 'normal', 1, True),
    ([(_normal_gate(), False, None, None), (_normal_gate(), True, None, None)], 'normal', 2, True),
    ([(_normal_gate(), False, None, None)] * 2, 'sensitive_unknown', 2, False),
    ([(_medical_gate(), False, 'medical', None), (_normal_gate(), True, None, None)], 'medical', 2, True),
    ([(_payroll_gate(), False, 'payroll', None), (_normal_gate(), True, None, None)], 'payroll', 2, True),
    ([(_normal_gate(), False, 'sensitive_unknown', None), (_normal_gate(), True, None, None)], 'sensitive_unknown', 2, True),
    ([(_medical_gate(), False, 'medical', None), (_payroll_gate(), True, 'payroll', None)], 'sensitive_unknown', 2, True),
    ([(_sensitive_gate(), False, None, None), (_normal_gate(), True, None, None)], 'sensitive_unknown', 2, True),
])
def test_two_pass_completeness_and_sensitive_evidence_are_sticky(monkeypatch, passes, kind, attempts, complete):
    calls = gate_passes(monkeypatch, passes)
    observed = pdf.observe_pdf(blank_pdf([(1913, 2702)]), 'source')
    page, = observed.pages
    assert page.classification == kind and page.render_attempts == attempts
    assert page.observation_complete is complete
    assert len(calls) == attempts <= bounded.MAX_PAGE_ATTEMPTS
    assert (page._payload is not None) == (kind == 'normal' and complete)
    if attempts == 2:
        assert page.effective_render_scale > bounded.render_scale(1913, 2702, 4_000_000)[0]
    assert pdf.valid_render_metadata(page.metadata())


def test_missing_continuation_information_triggers_only_one_reread(monkeypatch):
    hints = asdict(grouping.PageEvidence(printed_page=1, printed_count=2, continuation=True))
    calls = gate_passes(monkeypatch, [(_normal_gate(), True, None, hints)] * 2)
    observations = pdf.observe_pdf(blank_pdf([(72, 72)]), 'source')
    assert observations.pages[0].render_attempts == len(calls) == 2


def test_failed_first_extraction_cannot_be_erased_by_later_normal_reads(monkeypatch, local_ocr):
    from app import receipt_text_extraction as extraction
    original = pdf._extract_receipt_text
    calls = 0
    def extract(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return extraction._ReceiptTextExtraction('extraction_failed', 'image_ocr', None)
        return original(*args)
    monkeypatch.setattr(pdf, '_extract_receipt_text', extract)
    page, = pdf.observe_pdf(synthetic_pdf(['normal']), 'source').pages
    assert page.classification == 'sensitive_unknown'
    assert page.render_attempts == 2 and page._payload is None


@pytest.mark.parametrize('phase', ['render', 'ocr'])
def test_failure_on_second_pass_keeps_unknown_without_raw_exception(monkeypatch, phase):
    calls = 0
    original = pdf._render_png
    def render(*args):
        nonlocal calls
        calls += 1
        if phase == 'render' and calls == 2:
            raise RuntimeError('PRIVATE_DOCUMENT_CONTENT')
        return original(*args)
    monkeypatch.setattr(pdf, '_render_png', render)
    def check(*args, **kwargs):
        if phase == 'ocr' and calls == 2:
            raise RuntimeError('PRIVATE_DOCUMENT_CONTENT')
        return _normal_gate(), False
    monkeypatch.setattr(pdf, '_checked_gate', check)
    page, = pdf.observe_pdf(blank_pdf([(72, 72)]), 'source').pages
    assert page.classification == 'sensitive_unknown' and not page.observation_complete
    assert page.reason_code == ('render_failed' if phase == 'render' else 'ocr_failed')
    assert page._payload is None and 'PRIVATE_' not in json.dumps(page.metadata())


@pytest.mark.parametrize('limit,value', [('MAX_TOTAL_RENDER_PIXELS', 50_000),
    ('MAX_RENDER_CALLS', 1), ('MAX_TOTAL_OCR_PIXEL_WORK', 1_000_000)])
def test_total_work_exhaustion_keeps_every_remaining_page_restricted(monkeypatch, limit, value):
    monkeypatch.setattr(bounded, limit, value)
    gate_passes(monkeypatch, [(_normal_gate(), True, None, None)])
    observations = pdf.observe_pdf(blank_pdf([(72, 72)] * 14), 'source')
    assert len(observations.pages) == 14
    assert observations.pages[0].classification == 'normal'
    assert all(p.classification == 'sensitive_unknown' and p.reason_code == 'total_work_budget_exceeded'
               for p in observations.pages[1:])
    assert observations.work['render_calls'] == 1
    assert pdf.document_result(observations)['units'] == []


def test_live_reservation_rejects_overlap_and_releases_after_failure(monkeypatch):
    budget = bounded.WorkBudget()
    with pytest.raises(RuntimeError):
        with budget.page(4_000_000):
            with pytest.raises(bounded.RenderHold, match='live_pixel_budget_exceeded'):
                with budget.page(1):
                    pytest.fail('overlap accepted')
            raise RuntimeError('synthetic failure')
    assert budget.live_pages == budget.live_pixels == 0 and budget.peak_live_pages == 1
    monkeypatch.setattr(bounded, 'MAX_LIVE_PIXELS', 1)
    with pytest.raises(bounded.RenderHold, match='live_pixel_budget_exceeded'):
        with budget.page(1):
            pytest.fail('memory limit ignored')


def test_elapsed_work_guard_rejects_next_page(monkeypatch):
    budget = bounded.WorkBudget()
    monkeypatch.setattr(bounded.time, 'monotonic', lambda: budget.started + bounded.MAX_WORK_SECONDS + 1)
    with pytest.raises(bounded.RenderHold, match='total_work_budget_exceeded'):
        with budget.page(1):
            pytest.fail('time guard ignored')


@pytest.mark.parametrize('change', [
    {'effective_render_scale': float('nan')}, {'effective_render_scale': 4},
    {'observation_complete': False}, {'render_attempts': 3},
    {'grouping_hints': {'raw_ocr': 'PRIVATE_CONTENT'}},
    {'grouping_hints': asdict(grouping.PageEvidence(issuer='PRIVATE_STORE'))},
])
def test_extended_metadata_cannot_authorize_invalid_observations(local_ocr, change):
    svc, live, transport = context()
    pages = (replace(live.observations.pages[0], **change), live.observations.pages[1])
    live.observations = replace(live.observations, pages=pages)
    assert svc.display('drive-source-id')['status'] == 'grouping_required'
    assert not svc.confirmed_units('drive-source-id')
    # A failed-observation marker may persist; it contains no untrusted page
    # metadata, candidate or confirmation.
    state = svc.store.load()
    assert all(r['proposal'] is None and r['confirmation'] is None for r in state['records'].values())
    assert b'PRIVATE_' not in transport.payload


def test_new_render_snapshot_revokes_old_confirmation(local_ocr):
    svc, live, transport = context()
    view = svc.display('drive-source-id')
    svc.review(request(view))
    live.observations = replace(live.observations, pages=tuple(replace(p,
        effective_render_scale=None) for p in live.observations.pages))
    old = svc.display('drive-source-id')
    assert old['confirmation'] is None and old['status'] == 'grouping_required'
    assert not svc.confirmed_units('drive-source-id')


def test_unconfirmed_bounded_pdf_never_initializes_prohibited_clients(tmp_path, local_ocr):
    from app.receipt_pipeline import ReceiptPipeline
    db, ai, medical, factory = Mock(), Mock(), Mock(), Mock()
    p = ReceiptPipeline(db, ai, medical_review_observer=medical, gemini_factory=factory,
        pdf_manifest_store=pdf.PdfUnitManifestStore(tmp_path / 'state'))
    result = p.process_bytes(synthetic_pdf(['normal', 'medical', 'normal']), 'application/pdf', 'source')
    assert result['status'] == 'grouping_required' and result['archive_allowed'] is False
    assert db.mock_calls == ai.mock_calls == medical.mock_calls == factory.mock_calls == []
    assert all('_payload' not in page for page in result['pages'])
