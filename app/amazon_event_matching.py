from __future__ import annotations

from .sheets import SheetsDB


def match_status(order_id: object, existing_order_ids: set[str]) -> str:
    normalized_order_id = str(order_id).strip() if order_id is not None else ""
    if not normalized_order_id:
        return "missing_order_id"
    if normalized_order_id in existing_order_ids:
        return "matched"
    return "order_not_found"


class AmazonEventMatchingPipeline:
    """Match stored Amazon events to existing Amazon orders by Order ID only."""

    def __init__(self, db: SheetsDB):
        self.db = db

    def apply(self) -> dict[str, int]:
        existing_order_ids = {
            str(row[0]).strip()
            for row in self.db.get("Amazon注文!B2:B")
            if row and str(row[0]).strip()
        }
        event_rows = self.db.get("Amazonイベント!G2:T")
        updates: list[tuple[int, str]] = []
        summary = {
            "total": len(event_rows),
            "matched": 0,
            "order_not_found": 0,
            "missing_order_id": 0,
            "updated": 0,
            "unchanged": 0,
        }

        for row_num, raw_row in enumerate(event_rows, start=2):
            row = list(raw_row)
            new_status = match_status(row[0] if row else "", existing_order_ids)
            current_status = str(row[13]).strip() if len(row) > 13 else ""
            summary[new_status] += 1
            if current_status == new_status:
                summary["unchanged"] += 1
            else:
                updates.append((row_num, new_status))

        if updates:
            self.db.update_amazon_event_match_statuses(updates)
        summary["updated"] = len(updates)
        return summary
