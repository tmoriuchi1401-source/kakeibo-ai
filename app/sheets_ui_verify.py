"""Independent readback arithmetic; report booleans, never financial rows."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import math
import re

from .review_pipeline import is_reviewable_status
from .sheets_ui import AUTO_MONTH, CAP, CATEGORY_UI_ID, CATEGORY_UI_ROWS, IDS, SPREADSHEET_ID, home_cells


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


def category_queue_expected(view, ledger, products, categories, month):
    """Independent readback oracle. Never persists suggestions or changes categories."""
    def pair(row, a, b):
        return tuple(str(row[i]).strip() if len(row)>i else "" for i in (a, b))
    def known(p): return all(v and v != "未分類" for v in p)
    allowed = {tuple(map(str, r[:2])) for r in categories if len(r)>=2 and known(r[:2])}
    index = defaultdict(list)
    for i, r in enumerate(ledger, 2):
        if r and r[0]: index[str(r[0]).casefold()].append((i, list(r)+[""]*max(0, 13-len(r))))
    output = []
    for r in view:
        if len(r)<10 or not r[9] or month_key(r[0]) != month or known(pair(r, 4, 5)): continue
        matches = index.get(str(r[9]).casefold(), [])
        source_row, source = matches[0] if len(matches)==1 else (0, None)
        state, current = "元データ確認", "元データ確認"
        if source:
            current = "｜".join(pair(source, 5, 6))
            state = ("一覧更新待ち" if source[12] not in {"", "active"} or month_key(source[1]) != month
                     else "分類済・更新待ち" if known(pair(source, 5, 6)) else "修正可")
        product_pairs = {pair(p, 2, 3) for p in products if len(p)>=4 and str(p[1]).casefold()==str(r[2]).casefold() and known(pair(p, 2, 3))}
        past_pairs = {pair(p, 4, 5) for p in view if len(p)>=10 and p[9] and str(p[1]).casefold()==str(r[1]).casefold() and known(pair(p, 4, 5))}
        suggestion = "候補なし"
        if r[2] not in {"", "自動計上", "手動計上"} and len(product_pairs)==1 and product_pairs <= allowed:
            suggestion = "｜".join(next(iter(product_pairs)))+"\n（商品マスタ）"
        elif r[1] and len(past_pairs)==1 and past_pairs <= allowed:
            suggestion = "｜".join(next(iter(past_pairs)))+"\n（同じ店舗）"
        output.append({"id": str(r[9]), "row": source_row, "state": state, "current": current,
                       "suggestion": suggestion})
    return output


def verify_category_ui(service, meta, view):
    def values(rng):
        return service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=rng,
            valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
    sources = {}
    for title, cols in [("支出明細", "M"), ("商品マスタ", "F"), ("カテゴリ", "B")]:
        sheet = next(s for s in meta["sheets"] if s["properties"]["title"] == title)
        sources[title] = values(f"'{title}'!A2:{cols}{min(CAP+1, sheet['properties']['gridProperties']['rowCount'])}")
    month = month_key((values("'ホーム'!B3") or [[""]])[0][0])
    expected = category_queue_expected(view, sources["支出明細"], sources["商品マスタ"], sources["カテゴリ"], month)
    actual = values(f"'カテゴリ対応'!M2:Q{CAP+1}")
    seen = {str(r[0]): {"row": r[1], "current": r[2], "state": r[3], "suggestion": r[4]}
            for r in actual if len(r)>=5 and r[0]}
    raw = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID,
        ranges=[f"'カテゴリ対応'!A1:Q{CATEGORY_UI_ROWS}"],
        fields="sheets(data(startRow,startColumn,rowData(values(hyperlink,effectiveValue(errorValue)))))").execute()
    errors = sum("errorValue" in c.get("effectiveValue", {}) for s in raw.get("sheets", [])
                 for b in s.get("data", []) for r in b.get("rowData", []) for c in r.get("values", []))
    links = {(b.get("startRow", 0)+i, b.get("startColumn", 0)+j): c["hyperlink"]
             for s in raw.get("sheets", []) for b in s.get("data", [])
             for i, r in enumerate(b.get("rowData", [])) for j, c in enumerate(r.get("values", []))
             if b.get("startColumn", 0)+j == 2 and c.get("hyperlink")}
    by_id = {e["id"]: e for e in expected}
    expected_links = {}
    for i, row in enumerate(actual):
        entry = by_id.get(str(row[0])) if row else None
        if entry and entry["state"] == "修正可":
            source_row = entry["row"]
            expected_links[i+5, 2] = f"#gid={IDS['支出明細']}&range=F{source_row}:G{source_row}"
    return {"category_ui_formula_errors": errors,
        "category_edit_links_match": links == expected_links,
        "category_queue_matches": len(seen)==len(expected) and all(seen.get(e["id"])=={k:v for k,v in e.items() if k != "id"} for e in expected)}


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
        ranges=[f"'ホーム'!A1:I{CAP+1}"], fields="sheets(data(rowData(values(effectiveValue(errorValue)))))").execute()
    errors = sum("errorValue" in c.get("effectiveValue", {}) for s in cells.get("sheets", [])
                 for b in s.get("data", []) for r in b.get("rowData", []) for c in r.get("values", []))
    category_rows = values(f"'ホーム'!A35:B{CAP+1}")
    category_total = sum((amount_value(r[1]) or Decimal(0) for r in category_rows if len(r)>1), Decimal(0))
    selected = value(4, 2)
    home_meta = next(s for s in meta["sheets"] if s["properties"]["title"] == "ホーム")
    automatic = selected in {"", AUTO_MONTH}
    selector_matches = month is not None and (
        home_meta.get("monthState", {}).get("B3") == home_cells()[3, 2] if automatic else month == selected)
    result = {
        "formula_errors": errors,
        "month_valid": month is not None,
        "month_selector_matches": selector_matches,
        "automatic_month": automatic,
        "total_matches_view": amount_value(value(6, 1)) == expected["total"] if expected["invalid"]+overflow == 0 else value(6, 1) == "要データ確認",
        "unclassified_matches": amount_value(value(8, 2)) == expected["unclassified_count"] and amount_value(value(9, 2)) == expected["unclassified_amount"],
        "review_counts_match": amount_value(value(12, 2)) == expected["regular"] and amount_value(value(13, 2)) == expected["amazon"],
        "category_total_matches_view": category_total == expected["total"],
        "data_check_matches": amount_value(value(17, 2)) == expected["invalid"]+overflow,
        "source_invalid_rows": expected["invalid"], "overflow_rows": overflow,
    }
    if any(s["properties"]["sheetId"] == CATEGORY_UI_ID for s in meta["sheets"]):
        result.update(verify_category_ui(service, meta, rows["支出一覧"]))
    return result
