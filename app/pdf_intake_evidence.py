"""PR #91 local continuation hints only; no grouping/writer."""
from dataclasses import dataclass
import re
from .receipt_pdf_units import _digest

@dataclass(frozen=True)
class PageEvidence:
    # Evidence is ephemeral; raw names, dates, receipt numbers and OCR are not
    # written to the proposal. Digests are hints, never authority or unit IDs.
    issuer: str = ''
    date: str = ''
    receipt: str = ''
    printed_page: int = 0
    printed_count: int = 0
    has_total: bool = False
    continuation: bool = False


def evidence_from_text(text: str) -> PageEvidence:
    def hint(pattern):
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return _digest(match.group(1).strip().casefold()) if match else ''
    numbering = re.search(r'(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)', text)
    return PageEvidence(
        issuer=hint(r'^(?:店舗名|施設名|店名|merchant|store)\s*[:：]\s*(.+)$'),
        date=hint(r'(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)'),
        receipt=hint(r'(?:レシート番号|伝票番号|receipt\s*(?:no\.?|number))\s*[:：#]?\s*([\w-]+)'),
        printed_page=int(numbering[1]) if numbering else 0,
        printed_count=int(numbering[2]) if numbering else 0,
        has_total=bool(re.search(r'合計|総額|\btotal\b', text, re.IGNORECASE)),
        continuation=bool(re.search(r'続き|次頁|次ページ|繰越|continued|carry\s*forward', text, re.IGNORECASE)),
    )
