"""Execute only an immutable, explicitly submitted Sheets category request.

Private input stays in the existing spreadsheet. Actions receives only a UUID.
The workflow shares the production writer lock. A claimed request is never
automatically replayed after an uncertain write or a cancelled runner.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from .drive_run_state import StateError
from .sheets import (SheetsDB, CATEGORY_REQUEST_SHEET, CATEGORY_WORKFLOW_SHEET,
                     CATEGORY_WORKFLOW_MARKERS, OPTIONAL_WORKFLOW_MARKERS)

MAX_ROWS = 10000


def snapshot_digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_snapshot(rows):
    if not rows or len(rows) > MAX_ROWS or any(
            not isinstance(r, list) or len(r) != 12 or
            any(not isinstance(v, str) for v in r) for r in rows):
        raise StateError("category_snapshot_shape_invalid")
    markers = {}
    for section, marker in CATEGORY_WORKFLOW_MARKERS.items():
        hits = [i for i, row in enumerate(rows) if row[0] == marker]
        if len(hits) != 1:
            raise StateError("category_snapshot_markers_invalid")
        markers[section] = hits[0]
    for section, marker in OPTIONAL_WORKFLOW_MARKERS.items():
        hits = [i for i, row in enumerate(rows) if row[0] == marker]
        if len(hits) > 1:
            raise StateError("category_snapshot_markers_invalid")
        if hits:
            markers[section] = hits[0]
    if list(markers.values()) != sorted(markers.values()):
        raise StateError("category_snapshot_order_invalid")
    defaults = SheetsDB._category_workflow_defaults(None)
    if "bank" in markers:
        from .bank_review_ui import BANK_UI_HEADERS
        defaults["bank"] = list(BANK_UI_HEADERS)
    blocks = {}
    for section in markers:
        start = markers[section] + 2
        end = min([p for p in markers.values() if p > markers[section]] or [len(rows)])
        data = [SheetsDB._workflow_logical_row(section, r, compact=True)
                for r in rows[start:end] if any(v.strip() for v in r)]
        keys = [row[4 if section == "confirm" else 6] for row in data
                if row[4 if section == "confirm" else 6]]
        if len(keys) != len(set(keys)):
            raise StateError("category_snapshot_duplicate_key")
        header=list(defaults[section])
        if section == "rule":
            # The submitted header binds the checkbox's meaning. A request
            # captured under the former preview-only label stays preview-only.
            header[5]=rows[markers[section]+1][5]
        blocks[section] = (header, data)
    if "bank" in blocks:
        from .bank_review_ui import validate_rows
        try:
            validate_rows(blocks["bank"][1])
        except ValueError:
            raise StateError("bank_review_snapshot_invalid") from None
    return blocks


class CapturedCategoryDB:
    """Keep UI reads/writes in memory; all ledger gates use the real DB."""
    def __init__(self, db, blocks):
        self.db = db
        self.blocks = deepcopy(blocks)

    def __getattr__(self, name):
        return getattr(self.db, name)

    def category_rule_ui_rows(self): return deepcopy(self.blocks["rule"][1])
    def category_backfill_ui_rows(self): return deepcopy(self.blocks["backfill"][1])
    def category_backfill_ui_table(self): return deepcopy(self.blocks["backfill"])
    def category_backfill_confirmation_rows(self): return deepcopy(self.blocks["confirm"][1])
    def replace_category_rule_ui_rows(self, rows, header): self._replace("rule", rows, header)
    def replace_category_backfill_ui_rows(self, rows, header): self._replace("backfill", rows, header)
    def replace_category_backfill_confirmation_rows(self, rows, header): self._replace("confirm", rows, header)

    def _replace(self, section, rows, header):
        self.blocks[section] = (deepcopy(header), deepcopy(rows))

    def update_rows(self, sheet, updates):
        section = {"カテゴリ自動分類": "rule", "カテゴリ過去反映": "backfill",
                   "カテゴリ過去反映確認": "confirm"}.get(sheet)
        if section is None:
            return self.db.update_rows(sheet, updates)
        for row_num, row in updates:
            self.blocks[section][1][row_num - 2] = deepcopy(row)

    def consume_category_rule_ui_past_choice(self, key, result):
        for row in self.blocks["rule"][1]:
            if row[6] == key:
                row[5] = False
                row[1] += "\n過去プレビュー: " + result.get("state", "processed")
                return True
        return False


def merge_results(captured, result, live):
    """Keep later user edits by stable ID, including later approval checks.

    Compare whole logical rows, not row numbers: moved rows cannot retarget a
    submission. A subsequent edit remains an unsubmitted decision.
    """
    merged = deepcopy(result)
    retained = 0
    def normalized(row):
        return ["TRUE" if value is True else "FALSE" if value is False else str(value)
                for value in (row or [])]
    for section in ("rule", "backfill", "confirm"):
        key_col = 4 if section == "confirm" else 6
        key = lambda row: str(row[key_col]) if len(row) > key_col else ""
        old = {key(r): r for r in captured[section][1] if key(r)}
        later = {key(r): r for r in live[section][1] if key(r)
                 and normalized(r) != normalized(old.get(key(r)))}
        seen = set()
        output = []
        for row in merged[section][1]:
            identity = key(row)
            output.append(deepcopy(later.get(identity, row)))
            if identity in later:
                retained += 1
                seen.add(identity)
        for identity, row in later.items():
            if identity not in seen:
                output.append(deepcopy(row))
                retained += 1
        merged[section] = (merged[section][0], output)
    # Historical requests know nothing about a newly installed bank block.
    # Keep that live block, including its unsubmitted approvals, unchanged.
    if "bank" not in captured and "bank" in live:
        merged["bank"] = deepcopy(live["bank"])
    elif "bank" in captured:
        # Bank rows use G as their immutable key, including sender subrows.
        old = {str(row[6]): row for row in captured["bank"][1]}
        later = {str(row[6]): row for row in live.get("bank", ([], []))[1]
                 if normalized(row) != normalized(old.get(str(row[6]))) }
        rows = []
        seen = set()
        for row in merged["bank"][1]:
            key = str(row[6])
            rows.append(deepcopy(later.get(key, row)))
            if key in later:
                retained += 1
                seen.add(key)
        for key, row in later.items():
            if key not in seen:
                rows.append(deepcopy(row))
                retained += 1
        merged["bank"] = (merged["bank"][0], rows)
    from .category_ui_order import consolidate_blocks
    return consolidate_blocks(merged), retained


class SheetRequestStore:
    def __init__(self, db): self.db = db

    def read(self):
        values = self.db.get_raw(f"'{CATEGORY_REQUEST_SHEET}'!A2:J2")
        return (list(values[0]) + [""] * 10)[:10] if values else [""] * 10

    def snapshot(self, metadata):
        count = int(metadata[7])
        if not 1 <= count <= MAX_ROWS:
            raise StateError("category_snapshot_size_invalid")
        cells = self.db.get_raw(f"'{CATEGORY_REQUEST_SHEET}'!A4:A{count+3}")
        rows = [json.loads(r[0]) for r in cells]
        if len(rows) != count or snapshot_digest(rows) != metadata[8]:
            raise StateError("category_snapshot_changed")
        return parse_snapshot(rows)

    def update(self, metadata, state, message, *, run_id=""):
        current = self.read()
        if current[0] != metadata[0]:
            raise StateError("category_request_replaced")
        instant = datetime.now(timezone.utc)
        now = instant.isoformat(timespec="seconds")
        display_time = instant.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S JST")
        metadata[1], metadata[5] = state, message
        if state == "running": metadata[3], metadata[6] = now, run_id
        else: metadata[4] = now
        label = {"running": "実行中", "complete": "完了", "review": "完了・要確認あり",
                 "error": "エラー・内容を確認してください"}[state]
        data = [
            {"range": f"'{CATEGORY_REQUEST_SHEET}'!A2:J2", "values": [metadata]},
            {"range": f"'{CATEGORY_WORKFLOW_SHEET}'!C1", "values": [[label]]},
            {"range": f"'{CATEGORY_WORKFLOW_SHEET}'!B2", "values": [[message]]},
            {"range": f"'{CATEGORY_WORKFLOW_SHEET}'!B3", "values": [[display_time]]},
        ]
        if state != "running":
            data.append({"range": f"'{CATEGORY_WORKFLOW_SHEET}'!B1", "values": [[False]]})
        self.db.svc.spreadsheets().values().batchUpdate(spreadsheetId=self.db.sid,
            body={"valueInputOption": "RAW", "data": data}).execute(num_retries=0)


def execute_request(db, request_id, *, env, refresh_projection, store=None, bank_processor=None, bank_refresh=None, bank_replay=None):
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", request_id):
        raise StateError("category_request_id_invalid")
    store = store or SheetRequestStore(db)
    metadata = store.read()
    if metadata[0] != request_id or metadata[1] not in {"accepted", "dispatching"}:
        return {"category_request_ignored": 1}
    # Validate the complete captured input before claiming or touching ledgers.
    try:
        captured = store.snapshot(metadata)
        if "bank" in captured:
            from .bank_review_ui import checked, validate_rows
            validate_rows(captured["bank"][1])
            if (any(row[8] == "group" and checked(row[5]) for row in captured["bank"][1])
                    and env.get("BANK_REVIEW_ENABLED", "false") != "true"):
                raise StateError("bank_review_execution_not_enabled")
    except Exception:
        store.update(metadata, "error", "受付内容を検証できませんでした。入力を確認して再実行してください。")
        raise StateError("category_request_snapshot_invalid") from None
    store.update(metadata, "running", "受付時の入力を処理しています。", run_id=env.get("GITHUB_RUN_ID", ""))
    adapter = CapturedCategoryDB(db, captured)
    try:
        from .category_operations import process_category_operations
        enabled = lambda name: env.get(name, "false").lower() == "true"
        if not all(enabled(name) for name in ("CATEGORY_RULE_UI_ENABLED", "CATEGORY_RULE_SAVE_ENABLED",
                "CATEGORY_BACKFILL_PREVIEW_ENABLED", "CATEGORY_BACKFILL_APPLY_ENABLED")):
            raise StateError("category_request_features_disabled")
        from .category_past_all_months import PAST_HEADER
        bank_counts = {}
        if "bank" in captured and enabled("BANK_REVIEW_ENABLED"):
            from .bank_review_ui import checked
            if any(row[8] == "group" and checked(row[5]) for row in captured["bank"][1]):
                if bank_processor is None:
                    from .bank_review_requests import BankReviewRequestProcessor
                    from .bank_review_source import from_environment
                    bank_processor = BankReviewRequestProcessor(db, from_environment(db, env))
                bank_counts, bank_rows = bank_processor.process(captured["bank"][1], request_id)
                adapter.blocks["bank"] = (captured["bank"][0], bank_rows)
        result = process_category_operations(adapter, apply=True, rule_enabled=True,
            save_enabled=True, preview_enabled=True, backfill_enabled=True,
            all_months=captured["rule"][0][5] == PAST_HEADER)
        result.update(bank_counts)
        # Merge once, after all processing. UI refreshes during the pipeline can
        # never consume or overwrite changes made after the captured submission.
        live = db._category_workflow_blocks()
        merged, retained = merge_results(captured, adapter.blocks, live)
        prior = db._category_workflow_last_read
        db._category_workflow_blocks()
        db._check_category_workflow_input(prior)
        db._write_category_workflow_blocks(merged)
        result["category_later_edits_retained"] = retained
        if bank_counts:
            confirmed = bank_counts['bank_groups_confirmed'] + bank_counts['bank_groups_already_confirmed']
            try:
                if confirmed and bank_replay is not None:
                    result.update(bank_replay(confirmed))
            finally:
                # Even a failed settlement retains its confirmed meaning. Show
                # fresh ledger state and preserve unsent edits before reporting.
                if bank_refresh is not None:
                    result.update(bank_refresh())
        result.update(refresh_projection())
        message = (f"登録処理 {result['category_registration_processed']}件 / "
            f"プレビュー {result['category_previews_processed']}件 / "
            f"過去分反映 {result['category_expenses_applied']}件")
        if result["category_held"]:
            message += f" / 要確認 {result['category_held']}件"
        if bank_counts:
            message += (f" / 銀行確認 {bank_counts['bank_groups_confirmed'] + bank_counts['bank_groups_already_confirmed']}グループ"
                        f" / 銀行ルール登録 {bank_counts['bank_rules_registered']}件")
            if bank_counts["bank_held"]:
                message += f" / 銀行要確認 {bank_counts['bank_held']}グループ"
            if result.get("bank_replay_completed"):
                message += (f" / 銀行記帳 {result['bank_ledger_writes']}行"
                            f" / 原本処理 {result.get('bank_replay_files_processed', 0)}件")
                if result.get("bank_replay_files_withheld", 0):
                    message += f" / 原本保留 {result['bank_replay_files_withheld']}件"
        if retained: message += f" / 実行指示後の編集 {retained}行は次回分として保持"
        store.update(metadata, "review" if result["category_held"] or bank_counts.get("bank_held")
                     or result.get("bank_replay_files_withheld") else "complete", message)
        return result
    except Exception as exc:
        try:
            store.update(metadata, "error", "処理が中断しました。一部反映済みの場合があります。各行の状態を確認してください。")
        except Exception:
            pass  # Preserve the original error; never retry an uncertain write.
        from .category_operations import CategoryOperationFailure
        raise CategoryOperationFailure(exc) from None


def main():
    import os
    import subprocess
    from .production_flow import verify_execution_boundary
    from .sheets import SheetsReadPacer
    from .projection_runtime import run_projection
    from .bank_review_refresh import run_bank_review_refresh
    from .bank_review_replay import run_bank_review_replay
    env = dict(os.environ)
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        verify_execution_boundary(env, head)
        if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise StateError("category_request_dispatch_required")
        pacer = SheetsReadPacer()
        db = SheetsDB(env["SPREADSHEET_ID"], read_pacer=pacer, read_retry_base=20)
        result = execute_request(db, env.get("CATEGORY_REQUEST_ID", ""), env=env,
            refresh_projection=lambda: run_projection(env, apply=True, read_pacer=pacer),
            bank_refresh=lambda: run_bank_review_refresh(db, env, apply=True),
            bank_replay=lambda confirmed: run_bank_review_replay(env,
                request_id=env.get("CATEGORY_REQUEST_ID", ""), confirmed_groups=confirmed))
        print(json.dumps({"success": True, "counts": result}, sort_keys=True))
    except Exception as exc:
        from .category_operations import CategoryOperationFailure
        safe = exc if isinstance(exc, CategoryOperationFailure) else CategoryOperationFailure(exc)
        print(json.dumps({"success": False, "error": "category_request_failed", **safe.details}))
        raise SystemExit(1) from None


if __name__ == "__main__": main()
