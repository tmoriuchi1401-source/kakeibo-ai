from __future__ import annotations

from .amazon_review import AMAZON_REVIEW_HEADERS, AMAZON_REVIEW_SHEET


def install_amazon_review_schema(db) -> dict[str, int]:
    """Install only the Amazon review sheet and its fixed header."""

    summary = {
        "sheet_created": 0,
        "schema_already_valid": 0,
        "header_written": 0,
        "schema_mismatch": 0,
    }
    if AMAZON_REVIEW_SHEET in set(db.sheet_titles()):
        rows = db.get(f"{AMAZON_REVIEW_SHEET}!1:1")
        header = list(rows[0]) if rows else []
        if header == AMAZON_REVIEW_HEADERS:
            summary["schema_already_valid"] = 1
        else:
            summary["schema_mismatch"] = 1
        return summary

    db.svc.spreadsheets().batchUpdate(
        spreadsheetId=db.sid,
        body={"requests": [{
            "addSheet": {"properties": {
                "title": AMAZON_REVIEW_SHEET,
                "gridProperties": {"frozenRowCount": 1},
            }},
        }]},
    ).execute()
    summary["sheet_created"] = 1
    db.svc.spreadsheets().values().update(
        spreadsheetId=db.sid,
        range=f"{AMAZON_REVIEW_SHEET}!A1:N1",
        valueInputOption="RAW",
        body={"values": [AMAZON_REVIEW_HEADERS]},
    ).execute()
    summary["header_written"] = 1
    return summary
