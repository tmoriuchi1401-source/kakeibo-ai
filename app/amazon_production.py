"""Bounded Amazon Gmail write planning and recurring production execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Iterable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .amazon_gmail_storage import (
    GmailRawMessage,
    _unparsed_event,
    amazon_stored_event_from_mail,
)
from .amazon_email import AmazonMailEvent, parse_amazon_email
from .amazon_order_header import AmazonOrderHeader
from .aupay_card_recurring import SqliteRecurringRunState
from .aupay_card_writer import FixedSourceWindow
from .auto_expense import expense_id
from .utils import canonical_hash, now_jst_string


JST = ZoneInfo("Asia/Tokyo")
SOURCE_QUERY = "in:anywhere from:amazon.co.jp"
MAX_AUTHORITY_MESSAGES = 100
MAX_AUTHORITY_PURCHASES = 20
MAX_AUTHORITY_WINDOW_SECONDS = 370 * 86400


@dataclass(frozen=True)
class AmazonRecurringAuthority:
    policy_id: str
    source: str
    expected_spreadsheet_id: str = field(repr=False)
    max_messages: int
    max_purchases: int
    overlap_seconds: int
    max_window_seconds: int
    initial_start: datetime
    valid_from: datetime
    expires_at: datetime

    def validate(self) -> None:
        aware = (self.initial_start, self.valid_from, self.expires_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in aware):
            raise ValueError("amazon_authority_timezone_required")
        if (
            not self.policy_id
            or self.source != "amazon_gmail"
            or not self.expected_spreadsheet_id
            or not (1 <= self.max_messages <= MAX_AUTHORITY_MESSAGES)
            or not (1 <= self.max_purchases <= MAX_AUTHORITY_PURCHASES)
            or not (0 <= self.overlap_seconds <= 86400)
            or not (self.overlap_seconds < self.max_window_seconds <= MAX_AUTHORITY_WINDOW_SECONDS)
            or self.initial_start >= self.expires_at
            or self.valid_from >= self.expires_at
        ):
            raise ValueError("amazon_authority_invalid")


class ProtectedAmazonAuthorityProvider:
    def __init__(self, path, *, repo_root):
        self.path = Path(path).expanduser().resolve()
        root = Path(repo_root).resolve()
        try:
            self.path.relative_to(root)
        except ValueError:
            pass
        else:
            raise RuntimeError("amazon_authority_must_be_outside_repository")

    def load(self) -> AmazonRecurringAuthority:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        policy = AmazonRecurringAuthority(
            policy_id=str(raw["policy_id"]),
            source=str(raw["source"]),
            expected_spreadsheet_id=str(raw["expected_spreadsheet_id"]),
            max_messages=int(raw["max_messages"]),
            max_purchases=int(raw["max_purchases"]),
            overlap_seconds=int(raw["overlap_seconds"]),
            max_window_seconds=int(raw["max_window_seconds"]),
            initial_start=datetime.fromisoformat(str(raw["initial_start"])),
            valid_from=datetime.fromisoformat(str(raw["valid_from"])),
            expires_at=datetime.fromisoformat(str(raw["expires_at"])),
        )
        policy.validate()
        return policy


@dataclass(frozen=True)
class AmazonPurchaseCandidate:
    order_id: str = field(repr=False)
    order_date: str
    amount: int
    payment_method: str
    item_count: int | None

    @property
    def import_id(self) -> str:
        return f"amazon:{self.order_id}"

    @property
    def expense_id(self) -> str:
        return expense_id(self.import_id)

    @property
    def reference(self) -> str:
        digest = hashlib.sha256(self.order_id.encode("utf-8")).hexdigest()
        return f"amazon-order:{digest[:16]}"


@dataclass(frozen=True)
class AmazonWritePlan:
    window: FixedSourceWindow
    fetched: int
    collection_complete: bool
    event_rows: tuple[tuple, ...] = field(repr=False)
    purchases: tuple[AmazonPurchaseCandidate, ...] = field(repr=False)
    header_rows: tuple[tuple, ...] = field(repr=False)
    duplicate_events: int = 0
    duplicate_order_messages: int = 0
    already_present: int = 0
    cancellation: int = 0
    returns: int = 0
    refunds: int = 0
    parser_errors: int = 0
    needs_review: int = 0
    unusable: int = 0

    def anonymized(self) -> dict[str, object]:
        return {
            "source_window_start": self.window.start.isoformat(),
            "source_window_end": self.window.end.isoformat(),
            "source_timezone": self.window.timezone_name,
            "fetched": self.fetched,
            "collection_complete": self.collection_complete,
            "new_event_rows": len(self.event_rows),
            "eligible_purchases": len(self.purchases),
            "purchase_targets": [
                {"reference": item.reference, "date": item.order_date, "amount": item.amount}
                for item in self.purchases
            ],
            "new_header_rows": len(self.header_rows),
            "duplicate_events": self.duplicate_events,
            "duplicate_order_messages": self.duplicate_order_messages,
            "already_present": self.already_present,
            "cancellation": self.cancellation,
            "return": self.returns,
            "refund": self.refunds,
            "parser_errors": self.parser_errors,
            "needs_review": self.needs_review,
            "unusable": self.unusable,
            "write_rows_if_all_applied": (
                len(self.event_rows) + len(self.header_rows) + 2 * len(self.purchases)
            ),
        }


def fixed_amazon_window(start: datetime, end: datetime) -> FixedSourceWindow:
    start = start.astimezone(JST).replace(microsecond=0)
    end = end.astimezone(JST).replace(microsecond=0)
    query = f"{SOURCE_QUERY} after:{int(start.timestamp())} before:{int(end.timestamp())}"
    window = FixedSourceWindow(start, end, "Asia/Tokyo", query)
    window.validate()
    return window


def build_incremental_amazon_window(
    policy: AmazonRecurringAuthority,
    state: SqliteRecurringRunState,
    now: datetime,
) -> FixedSourceWindow:
    policy.validate()
    end = now.astimezone(JST).replace(microsecond=0)
    checkpoint = state.successful_window_end()
    boundary = checkpoint.astimezone(JST) if checkpoint else policy.initial_start.astimezone(JST)
    start = boundary - timedelta(seconds=policy.overlap_seconds)
    if start >= end:
        raise RuntimeError("amazon_incremental_window_not_started")
    if (end - start).total_seconds() > policy.max_window_seconds:
        raise RuntimeError("amazon_incremental_window_exceeds_authority")
    return fixed_amazon_window(start, end)


def fetch_bounded_amazon_messages(service, window: FixedSourceWindow, max_messages: int):
    if not (1 <= max_messages <= MAX_AUTHORITY_MESSAGES):
        raise ValueError("amazon_max_messages_invalid")
    api = service.users().messages()
    response = api.list(
        userId="me", q=window.query_representation, maxResults=max_messages,
    ).execute()
    items = response.get("messages", [])
    messages = []
    for item in items:
        message_id = str(item.get("id") or "")
        if not message_id:
            continue
        raw = api.get(userId="me", id=message_id, format="raw").execute()
        from .amazon_gmail_preview import _raw_bytes
        messages.append(GmailRawMessage(
            gmail_message_id=message_id,
            thread_id=str(raw.get("threadId") or item.get("threadId") or ""),
            raw_mime=_raw_bytes(str(raw.get("raw") or "")),
        ))
    return messages, "nextPageToken" not in response


def _text(row: list, index: int) -> str:
    return str(row[index]).strip() if len(row) > index else ""


def _event_row(
    event: AmazonMailEvent, message: GmailRawMessage, timestamp: str,
    *, force_review: bool = False,
) -> tuple:
    row = amazon_stored_event_from_mail(event, message, timestamp=timestamp).to_row()
    if force_review or event.event_type in {"cancellation", "return", "refund"}:
        row[20] = "review"
    elif event.event_type in {"shipment", "delivery"}:
        row[20] = "no_action"
    elif event.event_type == "unknown" and (event.order_id or event.order_amount is not None):
        row[20] = "review"
    return tuple(row)


def build_amazon_write_plan(
    messages: Iterable[GmailRawMessage], db, *, window: FixedSourceWindow,
    collection_complete: bool = True, timestamp: str | None = None,
) -> AmazonWritePlan:
    raw_messages = list(messages)
    timestamp = timestamp or datetime.now(JST).isoformat(timespec="seconds")
    identities = db.amazon_event_identity_index()
    gmail_ids = set(identities["gmail_message_ids"])
    rfc_ids = set(identities["rfc_message_ids"])
    hashes = set(identities["source_hashes"])
    new_rows: list[tuple] = []
    duplicate_events = 0
    parsed: list[AmazonMailEvent] = []
    seen_run: set[tuple[str, str]] = set()
    needs_review = unusable = parser_errors = 0

    for message in raw_messages:
        parse_failed = False
        try:
            event = parse_amazon_email(message.raw_mime)
        except Exception:
            event = _unparsed_event(message.raw_mime)
            parse_failed = True
            parser_errors += 1
            needs_review += 1
        parsed.append(event)
        rfc = str(event.message_id or "")
        identity = (message.gmail_message_id, event.source_hash)
        duplicate = (
            identity in seen_run or message.gmail_message_id in gmail_ids
            or bool(rfc and rfc in rfc_ids) or event.source_hash in hashes
        )
        if duplicate:
            duplicate_events += 1
        else:
            new_rows.append(_event_row(event, message, timestamp, force_review=parse_failed))
            gmail_ids.add(message.gmail_message_id)
            if rfc:
                rfc_ids.add(rfc)
            hashes.add(event.source_hash)
        seen_run.add(identity)
        if event.event_type == "unknown" and not parse_failed:
            if event.order_id or event.order_amount is not None:
                needs_review += 1
            else:
                unusable += 1

    import_rows_by_id = {
        _text(list(row), 0): list(row) for row in db.get("取込データ!A2:L") if row
    }
    expense_rows_by_id = {
        _text(list(row), 0): list(row) for row in db.get("支出明細!A2:M") if row
    }
    detail_orders = {_text(list(row), 1) for row in db.get("Amazon注文!A2:O") if row}
    header_map = {
        _text(list(row), 0): list(row)
        for row in db.get("Amazon注文ヘッダ!A2:O") if row and _text(list(row), 0)
    }
    stored_risky_orders = {
        _text(list(row), 1)
        for row in db.get("Amazonイベント!F2:G")
        if _text(list(row), 0) in {"cancellation", "return", "refund"}
        and _text(list(row), 1)
    }
    grouped: dict[str, list[AmazonMailEvent]] = {}
    for event in parsed:
        if event.order_id:
            grouped.setdefault(event.order_id, []).append(event)

    purchases: list[AmazonPurchaseCandidate] = []
    header_rows: list[tuple] = []
    already_present = duplicate_order_messages = 0
    cancellation = sum(event.event_type == "cancellation" for event in parsed)
    returns = sum(event.event_type == "return" for event in parsed)
    refunds = sum(event.event_type == "refund" for event in parsed)

    for order_id, events in sorted(grouped.items()):
        order_events = [event for event in events if event.event_type == "order"]
        if not order_events:
            continue
        duplicate_order_messages += max(0, len(order_events) - 1)
        if any(event.event_type in {"cancellation", "return", "refund"} for event in events):
            continue
        if order_id in stored_risky_orders:
            continue
        existing_header = header_map.get(order_id, [])
        if _text(existing_header, 5).lower() == "cancelled" or _text(existing_header, 7).lower() in {"partial", "full"}:
            continue
        complete = [
            event for event in order_events
            if event.event_date and event.order_amount is not None and event.order_amount > 0
        ]
        values = {(event.event_date, event.order_amount) for event in complete}
        if len(values) != 1:
            needs_review += 1
            continue
        chosen = complete[0]
        candidate = AmazonPurchaseCandidate(
            order_id=order_id, order_date=str(chosen.event_date),
            amount=int(chosen.order_amount), payment_method=str(chosen.payment_method or ""),
            item_count=chosen.item_count,
        )
        if order_id in detail_orders:
            already_present += 1
            continue
        has_import = candidate.import_id in import_rows_by_id
        has_expense = candidate.expense_id in expense_rows_by_id
        if has_import and has_expense:
            already_present += 1
            continue
        # A previous Sheets request may have succeeded before a later request failed.
        # Keep a half-written pair eligible so the apply path can validate and repair it.
        purchases.append(candidate)
        if order_id not in header_map:
            header_rows.append(tuple(AmazonOrderHeader(
                order_id=order_id, order_date=candidate.order_date,
                order_amount=candidate.amount, payment_method=candidate.payment_method or None,
                item_count=candidate.item_count, order_status="ordered", charged_amount=None,
                refund_status="none", refund_amount=None, shipment_amount=None,
                gift_card_amount=chosen.gift_card_amount, points_amount=chosen.points_amount,
                discount_amount=chosen.discount_amount, source="gmail", last_updated_at=timestamp,
            ).to_row()))

    return AmazonWritePlan(
        window=window, fetched=len(raw_messages), collection_complete=collection_complete,
        event_rows=tuple(new_rows), purchases=tuple(purchases), header_rows=tuple(header_rows),
        duplicate_events=duplicate_events, duplicate_order_messages=duplicate_order_messages,
        already_present=already_present, cancellation=cancellation, returns=returns,
        refunds=refunds, needs_review=needs_review, unusable=unusable,
        parser_errors=parser_errors,
    )


def _purchase_rows(candidate: AmazonPurchaseCandidate, timestamp: str):
    raw = {
        "order_id": candidate.order_id, "date": candidate.order_date,
        "amount": candidate.amount, "source": "amazon_gmail",
    }
    import_row = [
        candidate.import_id, timestamp, "Amazon", candidate.order_id,
        candidate.order_date, "Amazon.co.jp", candidate.amount,
        candidate.payment_method, "canonical_amazon", candidate.expense_id,
        canonical_hash(raw), "Amazon Gmail注文合計を1支出として計上",
    ]
    expense_row = [
        candidate.expense_id, candidate.order_date, "Amazon.co.jp", "Amazon注文",
        candidate.amount, "その他", "未分類", candidate.payment_method,
        "Amazon", "", candidate.import_id, "Amazon Gmail注文合計", "active",
    ]
    return import_row, expense_row


def apply_amazon_write_plan(
    db, plan: AmazonWritePlan, *, max_purchases: int, timestamp: str | None = None,
) -> dict[str, object]:
    if not plan.collection_complete:
        raise RuntimeError("amazon_gmail_collection_incomplete")
    if not (1 <= max_purchases <= MAX_AUTHORITY_PURCHASES):
        raise ValueError("amazon_apply_limit_invalid")
    selected = list(plan.purchases[:max_purchases])
    timestamp = timestamp or now_jst_string()
    current_import_rows = {
        _text(list(row), 0): list(row) for row in db.get("取込データ!A2:L") if row
    }
    current_expense_rows = {
        _text(list(row), 0): list(row) for row in db.get("支出明細!A2:M") if row
    }
    current_headers = {_text(list(row), 0) for row in db.get("Amazon注文ヘッダ!A2:O") if row}
    identities = db.amazon_event_identity_index()
    event_rows = [
        list(row) for row in plan.event_rows
        if _text(list(row), 1) not in identities["gmail_message_ids"]
        and (not _text(list(row), 2) or _text(list(row), 2) not in identities["rfc_message_ids"])
        and _text(list(row), 4) not in identities["source_hashes"]
    ]
    import_rows = []
    expense_rows = []
    applied: list[AmazonPurchaseCandidate] = []
    for candidate in selected:
        import_row, expense_row = _purchase_rows(candidate, timestamp)
        existing_import = current_import_rows.get(candidate.import_id)
        existing_expense = current_expense_rows.get(candidate.expense_id)
        if existing_import and not (
            _text(existing_import, 3) == candidate.order_id
            and _text(existing_import, 4) == candidate.order_date
            and _text(existing_import, 6) == str(candidate.amount)
            and _text(existing_import, 8) == "canonical_amazon"
        ):
            raise RuntimeError("amazon_import_identity_conflict")
        if existing_expense and not (
            _text(existing_expense, 1) == candidate.order_date
            and _text(existing_expense, 4) == str(candidate.amount)
            and _text(existing_expense, 10) == candidate.import_id
            and _text(existing_expense, 12) in {"", "active"}
        ):
            raise RuntimeError("amazon_expense_identity_conflict")
        if not existing_import:
            import_rows.append(import_row)
        if not existing_expense:
            expense_rows.append(expense_row)
        if existing_import and existing_expense:
            continue
        applied.append(candidate)
    header_rows = [
        list(row) for row in plan.header_rows
        if _text(list(row), 0) not in current_headers
        and _text(list(row), 0) in {item.order_id for item in applied}
    ]

    requests = 0
    if import_rows:
        db.append("取込データ", import_rows); requests += 1
    if expense_rows:
        db.append("支出明細", expense_rows); requests += 1
    if event_rows:
        db.append("Amazonイベント", event_rows); requests += 1
    if header_rows:
        db.append("Amazon注文ヘッダ", header_rows); requests += 1

    after_imports = {_text(list(row), 0) for row in db.get("取込データ!A2:L") if row}
    after_expenses = {_text(list(row), 0) for row in db.get("支出明細!A2:M") if row}
    after_headers = {_text(list(row), 0) for row in db.get("Amazon注文ヘッダ!A2:O") if row}
    after_events = db.amazon_event_identity_index()["gmail_message_ids"]
    exact = (
        all(item.import_id in after_imports and item.expense_id in after_expenses for item in applied)
        and all(_text(row, 0) in after_headers for row in header_rows)
        and all(_text(row, 1) in after_events for row in event_rows)
    )
    if not exact:
        raise RuntimeError("amazon_post_write_readback_mismatch")
    return {
        "status": "complete" if requests else "noop", "selected_purchases": len(selected),
        "written_purchases": len(applied), "event_rows_written": len(event_rows),
        "header_rows_written": len(header_rows), "import_rows_written": len(import_rows),
        "expense_rows_written": len(expense_rows), "write_requests": requests,
        "exact_readback": exact,
        "selected_targets": [item.reference for item in applied],
    }


def run_amazon_recurring(
    *, gmail_service, db, state: SqliteRecurringRunState,
    authority_provider: ProtectedAmazonAuthorityProvider, now: datetime,
    dry_run: bool = False, apply_limit: int | None = None,
) -> dict[str, object]:
    policy = authority_provider.load()
    if not (policy.valid_from <= now < policy.expires_at):
        raise RuntimeError("amazon_authority_expired_or_not_started")
    if db.sid != policy.expected_spreadsheet_id:
        raise RuntimeError("amazon_target_spreadsheet_mismatch")
    window = build_incremental_amazon_window(policy, state, now)
    messages, complete = fetch_bounded_amazon_messages(
        gmail_service, window, policy.max_messages,
    )
    plan = build_amazon_write_plan(messages, db, window=window, collection_complete=complete)
    run_id = str(uuid4())
    summary = {"schema_version": 1, "run_id": run_id, **plan.anonymized()}
    summary.update({
        "written_purchases": 0, "write_requests": 0,
        "checkpoint_advanced": False, "failure": 0,
    })
    if not complete:
        summary.update(
            status="failed", failure=1,
            failure_reason="amazon_gmail_collection_incomplete",
        )
        state.record(summary, advance_checkpoint=False)
        return summary
    if apply_limit is not None and not (1 <= apply_limit <= policy.max_purchases):
        raise ValueError("amazon_apply_limit_exceeds_authority")
    limit = apply_limit if apply_limit is not None else policy.max_purchases
    if dry_run:
        summary["status"] = "dry_run_ready" if plan.purchases or plan.event_rows else "dry_run_noop"
        state.record(summary, advance_checkpoint=False)
        return summary
    result = apply_amazon_write_plan(db, plan, max_purchases=limit)
    summary.update(result)
    fully_applied = len(plan.purchases) <= limit
    summary["checkpoint_advanced"] = fully_applied
    state.record(summary, advance_checkpoint=fully_applied)
    return summary
