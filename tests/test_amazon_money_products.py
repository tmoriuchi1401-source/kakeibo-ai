from copy import deepcopy
import re

import pytest

from app.amazon_money import MoneyItem
from app.amazon_money_products import SavedProducts
from test_amazon_money import record, setup


ORDER = "111-1111111-1111111"


def product(asin="B000000001", amount=600, category=("食費", "食品")):
    return [ORDER + "|" + asin, ORDER, asin, "2001-01-01", "商品" + asin, 2, amount,
            "保存済み支払方法", *category, "baseline", "saved-hash"]


class Source:
    sid = "source"

    def __init__(self, products=None):
        self.tables = {"Amazon注文": products if products is not None else {2:product(), 1002:product("B000000002", 400)},
            "カテゴリ": {2:["食費", "食品"], 3:["日用品", "消耗品"]},
            "商品マスタ": {2:["B000000002", "保存済み商品名", "日用品", "消耗品"]}}
        self.reads = []
        self.meta_reads = 0
        self.fail = False
        self.svc = self

    def spreadsheets(self):
        return self

    def get(self, **kwargs):
        self.meta_reads += 1
        return {"sheets": [{"properties": {"title": title, "gridProperties": {"rowCount": max(rows, default=1)}}}
            for title, rows in self.tables.items()]}

    def _execute_sheet_read(self, operation):
        return operation()

    def get_raw(self, a1):
        self.reads.append(a1)
        if self.fail:
            raise OSError("optional_source_unavailable")
        title, start, column, end = re.fullmatch(r"'(.+)'!A(\d+):([A-Z])(\d+)", a1).groups()
        rows = [self.tables[title].get(n, [])[:ord(column) - 64] for n in range(int(start), int(end) + 1)]
        while rows and not rows[-1]:
            rows.pop()
        return deepcopy(rows)


def test_saved_rows_are_supplement_only_and_quantity_is_not_multiplied():
    source = Source()
    source.tables["Amazon注文"][1002][8:10] = ["", ""]
    provider = SavedProducts(source)
    original = record(order_id=ORDER)
    assert provider(original) == (MoneyItem("商品B000000001",600,"食費","食品"),
        MoneyItem("商品B000000002",400,"日用品","消耗品"))
    assert original.items == ()
    assert source.reads[:2] == ["'Amazon注文'!A2:B1001", "'Amazon注文'!A1002:B1002"]
    count = len(source.reads)
    assert provider(original) and len(source.reads) == count and source.meta_reads == 1
    assert not any("イベント" in r or "ヘッダ" in r or "支出明細" in r for r in source.reads)


@pytest.mark.parametrize("change", ["total", "negative", "duplicate", "key", "name", "quantity", "fraction", "moved"])
def test_missing_or_ambiguous_details_do_not_prevent_confirmed_posting(change):
    source = Source()
    rows = source.tables["Amazon注文"]
    if change == "total": rows[2][6] = 599
    elif change == "negative": rows[2][6], rows[1002][6] = -100, 1100
    elif change == "duplicate": rows[3] = deepcopy(rows[2])
    elif change == "key": rows[2][0] = "unrelated-key"
    elif change == "name": rows[2][4] = ""
    elif change == "quantity": rows[2][5] = 0
    elif change == "fraction": rows[2][5] = 1.5
    else:
        old = source.get_raw
        source.get_raw = lambda a1: [] if a1 == "'Amazon注文'!A2:L2" else old(a1)
    store, ledger, writer = setup()
    writer.product_details = SavedProducts(source)
    result = writer.apply([record(order_id=ORDER)],limit=1)
    assert result["money_posted"] == 1 and result["expense_rows_written"] == 1
    row = next(iter(ledger.rows["支出明細"].values()))
    assert row[3:7] == ["Amazon／未分類", 1000, "その他", "未分類"]


