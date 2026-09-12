from datetime import datetime, timedelta
from email.message import EmailMessage
import json
from zoneinfo import ZoneInfo

import pytest

import app.amazon_production as production
from app.amazon_email import AmazonMailEvent
from app.amazon_gmail_storage import GmailRawMessage
from app.amazon_production import (
    ProtectedAmazonAuthorityProvider,
    apply_amazon_write_plan,
    build_amazon_write_plan,
    build_incremental_amazon_window,
    fetch_bounded_amazon_messages,
    fixed_amazon_window,
    run_amazon_recurring,
)
from app.aupay_card_recurring import SqliteRecurringRunState


JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 9, 12, 8, 0, tzinfo=JST)
ORDER_ID = "123-1234567-1234567"


def raw_mail(subject, body, *, gmail_id="gmail-1", message_id="<one@example.invalid>"):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Amazon.co.jp <no-reply@amazon.co.jp>"
    message["Date"] = "Fri, 11 Sep 2026 10:00:00 +0900"
    message["Message-ID"] = message_id
    message.set_content(body)
    return GmailRawMessage(gmail_id, "thread-1", message.as_bytes())


def order_mail(*, gmail_id="gmail-1", amount="1,500", order_id=ORDER_ID):
    return raw_mail(
        "Amazon.co.jp ご注文の確認",
        f"注文番号: {order_id}\n注文日: 2026年9月11日\n注文合計: {amount}円\n支払い方法: Visa\n商品点数: 2",
        gmail_id=gmail_id, message_id=f"<{gmail_id}@example.invalid>",
    )


class DB:
    def __init__(self):
        self.sid = "sheet-production"
        self.rows = {
            "取込データ": [], "支出明細": [], "Amazon注文": [],
            "Amazon注文ヘッダ": [], "Amazonイベント": [],
        }
        self.write_calls = []

    def get(self, rng):
        sheet = rng.split("!", 1)[0]
        rows = self.rows[sheet]
        if rng == "Amazonイベント!B2:E":
            return [[*row[1:5]] for row in rows]
        if rng == "Amazonイベント!F2:G":
            return [[row[5], row[6]] for row in rows]
        return rows

    def amazon_event_identity_index(self):
        identities = {"gmail_message_ids": set(), "rfc_message_ids": set(), "source_hashes": set()}
        for row in self.rows["Amazonイベント"]:
            identities["gmail_message_ids"].add(str(row[1]))
            if row[2]: identities["rfc_message_ids"].add(str(row[2]))
            if row[4]: identities["source_hashes"].add(str(row[4]))
        return identities

    def append(self, sheet, rows):
        self.write_calls.append((sheet, len(rows)))
        self.rows[sheet].extend([list(row) for row in rows])


def plan(db=None, messages=None, *, complete=True):
    return build_amazon_write_plan(
        messages or [order_mail()], db or DB(),
        window=fixed_amazon_window(NOW - timedelta(days=1), NOW),
        collection_complete=complete, timestamp=NOW.isoformat(),
    )


def event(event_type, *, order_id=ORDER_ID, date="2026-09-11", amount=1500):
    return AmazonMailEvent(
        event_type=event_type, order_id=order_id, event_date=date,
        charged_amount=None, order_amount=amount if event_type == "order" else None,
        refund_amount=amount if event_type == "refund" else None,
        gift_card_amount=None, points_amount=None, coupon_amount=None,
        discount_amount=None, payment_method="Visa", shipment_amount=None,
        item_count=1, message_id=None, source_hash=f"hash-{event_type}",
        gift_card_used=False, points_used=False,
    )


def test_plan_has_anonymized_bounded_targets_and_stable_ids():
    result = plan()
    assert result.collection_complete is True
    assert len(result.purchases) == len(result.header_rows) == 1
    assert result.purchases[0].import_id == f"amazon:{ORDER_ID}"
    rendered = result.anonymized()
    assert rendered["eligible_purchases"] == 1
    assert rendered["write_rows_if_all_applied"] == 4
    assert ORDER_ID not in json.dumps(rendered)
    assert rendered["purchase_targets"][0]["amount"] == 1500


