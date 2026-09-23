"""Stable whole-row date ordering at the end of the locked production run.

Date serials and literal ISO dates coexist in the canonical ledgers. A temporary
numeric rank column lets Sheets sort both consistently without rewriting dates,
IDs, amounts, formulas, notes or formatting. Its creation, sort and removal are
one atomic batch. Never call this while a writer still holds physical row hints.
"""
from .monthly_projection import ProjectionError, _date
from .projection_store import ProjectionJournal


LEDGERS = {"支出明細": ["支出ID", "日付"], "収入明細": ["収入ID", "入金日"]}


def date_order(rows):
    keys, seen = [], set()
    for raw in rows:
        identity, day = (list(raw) + ["", ""])[:2]
        if not identity and not day:
            keys.append("")
            continue
        if not identity or identity in seen:
            raise ProjectionError("ledger_order_identity_invalid")
        seen.add(identity)
        keys.append(_date(day))
    # Python's stable sort preserves item order within each date/receipt.
    return sorted(range(len(rows)), key=lambda i: keys[i], reverse=True)


def read_dates(db, props):
    rows = []
    title, extent = props["title"], props["gridProperties"]["rowCount"]
    for first in range(1, extent + 1, 2000):
        last = min(first + 1999, extent)
        block = db.get_raw(f"'{title}'!A{first}:B{last}")
        rows.extend([(list(r) + ["", ""])[:2] for r in block])
        rows.extend([["", ""] for _ in range(last - first + 1 - len(block))])
    if not rows or rows[0] != LEDGERS[title]:
        raise ProjectionError("ledger_order_header_changed")
    rows = rows[1:]
    while rows and rows[-1] == ["", ""]:
        rows.pop()
    return rows


def sort_requests(props, order):
    sid, col = props["sheetId"], props["gridProperties"]["columnCount"]
    ranks = [0] * len(order)
    for rank, original in enumerate(order):
        ranks[original] = rank
    return [
        {"appendDimension": {"sheetId": sid, "dimension": "COLUMNS", "length": 1}},
        {"updateCells": {"range": {"sheetId": sid, "startRowIndex": 1,
            "endRowIndex": len(order) + 1, "startColumnIndex": col, "endColumnIndex": col + 1},
            "rows": [{"values": [{"userEnteredValue": {"numberValue": n}}]} for n in ranks],
            "fields": "userEnteredValue"}},
        {"sortRange": {"range": {"sheetId": sid, "startRowIndex": 1,
            "endRowIndex": len(order) + 1, "startColumnIndex": 0, "endColumnIndex": col + 1},
            "sortSpecs": [{"dimensionIndex": col, "sortOrder": "ASCENDING"}]}},
        {"deleteDimension": {"range": {"sheetId": sid, "dimension": "COLUMNS",
            "startIndex": col, "endIndex": col + 1}}},
    ]


def order_ledgers(db, store, *, apply=False):
    meta = db._execute_sheet_read(lambda: db.svc.spreadsheets().get(
        spreadsheetId=db.sid, fields="sheets(properties)"))
    props = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    if not all(title in props for title in LEDGERS):
        raise ProjectionError("ledger_order_sheet_missing")
    plans = []
    for title in LEDGERS:
        rows = read_dates(db, props[title])
        order = date_order(rows)
        if order != list(range(len(rows))):
            plans.append((props[title], rows, order))
    counts = {"ledger_order_sheets": len(plans),
              "ledger_order_rows": sum(len(rows) for _, rows, _ in plans)}
    if not apply or not plans:
        return counts
    if any(p["title"] == "支出明細" for p, _, _ in plans) and store is not None:
        from .category_management import require_settled
        require_settled(store)
        # Persist before sorting, including an unknown Sheets response. A fresh
        # process must rebuild addresses before any fixed-ID correction lookup.
        ProjectionJournal(store).mark(rebuild=True)
    requests = [request for p, _, order in plans for request in sort_requests(p, order)]
    db.svc.spreadsheets().batchUpdate(spreadsheetId=db.sid,
        body={"requests": requests}).execute(num_retries=0)
    for p, rows, order in plans:
        expected = [rows[i] for i in order]
        while expected and expected[-1] == ["", ""]:
            expected.pop()
        if read_dates(db, p) != expected:
            raise ProjectionError("ledger_order_readback_failed")
    return counts


def run_ledger_order(env, *, apply=False):
    from .production_flow import verify_execution_boundary
    from .google_clients import sheets_service, read_only_sheets_service
    from .sheets import SheetsDB
    from .projection_store import store_from_environment
    from .projection_refresh import ProjectionRefresh
    from .monthly_projection_sheets import SheetsLedgerReader
    verify_execution_boundary(env, env.get("GITHUB_SHA", ""))
    sid = env.get("SPREADSHEET_ID", "")
    store = store_from_environment(sid, env)
    if store is None:
        raise ProjectionError("projection_binding_missing")
    db = SheetsDB(sid, service=sheets_service() if apply else read_only_sheets_service())
    result = order_ledgers(db, store, apply=apply)
    if apply:
        pending_rebuild = ProjectionJournal(store).read().get("rebuild", False)
        if result["ledger_order_sheets"] or pending_rebuild:
            result.update(ProjectionRefresh(store, SheetsLedgerReader(db)).refresh(db.categories()))
            from .daily_runtime import refresh_daily
            result.update(refresh_daily(db, store, env))
    return result