def test_same_date_amount_does_not_supply_a_missing_order_relationship():
    source = Source()
    provider = SavedProducts(source)
    assert provider(record()) == () and source.meta_reads == 0
    assert provider(record(order_id="other")) == ()
    assert provider(record(order_id=ORDER, amount=600)) == ()  # No partial allocation by amount.
    assert provider(record(order_id=ORDER, amount=-1000, kind="refund")) == ()


def test_optional_source_failure_and_absent_tabs_still_post():
    for failure in ("read", "missing"):
        source = Source()
        if failure == "read": source.fail = True
        else: source.tables = {}
        _, ledger, writer = setup()
        writer.product_details = SavedProducts(source)
        assert writer.apply([record(order_id=ORDER)],limit=1)["money_posted"] == 1
        assert len(ledger.rows["支出明細"]) == 1


def test_existing_category_wins_and_ambiguous_master_is_unclassified():
    source = Source()
    source.tables["商品マスタ"][3] = ["B000000001", "wrong", "日用品", "消耗品"]
    source.tables["商品マスタ"][4] = deepcopy(source.tables["商品マスタ"][2])
    source.tables["Amazon注文"][1002][8:10] = ["unknown", "unknown"]
    details = SavedProducts(source)(record(order_id=ORDER))
    assert (details[0].major, details[0].minor) == ("食費", "食品")
    assert (details[1].major, details[1].minor) == ("その他", "未分類")


def test_split_details_keep_authority_identity_and_freeze_across_unknown_write_response():
    source = Source()
    store, ledger, writer = setup()
    writer.product_details = SavedProducts(source)
    value = record(order_id=ORDER)
    assert writer.preview([value])[0].record.items
    assert ledger.calls == [] and store.data["money"]["records"] == {}
    ledger.fail_after = "取込データ"
    with pytest.raises(RuntimeError,match="lost_response"):
        writer.apply([value],limit=1)
    pending = store.data["money"]["records"][value.money_id]
    assert pending["fingerprint"] == value.fingerprint and pending["state"] == "pending"
    source.tables["Amazon注文"][2][4] = "later change"
    writer.product_details = lambda value: (_ for _ in ()).throw(AssertionError("must not re-read"))
    assert writer.apply([value],limit=1)["expense_rows_written"] == 2
    assert sum(r[4] for r in ledger.rows["支出明細"].values()) == 1000
    assert all(r[1] == value.day and r[10] == value.money_id for r in ledger.rows["支出明細"].values())
    assert all(r[3] != "later change" for r in ledger.rows["支出明細"].values())
    before = deepcopy(ledger.rows)
    assert writer.apply([value],limit=1)["expense_rows_written"] == 0 and ledger.rows == before


def test_dedup_review_and_legacy_link_do_not_retrieve_products():
    _, _, writer = setup()
    writer.possible_duplicates = lambda r: True
    writer.product_details = lambda r: (_ for _ in ()).throw(AssertionError("no product lookup for review"))
    assert writer.apply([record(order_id=ORDER)],limit=1)["money_review"] == 1
    assert writer.apply([record(source="amazon",order_id=ORDER)],limit=1)["money_supplement"] == 1


def test_full_refund_reuses_exact_original_products_without_changing_purchase():
    _, ledger, writer = setup()
    value = record(items=(MoneyItem("一",600,"食費","食品"),MoneyItem("二",400,"日用品","消耗品")))
    writer.apply([value],limit=1)
    before = deepcopy(ledger.rows["支出明細"])
    refund = record(source_id="refund",reference="transaction:refund",kind="refund",amount=-1000,
                    day="2026-10-01",related_id=value.money_id)
    assert writer.apply([refund],limit=1)["expense_rows_written"] == 2
    actual = [r for r in ledger.rows["支出明細"].values() if r[10] == refund.money_id]
    assert [(r[3],r[4],r[5],r[6]) for r in actual] == [("一",-600,"食費","食品"),("二",-400,"日用品","消耗品")]
    assert all(r[1] == "2026-10-01" for r in actual)
    assert all(ledger.rows["支出明細"][k] == v for k,v in before.items())
    assert writer.apply([refund],limit=1)["expense_rows_written"] == 0


