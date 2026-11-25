"""New-mode original identity. Optional version is audit text, never Drive proof."""
import re

FIELDS = {"source_id", "sha256", "mime_type", "classification", "transaction_kind"}


def checked_source(source, *, allow_single_page_pdf=False):
    if (type(source) is not dict or not FIELDS <= set(source) <= FIELDS | {"version"}
            or type(source["source_id"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", source["source_id"])
            or type(source["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
            or source["mime_type"] not in ({"image/png", "image/jpeg", "image/heic", "image/heif"}
                | ({"application/pdf"} if allow_single_page_pdf else set()))
            or source["classification"] != "normal" or source["transaction_kind"] != "purchase"
            or "version" in source and type(source["version"]) is not str):
        raise ValueError("receipt_sheets_source_invalid")
    return source
