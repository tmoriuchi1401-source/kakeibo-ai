import base64
from email.message import EmailMessage
import sys

from app import cli
from app.amazon_cancellation_item_count_fill_preview import (
    apply_amazon_cancellation_item_count_fills,
    plan_cancellation_item_count_fill,
    preview_amazon_cancellation_item_count_fills,
)


ORDER_ID = "123-1234567-1234567"


def _plan(**overrides):
    values = {
        "event_type": "cancellation",
        "saved_item_count": "",
        "saved_order_id": ORDER_ID,
        "parsed_order_id": ORDER_ID,
        "header_match_count": 1,
        "parsed_quantity": 2,
        "quantity_candidates": (2,),
        "order_total_quantity": 2,
    }
    values.update(overrides)
    return plan_cancellation_item_count_fill(**values)


def test_safe_cancellation_proposes_only_r_column_item_count():
    plan = _plan()

    assert plan.disposition == "fill"
    assert plan.reason == "safe_item_count_fill"
    assert len(plan.proposed_changes) == 1
    change = plan.proposed_changes[0]
    assert (change.sheet, change.column, change.column_index) == (
        "Amazonイベント", "Item Count", 17,
    )
    assert change.current_value == ""
    assert change.proposed_value == 2
    rendered = repr(plan.proposed_changes)
    for forbidden in (
        "Last Parsed At", "Order ID", "Event Type", "Event Date",
        "Parse Status", "Match Status", "Apply Status", "Parser Version",
        "Amount", "Payment Method", "Amazon注文ヘッダ",
    ):
        assert forbidden not in rendered


def test_non_cancellation_and_existing_item_count_are_skipped():
    assert _plan(event_type="order").reason == "not_cancellation"
    existing = _plan(saved_item_count=1, parsed_quantity=2)
    assert existing.disposition == "skip"
    assert existing.reason == "existing_item_count"
    assert existing.proposed_changes == ()


def test_order_id_safety_failures_are_blocked():
    cases = (
        ({"saved_order_id": ""}, "missing_saved_order_id"),
        ({"parsed_order_id": None}, "missing_parsed_order_id"),
        ({"parsed_order_id": "different"}, "order_id_mismatch"),
        ({"header_match_count": 0}, "order_not_found"),
        ({"header_match_count": 2}, "duplicate_order_header"),
    )
    for overrides, reason in cases:
        plan = _plan(**overrides)
        assert plan.disposition == "blocked"
        assert plan.reason == reason
        assert plan.proposed_changes == ()


def test_quantity_safety_failures_are_blocked():
    cases = (
        ({"parsed_quantity": None, "quantity_candidates": ()}, "missing_parsed_quantity"),
        ({"parsed_quantity": 0, "quantity_candidates": (0,)}, "invalid_parsed_quantity"),
        ({"parsed_quantity": "bad"}, "invalid_parsed_quantity"),
        ({"quantity_candidates": (1, 2)}, "ambiguous_parsed_quantity"),
        ({"order_total_quantity": None}, "missing_order_quantity"),
        ({"parsed_quantity": 3, "quantity_candidates": (3,)}, "quantity_exceeds_order_quantity"),
    )
    for overrides, reason in cases:
        plan = _plan(**overrides)
        assert plan.disposition == "blocked"
        assert plan.reason == reason
        assert plan.proposed_changes == ()


def test_same_quantity_repeated_in_plain_and_html_is_allowed():
    plan = _plan(quantity_candidates=(2, 2))

    assert plan.disposition == "fill"
    assert plan.proposed_changes[0].proposed_value == 2


def _raw(plain, html=None):
    message = EmailMessage()
    message["Subject"] = "商品が正常にキャンセルされました"
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


def _event(event_type="cancellation", item_count=""):
    row = [""] * 24
    row[1] = "gmail-id"
    row[5] = event_type
    row[6] = ORDER_ID
    row[17] = item_count
    return row


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class Messages:
    def __init__(self, raw):
        self.raw = raw

    def get(self, **kwargs):
        encoded = base64.urlsafe_b64encode(self.raw).decode().rstrip("=")
        return Request({"id": kwargs["id"], "threadId": "thread", "raw": encoded})

    def modify(self, **kwargs):
        raise AssertionError("Gmail write must not be used")


class Service:
    def __init__(self, raw):
        self.api = Messages(raw)

    def users(self):
        return self

    def messages(self):
        return self.api


class ReadOnlyDB:
    def __init__(self, events):
        self.events = events

    def get(self, rng):
        return {
            "Amazon注文!A2:O": [["key", ORDER_ID, "asin", "", "", 2]],
            "Amazon注文ヘッダ!A2:O": [[ORDER_ID, "", "", "", 2]],
        }[rng]

    def amazon_event_rows(self):
        return [(index, row) for index, row in enumerate(self.events, 2)]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


