"""Independent readback arithmetic; report booleans, never financial rows."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import math
import re

from .review_pipeline import is_reviewable_status
from .sheets_ui import CAP, SPREADSHEET_ID


def month_key(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (date(1899, 12, 30) + timedelta(days=math.floor(value))).strftime("%Y-%m")
        raw = str(value).strip().replace("/", "-")[:10]
        parts = raw.split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2])).strftime("%Y-%m")
    except (ValueError, TypeError, OverflowError, IndexError):
        return None


def amount_value(value):
    if isinstance(value, bool): return None
    try:
        result = Decimal(re.sub(r"[,¥￥円\s]", "", str(value)))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def source_summary(view, imports, amazon, month):
    categories = defaultdict(Decimal)
    unclassified_count = invalid = 0
    for raw in view:
        row = list(raw) + [""] * max(0, 10-len(raw))
        if not row[9]: continue
        key, amount = month_key(row[0]), amount_value(row[3])
        if key is None or amount is None: invalid += 1
        if key != month: continue
        major, minor = str(row[4]).strip(), str(row[5]).strip()
        category = "未分類" if major in {"", "未分類"} or minor in {"", "未分類"} else major
        categories[category] += amount if amount is not None else Decimal(0)
        if category == "未分類": unclassified_count += 1
    return {
        "total": sum(categories.values(), Decimal(0)), "categories": dict(categories),
        "unclassified_count": unclassified_count,
        "unclassified_amount": categories.get("未分類", Decimal(0)), "invalid": invalid,
        "regular": sum(bool(r and r[0]) and len(r)>8 and is_reviewable_status(str(r[8])) for r in imports),
        "amazon": sum(bool(r and r[0]) and (len(r)<2 or str(r[1]) != "反映済み") for r in amazon),
    }


def verify_home(service, meta):
    def values(rng):
        return service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=rng,
            valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
    home = values("'ホーム'!A1:B35")
    value = lambda r, c: home[r-1][c-1] if len(home)>=r and len(home[r-1])>=c else ""
    month = month_key(value(3, 2))
    rows = {}
    overflow = 0
    for title, cols, id_col in [("支出一覧", "J", 9), ("取込データ", "L", 0), ("Amazon要確認", "N", 0)]:
        p = next(s["properties"] for s in meta["sheets"] if s["properties"]["title"] == title)
        n = p["gridProperties"]["rowCount"]
        rows[title] = values(f"'{title}'!A2:{cols}{min(n,CAP+1)}")
        for start in range(CAP+2, n+1, CAP):
            tail = values(f"'{title}'!{chr(65+id_col)}{start}:{chr(65+id_col)}{min(n,start+CAP-1)}")
            overflow += sum(bool(r and r[0]) for r in tail)
    expected = source_summary(rows["支出一覧"], rows["取込データ"], rows["Amazon要確認"], month)
    cells = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID,
        ranges=[f"'ホーム'!A1:H{CAP+1}"], fields="sheets(data(rowData(values(effectiveValue(errorValue)))))").execute()
    errors = sum("errorValue" in c.get("effectiveValue", {}) for s in cells.get("sheets", [])
                 for b in s.get("data", []) for r in b.get("rowData", []) for c in r.get("values", []))
    category_rows = values(f"'ホーム'!A35:B{CAP+1}")
    category_total = sum((amount_value(r[1]) or Decimal(0) for r in category_rows if len(r)>1), Decimal(0))
    return {
        "formula_errors": errors,
        "month_valid": month is not None,
        "total_matches_view": amount_value(value(6, 1)) == expected["total"] if expected["invalid"]+overflow == 0 else value(6, 1) == "要データ確認",
        "unclassified_matches": amount_value(value(8, 2)) == expected["unclassified_count"] and amount_value(value(9, 2)) == expected["unclassified_amount"],
        "review_counts_match": amount_value(value(12, 2)) == expected["regular"] and amount_value(value(13, 2)) == expected["amazon"],
        "category_total_matches_view": category_total == expected["total"],
        "data_check_matches": amount_value(value(17, 2)) == expected["invalid"]+overflow,
        "source_invalid_rows": expected["invalid"], "overflow_rows": overflow,
    }