@pytest.mark.parametrize("risky", ["cancellation", "return", "refund"])
def test_cancel_return_and_refund_are_review_only(monkeypatch, risky):
    events = iter([event("order"), event(risky)])
    monkeypatch.setattr(production, "parse_amazon_email", lambda _raw: next(events))
    result = plan(messages=[order_mail(), order_mail(gmail_id="gmail-2")])
    assert result.purchases == ()
    assert getattr(result, "returns" if risky == "return" else f"{risky}s" if risky == "refund" else risky) == 1
    assert result.event_rows[1][20] == "review"


def test_ambiguous_order_duplicate_and_parser_failure_are_not_applied(monkeypatch):
    db = DB()
    original_parser = production.parse_amazon_email
    monkeypatch.setattr(
        production, "parse_amazon_email",
        lambda _raw: event("order", amount=None),
    )
    ambiguous = plan(db=db, messages=[order_mail()])
    assert ambiguous.purchases == ()
    assert ambiguous.needs_review == 1

    monkeypatch.setattr(production, "parse_amazon_email", original_parser)
    duplicate = plan(db=db, messages=[order_mail(), order_mail()])
    assert duplicate.duplicate_events == 1
    assert duplicate.duplicate_order_messages == 1
    assert len(duplicate.purchases) == 1

    monkeypatch.setattr(production, "parse_amazon_email", lambda _raw: (_ for _ in ()).throw(ValueError("bad")))
    failed = plan(db=db)
    assert failed.parser_errors == failed.needs_review == 1
    assert failed.purchases == ()
    assert failed.event_rows[0][18] == "unusable"
    assert failed.event_rows[0][20] == "review"


def test_stored_risky_event_blocks_later_purchase():
    db = DB()
    db.rows["Amazonイベント"].append([
        "event-old", "old-gmail", "", "thread", "old-hash", "refund", ORDER_ID,
    ] + [""] * 17)
    assert plan(db=db).purchases == ()


def test_apply_is_idempotent_and_repairs_half_written_pair():
    db = DB()
    write_plan = plan(db=db)
    first = apply_amazon_write_plan(db, write_plan, max_purchases=1, timestamp=NOW.isoformat())
    assert first["written_purchases"] == 1
    assert first["exact_readback"] is True
    assert dict(db.write_calls) == {
        "取込データ": 1, "支出明細": 1, "Amazonイベント": 1, "Amazon注文ヘッダ": 1,
    }
    second_plan = plan(db=db)
    second = apply_amazon_write_plan(db, second_plan, max_purchases=1)
    assert second["status"] == "noop"
    assert len(db.rows["取込データ"]) == len(db.rows["支出明細"]) == 1

    partial = DB()
    candidate = write_plan.purchases[0]
    import_row, _ = production._purchase_rows(candidate, NOW.isoformat())
    partial.rows["取込データ"].append(import_row)
    repair_plan = plan(db=partial)
    repaired = apply_amazon_write_plan(partial, repair_plan, max_purchases=1)
    assert repaired["expense_rows_written"] == 1
    assert repaired["import_rows_written"] == 0


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class MessagesAPI:
    def __init__(self, message=None, *, truncated=False):
        self.message = message
        self.truncated = truncated
        self.list_kwargs = None
    def list(self, **kwargs):
        self.list_kwargs = kwargs
        result = {"messages": [] if self.message is None else [{"id": self.message.gmail_message_id}]}
        if self.truncated: result["nextPageToken"] = "more"
        return Request(result)
    def get(self, **_kwargs):
        import base64
        encoded = base64.urlsafe_b64encode(self.message.raw_mime).decode().rstrip("=")
        return Request({"raw": encoded, "threadId": self.message.thread_id})


class Gmail:
    def __init__(self, message=None, *, truncated=False): self.api = MessagesAPI(message, truncated=truncated)
    def users(self): return self
    def messages(self): return self.api


