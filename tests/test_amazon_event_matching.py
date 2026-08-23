from copy import deepcopy

from app.amazon_event_matching import AmazonEventMatchingPipeline


def event_row(order_id, match_status="unmatched", *, marker="unchanged"):
    row = [order_id, "2026-08-22", 1000, 1100, "", "", "", "", "", "", "Visa", 1,
           "parsed", match_status]
    return row + ["pending", marker]


class FakeDB:
    def __init__(self, orders, events):
        self.orders = orders
        self.events = events
        self.calls = []
        self.update_calls = []

    def get(self, rng):
        self.calls.append(rng)
        if rng == "Amazon注文!B2:B":
            return [[order_id] for order_id in self.orders]
        if rng == "Amazonイベント!G2:T":
            return [row[:14] for row in self.events]
        raise AssertionError(f"unexpected read: {rng}")

    def update_amazon_event_match_statuses(self, updates):
        self.update_calls.append(list(updates))
        for row_num, status in updates:
            self.events[row_num - 2][13] = status


def test_matches_events_by_order_id_only_and_returns_required_summary():
    events = [
        event_row("111", "unmatched"),
        event_row("999", "unmatched"),
        event_row("", "unmatched"),
        event_row("111", "matched"),
    ]
    db = FakeDB(["111", "111"], events)

    result = AmazonEventMatchingPipeline(db).apply()

    assert [row[13] for row in events] == [
        "matched", "order_not_found", "missing_order_id", "matched",
    ]
    assert result == {
        "total": 4,
        "matched": 2,
        "order_not_found": 1,
        "missing_order_id": 1,
        "updated": 3,
        "unchanged": 1,
    }
    assert db.update_calls == [[
        (2, "matched"), (3, "order_not_found"), (4, "missing_order_id"),
    ]]
    assert "item_unresolved" not in {row[13] for row in events}


def test_rerun_reclassifies_order_not_found_after_order_is_added():
    events = [event_row("222", "order_not_found")]
    db = FakeDB([], events)
    assert AmazonEventMatchingPipeline(db).apply()["unchanged"] == 1
    assert db.update_calls == []

    db.orders.append("222")
    result = AmazonEventMatchingPipeline(db).apply()

    assert events[0][13] == "matched"
    assert result["updated"] == 1
    assert db.update_calls[-1] == [(2, "matched")]


def test_matching_changes_no_other_event_columns_and_reads_no_other_sheets():
    events = [event_row("111", "item_unresolved", marker="keep-me")]
    before = deepcopy(events[0])
    db = FakeDB(["111"], events)

    AmazonEventMatchingPipeline(db).apply()

    assert events[0][:13] == before[:13]
    assert events[0][14:] == before[14:]
    assert events[0][13] == "matched"
    assert db.calls == ["Amazon注文!B2:B", "Amazonイベント!G2:T"]
