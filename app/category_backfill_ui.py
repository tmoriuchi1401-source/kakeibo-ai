"""Phone-width Sheets controls for the separately approved historical backfill."""
from __future__ import annotations

import json
import calendar
import math
from datetime import date, datetime, timedelta, timezone

from .category_backfill import BackfillSpec, CategoryBackfillPipeline
from .category_rules import CategoryRule, narrow_text, parse_rules, valid_rule
from .category_ui_order import member_keys
from .category_past_all_months import identity, resolved_conditions

BACKFILL_UI_SHEET = "カテゴリ過去反映"
BACKFILL_CONFIRM_SHEET = "カテゴリ過去反映確認"
BACKFILL_UI_HEADERS = ["条件", "カテゴリ", "開始月", "終了月", "プレビューする", "種別", "ルールID", "revision", "条件JSON", "移行前期間"]
COMBINED_BACKFILL_UI_HEADERS = ["条件・カテゴリ", "開始月", "終了月", "プレビューする", "種別", "ルールID", "revision", "条件JSON", "移行前期間"]
LEGACY_BACKFILL_UI_HEADERS = ["条件・カテゴリ", "対象期間", "プレビューする", "種別", "ルールID", "revision", "条件JSON"]
BACKFILL_CONFIRM_HEADERS = ["反映要求", "対象", "この件数に反映", "状態", "反映要求ID"]

CONDITION, CATEGORY, START_MONTH, END_MONTH, PREVIEW, KIND, RULE_ID, REVISION, PAYLOAD, LEGACY_PERIOD = range(10)


def _is_checked(value: object) -> bool:
    return str(value).strip().upper() in {"TRUE", "1", "YES", "ON"}


def _period(value: object) -> tuple[str, str] | None:
    """Parse a legacy single-cell period during the one-way UI migration."""
    text = narrow_text(value)
    if text == "全期間": return "", ""
    if text.startswith("対象月:") and len(text) == 11:
        month = text[4:]
        try:
            year, number = (int(value) for value in month.split("-", 1))
            return f"{year:04d}-{number:02d}-01", f"{year:04d}-{number:02d}-{calendar.monthrange(year, number)[1]:02d}"
        except (TypeError, ValueError):
            return None
    if ".." in text:
        start, end = (narrow_text(x) for x in text.split("..", 1))
        # A phone can specify a whole-month range without typing calendar days.
        if len(start) == len(end) == 7 and start[4:5] == end[4:5] == "-":
            try:
                start_year, start_month = (int(value) for value in start.split("-", 1))
                end_year, end_month = (int(value) for value in end.split("-", 1))
                last = calendar.monthrange(end_year, end_month)[1]
                return f"{start_year:04d}-{start_month:02d}-01", f"{end_year:04d}-{end_month:02d}-{last:02d}"
            except (TypeError, ValueError):
                return None
        return start, end
    return None


def _month(value: object) -> str | None:
    # The Sheets Values API can expose a cell formatted as ``2026-07`` as
    # the underlying Google Sheets date serial (for example, 46204).  Keep
    # the control contract textual, including when a prior UI row has not
    # yet been rewritten by the new dropdown source.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return None
        try:
            return (date(1899, 12, 30) + timedelta(days=value)).strftime("%Y-%m")
        except (OverflowError, ValueError):
            return None
    text = narrow_text(value)
    # Values.get returns a date-formatted existing control as text.  It is
    # still the same selected month, so normalize it before the generated UI
    # is written back as a strict textual dropdown value.
    for pattern in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m")
        except ValueError:
            pass
    if len(text) != 7 or text[4:5] != "-":
        return None
    try:
        year, number = (int(item) for item in text.split("-", 1))
        if not 1 <= number <= 12:
            return None
    except (TypeError, ValueError):
        return None
    return f"{year:04d}-{number:02d}"


def _month_control(value: object) -> str:
    """Preserve textual controls and rewrite Sheets date serials as months."""
    month = _month(value)
    return month if month is not None else narrow_text(value)


