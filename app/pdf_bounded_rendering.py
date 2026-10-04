"""Data-free geometry and sequential work accounting; no external clients."""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import time

MIN_SCALE = .125
MAX_SCALE = 3.0
STANDARD_PAGE_PIXELS = 4_000_000
REREAD_PAGE_PIXELS = 12_000_000
MAX_ASPECT_RATIO = 32
MAX_DIMENSION_POINTS = 100_000
MAX_LIVE_PIXELS = 48_000_000
MAX_RENDER_CALLS = 100
MAX_TOTAL_RENDER_PIXELS = 400_000_000
MAX_TOTAL_OCR_PIXEL_WORK = 2_400_000_000
MAX_WORK_SECONDS = 900
MAX_PAGE_ATTEMPTS = 2


class RenderHold(ValueError):
    """Fixed reason codes only; never include parser/OCR exceptions."""


def render_scale(width, height, budget, *, preferred=MAX_SCALE):
    if (any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
            or v > MAX_DIMENSION_POINTS for v in (width, height))
            or max(width / height, height / width) > MAX_ASPECT_RATIO):
        raise RenderHold('invalid_page_geometry')
    if type(budget) is not int or budget < 1:
        raise RenderHold('page_pixel_budget_exceeded')
    scale = min(preferred, MAX_SCALE, math.sqrt(budget / (width * height)))
    # Quantize downward, then include PDFium's ceil on both dimensions. No
    # upward epsilon or speculative oversized render is necessary.
    units = math.floor(math.nextafter(scale * 1_000_000, math.inf))
    scale = units / 1_000_000
    while scale >= MIN_SCALE and math.ceil(width * scale) * math.ceil(height * scale) > budget:
        units -= 1
        scale = units / 1_000_000
    if scale < MIN_SCALE:
        raise RenderHold('page_too_large_at_minimum_scale')
    return scale, math.ceil(width * scale) * math.ceil(height * scale)


@dataclass
class WorkBudget:
    render_calls: int = 0
    render_pixels: int = 0
    ocr_pixel_work: int = 0
    live_pages: int = 0
    live_pixels: int = 0
    peak_live_pages: int = 0
    peak_live_pixels: int = 0
    started: float = 0

    def __post_init__(self):
        self.started = time.monotonic()

    def checkpoint(self):
        """Also fence downstream API/authority latency before accounting."""
        if (self.render_calls>MAX_RENDER_CALLS or self.render_pixels>MAX_TOTAL_RENDER_PIXELS
                or self.ocr_pixel_work>MAX_TOTAL_OCR_PIXEL_WORK
                or time.monotonic()-self.started>MAX_WORK_SECONDS):
            raise RenderHold('total_work_budget_exceeded')

    @contextmanager
    def page(self, pixels, *, render_calls=1, ocr_checks=1):
        # Existing gate performs text/token reads, a complete independent gate
        # and bounded classification re-reads. Charge their conservative worst
        # case, including grouping hints, not just the first bitmap allocation.
        if (type(render_calls) is not int or not 1<=render_calls<=50 or
                type(ocr_checks) is not int or not 1<=ocr_checks<=6):
            raise RenderHold('total_work_budget_exceeded')
        ocr = ocr_checks * (6 * pixels + 2 * min(4 * pixels, REREAD_PAGE_PIXELS))
        live = max(4 * pixels, 2 * pixels + REREAD_PAGE_PIXELS)
        if (self.render_calls + render_calls > MAX_RENDER_CALLS or
                self.render_pixels + pixels > MAX_TOTAL_RENDER_PIXELS or
                self.ocr_pixel_work + ocr > MAX_TOTAL_OCR_PIXEL_WORK or
                time.monotonic() - self.started > MAX_WORK_SECONDS):
            raise RenderHold('total_work_budget_exceeded')
        if self.live_pages or self.live_pixels + live > MAX_LIVE_PIXELS:
            raise RenderHold('live_pixel_budget_exceeded')
        self.render_calls += render_calls
        self.render_pixels += pixels
        self.ocr_pixel_work += ocr
        self.live_pages += 1
        self.live_pixels += live
        self.peak_live_pages = max(self.peak_live_pages, self.live_pages)
        self.peak_live_pixels = max(self.peak_live_pixels, self.live_pixels)
        try:
            yield
        finally:
            self.live_pages -= 1
            self.live_pixels -= live

    def metadata(self):
        return {k: getattr(self, k) for k in ('render_calls', 'render_pixels',
            'ocr_pixel_work', 'peak_live_pages', 'peak_live_pixels')}
