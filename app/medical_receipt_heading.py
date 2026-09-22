"""Exact local receipt-title grammar; never a transport authorization."""
import re
from .medical_anonymization import compact


def is_receipt_heading(text):
    # Normalize equivalent OCR glyphs only inside an allowlisted whole title.
    title = compact(text).translate(str.maketrans({'费': '費', '收': '収'}))
    return bool(re.fullmatch(
        r'(?:(?:診療費|医療費|調剤|薬剤費|請求書兼))*領収[書証]'
        r'|(?:診療費|医療費)請求\(領収\)書', title))