def test_partial_refund_does_not_guess_product_allocation():
    _, ledger, writer = setup()
    value = record(items=(MoneyItem("一",600),MoneyItem("二",400)))
    writer.apply([value],limit=1)
    refund = record(source_id="refund",reference="transaction:refund",kind="refund",amount=-400,related_id=value.money_id)
    assert writer.apply([refund],limit=1)["expense_rows_written"] == 1
    actual = [r for r in ledger.rows["支出明細"].values() if r[10] == refund.money_id]
    assert actual[0][3:5] == ["Amazon／未分類",-400]


@pytest.mark.parametrize("scenario", ["enabled", "disabled", "conflict", "future", "inactive"])
def test_saved_approved_product_rules_keep_opt_in_and_exact_identity(scenario, monkeypatch):
    from app.category_rules import RULE_SHEET
    monkeypatch.setattr("app.utils.now_jst_string", lambda: "2026-09-20 12:00:00")
    source = Source()
    source.tables["Amazon注文"][2][8:10] = ["", ""]
    approved = ["CR-synthetic", "product", "Amazon", "", "Amazon.co.jp", "Amazon.co.jp",
        "amazon:B000000001", "商品B000000001", "", "日用品", "消耗品", "old-expense", "2026-09-01 12:00:00", 1, True]
    source.tables[RULE_SHEET] = {2:approved}
    if scenario == "conflict":
        other = deepcopy(approved)
        other[0], other[9], other[10] = "CR-conflict", "食費", "食品"
        source.tables[RULE_SHEET][3] = other
    if scenario == "future": approved[12] = "2027-01-01 12:00:00"
    if scenario == "inactive": approved[14] = False
    items = SavedProducts(source,auto_apply=scenario != "disabled")(record(order_id=ORDER))
    assert (items[0].major,items[0].minor) == (("日用品","消耗品") if scenario == "enabled" else ("その他","未分類"))
    assert (items[1].major,items[1].minor) == ("食費","食品")
    if scenario == "disabled":
        assert not any(RULE_SHEET in r for r in source.reads)


def test_production_factory_connects_confirmed_card_adapter_to_saved_products(monkeypatch):
    from app.amazon_money_mail import card_money_from_mail
    from app.amazon_money_runtime import money_writer, run_money_records
    from test_amazon_money import Ledger, book, mail
    from test_projection_refresh import initialized, PAIRS
    source = Source()
    source.categories = lambda: PAIRS
    store, reader, _ = initialized([])
    store.data["money"] = book()
    ledger = Ledger()
    monkeypatch.setattr("app.projection_store.store_from_environment", lambda *args:store)
    monkeypatch.setattr("app.monthly_projection_sheets.SheetsLedgerReader", lambda db:reader)
    monkeypatch.setattr("app.amazon_money_runtime.MoneyLedger", lambda db:ledger)
    writer = money_writer(source,{"KAKEIBO_AMAZON_MONEY_MODE":"confirmed-v1"})
    raw = mail("【ご利用詳細】au PAY カード", "本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年9月20日\n▼ご利用先\nAMAZON.CO.JP " + ORDER + "\n▼ご利用金額\n1000円",
               sender="info@kddi-fs.com")
    records, reviews = card_money_from_mail(raw,gmail_id="synthetic")
    assert len(records) == 1 and reviews == 0 and records[0].order_id == ORDER
    assert run_money_records(writer,records,dry_run=True,limit=1)["money_eligible"] == 1
    assert ledger.calls == []
    assert run_money_records(writer,records,dry_run=False,limit=1)["expense_rows_written"] == 2
    assert {r[3] for r in ledger.rows["支出明細"].values()} == {"商品B000000001","商品B000000002"}
    writer.verify_posted(records[0])
    assert run_money_records(writer,records,dry_run=False,limit=1)["expense_rows_written"] == 0