def authority_components(tmp_path, *, max_messages=10):
    repo = tmp_path / "repo"; repo.mkdir(parents=True)
    state_dir = tmp_path / "state"; state_dir.mkdir()
    authority = state_dir / "authority.json"
    authority.write_text(json.dumps({
        "policy_id": "amazon-daily-v1", "source": "amazon_gmail",
        "expected_spreadsheet_id": "sheet-production", "max_messages": max_messages,
        "max_purchases": 3, "overlap_seconds": 7200,
        "max_window_seconds": 3 * 86400,
        "initial_start": (NOW - timedelta(days=1)).isoformat(),
        "valid_from": (NOW - timedelta(days=30)).isoformat(),
        "expires_at": (NOW + timedelta(days=365)).isoformat(),
    }), encoding="utf-8")
    state = SqliteRecurringRunState(state_dir / "recurring.sqlite3", repo_root=repo)
    provider = ProtectedAmazonAuthorityProvider(authority, repo_root=repo)
    return state, provider, DB()


def test_absolute_window_overlap_collection_bound_and_checkpoint(tmp_path):
    state, provider, db = authority_components(tmp_path)
    window = build_incremental_amazon_window(provider.load(), state, NOW)
    assert f"after:{int(window.start.timestamp())}" in window.query_representation
    assert "newer_than:" not in window.query_representation
    gmail = Gmail(order_mail())
    messages, complete = fetch_bounded_amazon_messages(gmail, window, 10)
    assert len(messages) == 1 and complete
    assert gmail.api.list_kwargs["maxResults"] == 10

    dry = run_amazon_recurring(
        gmail_service=Gmail(order_mail()), db=db, state=state,
        authority_provider=provider, now=NOW, dry_run=True,
    )
    assert dry["status"] == "dry_run_ready"
    assert dry["write_requests"] == 0
    assert state.successful_window_end() is None

    applied = run_amazon_recurring(
        gmail_service=Gmail(order_mail()), db=db, state=state,
        authority_provider=provider, now=NOW, apply_limit=1,
    )
    assert applied["status"] == "complete"
    assert applied["checkpoint_advanced"] is True
    assert state.successful_window_end() == NOW
    next_window = build_incremental_amazon_window(provider.load(), state, NOW + timedelta(days=1))
    assert next_window.start == NOW - timedelta(hours=2)


def test_incomplete_collection_and_zero_result_do_not_write(tmp_path):
    state, provider, db = authority_components(tmp_path, max_messages=1)
    failed = run_amazon_recurring(
        gmail_service=Gmail(order_mail(), truncated=True), db=db, state=state,
        authority_provider=provider, now=NOW,
    )
    assert failed["status"] == "failed"
    assert failed["failure"] == 1
    assert db.write_calls == []
    assert state.successful_window_end() is None

    noop = run_amazon_recurring(
        gmail_service=Gmail(), db=db, state=state,
        authority_provider=provider, now=NOW,
    )
    assert noop["status"] == "noop"
    assert noop["write_requests"] == 0
    assert state.successful_window_end() == NOW


def test_canary_is_bound_to_approved_target_and_counts_without_checkpoint(tmp_path):
    state, provider, db = authority_components(tmp_path)
    approved = plan(db=db).purchases[0].reference
    result = run_amazon_recurring(
        gmail_service=Gmail(order_mail()), db=db, state=state,
        authority_provider=provider, now=NOW, apply_limit=1,
        approved_reference=approved, expected_event_rows=1,
        expected_header_rows=1,
    )
    assert result["selected_targets"] == [approved]
    assert result["written_purchases"] == 1
    assert result["checkpoint_advanced"] is False
    assert state.successful_window_end() is None

    drift_state, drift_provider, drift_db = authority_components(tmp_path / "drift")
    with pytest.raises(RuntimeError, match="approved_event_row_count_changed"):
        run_amazon_recurring(
            gmail_service=Gmail(order_mail()), db=drift_db, state=drift_state,
            authority_provider=drift_provider, now=NOW, apply_limit=1,
            approved_reference=approved, expected_event_rows=0,
            expected_header_rows=1,
        )
    assert drift_db.write_calls == []


def test_workflow_is_manual_only_before_canary():
    text = open(".github/workflows/amazon-daily-import.yml", encoding="utf-8").read()
    assert "workflow_dispatch:" in text
    assert "schedule:" not in text
    assert "amazon-production-preview" in text
    assert "amazon-gmail-recurring --apply" in text
    assert "--approved-target" in text
    assert "--expected-event-rows" in text
    assert "cancel-in-progress: false" in text