class WritableDB(ReadOnlyDB):
    def __init__(self, events):
        super().__init__(events)
        self.writes = []
        self.stale_item_count = None
        self.fail_writes = False

    def get(self, rng):
        if rng.startswith("Amazonイベント!A"):
            row_number = int(rng.split("!A", 1)[1].split(":", 1)[0])
            row = list(self.events[row_number - 2])
            if self.stale_item_count is not None:
                row[17] = self.stale_item_count
            return [row]
        return super().get(rng)

    def update_amazon_event_item_count(self, row_number, value):
        if self.fail_writes:
            raise RuntimeError("write failed")
        self.writes.append((row_number, value))
        self.events[row_number - 2][17] = value


def test_preview_reads_only_and_counts_safe_fill_and_skips():
    raw = _raw(
        f"注文番号: {ORDER_ID}\n数量: 2",
        f"<p>注文番号: {ORDER_ID}</p><p>数量: 2</p>",
    )
    result = preview_amazon_cancellation_item_count_fills(
        Service(raw), ReadOnlyDB([_event(), _event(item_count=2), _event("order")]),
        thread_fetcher=lambda *_: [],
    )

    assert result["cancellation_event_count"] == 2
    assert result["would_fill_item_count"] == 1
    assert result["already_has_item_count"] == 1
    assert result["blocked_count"] == 0
    assert result["skipped_count"] == 2
    assert result["non_cancellation_event_count"] == 1
    assert result["reason_safe_item_count_fill_count"] == 1


def test_cli_uses_read_only_services(monkeypatch, capsys):
    class Settings:
        spreadsheet_id = "sheet"
        gmail_token_json = "token"

        def validate(self, **kwargs):
            assert kwargs == {"need_sheet": True, "need_gmail": True}

    sheets_service = object()
    gmail_service = object()
    db = object()
    monkeypatch.setattr(cli, "Settings", Settings)
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: sheets_service)
    monkeypatch.setattr(cli, "gmail_readonly_service", lambda token: gmail_service)
    monkeypatch.setattr(cli, "SheetsDB", lambda sid, service: db)
    monkeypatch.setattr(
        cli, "preview_amazon_cancellation_item_count_fills",
        lambda gmail, sheets: {"would_fill_item_count": int(
            gmail is gmail_service and sheets is db
        )},
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-item-count-fill-preview"],
    )

    cli.main()

    assert capsys.readouterr().out.strip() == "{'would_fill_item_count': 1}"


def test_apply_updates_only_item_count_and_is_idempotent():
    raw = _raw(f"注文番号: {ORDER_ID}\n数量: 2")
    db = WritableDB([_event()])

    first = apply_amazon_cancellation_item_count_fills(
        Service(raw), db, thread_fetcher=lambda *_: [],
    )
    second = apply_amazon_cancellation_item_count_fills(
        Service(raw), db, thread_fetcher=lambda *_: [],
    )

    assert db.writes == [(2, 2)]
    assert first["eligible_fill_count"] == first["updated_item_count_count"] == 1
    assert second["eligible_fill_count"] == second["updated_item_count_count"] == 0
    assert second["already_has_item_count"] == 1


def test_apply_skips_when_item_count_became_nonblank_after_planning():
    raw = _raw(f"注文番号: {ORDER_ID}\n数量: 2")
    db = WritableDB([_event()])
    db.stale_item_count = 9

    result = apply_amazon_cancellation_item_count_fills(
        Service(raw), db, thread_fetcher=lambda *_: [],
    )

    assert db.writes == []
    assert result["updated_item_count_count"] == 0
    assert result["skipped_existing_item_count_count"] == 1


def test_apply_reports_write_failure_without_other_cell_writes():
    raw = _raw(f"注文番号: {ORDER_ID}\n数量: 2")
    db = WritableDB([_event()])
    db.fail_writes = True

    result = apply_amazon_cancellation_item_count_fills(
        Service(raw), db, thread_fetcher=lambda *_: [],
    )

    assert db.writes == []
    assert result["updated_item_count_count"] == 0
    assert result["error_count"] == 1


def test_apply_cli_requires_explicit_flag_before_initializing(monkeypatch):
    monkeypatch.setattr(
        cli, "Settings",
        lambda: (_ for _ in ()).throw(AssertionError("must not initialize")),
    )
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "amazon-cancellation-item-count-fill-apply"],
    )

    try:
        cli.main()
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("CLI must reject missing --apply")
