"""Phone-width Sheets controls for the separately approved historical backfill."""
from __future__ import annotations

import json
import calendar
from datetime import datetime, timezone

from .category_backfill import BackfillSpec, CategoryBackfillPipeline
from .category_rules import CategoryRule, narrow_text, parse_rules, valid_rule

BACKFILL_UI_SHEET = "カテゴリ過去反映"
BACKFILL_CONFIRM_SHEET = "カテゴリ過去反映確認"
BACKFILL_UI_HEADERS = ["条件・カテゴリ", "対象期間", "プレビューする", "種別", "ルールID", "revision", "条件JSON"]
BACKFILL_CONFIRM_HEADERS = ["反映要求", "対象", "この件数に反映", "状態", "反映要求ID"]


def _is_checked(value: object) -> bool:
    return str(value).strip().upper() in {"TRUE", "1", "YES", "ON"}


def _period(value: object) -> tuple[str, str] | None:
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
        old = {narrow_text(row[4]): row for row in self.db.category_backfill_ui_rows()
               if len(row) > 4 and narrow_text(row[4])}
        categories = set(self.db.categories()); default_period = "対象月:" + self._home_month(); rows=[]
        for rule in parse_rules(self.db.category_rules()):
            if not rule.active or not valid_rule(rule, categories): continue
            payload = self._payload(rule, saved_rule=True); prior = old.get(rule.rule_id, [])
            unchanged = len(prior) > 6 and prior[6] == payload
            rows.append([
                f"{condition_label(json.loads(payload))}\n{rule.category[0]} / {rule.category[1]}",
                prior[1] if unchanged and len(prior) > 1 else default_period,
                _is_checked(prior[2]) if unchanged and len(prior) > 2 else False,
                "保存済みルール", rule.rule_id, rule.revision, payload,
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
            if (snapshot.get("proposal") != "fallback_group" or snapshot.get("kind") != "service"
                    or len(category) != 2 or not all(narrow_text(value) for value in category)):
                continue
            key = "displayed:" + narrow_text(shown[6])
            rule = CategoryRule("adhoc", "service", narrow_text(snapshot.get("source")), narrow_text(snapshot.get("account_alias")),
                                narrow_text(snapshot.get("merchant")), "", "", "", None,
                                (narrow_text(category[0]), narrow_text(category[1])), "", datetime.now(timezone.utc), 1, True)
            if not valid_rule(rule, categories): continue
            payload = self._payload(rule, saved_rule=False); prior = old.get(key, [])
            rows.append([f"表示中: {condition_label(json.loads(payload))}\n{rule.category[0]} / {rule.category[1]}",
                         prior[1] if len(prior) > 1 and len(prior) > 6 and prior[6] == payload else default_period,
                         _is_checked(prior[2]) if len(prior) > 6 and prior[6] == payload else False,
                         "表示中の条件（過去分のみ）", key, 1, payload])
        self.db.ensure_category_backfill_ui_sheet(BACKFILL_UI_HEADERS)
        self.db.clear(f"{BACKFILL_UI_SHEET}!A2:G"); self.db.append(BACKFILL_UI_SHEET, rows)
        return {"state": "refreshed", "conditions": len(rows), "default_period": default_period}

    def preview_checked(self):
        if not self.ui_enabled:
            return {"state": "disabled", "reason": "category_backfill_preview_disabled"}
        results=[]; updates=[]
        for row_num, row in enumerate(self.db.category_backfill_ui_rows(), start=2):
            cells=list(row)+[""]*max(0, 7-len(row))
            if not _is_checked(cells[2]): continue
            period=_period(cells[1])
            try: payload=json.loads(cells[6])
            except (TypeError, ValueError, json.JSONDecodeError): payload={}
            rule=_condition_from_payload(payload)
            if not period or not rule:
                result={"state":"held", "reason":"invalid_period_or_condition"}
            else:
                result=CategoryBackfillPipeline(self.db, preview_enabled=True, apply_enabled=self.apply_enabled).preview(
                    BackfillSpec(rule, period[0], period[1], bool(payload.get("saved_rule"))))
            results.append(result); cells[2]=False
            if result.get("state") == "previewed":
                excluded = "、".join(f"{key}:{value}" for key,value in result.get("excluded", {}).items()) or "なし"
                cells[0] += (f"\n要求={result['request_id']}\nその他/未分類 → {rule.category[0]} / {rule.category[1]}"
                             f"\n対象 {result['targets']}件 / {result['total_amount']}円\n除外: {excluded}")
            else:
                cells[0] += "\n" + (result.get("state", "held") + ": " + result.get("reason", ""))
            updates.append((row_num, cells))
        if updates: self.db.update_rows(BACKFILL_UI_SHEET, updates)
        return {"state":"previewed", "results":results}

    def refresh_confirmations(self):
        if not self.ui_enabled:
            return {"state": "disabled", "reason": "category_backfill_preview_disabled"}
        old={narrow_text(row[4]): row for row in self.db.category_backfill_confirmation_rows() if len(row)>4}
        rows=[]
        for request in self.db.category_backfill_requests():
            cells=list(request)+[""]*11
            if cells[2] not in {"previewed", "confirmed", "partial"}: continue
            prior=old.get(narrow_text(cells[0]), [])
            try: payload = json.loads(cells[3]) if narrow_text(cells[3]) else {}
            except (TypeError, ValueError, json.JSONDecodeError): payload = {}
            category = payload.get("category", ["?", "?"]) if isinstance(payload, dict) else ["?", "?"]
            excluded = payload.get("excluded", {}) if isinstance(payload, dict) else {}
            exclusion_text = "、".join(f"{key}:{value}" for key,value in excluded.items()) or "なし"
            rows.append([f"{cells[0]}\n{cells[4]} .. {cells[5]}\n{condition_label(payload if isinstance(payload, dict) else {})}\nその他/未分類 → {category[0]} / {category[1]}", f"{cells[6]}件 / {cells[7]}円\n除外: {exclusion_text}",
                         _is_checked(prior[2]) if len(prior)>2 else False, cells[2], cells[0]])
            for target in self.db.category_backfill_targets():
                detail=list(target)+[""]*15
                if narrow_text(detail[0]) == narrow_text(cells[0]):
                    rows.append([f"{detail[3]}\n{detail[1]}", f"{detail[4]}円\n{detail[5]} / {detail[6]} → {detail[7]} / {detail[8]}", False, "内訳", ""])
        self.db.ensure_category_backfill_confirmation_sheet(BACKFILL_CONFIRM_HEADERS)
        self.db.clear(f"{BACKFILL_CONFIRM_SHEET}!A2:E"); self.db.append(BACKFILL_CONFIRM_SHEET, rows)
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
