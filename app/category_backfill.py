"""Explicit, immutable backfill of approved category conditions.

This is deliberately separate from import-time rule application.  A preview
captures exact ledger IDs and source evidence; apply only revisits those IDs
and never searches for new candidates.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .auto_expense import FALLBACK_CATEGORY
from .category_rules import AGGREGATE_ITEM_NAMES, CategoryRule, match_transaction, narrow_text, parse_rules
from .expense_view import ExpenseViewPipeline
from .reconciliation import parse_import_rows

BACKFILL_REQUEST_SHEET = "カテゴリ過去反映要求"
BACKFILL_TARGET_SHEET = "カテゴリ過去反映対象"
BACKFILL_REQUEST_HEADERS = [
    "反映要求ID", "作成日時", "状態", "条件JSON", "期間開始", "期間終了", "対象件数", "対象金額",
    "対象digest", "確認済み", "最終更新日時",
]
BACKFILL_TARGET_HEADERS = [
    "反映要求ID", "支出ID", "支出行", "支出日", "金額", "変更前大", "変更前小", "変更後大", "変更後小",
    "入力スナップショットJSON", "結果", "理由", "反映日時", "復元日時", "最終更新日時",
]


def _text(value: object) -> str:
    return narrow_text(value)


def _bool(value: object) -> bool:
    return str(value).strip().upper() in {"TRUE", "1", "YES", "ON"}


def _ymd(value: object) -> date | None:
    text = _text(value).replace("/", "-")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _json(value: object) -> dict:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _amount(value: object) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _product_id(expense: list) -> str:
    """Extract the one stable product namespace currently written to the ledger."""
    note = str(expense[11] if len(expense) > 11 else "")
    if note.startswith("Amazonキー="):
        key = note.split("=", 1)[1].split(";", 1)[0]
        parts = key.split("|")
        if len(parts) == 2 and all(_text(x) for x in parts):
            return "amazon:" + _text(parts[1])
    return ""


def _unsafe_source(tx) -> str:
    text = " ".join((str(tx.merchant), str(tx.note), str(tx.status))).upper()
    if tx.amount <= 0:
        return "refund_or_nonpositive"
    if any(word in text for word in ("REFUND", "返金", "取消", "取り消し", "キャンセル", "返品")):
        return "refund_or_cancelled"
    if any(word in text for word in ("重複", "DUPLICATE", "NEEDS_REVIEW", "要確認", "MATCHED_", "照合")):
        return "duplicate_or_reconciliation_uncertain"
    if any(word in text for word in ("送金", "振替", "資金移動", "チャージ")):
        return "transfer_or_cash_movement"
    return ""


def _request_digest(payload: dict, start_date: object, end_date: object, rows: list[list]) -> str:
    """Bind condition, category, period, and immutable targets as one request."""
    immutable = [list(row[:10]) for row in rows]
    return hashlib.sha256(_canonical({
        "payload": payload, "start_date": _text(start_date), "end_date": _text(end_date), "targets": immutable,
    }).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BackfillSpec:
    """The single condition that is allowed to be applied in one request."""
    condition: CategoryRule
    start_date: str = ""
    end_date: str = ""
    saved_rule: bool = False

    def payload(self) -> dict:
        rule = self.condition
        return {
            "v": 1,
            "kind": rule.kind, "source": rule.source, "account_alias": rule.account_alias,
            "billing_name": rule.billing_name, "merchant": rule.merchant,
            "product_id": rule.product_id, "product_name": rule.product_name,
            "amount": rule.amount, "category": list(rule.category),
            "period": {"start": self.start_date, "end": self.end_date},
            "saved_rule": {"id": rule.rule_id, "revision": rule.revision} if self.saved_rule else None,
        }


class CategoryBackfillPipeline:
    def __init__(self, db, *, preview_enabled: bool, apply_enabled: bool, now=None, id_factory=None):
        self.db = db
        self.preview_enabled = preview_enabled
        self.apply_enabled = apply_enabled
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: "CB-" + uuid.uuid4().hex)

    def _now(self) -> str:
        return self.now().astimezone(timezone.utc).isoformat()

    def _requests(self):
        if not hasattr(self.db, "category_backfill_requests"):
            return []
        return [(row_num, list(row) + [""] * max(0, len(BACKFILL_REQUEST_HEADERS) - len(row)))
                for row_num, row in enumerate(self.db.category_backfill_requests(), start=2) if row and row[0]]

    def _targets(self):
        if not hasattr(self.db, "category_backfill_targets"):
            return []
        return [(row_num, list(row) + [""] * max(0, len(BACKFILL_TARGET_HEADERS) - len(row)))
                for row_num, row in enumerate(self.db.category_backfill_targets(), start=2) if row and row[0]]

    def _source_snapshot(self, tx, expense: list) -> dict:
        return {
            "import_id": _text(tx.import_id), "source": _text(tx.source), "status": _text(tx.status),
            "target_id": _text(tx.target_id), "merchant": _text(tx.merchant), "amount": tx.amount,
            "date": _text(tx.date), "item_name": _text(expense[3]), "product_id": _product_id(expense),
        }

    def _in_period(self, expense: list, spec: BackfillSpec) -> bool:
        value = _ymd(expense[1])
        if not value:
            return False
        start = _ymd(spec.start_date) if spec.start_date else None
        end = _ymd(spec.end_date) if spec.end_date else None
        return (start is None or value >= start) and (end is None or value <= end)

    def preview(self, spec: BackfillSpec) -> dict:
        if not self.preview_enabled:
            return {"state": "disabled", "reason": "category_backfill_preview_disabled"}
        categories = set(self.db.categories())
        if spec.condition.category not in categories:
            return {"state": "held", "reason": "invalid_target_category"}
        if spec.condition.kind not in {"service", "store_total", "product"}:
            return {"state": "held", "reason": "invalid_condition_kind"}
        start, end = _ymd(spec.start_date) if spec.start_date else None, _ymd(spec.end_date) if spec.end_date else None
        if start and end and start > end:
            return {"state": "held", "reason": "invalid_period"}
        transactions = {tx.import_id: tx for tx in parse_import_rows(self.db.get("取込データ!A2:L"))}
        targets, excluded = [], {}
        for expense_id, (row_num, expense) in sorted(self.db.expense_records().items()):
            expense = list(expense) + [""] * max(0, 13 - len(expense))
            if _text(expense[12]) != "active":
                excluded["inactive"] = excluded.get("inactive", 0) + 1; continue
            if not self._in_period(expense, spec):
                excluded["outside_period"] = excluded.get("outside_period", 0) + 1; continue
            category = (_text(expense[5]), _text(expense[6]))
            if category != FALLBACK_CATEGORY:
                reason = "partial_or_invalid_category" if not all(category) or category not in categories else "already_classified"
                excluded[reason] = excluded.get(reason, 0) + 1; continue
            tx = transactions.get(str(expense[10]))
            if not tx:
                excluded["source_import_not_found"] = excluded.get("source_import_not_found", 0) + 1; continue
            if tx.status not in {"auto_expense", "canonical_amazon", "unclassified_amazon"}:
                excluded["source_not_authoritative"] = excluded.get("source_not_authoritative", 0) + 1; continue
            if spec.condition.kind in {"service", "store_total"} and (
                tx.status != "auto_expense" or _text(tx.target_id) != _text(expense_id)
            ):
                excluded["source_not_bound_to_expense"] = excluded.get("source_not_bound_to_expense", 0) + 1; continue
            unsafe = _unsafe_source(tx)
            if unsafe:
                excluded[unsafe] = excluded.get(unsafe, 0) + 1; continue
            product_id = _product_id(expense)
            matched = match_transaction(
                [spec.condition], tx, categories, product_name=_text(expense[3]), product_id=product_id,
                aggregate_only=_text(expense[3]) in AGGREGATE_ITEM_NAMES,
                require_newer_than_approval=False,
            )
            if matched.state != "matched":
                excluded["condition_not_matched"] = excluded.get("condition_not_matched", 0) + 1; continue
            targets.append((str(expense_id), row_num, expense, self._source_snapshot(tx, expense)))
        if not targets:
            return {"state": "preview_empty", "targets": 0, "total_amount": 0, "excluded": excluded}
        request_id = self.id_factory()
        payload = spec.payload()
        payload["excluded"] = excluded
        now = self._now()
        self.db.ensure_category_backfill_sheets()
        target_rows = []
        for expense_id, row_num, expense, source_snapshot in targets:
            target_rows.append([
                request_id, expense_id, row_num, expense[1], _amount(expense[4]), FALLBACK_CATEGORY[0], FALLBACK_CATEGORY[1],
                spec.condition.category[0], spec.condition.category[1], _canonical(source_snapshot), "previewed", "", "", "", now,
            ])
        self.db.append(BACKFILL_REQUEST_SHEET, [[
            request_id, now, "previewed", _canonical(payload), spec.start_date, spec.end_date,
            len(targets), sum(_amount(row[4]) for _, _, row, _ in targets),
            _request_digest(payload, spec.start_date, spec.end_date, target_rows), False, now,
        ]])
        self.db.append(BACKFILL_TARGET_SHEET, target_rows)
        return {"state": "previewed", "request_id": request_id, "targets": len(targets),
                "total_amount": sum(row[4] for row in target_rows), "excluded": excluded,
                "requires_confirmation": f"この{len(targets)}件に反映"}

    def confirm(self, request_id: str, *, expected_count: int) -> dict:
        if not self.preview_enabled:
            return {"state": "disabled", "reason": "category_backfill_preview_disabled"}
        found = next(((n, row) for n, row in self._requests() if _text(row[0]) == _text(request_id)), None)
        if not found:
            return {"state": "not_found"}
        row_num, row = found
        target_count = sum(1 for _, target in self._targets() if _text(target[0]) == _text(request_id))
        if not target_count or target_count != expected_count or _amount(row[6]) != expected_count:
            return {"state": "held", "reason": "target_count_changed"}
        payload = _json(row[3])
        targets = [target for _, target in self._targets() if _text(target[0]) == _text(request_id)]
        if _text(row[8]) != _request_digest(payload, row[4], row[5], targets):
            return {"state": "held", "reason": "request_snapshot_changed"}
        row[2], row[9], row[10] = "confirmed", True, self._now()
        self.db.update_rows(BACKFILL_REQUEST_SHEET, [(row_num, row)])
        return {"state": "confirmed", "request_id": request_id, "targets": target_count}

    def _request_rule_valid(self, payload: dict) -> bool:
        saved = payload.get("saved_rule")
        if not saved:
            return True
        rule = next((item for item in parse_rules(self.db.category_rules())
                     if item.rule_id == saved.get("id")), None)
        return bool(rule and rule.active and rule.revision == saved.get("revision")
                    and list(rule.category) == payload.get("category") and rule.kind == payload.get("kind"))

    def apply(self, request_id: str, *, expected_count: int) -> dict:
        if not self.apply_enabled:
            return {"state": "disabled", "reason": "category_backfill_apply_disabled"}
        found = next(((n, row) for n, row in self._requests() if _text(row[0]) == _text(request_id)), None)
        if not found:
            return {"state": "not_found"}
        request_row_num, request = found
        targets = [(n, row) for n, row in self._targets() if _text(row[0]) == _text(request_id)]
        # A partial run is resumable against the original fixed target list;
        # it does not require a fresh preview or a broadened selection.
        if request[2] not in {"confirmed", "partial"} or not _bool(request[9]) or not targets:
            return {"state": "held", "reason": "explicit_confirmation_required"}
        if expected_count != len(targets) or _amount(request[6]) != len(targets):
            return {"state": "held", "reason": "target_count_changed"}
        payload = _json(request[3])
        if _text(request[8]) != _request_digest(payload, request[4], request[5], [row for _, row in targets]):
            return {"state": "held", "reason": "request_snapshot_changed"}
        if not self._request_rule_valid(payload):
            return {"state": "held", "reason": "rule_or_category_changed"}
        records = self.db.expense_records()
        transactions = {tx.import_id: tx for tx in parse_import_rows(self.db.get("取込データ!A2:L"))}
        allowed = set(self.db.categories())
        writes, updates, applied, skipped = [], [], 0, {}
        now = self._now()
        for target_row_num, target in targets:
            if target[10] in {"applied", "restored"}:
                continue
            found_expense = records.get(_text(target[1]))
            reason = ""
            if not found_expense:
                reason = "expense_not_found"
            else:
                ledger_row_num, expense = found_expense
                expense = list(expense) + [""] * max(0, 13 - len(expense))
                tx = transactions.get(str(expense[10]))
                if _text(expense[12]) != "active": reason = "inactive"
                elif _text(expense[1]) != _text(target[3]): reason = "expense_date_changed_concurrently"
                elif (_text(expense[5]), _text(expense[6])) != (_text(target[5]), _text(target[6])): reason = "category_changed_concurrently"
                elif (_text(target[7]), _text(target[8])) not in allowed: reason = "target_category_invalid"
                elif not tx: reason = "source_import_not_found"
                elif _canonical(self._source_snapshot(tx, expense)) != _text(target[9]): reason = "source_changed_concurrently"
            if reason:
                target[10], target[11], target[14] = "skipped", reason, now
                skipped[reason] = skipped.get(reason, 0) + 1
                updates.append((target_row_num, target)); continue
            writes.append((ledger_row_num, _text(target[7]), _text(target[8])))
            target[10], target[11], target[12], target[14] = "applied", "", now, now
            updates.append((target_row_num, target)); applied += 1
        if writes:
            self.db.update_expense_categories(writes)
        if updates:
            self.db.update_rows(BACKFILL_TARGET_SHEET, updates)
        request[2] = "complete" if not skipped else "partial"
        request[10] = now
        self.db.update_rows(BACKFILL_REQUEST_SHEET, [(request_row_num, request)])
        result = {"state": request[2], "request_id": request_id, "applied": applied, "skipped": skipped}
        if writes:
            try:
                result["expense_view"] = ExpenseViewPipeline(self.db).refresh()
            except Exception as exc:  # classification is durable; display can be retried independently
                result["expense_view"] = {"state": "refresh_pending", "reason": type(exc).__name__}
        return result

    def restore(self, request_id: str) -> dict:
        if not self.apply_enabled:
            return {"state": "disabled", "reason": "category_backfill_apply_disabled"}
        records = self.db.expense_records(); writes=[]; updates=[]; skipped={}; now=self._now()
        for target_row_num, target in self._targets():
            if _text(target[0]) != _text(request_id) or target[10] != "applied":
                continue
            current = records.get(_text(target[1]))
            if not current:
                target[11] = "expense_not_found"; skipped["expense_not_found"] = skipped.get("expense_not_found", 0) + 1
            else:
                ledger_row_num, expense = current
                if (_text(expense[5]), _text(expense[6])) != (_text(target[7]), _text(target[8])):
                    target[11] = "edited_after_apply"; skipped["edited_after_apply"] = skipped.get("edited_after_apply", 0) + 1
                else:
                    writes.append((ledger_row_num, _text(target[5]), _text(target[6])))
                    target[10], target[13] = "restored", now
            target[14] = now; updates.append((target_row_num, target))
        if writes: self.db.update_expense_categories(writes)
        if updates: self.db.update_rows(BACKFILL_TARGET_SHEET, updates)
        result={"state":"restored", "request_id":request_id, "restored":len(writes), "skipped":skipped}
        if writes:
            try: result["expense_view"] = ExpenseViewPipeline(self.db).refresh()
            except Exception as exc: result["expense_view"] = {"state":"refresh_pending", "reason":type(exc).__name__}
        return result
