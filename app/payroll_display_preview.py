from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .payroll_display import payroll_display_header
from .payroll_sheets import PayrollSheetsReadRepository, SHEET_TITLES
from .payroll_storage import PAYROLL_SCHEMAS


DisplayUpdateAction = Literal["update", "noop", "blocked"]


class PayrollDisplayHeaderUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    sheet_key: str
    sheet_title: str
    range_name: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    action: DisplayUpdateAction
    changed_cell_count: int
    reason: str | None = None


class PayrollDisplayUpdatePreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    spreadsheet_fingerprint: str
    blocked: bool
    updates: tuple[PayrollDisplayHeaderUpdate, ...]
    changed_header_cell_count: int
    changed_data_cell_count: int = 0
    row_additions: int = 0
    row_deletions: int = 0


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def build_payroll_display_update_preview(
    repository: PayrollSheetsReadRepository,
) -> PayrollDisplayUpdatePreview:
    """Plan display-header changes only; this module exposes no write method."""
    titles = repository.sheet_titles()
    updates = []
    for sheet_key, sheet_title in SHEET_TITLES.items():
        canonical = tuple(PAYROLL_SCHEMAS[sheet_key])
        after = payroll_display_header(sheet_key)
        range_name = f"'{sheet_title}'!A1:{_column_name(len(canonical))}1"
        if sheet_title not in titles:
            before = ()
            action: DisplayUpdateAction = "blocked"
            reason = "sheet_missing"
            changed = 0
        else:
            before = tuple(repository.header(sheet_title))
            if before == after:
                action = "noop"
                reason = None
                changed = 0
            elif before == canonical:
                action = "update"
                reason = None
                changed = sum(left != right for left, right in zip(before, after))
            else:
                action = "blocked"
                reason = "header_mismatch"
                changed = 0
        updates.append(PayrollDisplayHeaderUpdate(
            sheet_key=sheet_key,
            sheet_title=sheet_title,
            range_name=range_name,
            before=before,
            after=after,
            action=action,
            changed_cell_count=changed,
            reason=reason,
        ))
    result = tuple(updates)
    return PayrollDisplayUpdatePreview(
        spreadsheet_fingerprint=hashlib.sha256(
            repository.spreadsheet_id.encode("utf-8")
        ).hexdigest()[:16],
        blocked=any(update.action == "blocked" for update in result),
        updates=result,
        changed_header_cell_count=sum(
            update.changed_cell_count for update in result
        ),
    )
