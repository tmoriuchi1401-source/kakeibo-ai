"""UI-only, fixed-ID navigation. No accounting or approval writes."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import unicodedata

WORKFLOW = "カテゴリ操作"
LEDGER = "支出明細"
HEADER = "カテゴリ操作へ戻る"
MARKER = "kakeibo.category.navigation"
VERSION = "v1"
INLINE_MARKER = "kakeibo.category.inline"
CANARY_SCOPE = "kakeibo.category.inline.canary"
INLINE_VERSION = "inline-v3"
LEGACY_VIEW = "代表取引確認"
UNAVAILABLE = "代表取引を確認できません"
A_WIDTH = 180
RULE_MARKER = "■ 1. カテゴリを選ぶ・今後の自動分類"
NEXT_MARKER = "■ 2. 過去分の固定プレビュー"


def sheet_url(spreadsheet_id, sheet_id):
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit#gid={sheet_id}"


def representative_summary_formula():
    # H/ROW is the sole relation key. Dates and amounts are display fields only.
    def value(column): return f"INDEX('{LEDGER}'!{column}:{column},pos)"
    def shown(column): return f'IF(ISBLANK({value(column)}),"—",{value(column)})'
    date = f'IF(ISBLANK({value("B")}),"—",TEXT({value("B")},"yyyy/mm/dd"))'
    amount = f'IF(ISBLANK({value("E")}),"—",TEXT({value("E")},"#,##0")&"円")'
    return (f'=LET(id,INDEX($H:$H,ROW()),n,SUMPRODUCT(N(\'{LEDGER}\'!A:A=id)),'
        f'IF(OR(id="",n<>1),"{UNAVAILABLE}",IFERROR(LET(pos,'
        f'MATCH(TRUE,ARRAYFORMULA(\'{LEDGER}\'!A:A=id),0),'
        f'"代表取引"&CHAR(10)&{shown("C")}&CHAR(10)&{date}&"｜"&{amount}'
        f'&CHAR(10)&{shown("H")}&"｜"&{shown("I")}&CHAR(10)&"内容："&{shown("D")}),'
        f'"{UNAVAILABLE}")))')


def representative_summary(record):
    """Independent expected display for native readback; record is ledger A:M."""
    def field(index): return "—" if record[index] in ("", None) else str(record[index])
    date = record[1]
    if isinstance(date, (int, float)):
        date = (datetime(1899, 12, 30) + timedelta(days=date)).strftime("%Y/%m/%d")
    elif date:
        date = datetime.fromisoformat(str(date)[:10]).strftime("%Y/%m/%d")
    else: date = "—"
    if record[4] in ("", None): amount = "—"
    else:
        try:
            amount = format(Decimal(str(record[4])).quantize(Decimal("1"), rounding=ROUND_HALF_UP), ",f") + "円"
        except (InvalidOperation, ValueError):
            raise ValueError("category_navigation_amount_invalid") from None
    return f"代表取引\n{field(2)}\n{date}｜{amount}\n{field(7)}｜{field(8)}\n内容：{field(3)}"


def summary_height(text):
    # Conservative width estimate with explicit and wrapped lines, no truncation.
    lines = sum(max(1, (sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
        for c in line) + 25)//26) for line in text.split("\n"))
    return max(132, lines*18+18)


def inline_scope(meta):
    import json
    sheet = next((s for s in meta.get("sheets", []) if s["properties"]["title"] == WORKFLOW), {})
    marker = next((m for m in sheet.get("developerMetadata", []) if m.get("metadataKey") == INLINE_MARKER), None)
    if marker:
        phase = marker.get("metadataValue")
        if phase == INLINE_VERSION: return None
        if phase not in ("canary-inline-v3", "verified-inline-v3"):
            raise ValueError("category_navigation_inline_phase_changed")
        scopes = [m for m in sheet.get("developerMetadata", []) if m.get("metadataKey") == CANARY_SCOPE]
        if len(scopes) != 1: raise ValueError("category_navigation_canary_scope_changed")
        values = json.loads(scopes[0]["metadataValue"])
        if not isinstance(values, list) or not 1 <= len(values) <= 3 or len(set(values)) != len(values):
            raise ValueError("category_navigation_canary_scope_changed")
        return set(values)
    # Before migration keep the existing rollout scope, without generating old links.
    return set() if any(s["properties"]["title"] == LEGACY_VIEW for s in meta.get("sheets", [])) else None


def return_formula(spreadsheet_id, workflow_sheet_id):
    # One identical formula per occupied ledger row, no captured physical row.
    # Empty outputs on non-representatives also survive A:M-only sorting.
    # Scope to the rule block; bank group IDs and preview payloads are not sources.
    base = sheet_url(spreadsheet_id, workflow_sheet_id)
    first = f'IFNA(MATCH("{RULE_MARKER}",\'{WORKFLOW}\'!A:A,0),0)'
    last = f'IFNA(MATCH("{NEXT_MARKER}",\'{WORKFLOW}\'!A:A,0),0)'
    markers_valid = (f'SUMPRODUCT(N(\'{WORKFLOW}\'!A:A="{RULE_MARKER}"))=1,'
                     f'SUMPRODUCT(N(\'{WORKFLOW}\'!A:A="{NEXT_MARKER}"))=1')
    return (f'=LET(id,INDEX($A:$A,ROW()),first,{first},last,{last},'
            f'IF(OR(id="",first=0,last<=first+2,NOT(AND({markers_valid}))),"",'
            f'LET(ids,INDEX(\'{WORKFLOW}\'!H:H,first+2):INDEX(\'{WORKFLOW}\'!H:H,last-1),'
            'n,SUMPRODUCT(N(ids=id)),IF(n=0,"",IF(n=1,'
            f'HYPERLINK("{base}&range=A"&(MATCH(TRUE,ARRAYFORMULA(ids=id),0)+first+1),'
            '"↩ カテゴリ操作に戻る"),'
            f'HYPERLINK("{base}&range=A1","↩ カテゴリ操作の一覧（複数候補）"))))))')


def source_rows(rows):
    """Read actual marker positions, never recompute positions from row counts."""
    markers = {}
    for marker in (RULE_MARKER, NEXT_MARKER):
        positions = [i for i, row in enumerate(rows) if row and row[0] == marker]
        if len(positions) != 1:
            raise ValueError("category_navigation_markers_invalid")
        markers[marker] = positions[0]
    first, last = markers[RULE_MARKER], markers[NEXT_MARKER]
    if last <= first + 1:
        raise ValueError("category_navigation_markers_invalid")
    return [(i + 1, str(rows[i][7])) for i in range(first + 2, last)
            if len(rows[i]) > 7 and rows[i][7]]


def installed(meta):
    return any(s.get("properties", {}).get("title") == LEDGER and any(
        m.get("metadataKey") == MARKER and m.get("metadataValue") == VERSION
        for m in s.get("developerMetadata", [])) for s in meta.get("sheets", []))


def refresh_navigation(db):
    """Maintain installed inline summaries without broadening the canary scope."""
    meta = db._sheet_metadata()
    if not any(m.get("metadataKey") == INLINE_MARKER for s in meta.get("sheets", [])
               if s["properties"]["title"] == WORKFLOW for m in s.get("developerMetadata", [])):
        return {"installed": False}
    from .category_navigation_maintenance import NavigationMaintenance
    return NavigationMaintenance(db).apply("refresh")