def _month_period(start_value: object, end_value: object) -> tuple[str, str] | None:
    """Convert the two phone controls to the immutable preview date bounds."""
    if narrow_text(end_value) == "過去すべて":
        # The visible start value is deliberately retained, but has no effect.
        return "", ""
    start, end = _month(start_value), _month(end_value)
    if not start or not end or start > end:
        return None
    year, number = (int(item) for item in end.split("-", 1))
    return f"{start}-01", f"{end}-{calendar.monthrange(year, number)[1]:02d}"


def _legacy_period_to_months(value: object) -> tuple[str, str, str] | None:
    """Return start/end controls and an optional exact legacy value to retain.

    A non-month-aligned date range is intentionally not rounded out: it stays
    in the hidden migration record and the row is made to require new input.
    """
    text = narrow_text(value)
    if text == "全期間":
        return "", "過去すべて", ""
    bounds = _period(text)
    if not bounds:
        return None
    start, end = bounds
    try:
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        return None
    if start_date.day != 1 or end_date.day != calendar.monthrange(end_date.year, end_date.month)[1]:
        return "", "", text
    return start[:7], end[:7], ""


def condition_label(payload: dict) -> str:
    """Expose every matching limiter before the operator confirms a request."""
    parts = [f"種別={narrow_text(payload.get('kind'))}", f"データ元={narrow_text(payload.get('source'))}"]
    labels = (("口座", "account_alias"), ("請求名", "billing_name"), ("店舗名", "merchant"),
              ("商品ID", "product_id"), ("商品名", "product_name"))
    parts.extend(f"{label}={narrow_text(payload[key])}" for label,key in labels if narrow_text(payload.get(key)))
    if payload.get("amount") is not None:
        parts.append(f"金額={payload['amount']}円")
    return " / ".join(parts)


def _condition_from_payload(payload: dict) -> CategoryRule | None:
    try:
        category = tuple(payload["category"])
        if len(category) != 2: return None
        return CategoryRule(
            str(payload.get("rule_id", "adhoc")), narrow_text(payload["kind"]), narrow_text(payload["source"]),
            narrow_text(payload.get("account_alias")), narrow_text(payload.get("billing_name")),
            narrow_text(payload.get("merchant")), narrow_text(payload.get("product_id")),
            narrow_text(payload.get("product_name")), payload.get("amount"),
            (narrow_text(category[0]), narrow_text(category[1])), "", datetime.now(timezone.utc),
            int(payload.get("revision", 1)), True,
        )
    except (KeyError, TypeError, ValueError):
        return None


