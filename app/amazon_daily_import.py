from __future__ import annotations

from collections.abc import Callable

from .amazon_gmail_storage import import_amazon_gmail_events
from .amazon_order_header_creation import create_amazon_order_headers


def run_amazon_daily_import(
    service,
    db,
    *,
    gmail_importer: Callable = import_amazon_gmail_events,
    order_header_creator: Callable = create_amazon_order_headers,
) -> dict[str, int]:
    """Append new Gmail events, then append headers for missing Amazon orders."""

    gmail = gmail_importer(service, db)
    headers = order_header_creator(db)
    return {
        "Gmail fetched": gmail["fetched"],
        "Amazonイベント new": gmail["new"],
        "duplicate Gmail ID": gmail["duplicate_gmail_id"],
        "duplicate RFC Message-ID": gmail["duplicate_rfc_message_id"],
        "duplicate source hash": gmail["duplicate_source_hash"],
        "parser errors": gmail["parser_errors"],
        "unknown fetched": gmail["unknown"],
        "unknown new": gmail["unknown_new"],
        "Amazon注文ヘッダ created": headers["created"],
        "skipped existing": headers["skipped_existing"],
        "skipped missing order_id": headers["skipped_missing_order_id"],
    }
