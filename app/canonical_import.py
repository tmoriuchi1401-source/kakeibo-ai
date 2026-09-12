"""Shared materialization for the canonical ``取込データ`` row schema."""
from __future__ import annotations

from .aupay_card_contract import format_import_timestamp


def materialize_import_row(
    transaction,
    *,
    imported_at,
    status: str,
    target_id: str = "",
) -> list:
    """Project a validated canonical transaction into the fixed 12-column row."""
    if not status or not str(status).strip():
        raise ValueError("import_status_required")
    timestamp = format_import_timestamp(imported_at)
    return [
        transaction.identity,
        timestamp,
        transaction.source,
        transaction.source_record_id,
        transaction.transaction_date,
        transaction.merchant,
        transaction.amount_yen,
        transaction.payment_method,
        str(status).strip(),
        target_id,
        transaction.source_hash,
        transaction.memo,
    ]
