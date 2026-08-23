from __future__ import annotations

from .sheets import HEADERS


AMAZON_SCHEMA_SHEETS = ("Amazonイベント", "Amazon注文ヘッダ")


def _summary_entry(sheet_names: list[str]) -> dict:
    return {"count": len(sheet_names), "sheets": sheet_names}


def install_amazon_schema(db) -> dict:
    """Safely install only the two Amazon event/header sheets."""

    existing_titles = set(db.sheet_titles())
    created = [title for title in AMAZON_SCHEMA_SHEETS if title not in existing_titles]
    initialized = []
    ready = []
    conflicts = []

    if created:
        db.svc.spreadsheets().batchUpdate(
            spreadsheetId=db.sid,
            body={"requests": [
                {"addSheet": {"properties": {
                    "title": title,
                    "gridProperties": {"frozenRowCount": 1},
                }}}
                for title in created
            ]},
        ).execute()

    for title in AMAZON_SCHEMA_SHEETS:
        expected = HEADERS[title]
        if title in created:
            header_row = []
        else:
            rows = db.get(f"{title}!1:1")
            header_row = rows[0] if rows else []

        if not any(str(value).strip() for value in header_row):
            db.svc.spreadsheets().values().update(
                spreadsheetId=db.sid,
                range=f"{title}!A1",
                valueInputOption="RAW",
                body={"values": [expected]},
            ).execute()
            initialized.append(title)
        elif header_row == expected:
            ready.append(title)
        else:
            conflicts.append(title)

    return {
        "created_sheets": _summary_entry(created),
        "initialized_headers": _summary_entry(initialized),
        "already_ready": _summary_entry(ready),
        "conflicts": _summary_entry(conflicts),
    }
