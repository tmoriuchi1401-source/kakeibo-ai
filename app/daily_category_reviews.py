"""Read-only links to the three existing, independently approved category stages."""
from __future__ import annotations

from .daily_view import ReviewItem
from .monthly_projection import ProjectionError
from .sheets import CATEGORY_WORKFLOW_MARKERS, CATEGORY_WORKFLOW_SHEET, LEGACY_CATEGORY_RULE_UI_SHEET
from .category_rule_choices import DECLINE, checked as _checked


def _rows(db, properties, width, first=1):
    title = properties["title"]
    extent = properties["gridProperties"]["rowCount"]
    for start in range(first, extent + 1, 1000):
        end = min(start + 999, extent)
        for offset, raw in enumerate(db.get_raw(f"'{title}'!A{start}:{chr(64 + width)}{end}")):
            yield start + offset, list(raw) + [""] * max(0, width - len(raw))


def _url(db, properties, row):
    return f"https://docs.google.com/spreadsheets/d/{db.sid}/edit#gid={properties['sheetId']}&range=A{row}"


def _item(section, row, url, requests, *, combined):
    if section == "rule":
        key = str(row[6]).strip()
        if not key:
            return None
        lines = [line for line in str(row[1]).strip().split("\n") if not line.startswith("過去プレビュー:")]
        latest = lines[-1] if lines else ""
        future, past = _checked(row[4]), _checked(row[5])
        held = latest.startswith("held") or "競合" in latest or "再承認" in latest
        unresolved = latest == "カテゴリを選択" or (key.startswith("group:") and latest == "登録待ち")
        if row[4] == DECLINE:
            held = unresolved = False
        if not (future or past or held or unresolved):
            return None  # An unused future-rule suggestion is not an unresolved issue.
        stage = "今後の自動分類" if future or not past else "過去分のプレビュー"
        if future and past:
            stage += "・過去分のプレビュー（別承認）"
        detail = f"{stage}\n{row[0]}\n{latest}"
        status = "保留" if held else "処理待ち" if future or past else "要確認"
    elif section == "backfill":
        key = str(row[7 if combined else 6]).strip()
        latest = str(row[0]).strip().split("\n")[-1]
        held = latest.startswith("held:")
        if not key or not (_checked(row[4]) or held):
            return None
        detail = f"過去分のプレビュー\n{row[0]}\n期間: {row[2]} .. {row[3]}"
        status = "保留" if held else "処理待ち"
    else:
        key = str(row[6 if combined else 4]).strip()
        if not key:
            return None  # Target detail rows are context, never approvals.
        state = str(row[3]).strip()
        saved = requests.get(key)
        if saved and saved[0] == "complete":
            return None  # A stale UI row must not resurrect a completed request.
        if saved and saved[0] in {"confirmed", "partial"} and state == "previewed":
            state = saved[0]
        if state == "complete" and not saved:
            return None
        if saved and state == "complete":
            state = "inconsistent"
        detail = f"対象件数を確認して反映\n{row[1]}\n{row[0]}"
        status = {"previewed": "要確認", "confirmed": "処理待ち", "partial": "一部未反映"}.get(state, "保留")
        if state == "previewed" and _checked(row[2]):
            status = "処理待ち"
    return ReviewItem(f"分類:{section}:{key}", "分類・" + {"rule": "ルール承認", "backfill": "プレビュー", "confirm": "過去分反映"}[section], detail, status, url)


def read_category_reviews(db, metadata):
    """Scan finite queue ranges only; never read ledger/targets/approval snapshots."""
    sheets = {s["properties"]["title"]: s["properties"] for s in metadata.get("sheets", [])}
    requests = {}
    if "カテゴリ過去反映要求" in sheets:
        for number, row in _rows(db, sheets["カテゴリ過去反映要求"], 3, 2):
            key = str(row[0]).strip()
            if not key:
                continue
            if key in requests:
                raise ProjectionError("duplicate_category_review_request")
            requests[key] = (str(row[2]).strip(), number)
    result = []
    seen_confirm = set()

    def collect(section, number, row, properties, combined):
        if section == "confirm":
            key = str(row[6 if combined else 4]).strip()
            if key:
                seen_confirm.add(key)
        item = _item(section, row, _url(db, properties, number), requests, combined=combined)
        if item:
            result.append(item)

    if CATEGORY_WORKFLOW_SHEET in sheets:
        properties = sheets[CATEGORY_WORKFLOW_SHEET]
        markers = {value: key for key, value in CATEGORY_WORKFLOW_MARKERS.items()}
        section, header_row = None, None
        for number, row in _rows(db, properties, 8):
            if row[0] in markers:
                section, header_row = markers[row[0]], number + 1
            elif section and number != header_row and row[0]:
                collect(section, number, row, properties, True)
    else:
        # Only use the legacy tabs when the unified source UI is absent.
        for title, section, width in [(LEGACY_CATEGORY_RULE_UI_SHEET, "rule", 8),
                                      ("カテゴリ過去反映", "backfill", 7),
                                      ("カテゴリ過去反映確認", "confirm", 5)]:
            if title in sheets:
                for number, row in _rows(db, sheets[title], width, 2):
                    if row[0]:
                        collect(section, number, row, sheets[title], False)
    # A delayed UI refresh must not conceal a durable, unfinished request.
    for key, (state, number) in requests.items():
        if key not in seen_confirm and state != "complete":
            properties = sheets.get(CATEGORY_WORKFLOW_SHEET, sheets.get("カテゴリ過去反映確認"))
            url = _url(db, properties, 1) if properties else _url(db, sheets["カテゴリ過去反映要求"], number)
            result.append(ReviewItem(f"分類:confirm:{key}", "分類・過去分反映",
                f"{key}\n確認画面の再表示が必要です。固定プレビューの件数・対象を確認してください。",
                "一部未反映" if state == "partial" else "要確認", url))
    return result
