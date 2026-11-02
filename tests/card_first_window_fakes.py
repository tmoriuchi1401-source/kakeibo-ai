"""Invented first-window mail and read-only API fixtures; no live network."""
import base64
from copy import deepcopy
import re
import pytest
from test_amazon_money import mail
from test_aupay_card_window_preview import START


class Request:
    method = "GET"
    def __init__(self, value): self.value = value
    def execute(self, **kwargs): return deepcopy(self.value)


class Gmail:
    def __init__(self, raws):
        self.raws, self.gets, self.lists = raws, [], []
        self.missing_raw = False
        self.stamp = str(int(START.timestamp())*1000+1)
    def users(self): return self
    def messages(self): return self
    def list(self, **kwargs):
        self.lists.append(kwargs)
        start = int(kwargs.get("pageToken") or 0)
        end = min(start+kwargs["maxResults"], len(self.raws))
        result = {"messages": [{"id": f"g{i}"} for i in range(start, end)]}
        if end < len(self.raws): result["nextPageToken"] = str(end)
        return Request(result)
    def get(self, **kwargs):
        self.gets.append(kwargs["id"])
        return Request({"id": kwargs["id"], "internalDate": self.stamp,
                        "raw": "" if self.missing_raw else self.raws[int(kwargs["id"][1:])]})


class DB:
    sid = "sheet-production"
    def __init__(self): self.rows, self.expenses = [], []; self.svc = self
    def spreadsheets(self): return self
    def get(self, rng=None, **kwargs):
        if rng is None:
            return Request({"sheets": [{"properties": {"title": title,
                "gridProperties": {"rowCount": len(rows)+1}}}
                for title, rows in (("取込データ", self.rows), ("支出明細", self.expenses))]})
        if rng == "取込データ!A2:L": return deepcopy(self.rows)
        if rng == "Amazon注文!A2:M": return []
        raise AssertionError(rng)
    def _execute_sheet_read(self, factory): return factory().execute()
    def get_raw(self, rng):
        title, bounds = rng.split("!")
        match = re.fullmatch(r"([A-Z])(\d+):([A-Z])(\d+)", bounds)
        assert match
        first, a, last, b = match.groups()
        rows = self.rows if title.strip("'") == "取込データ" else self.expenses
        return [row[ord(first)-65:ord(last)-64] for row in rows[int(a)-2:int(b)-1]]
    def append_raw(self, *_): pytest.fail("financial_append_forbidden")


def encoded(index, amount, money=True):
    body = ("本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年10月20日\n"
            f"▼ご利用先\n{'AMAZON.CO.JP' if money else '合成店舗'+str(index)}\n▼ご利用金額\n{amount}円")
    raw = mail("【ご利用詳細】au PAY カード", body, sender="info@kddi-fs.com",
               message_id=f"<synthetic-audit-{index}@example.invalid>")
    return base64.urlsafe_b64encode(raw).decode()