class CategoryBackfillUIPipeline:
    def __init__(self, db, *, ui_enabled: bool, apply_enabled: bool):
        self.db = db; self.ui_enabled = ui_enabled; self.apply_enabled = apply_enabled

    def _home_month(self) -> str:
        try:
            rows = self.db.get("'ホーム'!B4")
            selected = narrow_text(rows[0][0]) if rows and rows[0] else ""
        except Exception:
            selected = ""
        if len(selected) == 7 and selected[4] == "-": return selected
        return datetime.now().strftime("%Y-%m")

    def _existing_ui(self) -> tuple[list[str], list[list]]:
        """Read header and rows together so legacy C booleans are never an end month."""
        reader=getattr(self.db, "category_backfill_ui_table", None)
        if reader:
            header, rows=reader()
            return list(header), [list(row) for row in rows]
        return BACKFILL_UI_HEADERS, [list(row) for row in self.db.category_backfill_ui_rows()]

    @staticmethod
    def _old_rows(header: list[str], rows: list[list]) -> dict[str, tuple[list, bool]]:
        legacy=header[:len(LEGACY_BACKFILL_UI_HEADERS)] == LEGACY_BACKFILL_UI_HEADERS
        old={}
        for row in rows:
            if legacy:
                cells=row+[""]*max(0, 7-len(row))
                key=narrow_text(cells[4])
            elif header[:len(COMBINED_BACKFILL_UI_HEADERS)] == COMBINED_BACKFILL_UI_HEADERS:
                prior=row+[""]*max(0, len(COMBINED_BACKFILL_UI_HEADERS)-len(row))
                cells=["", "", prior[1], prior[2], prior[3], *prior[4:]]
                key=narrow_text(cells[RULE_ID])
            else:
                cells=row+[""]*max(0, len(BACKFILL_UI_HEADERS)-len(row))
                key=narrow_text(cells[RULE_ID])
            if key:
                old[key]=(cells, legacy)
        return old

    @staticmethod
    def _restored_controls(prior: list, legacy: bool, payload: str, default_month: str) -> tuple[str, str, bool, str]:
        if legacy:
            unchanged=len(prior) > 6 and prior[6] == payload
            if not unchanged:
                return default_month, default_month, False, ""
            migrated=_legacy_period_to_months(prior[1])
            if not migrated:
                return "", "", False, narrow_text(prior[1])
            start, end, retained=migrated
            return start, end, _is_checked(prior[2]) if not retained else False, retained
        unchanged=len(prior) > PAYLOAD and prior[PAYLOAD] == payload
        if not unchanged:
            return default_month, default_month, False, ""
        return (_month_control(prior[START_MONTH]),
                "過去すべて" if narrow_text(prior[END_MONTH]) == "過去すべて" else _month_control(prior[END_MONTH]),
                _is_checked(prior[PREVIEW]), narrow_text(prior[LEGACY_PERIOD]))

    @staticmethod
    def _label_with_period_notice(label: str, end_month: str, legacy_period: str) -> str:
        if legacy_period:
            return label + f"\n再設定待ち: 旧期間={legacy_period}（月単位で再設定してください）"
        if end_month == "過去すべて":
            return label + "\n過去すべて選択中は開始月を使用しません。"
        return label

    @staticmethod
    def _payload(rule: CategoryRule, *, saved_rule: bool) -> str:
        return json.dumps({
            "kind": rule.kind, "source": rule.source, "account_alias": rule.account_alias,
            "billing_name": rule.billing_name, "merchant": rule.merchant, "product_id": rule.product_id,
            "product_name": rule.product_name, "amount": rule.amount, "category": list(rule.category),
            "rule_id": rule.rule_id, "revision": rule.revision, "saved_rule": saved_rule,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def refresh(self):
        if not self.ui_enabled:
            return {"state": "disabled", "reason": "category_backfill_preview_disabled"}
        header, existing=self._existing_ui(); old=self._old_rows(header, existing)
        categories = set(self.db.categories()); default_month = self._home_month()
        default_period = "対象月:" + default_month; rows=[]
        for rule in parse_rules(self.db.category_rules()):
            if not rule.active or not valid_rule(rule, categories): continue
            payload = self._payload(rule, saved_rule=True); prior,legacy=old.get(rule.rule_id, ([], False))
            start,end,checked,retained=self._restored_controls(prior, legacy, payload, default_month)
            label=self._label_with_period_notice(condition_label(json.loads(payload)), end, retained)
            rows.append([
                label, f"{rule.category[0]}\n{rule.category[1]}", start, end, checked,
                "保存済みルール", rule.rule_id, rule.revision, payload, retained,
            ])
        # A displayed, not-yet-saved condition is also allowed for a past-only
        # request.  It never writes a future rule because saved_rule is false.
        for shown in self.db.category_rule_ui_rows():
            # Only the separate past checkbox creates an ad-hoc historical
            # choice.  Selecting a category or checking future registration
            # alone never previews (and therefore cannot touch F:G).
            if len(shown) < 12 or not _is_checked(shown[5]): continue
            try: snapshot = json.loads(shown[11]) if narrow_text(shown[11]) else {}
            except (TypeError, ValueError, json.JSONDecodeError): snapshot = {}
            if not isinstance(snapshot, dict): continue
            category = snapshot.get("category", [])
            # Both a categorized representative and an unclassified condition
            # group can request a past-only preview.  The preview engine still
            # targets only current fallback F:G rows, so this never reclassifies
            # the already-categorized representative that exposed the choice.
            if (snapshot.get("proposal") not in {"", "fallback_group", "classified_override"}
                    or snapshot.get("kind") != "service" or len(category) != 2
                    or not all(narrow_text(value) for value in category)):
                continue
            key = "displayed:" + narrow_text(shown[6])
            rule = CategoryRule("adhoc", "service", narrow_text(snapshot.get("source")), narrow_text(snapshot.get("account_alias")),
                                narrow_text(snapshot.get("merchant")), "", "", "", None,
                                (narrow_text(category[0]), narrow_text(category[1])), "", datetime.now(timezone.utc), 1, True)
            if not valid_rule(rule, categories): continue
            payload = self._payload(rule, saved_rule=False)
            aliases=[key]+["displayed:"+alias for alias in sorted(member_keys(shown)) if "displayed:"+alias != key]
            prior,legacy=next((old[alias] for alias in aliases if alias in old), ([], False))
            start,end,checked,retained=self._restored_controls(prior, legacy, payload, default_month)
            label=self._label_with_period_notice(
                f"表示中: {condition_label(json.loads(payload))}", end, retained)
            rows.append([label, f"{rule.category[0]}\n{rule.category[1]}", start, end, checked,
                         "表示中の条件（過去分のみ）", key, 1, payload, retained])
        resolved=resolved_conditions(self.db)
        rows=[row for row in rows if identity(json.loads(row[PAYLOAD])) not in resolved or _is_checked(row[PREVIEW])]
        _replace_rows(self.db, "replace_category_backfill_ui_rows",
                      BACKFILL_UI_SHEET, BACKFILL_UI_HEADERS, rows)
        return {"state": "refreshed", "conditions": len(rows), "default_period": default_period}

    def preview_checked(self):
        if not self.ui_enabled:
            return {"state": "disabled", "reason": "category_backfill_preview_disabled"}
        results=[]; updates=[]; display_read_cache={}
        for row_num, row in enumerate(self.db.category_backfill_ui_rows(), start=2):
            cells=list(row)+[""]*max(0, len(BACKFILL_UI_HEADERS)-len(row))
            if not _is_checked(cells[PREVIEW]): continue
            period=_month_period(cells[START_MONTH], cells[END_MONTH])
            try: payload=json.loads(cells[PAYLOAD])
            except (TypeError, ValueError, json.JSONDecodeError): payload={}
            rule=_condition_from_payload(payload)
            if not period or not rule:
                result={"state":"held", "reason":"invalid_period_or_condition"}
            else:
                result=CategoryBackfillPipeline(self.db, preview_enabled=True, apply_enabled=self.apply_enabled).preview(
                    BackfillSpec(rule, period[0], period[1], bool(payload.get("saved_rule"))),
                    display_read_cache=display_read_cache)
            results.append(result); cells[PREVIEW]=False
            # A fixed preview is the terminal processing of the source UI's
            # past choice.  Consume that choice by its immutable key so the
            # next runner refresh cannot create the same request again.
            shown_key = narrow_text(cells[RULE_ID])
            source_key = shown_key.removeprefix("displayed:") if shown_key.startswith("displayed:") else ""
            if (source_key and result.get("state") in {"previewed", "preview_empty", "held"}
                    and hasattr(self.db, "consume_category_rule_ui_past_choice")):
                self.db.consume_category_rule_ui_past_choice(source_key, result)
            if result.get("state") == "previewed":
                excluded = "、".join(f"{key}:{value}" for key,value in result.get("excluded", {}).items()) or "なし"
                cells[CONDITION] += (f"\n要求={result['request_id']}\nその他/未分類 → {rule.category[0]} / {rule.category[1]}"
                             f"\n対象 {result['targets']}件 / {result['total_amount']}円\n除外: {excluded}")
            else:
                cells[CONDITION] += "\n" + (result.get("state", "held") + ": " + result.get("reason", ""))
            updates.append((row_num, cells))
        if updates: self.db.update_rows(BACKFILL_UI_SHEET, updates)
        return {"state":"previewed", "results":results}

    def refresh_confirmations(self):
        if not self.ui_enabled:
            return {"state": "disabled", "reason": "category_backfill_preview_disabled"}
        old={narrow_text(row[4]): row for row in self.db.category_backfill_confirmation_rows() if len(row)>4}
        request_rows=[list(request)+[""]*11 for request in self.db.category_backfill_requests()]
        visible_requests=[request for request in request_rows
                          if request[2] in {"previewed", "confirmed", "partial"}]
        targets_by_request={}
        if visible_requests:
            visible_ids={narrow_text(request[0]) for request in visible_requests}
            for target in self.db.category_backfill_targets():
                detail=list(target)+[""]*15
                request_id=narrow_text(detail[0])
                if request_id in visible_ids:
                    targets_by_request.setdefault(request_id, []).append(detail)
        rows=[]
        for cells in visible_requests:
            prior=old.get(narrow_text(cells[0]), [])
            try: payload = json.loads(cells[3]) if narrow_text(cells[3]) else {}
            except (TypeError, ValueError, json.JSONDecodeError): payload = {}
            category = payload.get("category", ["?", "?"]) if isinstance(payload, dict) else ["?", "?"]
            excluded = payload.get("excluded", {}) if isinstance(payload, dict) else {}
            exclusion_text = "、".join(f"{key}:{value}" for key,value in excluded.items()) or "なし"
            period_label="過去すべて" if not narrow_text(cells[4]) and not narrow_text(cells[5]) else f"{cells[4]} .. {cells[5]}"
            rows.append([f"{cells[0]}\n{period_label}\n{condition_label(payload if isinstance(payload, dict) else {})}\nその他/未分類 → {category[0]} / {category[1]}", f"{cells[6]}件 / {cells[7]}円\n除外: {exclusion_text}",
                         _is_checked(prior[2]) if len(prior)>2 else False, cells[2], cells[0]])
            for detail in targets_by_request.get(narrow_text(cells[0]), []):
                # A target detail is context only, never an approval control.
                # Keep C visibly empty even before formatting.
                rows.append([f"{detail[3]}\n{detail[1]}", f"{detail[4]}円\n{detail[5]} / {detail[6]} → {detail[7]} / {detail[8]}", "", "内訳", ""])
        _replace_rows(self.db, "replace_category_backfill_confirmation_rows",
                      BACKFILL_CONFIRM_SHEET, BACKFILL_CONFIRM_HEADERS, rows)
        return {"state":"refreshed", "requests":len(rows)}

    def apply_confirmed(self):
        if not self.ui_enabled or not self.apply_enabled:
            return {"state":"disabled", "reason":"category_backfill_ui_or_apply_disabled"}
        results=[]; updates=[]; pipeline=CategoryBackfillPipeline(self.db, preview_enabled=True, apply_enabled=True)
        for row_num,row in enumerate(self.db.category_backfill_confirmation_rows(), start=2):
            cells=list(row)+[""]*5
            if not _is_checked(cells[2]) or not narrow_text(cells[4]): continue
            try: count=int(str(cells[1]).split("件",1)[0])
            except ValueError: count=0
            confirmed=pipeline.confirm(cells[4], expected_count=count)
            result=pipeline.apply(cells[4], expected_count=count) if confirmed.get("state")=="confirmed" else confirmed
            cells[2]=False; cells[3]=result.get("state", "held"); updates.append((row_num,cells)); results.append(result)
        if updates: self.db.update_rows(BACKFILL_CONFIRM_SHEET, updates)
        return {"state":"applied", "results":results}


def _replace_rows(db, method_name, sheet, header, rows):
    """Use the in-place production writer; retain minimal test-double support."""
    writer=getattr(db, method_name, None)
    if writer:
        writer(rows, header)
        return
    # Existing lightweight test doubles do not model Google row insertion.
    # Production SheetsDB always has the replacement method above.
    if sheet == BACKFILL_UI_SHEET:
        db.ensure_category_backfill_ui_sheet(header)
    else:
        db.ensure_category_backfill_confirmation_sheet(header)
    db.clear(f"{sheet}!A2:{chr(64 + len(header))}")
    db.append(sheet, rows)
